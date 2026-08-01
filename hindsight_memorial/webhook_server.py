"""Thin HTTP server fronting :mod:`hindsight_memorial.webhook_handlers`.

Stdlib only (no Flask/FastAPI) so the dependency surface stays at zero — the
rest of this package is stdlib-only too (with one exception: ``db.py`` is
pluggable so production can swap to MySQL via ``PyMySQL``). Uses
``ThreadingHTTPServer`` so concurrent webhooks do not serialize.

The server's lifecycle (2026-08-01 redesign):

  1. ``init_db()`` — apply the schema to the backend connection.
  2. Start the :class:`~hindsight_memorial.poller.ReconcilerPoller` daemon
     thread. It is the slow half; the HTTP request thread is fast.
  3. Serve HTTP. The request thread authenticates, parses, ingests to
     the local table, and returns 200. The poller thread drains the
     table.
  4. On shutdown, stop the poller first (it will finish its current
     row, if any) and then close the HTTP server.

Run::

    python -m hindsight_memorial.webhook_server \
        --host 0.0.0.0 --port 9602 \
        --secret <hex-encoded-shared-secret>

The secret is read from ``--secret`` or ``HINDSIGHT_WEBHOOK_SECRET`` (the env
var wins). It must match the secret configured in the Hindsight webhooks UI.

Logs go to stderr AND, when ``HINDSIGHT_MEMORIAL_LOG_FILE`` is set, a rotating
file at that path. See :func:`hindsight_memorial.webhook_handlers.configure_logging`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import db, poller, webhook_handlers
from .client import HindsightClient

log = logging.getLogger("hindsight_memorial.webhook_server")

DEFAULT_PORT = 9602


def _resolve_secret(arg: str | None) -> bytes:
    secret = arg or os.environ.get("HINDSIGHT_WEBHOOK_SECRET", "")
    if not secret:
        raise SystemExit(
            "webhook secret required: pass --secret or set HINDSIGHT_WEBHOOK_SECRET"
        )
    return secret.encode("utf-8")


def _make_fetch_units():
    """Return a ``fetch_units(bank_id, document_id)`` callable for
    :func:`webhook_handlers.handle_event`.

    The callable routes through :class:`HindsightClient` against
    ``HINDSIGHT_API_URL``. It is constructed lazily so a deployment
    that never receives a webhook (e.g. healthz-only) does not need a
    working Hindsight URL.
    """
    def fetch_units(bank_id: str, document_id: str | None):
        api_url = os.environ.get("HINDSIGHT_API_URL", "").strip()
        if not api_url:
            log.warning(
                "fetch_units called but HINDSIGHT_API_URL is unset "
                "(bank=%s document=%s)",
                bank_id, document_id,
            )
            return []
        client = HindsightClient(
            base_url=api_url.rstrip("/"),
            api_key=os.environ.get("HINDSIGHT_API_KEY"),
        )
        if document_id is None:
            # Defensive: handle_event only calls fetch_units with a
            # non-None docId (it may have come from the fallback path).
            # Returning [] keeps the handler's "no units" branch
            # predictable.
            return []
        return client.list_memory_units(bank_id, document_id)

    return fetch_units


def _make_fetch_recent_doc():
    """Return the fallback ``fetch_recent_doc(bank_id)`` callable."""
    def fetch_recent_doc(bank_id: str):
        """Read the most recently mentioned memory_unit and return its document_id.

        Fallback for Hindsight retain paths that emit ``retain.completed`` with
        ``data={}`` (no document_id) — the server generated a doc_id for the
        commit but did not propagate it into the outbox payload.
        """
        api_url = os.environ.get("HINDSIGHT_API_URL", "").strip()
        if not api_url:
            return None
        client = HindsightClient(
            base_url=api_url.rstrip("/"),
            api_key=os.environ.get("HINDSIGHT_API_KEY"),
        )
        units = client.list_recent_units(bank_id, limit=5)
        for u in units:
            did = u.get("document_id")
            if not isinstance(did, str) or not did:
                continue
            ts = u.get("mentioned_at") or u.get("date")
            ts_str = ts if isinstance(ts, str) else None
            return (did, ts_str)
        return None

    return fetch_recent_doc


def _make_handler(secret: bytes):
    """Build a request handler bound to the given secret.

    The handler is stateless: every request opens a fresh call into
    ``handle_event`` (which opens a fresh DB connection via
    :func:`db.get_connection`). The persistent reconciler state lives
    in the database, not in this object.
    """

    class WebhookHandler(BaseHTTPRequestHandler):
        # Silence the default per-request stderr log; we have structured logging instead.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.info(format, *args)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/webhook/hindsight":
                self.send_error(HTTPStatus.NOT_FOUND, "unknown endpoint")
                return
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(length) if length > 0 else b""
                headers = {k: v for k, v in self.headers.items()}
                payload, status = _process_post(raw_body, headers, secret=secret)
            except Exception:
                # Reading the body or running the handler failed. Still answer
                # 200: a non-2xx here restarts Hindsight's retry ladder, and
                # a retry cannot fix a bug on our side.
                log.exception("failed to admit request (path=%s)", self.path)
                payload = json.dumps(
                    {"status": "error", "error": "admission failed (see logs)"}
                ).encode("utf-8")
                status = HTTPStatus.OK
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                # The handler does not have a reference to the poller; the
                # /healthz route is wired in ``_make_healthz_handler`` below
                # by the production main(). In tests we exercise the
                # ``_healthz`` function directly. If a deployment reaches
                # this branch without the poller wired, we still answer
                # 200 with a minimal payload.
                body, status = _healthz(None)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "unknown endpoint")

    return WebhookHandler


def _process_post(
    raw_body: bytes,
    headers: dict[str, str],
    *,
    secret: bytes,
) -> tuple[bytes, int]:
    """Admit one /webhook/hindsight request and answer immediately.

    This is the *fast* half of the request path. The handler does:

      1. Verify the HMAC signature. An unsigned/forged body is dropped
         here and never reaches the database.
      2. Parse the event, recover a missing document_id if needed.
      3. Fetch the document's memory_units from Hindsight.
      4. Upsert each one into the local ``memory_units`` table.
      5. Return 200 with an acknowledgement.

    The slow half (reflect + curate) is owned by the poller thread and
    runs out-of-band on the rows this function wrote.
    """
    sig = None
    event_name = None
    for k, v in headers.items():
        lk = k.lower()
        if lk == "x-hindsight-signature":
            sig = v
        elif lk == "x-hindsight-event":
            event_name = v

    log.info(
        "webhook received: bytes=%d event_header=%r sig_present=%s",
        len(raw_body), event_name, bool(sig),
    )

    if not webhook_handlers.verify_signature(raw_body, sig, secret):
        # Do not ingest. Answer 200 anyway: a 401 makes Hindsight retry,
        # and retrying will not produce a valid signature.
        log.warning("signature verification failed (event=%r)", event_name)
        return (
            json.dumps(
                {"status": "ignored", "error": "signature verification failed"}
            ).encode("utf-8"),
            HTTPStatus.OK,
        )

    outcome = webhook_handlers.handle_event(
        raw_body,
        headers,
        secret=secret,
        fetch_units=_make_fetch_units(),
        fetch_recent_doc=_make_fetch_recent_doc(),
    )

    # Map the outcome to an HTTP response. The handler always returns
    # 200 from the HTTP layer's point of view; we just translate the
    # outcome into a status string for the body.
    if outcome.status == "ok":
        body = {
            "status": "accepted",
            "ingest_stats": outcome.ingest_stats,
        }
    else:
        body = {
            "status": outcome.status,
            "reason": outcome.reason,
            "error": outcome.error,
        }
    return json.dumps(body).encode("utf-8"), HTTPStatus.OK


def _healthz(p: poller.ReconcilerPoller | None) -> tuple[bytes, int]:
    """Build the /healthz body.

    Returns the row-count snapshot from the database plus the poller's
    lifecycle state. ``p`` may be None in deployments that disable the
    poller (``HINDSIGHT_POLLER_ENABLED=0``).
    """
    try:
        conn = db.get_connection()
        stats = db.health_stats_on_conn(conn)
    except Exception as e:
        log.exception("healthz db query failed")
        body = json.dumps({"status": "degraded", "error": str(e)[:200]}).encode("utf-8")
        return body, HTTPStatus.SERVICE_UNAVAILABLE
    body = {
        "status": "ok",
        **stats,
        "poller_running": p.is_alive() if p is not None else False,
    }
    return json.dumps(body).encode("utf-8"), HTTPStatus.OK


def _make_healthz_handler(p: poller.ReconcilerPoller | None):
    """Return a BaseHTTPRequestHandler subclass that serves /healthz
    with the given poller reference baked in. This is the production
    wiring: the plain ``_make_handler`` above is used for the
    /webhook/hindsight route only and answers /healthz with no poller
    info. ``serve()`` and ``main()`` use the wired version."""

    class HealthzHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.info(format, *args)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                body, status = _healthz(p)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/webhook/hindsight":
                self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "use POST")
                return
            self.send_error(HTTPStatus.NOT_FOUND, "unknown endpoint")

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/webhook/hindsight":
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                    raw_body = self.rfile.read(length) if length > 0 else b""
                    headers = {k: v for k, v in self.headers.items()}
                    payload, status = _process_post(raw_body, headers, secret=SERVER_SECRET)
                except Exception:
                    log.exception("failed to admit request (path=%s)", self.path)
                    payload = json.dumps(
                        {"status": "error", "error": "admission failed (see logs)"}
                    ).encode("utf-8")
                    status = HTTPStatus.OK
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "unknown endpoint")

    return HealthzHandler


# Module-level secret, set by serve()/main() right before the server
# starts. ``_make_healthz_handler`` reads it because the inner
# BaseHTTPRequestHandler cannot capture loop-local variables via the
# usual closure (each request creates a new instance, so we have to
# keep the secret in a module global).
SERVER_SECRET: bytes = b""


def serve(host: str, port: int, secret: bytes) -> None:
    """Start the HTTP server, the poller, and block until shutdown."""
    global SERVER_SECRET
    SERVER_SECRET = secret

    # Initialise the DB schema. ``init_db`` is a no-op on subsequent
    # boots (CREATE TABLE IF NOT EXISTS).
    try:
        init_conn = db.get_connection()
        db.init_db_on_conn(init_conn)
    except Exception:
        log.exception("init_db failed; refusing to start")
        raise

    # Start the poller (unless explicitly disabled).
    p: poller.ReconcilerPoller | None = None
    if os.environ.get("HINDSIGHT_POLLER_ENABLED", "1") != "0":
        poller_conn = db.get_connection()
        p = poller.ReconcilerPoller(
            poller_conn,
            _build_poller_run_reconcile(),
            poll_interval_sec=float(
                os.environ.get("HINDSIGHT_POLLER_INTERVAL_SEC", "1.0")
            ),
        )
        p.start()
    else:
        log.info("poller disabled (HINDSIGHT_POLLER_ENABLED=0)")

    server = ThreadingHTTPServer((host, port), _make_healthz_handler(p))
    log.info("hindsight-memorial webhook server listening on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        if p is not None:
            p.stop()
        server.server_close()


def _build_poller_run_reconcile():
    """Build the callable the poller will use for ``run_reconcile``.

    Wires up the Hindsight client + webhook config loader so the
    poller can drive the standard pipeline. Kept as a closure so the
    poller test suite can pass its own callable.
    """
    from . import reconcile
    from .webhook_handlers import webhook_config_loader

    def _run(bank_id: str, unit_id: str, content: str):
        loader = webhook_config_loader(bank_id)
        return reconcile.run_reconcile(
            bank_id=bank_id,
            unit_id=unit_id,
            content=content,
            load_cfg=loader,
        )

    return _run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--secret", default=None, help="HMAC shared secret")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("HINDSIGHT_MEMORIAL_LOG_LEVEL", "INFO"),
    )
    args = parser.parse_args(argv)

    webhook_handlers.configure_logging(level=args.log_level)
    serve(args.host, args.port, _resolve_secret(args.secret))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())