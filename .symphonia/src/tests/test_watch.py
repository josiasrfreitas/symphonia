"""Tests for `watch` (pidfile primitives), `journal.read_events`, and the
`spawn` watcher: `watch`/`watch_daemon`, and the watcher-view mode `wait`
falls into while one is alive. Run either way:

    cd .symphonia/src && python3 -m unittest tests.test_watch
    python3 .symphonia/src/tests/test_watch.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import journal as JOURNAL
import spawn as SPAWN
import watch as WATCH


class Pidfile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runtime_dir = Path(self._tmp.name)

    def test_write_then_read_round_trips(self):
        WATCH.write_pidfile(self.runtime_dir, 4242)
        self.assertEqual(WATCH.read_pidfile(self.runtime_dir), 4242)

    def test_no_pidfile_and_removing_it_twice_both_read_none(self):
        self.assertIsNone(WATCH.read_pidfile(self.runtime_dir))
        WATCH.remove_pidfile(self.runtime_dir)  # no prior write, no raise
        WATCH.write_pidfile(self.runtime_dir, 1)
        WATCH.remove_pidfile(self.runtime_dir)
        WATCH.remove_pidfile(self.runtime_dir)  # already gone, no raise
        self.assertIsNone(WATCH.read_pidfile(self.runtime_dir))

    def test_replace_is_atomic_no_tmp_left_behind(self):
        WATCH.write_pidfile(self.runtime_dir, 1)
        WATCH.write_pidfile(self.runtime_dir, 2)
        self.assertEqual(list(self.runtime_dir.glob("*.tmp")), [])
        self.assertEqual(WATCH.read_pidfile(self.runtime_dir), 2)

    def test_corrupt_pidfile_reads_none(self):
        (self.runtime_dir / WATCH.PIDFILE).write_text("not-a-pid\n")
        self.assertIsNone(WATCH.read_pidfile(self.runtime_dir))

    def test_own_pid_is_alive_impossible_and_stale_pid_are_not(self):
        self.assertTrue(WATCH.alive(os.getpid()))
        self.assertFalse(WATCH.alive(2**30))
        WATCH.write_pidfile(self.runtime_dir, 2**30)
        self.assertFalse(WATCH.alive(WATCH.read_pidfile(self.runtime_dir)))


class ReadEvents(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runtime_dir = Path(self._tmp.name)

    def _msg(self, msg_id):
        return {"id": msg_id, "type": "x", "subject": "s", "body": "b",
                "payload": {}, "sender": "t"}

    def test_no_file_and_zero_limit_both_return_empty(self):
        self.assertEqual(JOURNAL.read_events(self.runtime_dir, 10), [])
        JOURNAL.append_events(self.runtime_dir, "d-1", [self._msg("m-1")])
        self.assertEqual(JOURNAL.read_events(self.runtime_dir, 0), [])

    def test_returns_the_tail_oldest_first_capped_by_limit(self):
        for i in range(5):
            JOURNAL.append_events(self.runtime_dir, f"d-{i}", [self._msg(f"m-{i}")])
        events = JOURNAL.read_events(self.runtime_dir, 2)
        self.assertEqual([e["id"] for e in events], ["m-3", "m-4"])
        self.assertEqual(len(JOURNAL.read_events(self.runtime_dir, 100)), 5)

    def test_a_truncated_trailing_line_is_skipped_not_raised(self):
        JOURNAL.append_events(self.runtime_dir, "d-1", [self._msg("m-1")])
        path = self.runtime_dir / JOURNAL.EVENTS_FILE
        with path.open("a") as handle:
            handle.write('{"id": "m-2", "type": "x", "subject":')  # torn write, no newline
        events = JOURNAL.read_events(self.runtime_dir, 10)
        self.assertEqual([e["id"] for e in events], ["m-1"])


class RuntimeCase(unittest.TestCase):
    """A redirected `SYMPHONIA_RUNTIME`, for tests that go through `spawn`
    verbs rather than calling `watch`/`journal` directly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SYMPHONIA_RUNTIME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SYMPHONIA_RUNTIME", None)
        self.runtime_dir = Path(self.tmp.name)

    def _batch(self, delivery_id="", messages=()):
        return SimpleNamespace(delivery_id=delivery_id, messages=messages)

    def _message(self, msg_id="m-1", type_="escalation", body="hi", payload=None):
        return SimpleNamespace(
            id=msg_id, type=type_, subject="s", body=body,
            payload=payload or {}, sender="term-x",
        )


