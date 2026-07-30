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
from .webhook_handlers import configure_logging, handle_event

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

            outcome = handle_event(
                raw_body, headers, secret=secret, fetch_units=fetch_units
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
            # 200 for every well-formed-but-rejected request (bad signature,
            # non-retain event, empty doc) so Hindsight's outbox doesn't enter
            # its 5s/5min/30min/2h/5h retry storm. 400 only for malformed bytes.
            status = (
                HTTPStatus.BAD_REQUEST
                if outcome.error == "malformed payload"
                else HTTPStatus.OK
            )
            payload = json.dumps(outcome.to_dict()).encode("utf-8")
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