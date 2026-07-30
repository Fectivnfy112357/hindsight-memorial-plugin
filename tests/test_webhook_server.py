"""Unit tests for ``hindsight_memorial.webhook_server`` exception handling.

``do_POST`` lives inside ``BaseHTTPRequestHandler`` and depends on full HTTP
request parsing (raw_requestline, requestline, command, etc.), which makes
spinning it up in tests painful. Instead we exercise ``_process_post``,
which is the actual webhook-handling logic that ``do_POST`` delegates to
after reading the body.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest import mock

from hindsight_memorial import webhook_server


def _signed_body(secret: bytes = b"test") -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "event": "retain.completed",
            "bank_id": "bank-1",
            "operation_id": "op-1",
            "status": "completed",
            "timestamp": "2026-01-01T00:00:00Z",
            "data": {"document_id": "doc-1", "memory_unit_count": 1},
        }
    ).encode()
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return body, {
        "X-Hindsight-Signature": sig,
        "X-Hindsight-Event": "retain.completed",
    }


class UnhandledExceptionTest(unittest.TestCase):
    """Any uncaught exception inside ``_process_post`` must:
      - emit a full traceback via ``log.exception`` (so the log file has it)
      - return HTTP 200 with status=error (no Hindsight outbox retry storm)
      - still produce a JSON body so the caller sees status='error'
    """

    def test_unhandled_exception_logs_traceback_and_returns_200(self):
        body, headers = _signed_body()
        with mock.patch.object(webhook_server, "handle_event") as mock_handle:
            mock_handle.side_effect = RuntimeError("simulated bug")

            with self.assertLogs(webhook_server.log, level="ERROR") as captured:
                payload, status = webhook_server._process_post(
                    body, headers, secret=b"test"
                )

        # 1. The exception's error message reached the log.
        joined = "\n".join(captured.output)
        self.assertIn("simulated bug", joined)
        self.assertIn("unhandled exception", joined)
        # log.exception emits the traceback lines; assert at least one frame.
        self.assertTrue(any("Traceback" in line for line in captured.output))

        # 2. HTTP 200 (NOT 500) so Hindsight's outbox doesn't retry.
        self.assertEqual(status, 200)

        # 3. JSON body says status=error with a placeholder error message.
        body_dict = json.loads(payload)
        self.assertEqual(body_dict["status"], "error")
        self.assertIn("internal handler error", body_dict["error"])

    def test_normal_path_returns_200(self):
        """Sanity: a clean handle_event call still returns 200 + ok status."""
        from hindsight_memorial.webhook_handlers import WebhookOutcome

        body, headers = _signed_body()
        outcome = WebhookOutcome(
            status="ok",
            bank_id="bank-1",
            document_id="doc-1",
            units_processed=1,
            total_superseded=1,
        )
        with mock.patch.object(
            webhook_server, "handle_event", return_value=outcome
        ):
            payload, status = webhook_server._process_post(
                body, headers, secret=b"test"
            )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "ok")

    def test_malformed_payload_returns_400(self):
        """If ``handle_event`` reports 'malformed payload', we send 400."""
        from hindsight_memorial.webhook_handlers import WebhookOutcome

        body, headers = _signed_body()
        outcome = WebhookOutcome(status="skipped", error="malformed payload")
        with mock.patch.object(
            webhook_server, "handle_event", return_value=outcome
        ):
            payload, status = webhook_server._process_post(
                body, headers, secret=b"test"
            )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["status"], "skipped")


if __name__ == "__main__":
    unittest.main()