class WaitGuard(RuntimeCase):
    def test_watcher_alive_returns_the_journal_view_without_touching_the_mailbox(self):
        WATCH.write_pidfile(self.runtime_dir, os.getpid())
        JOURNAL.append_events(self.runtime_dir, "d-1", [
            {"id": "m-1", "type": "worker_done", "subject": "s", "body": "b",
             "payload": {}, "sender": "t"},
        ])
        with mock.patch.object(SPAWN._orca, "check_wait") as check:
            result = SPAWN.wait(ack=None, timeout_ms=1000)
        check.assert_not_called()
        self.assertEqual(result["mode"], "watcher")
        self.assertEqual(result["watcher_pid"], os.getpid())
        self.assertEqual(result["actions"], [])
        self.assertEqual([e["id"] for e in result["events"]], ["m-1"])
        self.assertEqual(
            {k: result[k] for k in ("delivery_id", "acked", "elapsed_ms", "unattributed")},
            {"delivery_id": None, "acked": None, "elapsed_ms": 0, "unattributed": []},
        )

    def test_explicit_ack_with_watcher_alive_refuses_instead_of_being_dropped(self):
        WATCH.write_pidfile(self.runtime_dir, os.getpid())
        with mock.patch.object(SPAWN._orca, "check_wait") as check:
            with self.assertRaisesRegex(SPAWN.Refusal, "watcher"):
                SPAWN.wait(ack="d-1", timeout_ms=1000)
        check.assert_not_called()

    def test_stale_pidfile_and_no_pidfile_both_consume_directly(self):
        for pid in (2**30, None):
            if pid:
                WATCH.write_pidfile(self.runtime_dir, pid)
            with mock.patch.object(SPAWN._orca, "check_wait", return_value=self._batch()), \
                 mock.patch.object(SPAWN.time, "monotonic", return_value=0.0):
                result = SPAWN.wait(ack=None, timeout_ms=1000)
            self.assertEqual(result["mode"], "consumed")


