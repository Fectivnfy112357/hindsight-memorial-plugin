"""Unit tests for ``hindsight_memorial.webhook_handlers``.

Exercises the handler as a plain function over ``(raw_body, headers, secret)``
so no HTTP server is spun up. The ``fetch_units`` callable is the seam where
the Hindsight HTTP client plugs in.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest import mock

from hindsight_memorial import webhook_handlers as wh
from hindsight_memorial.config import MemorialConfig
from hindsight_memorial.reconcile import HindsightAPIError


SECRET = b"test-secret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def _retain_event(
    *,
    bank_id: str = "hindsight-memorial",
    document_id: str = "doc-abc",
    memory_unit_count: int = 1,
    event: str = "retain.completed",
) -> bytes:
    payload = {
        "event": event,
        "bank_id": bank_id,
        "operation_id": "op-123",
        "status": "completed",
        "timestamp": "2026-07-30T07:30:00Z",
        "data": {
            "document_id": document_id,
            "tags": ["auto"],
            "memory_unit_count": memory_unit_count,
        },
    }
    return json.dumps(payload).encode("utf-8")


class SignatureTest(unittest.TestCase):
    def test_valid_signature_passes(self):
        body = b"hello"
        self.assertTrue(wh.verify_signature(body, _sign(body), SECRET))

    def test_wrong_signature_fails(self):
        body = b"hello"
        bad = "sha256=" + ("0" * 64)
        self.assertFalse(wh.verify_signature(body, bad, SECRET))

    def test_missing_signature_fails(self):
        self.assertFalse(wh.verify_signature(b"hello", None, SECRET))

    def test_tampered_body_fails(self):
        body = _retain_event()
        sig = _sign(body)
        tampered = body + b" "
        self.assertFalse(wh.verify_signature(tampered, sig, SECRET))

    def test_uppercase_hex_rejected(self):
        body = b"x"
        hexsig = hmac.new(SECRET, body, hashlib.sha256).hexdigest().upper()
        # verify_signature is strict-lowercase per hindsight source.
        self.assertFalse(wh.verify_signature(body, f"sha256={hexsig}", SECRET))


class ParseEventTest(unittest.TestCase):
    def test_parses_retain_completed(self):
        evt = wh.parse_event(_retain_event(document_id="doc-1", memory_unit_count=3))
        self.assertIsNotNone(evt)
        assert evt is not None
        self.assertEqual(evt.event, "retain.completed")
        self.assertEqual(evt.bank_id, "hindsight-memorial")
        self.assertEqual(evt.document_id, "doc-1")
        self.assertEqual(evt.memory_unit_count, 3)
        self.assertEqual(evt.operation_id, "op-123")

    def test_ignores_wrong_event_type(self):
        body = _retain_event(event="consolidation.completed")
        self.assertIsNone(wh.parse_event(body))

    def test_returns_event_with_none_document_id_when_missing(self):
        # Missing document_id is no longer a rejection — callers must fall back
        # to list_recent_units (see Hindsight bug at orchestrator.py:757).
        body = json.dumps(
            {"event": "retain.completed", "bank_id": "b", "data": {"memory_unit_count": 1}}
        ).encode()
        evt = wh.parse_event(body)
        self.assertIsNotNone(evt)
        assert evt is not None
        self.assertIsNone(evt.document_id)
        self.assertEqual(evt.bank_id, "b")

    def test_ignores_non_json(self):
        self.assertIsNone(wh.parse_event(b"not json"))

    def test_memory_unit_count_defaults_to_zero(self):
        body = json.dumps(
            {
                "event": "retain.completed",
                "bank_id": "b",
                "data": {"document_id": "d"},
            }
        ).encode()
        evt = wh.parse_event(body)
        self.assertIsNotNone(evt)
        assert evt is not None
        self.assertEqual(evt.memory_unit_count, 0)


class HandleEventTest(unittest.TestCase):
    def _headers(self, body: bytes) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Hindsight-Event": "retain.completed",
            "X-Hindsight-Signature": _sign(body),
        }

    def _run(self, *, body: bytes, units, env=None):
        if env:
            with mock.patch.dict("os.environ", env, clear=False):
                return wh.handle_event(
                    body, self._headers(body), secret=SECRET, fetch_units=lambda b, d: units
                )
        return wh.handle_event(
            body, self._headers(body), secret=SECRET, fetch_units=lambda b, d: units
        )

    def test_signature_mismatch_returns_ignored(self):
        body = _retain_event()
        headers = self._headers(body)
        headers["X-Hindsight-Signature"] = "sha256=" + ("0" * 64)
        outcome = wh.handle_event(
            body, headers, secret=SECRET, fetch_units=lambda b, d: []
        )
        self.assertEqual(outcome.status, "ignored")
        self.assertIn("signature", outcome.error or "")

    def test_non_retain_event_returns_ignored(self):
        body = _retain_event(event="consolidation.completed")
        outcome = self._run(body=body, units=[])
        self.assertEqual(outcome.status, "ignored")

    def test_zero_hint_but_no_units_skipped(self):
        """The webhook server's count hint is not authoritative — we always
        query /memories/list and only skip when that returns 0. So even
        count_hint=0 + units=[] results in a skip, but the reason comes
        from the (empty) list query, not the count hint.
        """
        body = _retain_event(memory_unit_count=0)
        outcome = self._run(body=body, units=[])
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(outcome.memory_unit_count, 0)
        self.assertIn("list", outcome.reason or "")

    def test_no_units_returned_skipped(self):
        body = _retain_event(memory_unit_count=2)
        outcome = self._run(body=body, units=[])
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("list", outcome.reason or "")

    def test_per_unit_reconcile_each_call(self):
        """One unit with stale fact → reconcile runs reflect+curate per unit."""
        body = _retain_event(memory_unit_count=1)
        units = [{"id": "u1", "text": "user moved to Shenzhen"}]

        fake_client = mock.MagicMock()
        fake_client.list_banks.return_value = ["hindsight-memorial"]
        fake_client.reflect.return_value = {
            "structured_output": {
                "superseded_fact_ids": ["00000000-0000-0000-0000-000000000001"],
                "reasoning": "old location",
            }
        }
        fake_client.update_memory.return_value = {"memory": {"id": "u1", "state": "invalidated"}}
        fake_client.clear_memory_observations.return_value = {"deleted_count": 1}

        with mock.patch.dict(
            "os.environ", {"HINDSIGHT_API_URL": "http://api"}, clear=False
        ), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=fake_client,
        ):
            outcome = self._run(body=body, units=units)

        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.units_processed, 1)
        self.assertEqual(outcome.total_superseded, 1)
        # Confirm reflect was called once with the unit's text (not merged with anything).
        self.assertEqual(fake_client.reflect.call_count, 1)
        sent_query = fake_client.reflect.call_args[0][1]
        self.assertIn("Shenzhen", sent_query)
        self.assertNotIn("combined", sent_query.lower())

    def test_multiple_units_run_independently(self):
        body = _retain_event(memory_unit_count=2)
        units = [
            {"id": "u1", "text": "user moved to Shenzhen"},
            {"id": "u2", "text": "user now uses macOS 26"},
        ]

        # Per-unit responses — first has stale, second has none.
        fake_client = mock.MagicMock()
        fake_client.list_banks.return_value = ["hindsight-memorial"]
        fake_client.reflect.side_effect = [
            {  # unit 1 — superseded
                "structured_output": {
                    "superseded_fact_ids": ["00000000-0000-0000-0000-000000000001"],
                    "reasoning": "old",
                }
            },
            {"structured_output": {"superseded_fact_ids": [], "reasoning": "none"}},
        ]
        fake_client.update_memory.return_value = {"memory": {"state": "invalidated"}}
        fake_client.clear_memory_observations.return_value = {"deleted_count": 1}

        with mock.patch.dict(
            "os.environ", {"HINDSIGHT_API_URL": "http://api"}, clear=False
        ), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=fake_client,
        ):
            outcome = self._run(body=body, units=units)

        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.units_processed, 2)
        self.assertEqual(outcome.total_superseded, 1)
        self.assertEqual(fake_client.reflect.call_count, 2)

    def test_unit_with_no_text_is_skipped(self):
        body = _retain_event(memory_unit_count=2)
        units = [{"id": "u1", "text": "valid fact"}, {"id": "u2"}]  # u2 has no text

        fake_client = mock.MagicMock()
        fake_client.list_banks.return_value = ["hindsight-memorial"]
        fake_client.reflect.return_value = {
            "structured_output": {"superseded_fact_ids": [], "reasoning": ""}
        }
        with mock.patch.dict(
            "os.environ", {"HINDSIGHT_API_URL": "http://api"}, clear=False
        ), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=fake_client,
        ):
            outcome = self._run(body=body, units=units)

        # unit 1 ran (abandoned, no supersede); unit 2 skipped (no text).
        self.assertEqual(outcome.units_processed, 1)
        self.assertEqual(outcome.units_skipped, 1)
        self.assertEqual(outcome.status, "abandoned")

    def test_reflect_error_promotes_status(self):
        body = _retain_event(memory_unit_count=1)
        units = [{"id": "u1", "text": "any fact"}]

        fake_client = mock.MagicMock()
        fake_client.list_banks.return_value = ["hindsight-memorial"]
        fake_client.reflect.side_effect = HindsightAPIError(500, "boom", "http://api/reflect")
        with mock.patch.dict(
            "os.environ", {"HINDSIGHT_API_URL": "http://api"}, clear=False
        ), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=fake_client,
        ):
            outcome = self._run(body=body, units=units)

        self.assertEqual(outcome.status, "reflect_failed")
        self.assertIn("boom", outcome.error or "")

    def test_missing_env_api_url_skips_cleanly(self):
        body = _retain_event(memory_unit_count=1)
        units = [{"id": "u1", "text": "any fact"}]
        with mock.patch.dict("os.environ", {}, clear=True):
            outcome = self._run(body=body, units=units)
        # All units skipped at the reconcile layer; aggregate status = abandoned.
        self.assertIn(outcome.status, {"abandoned", "skipped"})
        self.assertEqual(outcome.units_processed, 0)

    # ── fallback: data={} (no document_id) → recover from recent units ──

    def _run_with_fallback(self, body, units, recent_units, fetch_recent_returns=None):
        """Drive handle_event with both fetch_units and fetch_recent_doc stubs."""
        if fetch_recent_returns is None:
            # By default, fetch_recent_doc returns the document_id of the first
            # recent unit that has one.
            def fetch_recent_doc(bank_id: str) -> str | None:
                for u in recent_units:
                    did = u.get("document_id")
                    if isinstance(did, str) and did:
                        return did
                return None
        else:
            fetch_recent_doc = fetch_recent_returns
        return wh.handle_event(
            body,
            self._headers(body),
            secret=SECRET,
            fetch_units=lambda b, d: units,
            fetch_recent_doc=fetch_recent_doc,
        )

    def test_data_empty_falls_back_to_recent_doc_id(self):
        # Rewrite payload to simulate Hindsight's bug: data={}.
        body_dict = {
            "event": "retain.completed",
            "bank_id": "hindsight-memorial",
            "operation_id": "op-x",
            "data": {},
        }
        empty_body = json.dumps(body_dict).encode()
        recent = [{"id": "u-recent", "document_id": "recovered-doc", "text": "x"}]
        units = [{"id": "u-recent", "text": "the recovered fact"}]

        fake_client = mock.MagicMock()
        fake_client.list_banks.return_value = ["hindsight-memorial"]
        fake_client.reflect.return_value = {
            "structured_output": {"superseded_fact_ids": [], "reasoning": "none"}
        }

        with mock.patch.dict(
            "os.environ", {"HINDSIGHT_API_URL": "http://api"}, clear=False
        ), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=fake_client,
        ):
            outcome = self._run_with_fallback(empty_body, units, recent)

        # The fallback recovered the document_id; reconcile ran normally.
        self.assertEqual(outcome.status, "abandoned")  # 0 superseded is "abandoned"
        self.assertEqual(outcome.document_id, "recovered-doc")
        self.assertEqual(outcome.units_processed, 1)

    def test_data_empty_with_no_recent_fallback_returns_skipped(self):
        body_dict = {
            "event": "retain.completed",
            "bank_id": "hindsight-memorial",
            "operation_id": "op-x",
            "data": {},
        }
        empty_body = json.dumps(body_dict).encode()
        # fetch_recent_doc returns None → no doc_id recoverable.
        outcome = self._run_with_fallback(
            empty_body, units=[], recent_units=[], fetch_recent_returns=lambda b: None
        )
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(outcome.units_processed, 0)
        self.assertEqual(outcome.units_skipped, 0)

    def test_data_with_doc_id_skips_fallback(self):
        """If the webhook already carries a document_id, fetch_recent_doc must
        NOT be called (no needless Hindsight load)."""
        body = _retain_event(memory_unit_count=1)
        units = [{"id": "u1", "text": "normal flow"}]

        recent_called = {"n": 0}

        def fetch_recent_doc(bank_id):
            recent_called["n"] += 1
            return "should-not-be-used"

        fake_client = mock.MagicMock()
        fake_client.list_banks.return_value = ["hindsight-memorial"]
        fake_client.reflect.return_value = {
            "structured_output": {"superseded_fact_ids": [], "reasoning": ""}
        }
        with mock.patch.dict(
            "os.environ", {"HINDSIGHT_API_URL": "http://api"}, clear=False
        ), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=fake_client,
        ):
            outcome = self._run_with_fallback(body, units, [], fetch_recent_doc)
        self.assertEqual(recent_called["n"], 0)
        self.assertEqual(outcome.document_id, "doc-abc")


class WebhookConfigLoaderTest(unittest.TestCase):
    def test_returns_memorial_config_with_event_bank_id(self):
        loader = wh.webhook_config_loader("b-from-event")
        with mock.patch.dict(
            "os.environ",
            {"HINDSIGHT_API_URL": "http://api", "HINDSIGHT_API_KEY": "k"},
            clear=False,
        ):
            cfg = loader(cwd="/anywhere")
        assert cfg is not None
        self.assertEqual(cfg.bank_id, "b-from-event")
        self.assertEqual(cfg.bank_source, "event")
        self.assertEqual(cfg.api_url, "http://api")
        self.assertEqual(cfg.api_key, "k")

    def test_returns_none_when_no_api_url(self):
        loader = wh.webhook_config_loader("b")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(loader(cwd=None))


if __name__ == "__main__":
    unittest.main()