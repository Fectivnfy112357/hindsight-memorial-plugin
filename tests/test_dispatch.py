"""Tests for ``hindsight_memorial.dispatch``.

Covers the two properties the 2026-07-30 incident needed and did not have:
a replay is dropped rather than reprocessed, and the worker survives a
failing job.
"""
from __future__ import annotations

import threading
import unittest

from hindsight_memorial import dispatch


class _Recorder:
    """Callable that records every (raw_body, headers) it receives."""

    def __init__(self, fail: bool = False, block: threading.Event | None = None):
        self.calls: list[bytes] = []
        self.fail = fail
        self.block = block
        self._lock = threading.Lock()

    def __call__(self, raw_body: bytes, headers: dict[str, str]):
        with self._lock:
            self.calls.append(raw_body)
        if self.block is not None:
            self.block.wait(timeout=5)
        if self.fail:
            raise RuntimeError("simulated reconcile failure")
        return None


class EventKeyTest(unittest.TestCase):
    def test_identical_bodies_share_a_key(self):
        body = b'{"event":"retain.completed"}'
        self.assertEqual(dispatch.event_key(body), dispatch.event_key(bytes(body)))

    def test_different_bodies_differ(self):
        self.assertNotEqual(dispatch.event_key(b"a"), dispatch.event_key(b"b"))


class DedupTest(unittest.TestCase):
    def setUp(self):
        self.rec = _Recorder()
        self.d = dispatch.Dispatcher(self.rec)
        self.d.start()
        self.addCleanup(self.d.stop)

    def test_replayed_body_is_processed_once(self):
        """The incident's core failure: 5 identical deliveries, 5 reconciles.

        Hindsight replayed one operation at +5min/+30min/+2h/+5h with a
        byte-identical body. Only the first may reach the worker.
        """
        body = b'{"event":"retain.completed","operation_id":"d1b21d2e"}'
        headers = {"X-Hindsight-Event": "retain.completed"}

        results = [self.d.submit(body, headers) for _ in range(5)]

        self.assertTrue(self.d.wait_idle())
        self.assertEqual(results[0], dispatch.QUEUED)
        self.assertEqual(results[1:], [dispatch.DUPLICATE] * 4)
        self.assertEqual(len(self.rec.calls), 1)

    def test_distinct_bodies_all_run(self):
        """Dedup must not swallow genuinely different events."""
        headers = {}
        for i in range(3):
            self.d.submit(f'{{"op":{i}}}'.encode(), headers)

        self.assertTrue(self.d.wait_idle())
        self.assertEqual(len(self.rec.calls), 3)

    def test_same_document_ingested_twice_is_not_deduped(self):
        """Re-ingesting a document is legitimate work, not a replay.

        This is why the key is the body hash and not document_id: these two
        payloads name the same document but are separate retains.
        """
        headers = {}
        first = b'{"operation_id":"op-1","data":{"document_id":"doc-1"}}'
        second = b'{"operation_id":"op-2","data":{"document_id":"doc-1"}}'

        self.d.submit(first, headers)
        self.d.submit(second, headers)

        self.assertTrue(self.d.wait_idle())
        self.assertEqual(len(self.rec.calls), 2)

    def test_duplicate_arriving_mid_flight_is_dropped(self):
        """A retry landing while the first is still running must not start a second.

        This is the case that forces marking in_flight at enqueue time rather
        than on completion — reconciles take 10-70s and retries arrive inside
        that window.
        """
        gate = threading.Event()
        rec = _Recorder(block=gate)
        d = dispatch.Dispatcher(rec)
        d.start()
        self.addCleanup(d.stop)

        body = b'{"operation_id":"slow"}'
        self.assertEqual(d.submit(body, {}), dispatch.QUEUED)

        # Wait until the worker has actually picked the job up.
        for _ in range(500):
            if rec.calls:
                break
            threading.Event().wait(0.01)
        self.assertEqual(d.state_of(body), dispatch.IN_FLIGHT)

        # The retry arrives while the first is mid-reconcile.
        self.assertEqual(d.submit(body, {}), dispatch.DUPLICATE)

        gate.set()
        self.assertTrue(d.wait_idle())
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(d.state_of(body), dispatch.DONE)


class WorkerResilienceTest(unittest.TestCase):
    def test_worker_survives_a_failing_job(self):
        """One bad event must not wedge the queue for every later event."""
        rec = _Recorder(fail=True)
        d = dispatch.Dispatcher(rec)
        d.start()
        self.addCleanup(d.stop)

        with self.assertLogs(dispatch.log, level="ERROR") as captured:
            d.submit(b'{"op":1}', {})
            self.assertTrue(d.wait_idle())

        joined = "\n".join(captured.output)
        self.assertIn("processing failed", joined)
        self.assertIn("simulated reconcile failure", joined)
        self.assertTrue(any("Traceback" in line for line in captured.output))

        # Worker still alive: a subsequent event is processed.
        rec.fail = False
        d.submit(b'{"op":2}', {})
        self.assertTrue(d.wait_idle())
        self.assertEqual(len(rec.calls), 2)

    def test_failed_job_is_not_retried_by_a_replay(self):
        """A failure still marks the key done — a replay would fail identically."""
        rec = _Recorder(fail=True)
        d = dispatch.Dispatcher(rec)
        d.start()
        self.addCleanup(d.stop)

        body = b'{"op":"boom"}'
        with self.assertLogs(dispatch.log, level="ERROR"):
            d.submit(body, {})
            self.assertTrue(d.wait_idle())

        self.assertEqual(d.state_of(body), dispatch.DONE)
        self.assertEqual(d.submit(body, {}), dispatch.DUPLICATE)


class EvictionTest(unittest.TestCase):
    def test_table_is_bounded_and_evicts_oldest_first(self):
        d = dispatch.Dispatcher(_Recorder(), max_keys=3)
        d.start()
        self.addCleanup(d.stop)

        for i in range(5):
            d.submit(f'{{"op":{i}}}'.encode(), {})
        self.assertTrue(d.wait_idle())

        self.assertEqual(d.stats()["keys"], 3)
        # Oldest evicted, newest retained.
        self.assertIsNone(d.state_of(b'{"op":0}'))
        self.assertEqual(d.state_of(b'{"op":4}'), dispatch.DONE)

    def test_evicting_an_in_flight_key_is_logged(self):
        """Evicting in_flight means the table is too small for the retry window."""
        gate = threading.Event()
        d = dispatch.Dispatcher(_Recorder(block=gate), max_keys=1)
        d.start()
        self.addCleanup(d.stop)

        with self.assertLogs(dispatch.log, level="WARNING") as captured:
            d.submit(b'{"op":1}', {})
            d.submit(b'{"op":2}', {})

        self.assertIn("evicted an in_flight dedup key", "\n".join(captured.output))
        gate.set()


if __name__ == "__main__":
    unittest.main()
