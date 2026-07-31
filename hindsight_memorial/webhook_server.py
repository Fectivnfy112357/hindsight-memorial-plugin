"""Thin HTTP server fronting :mod:`hindsight_memorial.webhook_handlers`.

Stdlib only (no Flask/FastAPI) so the dependency surface stays at zero — the
rest of this package is stdlib-only too. Uses ``ThreadingHTTPServer`` so
concurrent webhooks do not serialize.

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

from .client import HindsightClient
from .dispatch import DUPLICATE, Dispatcher
from .webhook_handlers import configure_logging, handle_event, verify_signature

log = logging.getLogger("hindsight_memorial.webhook_server")

DEFAULT_PORT = 9602


def _resolve_secret(arg: str | None) -> bytes:
    secret = arg or os.environ.get("HINDSIGHT_WEBHOOK_SECRET", "")
    if not secret:
        raise SystemExit(
            "webhook secret required: pass --secret or set HINDSIGHT_WEBHOOK_SECRET"
        )
    return secret.encode("utf-8")


def _make_handler(secret: bytes, dispatcher: Dispatcher):
    """Build a request handler bound to the given secret + dispatcher."""

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
                payload, status = _process_post(
                    raw_body, headers, secret=secret, dispatcher=dispatcher
                )
            except Exception:
                # Reading the body or admitting the event failed. Still answer
                # 200: a non-2xx here restarts Hindsight's retry ladder, and a
                # retry cannot fix a bug on our side.
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
                body = json.dumps({"status": "ok", **dispatcher.stats()}).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "unknown endpoint")

    return WebhookHandler


def run_event(raw_body: bytes, headers: dict[str, str], *, secret: bytes):
    """The slow half: fetch units and reconcile. Runs on the dispatch worker.

    Called only after ``_process_post`` has authenticated the payload and
    admitted it past dedup, so the HTTP caller is already answered. The
    return value goes to the log, not to Hindsight.
    """
    def fetch_units(bank_id: str, document_id: str):
        api_url = os.environ.get("HINDSIGHT_API_URL", "").strip()
        if not api_url:
            log.warning(
                "fetch_units called but HINDSIGHT_API_URL is unset "
                "(bank=%s document=%s)",
                bank_id,
                document_id,
            )
            return []
        client = HindsightClient(
            base_url=api_url.rstrip("/"),
            api_key=os.environ.get("HINDSIGHT_API_KEY"),
        )
        return client.list_memory_units(bank_id, document_id)

    def fetch_recent_doc(bank_id: str) -> tuple[str, str | None] | None:
        """Read the most recently mentioned memory_unit and return its document_id.

        Fallback for Hindsight retain paths that emit ``retain.completed`` with
        ``data={}`` (no document_id) — the server generated a doc_id for the
        commit but did not propagate it into the outbox payload. We pull the
        bank's most recent unit to recover the linkage.

        Returns ``(document_id, mentioned_at)`` so the handler can bounds-check
        the recovered unit against the event timestamp. ``mentioned_at`` is
        preferred (the unit is sorted by it on the server); we fall back to
        ``created_at`` if the field is missing.
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
            ts = u.get("mentioned_at") or u.get("created_at")
            ts_str = ts if isinstance(ts, str) else None
            return (did, ts_str)
        return None

    return handle_event(
        raw_body,
        headers,
        secret=secret,
        fetch_units=fetch_units,
        fetch_recent_doc=fetch_recent_doc,
    )


def _process_post(
    raw_body: bytes,
    headers: dict[str, str],
    *,
    secret: bytes,
    dispatcher: Dispatcher,
) -> tuple[bytes, int]:
    """Admit one /webhook/hindsight request and answer immediately.

    This is the *fast* half of the request path and it must stay fast: the
    reconcile it schedules takes 10-70s (an LLM reflect call), and answering
    only after that work finished is what triggered Hindsight's outbox retry
    ladder during the 2026-07-30 incident. Each retry re-ran reflect on the
    same fact and the invalidation count escalated 1 -> 10 -> 25.

    Steps, all cheap:

      1. Verify the HMAC signature. An unsigned/forged body is dropped here
         and never reaches the queue.
      2. Hand the body to the dispatcher, which drops replays and enqueues
         everything else.
      3. Return 200 with an acknowledgement.

    The response no longer carries reconcile results — by design, since the
    reconcile has not run yet. Outcomes are in the log (see
    ``dispatch`` for the processing start/done/failed lines).
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
        len(raw_body),
        event_name,
        bool(sig),
    )

    if not verify_signature(raw_body, sig, secret):
        # Do not queue. Answer 200 anyway: a 401 makes Hindsight retry, and
        # retrying will not produce a valid signature.
        log.warning("signature verification failed (event=%r)", event_name)
        return (
            json.dumps(
                {"status": "ignored", "error": "signature verification failed"}
            ).encode("utf-8"),
            HTTPStatus.OK,
        )

    admission = dispatcher.submit(raw_body, headers)
    status_str = "duplicate" if admission == DUPLICATE else "accepted"
    return (
        json.dumps({"status": status_str}).encode("utf-8"),
        HTTPStatus.OK,
    )


def serve(host: str, port: int, secret: bytes) -> None:
    dispatcher = Dispatcher(
        lambda raw_body, headers: run_event(raw_body, headers, secret=secret)
    )
    dispatcher.start()
    server = ThreadingHTTPServer((host, port), _make_handler(secret, dispatcher))
    log.info("hindsight-memorial webhook server listening on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        # Give the in-flight reconcile a chance to finish before the process
        # exits; its dedup key stays in_flight and is never retried if it dies.
        dispatcher.stop()
        server.server_close()


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

    configure_logging(level=args.log_level)
    serve(args.host, args.port, _resolve_secret(args.secret))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())