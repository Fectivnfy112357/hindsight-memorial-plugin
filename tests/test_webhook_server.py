"""Unit tests for ``hindsight_memorial.webhook_server`` admission logic.

``do_POST`` lives inside ``BaseHTTPRequestHandler`` and depends on full HTTP
request parsing, which makes spinning it up in tests painful. Instead we
exercise ``_process_post``, the admission path ``do_POST`` delegates to.

The contract changed on 2026-07-31. ``_process_post`` used to run the whole
reconcile inline and return its results; it now only authenticates and
enqueues, because the inline reconcile took 10-70s and blew Hindsight's
outbox timeout, which triggered the retry ladder behind the mass-invalidation
incident. Reconcile outcomes are asserted in ``test_dispatch`` instead.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest import mock

from hindsight_memorial import dispatch, webhook_server


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


class _StubDispatcher:
    """Records submissions without spawning a worker thread."""

    def __init__(self, verdict: str = dispatch.QUEUED):
        self.submitted: list[bytes] = []
        self.verdict = verdict

    def submit(self, raw_body: bytes, headers: dict[str, str]) -> str:
        self.submitted.append(raw_body)
        return self.verdict


class AdmissionTest(unittest.TestCase):
    def test_valid_event_is_queued_and_acked(self):
        body, headers = _signed_body()
        d = _StubDispatcher()

        payload, status = webhook_server._process_post(
            body, headers, secret=b"test", dispatcher=d
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "accepted")
        self.assertEqual(d.submitted, [body])

    def test_bad_signature_is_not_queued(self):
        """A forged body must never reach the worker."""
        body, headers = _signed_body()
        headers["X-Hindsight-Signature"] = "sha256=" + ("0" * 64)
        d = _StubDispatcher()

        payload, status = webhook_server._process_post(
            body, headers, secret=b"test", dispatcher=d
        )

        # 200, not 401: a retry cannot produce a valid signature, so inviting
        # one just restarts the retry ladder.
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "ignored")
        self.assertEqual(d.submitted, [])

    def test_duplicate_verdict_is_reported(self):
        body, headers = _signed_body()
        d = _StubDispatcher(verdict=dispatch.DUPLICATE)

        payload, status = webhook_server._process_post(
            body, headers, secret=b"test", dispatcher=d
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "duplicate")

    def test_admission_does_not_call_handle_event(self):
        """The slow path must not run on the request thread.

        This is the regression guard for the incident: handle_event makes the
        LLM reflect call, and running it here is what caused the timeout.
        """
        body, headers = _signed_body()
        with mock.patch.object(webhook_server, "handle_event") as mock_handle:
            webhook_server._process_post(
                body, headers, secret=b"test", dispatcher=_StubDispatcher()
            )
        mock_handle.assert_not_called()


class RunEventTest(unittest.TestCase):
    """``run_event`` is the slow half that the worker calls."""

    def test_delegates_to_handle_event_with_both_fetchers(self):
        body, headers = _signed_body()
        from hindsight_memorial.webhook_handlers import WebhookOutcome

        outcome = WebhookOutcome(status="ok", bank_id="bank-1")
        with mock.patch.object(
            webhook_server, "handle_event", return_value=outcome
        ) as mock_handle:
            result = webhook_server.run_event(body, headers, secret=b"test")

        self.assertIs(result, outcome)
        kwargs = mock_handle.call_args.kwargs
        self.assertEqual(kwargs["secret"], b"test")
        self.assertTrue(callable(kwargs["fetch_units"]))
        self.assertTrue(callable(kwargs["fetch_recent_doc"]))

    def test_exception_propagates_to_the_worker(self):
        """run_event does not swallow errors; the dispatcher logs them."""
        body, headers = _signed_body()
        with mock.patch.object(
            webhook_server, "handle_event", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                webhook_server.run_event(body, headers, secret=b"test")


if __name__ == "__main__":
    unittest.main()