class WatchLoop(RuntimeCase):
    def setUp(self):
        super().setUp()
        self.sleep_calls: list[float] = []
        patcher = mock.patch.object(SPAWN.time, "sleep", side_effect=self.sleep_calls.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_watch_refuses_but_a_stale_pidfile_does_not_block_one(self):
        WATCH.write_pidfile(self.runtime_dir, os.getpid())
        with self.assertRaisesRegex(SPAWN.Refusal, "already running"):
            SPAWN.watch(timeout_ms=900000)

        WATCH.write_pidfile(self.runtime_dir, 2**30)
        with mock.patch.object(SPAWN._orca, "check_wait") as check:
            SPAWN.watch(timeout_ms=900000, _max_cycles=0)
        check.assert_not_called()
        self.assertIsNone(WATCH.read_pidfile(self.runtime_dir), "cleaned up on the way out")

    def test_empty_batch_floor_sleeps_the_complement_or_nothing_if_timeout_was_full(self):
        with mock.patch.object(SPAWN._orca, "check_wait", return_value=self._batch()), \
             mock.patch.object(SPAWN.time, "monotonic", side_effect=[0.0, 0.0]):
            SPAWN.watch(timeout_ms=900000, _max_cycles=1)
        self.assertEqual(len(self.sleep_calls), 1)
        self.assertAlmostEqual(self.sleep_calls[0], SPAWN.EMPTY_BATCH_FLOOR_MS / 1000)

        self.sleep_calls.clear()
        full = SPAWN.EMPTY_BATCH_FLOOR_MS / 1000 + 1
        with mock.patch.object(SPAWN._orca, "check_wait", return_value=self._batch()), \
             mock.patch.object(SPAWN.time, "monotonic", side_effect=[0.0, full]):
            SPAWN.watch(timeout_ms=900000, _max_cycles=1)
        self.assertEqual(self.sleep_calls, [])

    def test_batch_with_events_is_journaled_and_the_receipt_lands_after_processing(self):
        seen_receipt_during_processing = []

        def fake_gate_run(events, raw_by_id, data, **kwargs):
            seen_receipt_during_processing.append(JOURNAL.read_receipt(self.runtime_dir))
            return [], []

        batch = self._batch("d-1", (self._message(),))
        with mock.patch.object(SPAWN._orca, "check_wait", return_value=batch), \
             mock.patch.object(SPAWN.time, "monotonic", return_value=0.0), \
             mock.patch.object(SPAWN._gate, "run", side_effect=fake_gate_run):
            SPAWN.watch(timeout_ms=900000, _max_cycles=1)

        self.assertEqual(
            seen_receipt_during_processing, [None],
            "the receipt must not exist yet while gate.run is still processing",
        )
        self.assertEqual(JOURNAL.read_receipt(self.runtime_dir), "d-1")
        lines = (self.runtime_dir / JOURNAL.EVENTS_FILE).read_text().splitlines()
        self.assertEqual(len(lines), 1)

    def test_check_error_is_transient_but_a_processing_error_stops_the_loop(self):
        attempts = iter([SPAWN._orca.OrcaCliError("boom"), self._batch()])

        def side_effect(**kwargs):
            item = next(attempts)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(SPAWN._orca, "check_wait", side_effect=side_effect), \
             mock.patch.object(SPAWN.time, "monotonic", return_value=0.0):
            SPAWN.watch(timeout_ms=900000, _max_cycles=2)
        self.assertEqual(len(self.sleep_calls), 2, "both the error and the empty batch sleep")
        log = (self.runtime_dir / "watch.log").read_text()
        self.assertIn("check failed", log)
        self.assertIn("boom", log)

        with mock.patch.object(SPAWN._orca, "check_wait", return_value=self._batch()), \
             mock.patch.object(SPAWN._gate, "run", side_effect=ValueError("corrupted gate_state")):
            with self.assertRaises(ValueError):
                SPAWN.watch(timeout_ms=900000)
        self.assertIsNone(WATCH.read_pidfile(self.runtime_dir))
        log = (self.runtime_dir / "watch.log").read_text()
        self.assertIn("stopping on error", log)
        self.assertIn("corrupted gate_state", log)


class WatchDaemon(RuntimeCase):
    def test_returns_pid_and_log_once_the_pidfile_appears(self):
        fake_proc = mock.Mock(pid=4242)

        def fake_popen(*args, **kwargs):
            WATCH.write_pidfile(self.runtime_dir, 4242)
            return fake_proc

        with mock.patch.object(SPAWN.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(SPAWN._watch, "alive", return_value=True):
            result = SPAWN.watch_daemon(timeout_ms=1000)
        self.assertEqual(result["pid"], 4242)
        self.assertTrue(result["log"].endswith("watch.log"))

    def test_refuses_before_popen_if_a_watcher_is_already_alive(self):
        WATCH.write_pidfile(self.runtime_dir, os.getpid())
        with mock.patch.object(SPAWN.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(SPAWN.Refusal, "already running"):
                SPAWN.watch_daemon(timeout_ms=1000)
        popen.assert_not_called()

    def test_kills_the_child_and_refuses_if_the_pidfile_never_appears(self):
        fake_proc = mock.Mock(pid=4242)
        with mock.patch.object(SPAWN.subprocess, "Popen", return_value=fake_proc), \
             mock.patch.object(SPAWN, "_DAEMON_STARTUP_TIMEOUT_S", 0.05), \
             mock.patch.object(SPAWN, "_DAEMON_STARTUP_POLL_S", 0.01):
            with self.assertRaisesRegex(SPAWN.Refusal, "did not write its pidfile"):
                SPAWN.watch_daemon(timeout_ms=1000)
        fake_proc.kill.assert_called_once()
        fake_proc.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
