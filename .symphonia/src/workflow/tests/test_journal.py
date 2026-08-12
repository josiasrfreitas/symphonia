"""Tests for `workflow.journal` — the event journal and delivery receipt
persisted by `spawn.wait`. Run either way:

    cd .symphonia/src && python3 -m unittest workflow.tests.test_journal
    python3 .symphonia/src/workflow/tests/test_journal.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

from workflow import journal as JOURNAL

MESSAGE = {
    "id": "msg-1", "type": "worker_done", "subject": "done",
    "body": "## Report\nfull body here", "payload": {"outcome": "succeeded"}, "sender": "term-9",
}


class AppendEvents(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runtime_dir = Path(self._tmp.name)

    def _lines(self):
        path = self.runtime_dir / journal_events_file()
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_appends_one_line_per_message(self):
        JOURNAL.append_events(self.runtime_dir, "d-1", [MESSAGE, {**MESSAGE, "id": "msg-2"}])

        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["id"], "msg-1")
        self.assertEqual(lines[1]["id"], "msg-2")

    def test_accumulates_across_calls(self):
        JOURNAL.append_events(self.runtime_dir, "d-1", [MESSAGE])
        JOURNAL.append_events(self.runtime_dir, "d-2", [{**MESSAGE, "id": "msg-2"}])

        lines = self._lines()
        self.assertEqual([line["delivery_id"] for line in lines], ["d-1", "d-2"])

    def test_empty_batch_writes_nothing(self):
        JOURNAL.append_events(self.runtime_dir, "", [])

        self.assertFalse((self.runtime_dir / journal_events_file()).exists())

    def test_line_carries_full_body(self):
        JOURNAL.append_events(self.runtime_dir, "d-1", [MESSAGE])

        self.assertEqual(self._lines()[0]["body"], MESSAGE["body"])

    def test_line_has_received_at_and_delivery_id(self):
        JOURNAL.append_events(self.runtime_dir, "d-1", [MESSAGE])

        line = self._lines()[0]
        self.assertEqual(line["delivery_id"], "d-1")
        self.assertIn("received_at", line)


class Receipt(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runtime_dir = Path(self._tmp.name)

    def test_no_receipt_reads_none(self):
        self.assertIsNone(JOURNAL.read_receipt(self.runtime_dir))

    def test_write_then_read_round_trips(self):
        JOURNAL.write_receipt(self.runtime_dir, "d-1")

        self.assertEqual(JOURNAL.read_receipt(self.runtime_dir), "d-1")

    def test_empty_delivery_id_removes_receipt(self):
        JOURNAL.write_receipt(self.runtime_dir, "d-1")
        JOURNAL.write_receipt(self.runtime_dir, "")

        self.assertIsNone(JOURNAL.read_receipt(self.runtime_dir))
        self.assertFalse((self.runtime_dir / journal_receipt_file()).exists())

    def test_removing_receipt_when_none_exists_does_not_raise(self):
        JOURNAL.write_receipt(self.runtime_dir, "")  # no prior write_receipt call

        self.assertIsNone(JOURNAL.read_receipt(self.runtime_dir))

    def test_replace_is_atomic_no_tmp_left_behind(self):
        JOURNAL.write_receipt(self.runtime_dir, "d-1")
        JOURNAL.write_receipt(self.runtime_dir, "d-2")

        leftovers = list(self.runtime_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        self.assertEqual(JOURNAL.read_receipt(self.runtime_dir), "d-2")


def journal_events_file() -> str:
    return JOURNAL.EVENTS_FILE


def journal_receipt_file() -> str:
    return JOURNAL.RECEIPT_FILE


if __name__ == "__main__":
    unittest.main(verbosity=2)
