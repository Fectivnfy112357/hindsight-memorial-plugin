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
from .webhook_handlers import WebhookOutcome, configure_logging, handle_event

log = logging.getLogger("hindsight_memorial.webhook_server")

DEFAULT_PORT = 9602


def _resolve_secret(arg: str | None) -> bytes:
    secret = arg or os.environ.get("HINDSIGHT_WEBHOOK_SECRET", "")
    if not secret:
        raise SystemExit(
            "webhook secret required: pass --secret or set HINDSIGHT_WEBHOOK_SECRET"
        )
    return secret.encode("utf-8")


def _make_handler(secret: bytes):
    """Build a request handler bound to the given secret + shared client builder."""

    class WebhookHandler(BaseHTTPRequestHandler):
        # Silence the default per-request stderr log; we have structured logging instead.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.info(format, *args)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/webhook/hindsight":
                self.send_error(HTTPStatus.NOT_FOUND, "unknown endpoint")
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b""
            headers = {k: v for k, v in self.headers.items()}
            payload, status = _process_post(raw_body, headers, secret=secret)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            self.send_error(HTTPStatus.NOT_FOUND, "unknown endpoint")

    return WebhookHandler


def _process_post(
    raw_body: bytes,
    headers: dict[str, str],
    *,
    secret: bytes,
) -> tuple[bytes, int]:
    """Process one /webhook/hindsight request and return (response_body, status).

    Extracted from ``do_POST`` so it can be exercised without spinning up a
    real BaseHTTPRequestHandler. Handles the full chain:

      1. Build a fetch_units closure that talks to Hindsight's /memories/list.
      2. Call ``handle_event`` inside a try/except so any unhandled exception
         still produces a 200 response with a status=error body — this keeps
         Hindsight's outbox from entering its 5s/5min/30min/2h/5h retry storm.
      3. Pick HTTP status: 400 only if the body was malformed bytes; 200
         otherwise (including signature failures, non-retain events, empty
         documents, and unhandled bugs).
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

    def fetch_recent_doc(bank_id: str) -> str | None:
        """Read the most recently mentioned memory_unit and return its document_id.

        Fallback for Hindsight retain paths that emit ``retain.completed`` with
        ``data={}`` (no document_id) — the server generated a doc_id for the
        commit but did not propagate it into the outbox payload. We pull the
        bank's most recent unit to recover the linkage.
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
            if isinstance(did, str) and did:
                return did
        return None

    try:
        outcome = handle_event(
            raw_body,
            headers,
            secret=secret,
            fetch_units=fetch_units,
            fetch_recent_doc=fetch_recent_doc,
        )
    except Exception:  # noqa: BLE001 — we want to log literally everything
        # Catch-all so any unhandled bug still returns 200 to Hindsight
        # (avoids the 5s/5min/30min/2h/5h outbox retry storm) and writes
        # a full traceback to the log file. Without this, an uncaught
        # exception produces a bare 500 with no diagnostic output.
        log.exception(
            "unhandled exception in webhook handler "
            "(bytes=%d event_header=%r)",
            len(raw_body),
            headers.get("X-Hindsight-Event") or headers.get("x-hindsight-event"),
        )
        outcome = WebhookOutcome(
            status="error",
            error="internal handler error (see logs)",
        )

    log.info(
        "webhook processed: status=%s bank=%s document=%s "
        "units=%d superseded=%d observations_cleared=%d",
        outcome.status,
        outcome.bank_id,
        outcome.document_id,
        outcome.units_processed,
        outcome.total_superseded,
        outcome.total_observations_cleared,
    )
    status = (
        HTTPStatus.BAD_REQUEST
        if outcome.error == "malformed payload"
        else HTTPStatus.OK
    )
    return json.dumps(outcome.to_dict()).encode("utf-8"), status


def serve(host: str, port: int, secret: bytes) -> None:
    server = ThreadingHTTPServer((host, port), _make_handler(secret))
    log.info("hindsight-memorial webhook server listening on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
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