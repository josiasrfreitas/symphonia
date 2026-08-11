"""`retire` must refuse to close the terminal it is running in.

Found during the GRE-188 smoke run, by the role that was about to do it:
`retire GRE-188 planner`, typed inside the planner's own pane, would call
`stop_worker` and `close_terminal` on that very pane. The process dies
mid-function — the task never gets settled, the registry never records the
retirement, and the caller sees no error, because the terminal carrying the
error is the thing that was just closed.

Run either way:

    cd .symphonia/src && python3 -m unittest adapters.tests.test_retire_self_guard
    python3 .symphonia/src/adapters/tests/test_retire_self_guard.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE))

from adapters.tests.test_brief import _load_spawn

RECORD = {
    "GRE-900/planner": {
        "ticket": "GRE-900",
        "role": "planner",
        "terminal": "term_the_callers_own_pane",
        "task": "task_1",
        "dispatch": "ctx_1",
        "worktree": "/workspaces/gre-900",
    }
}


class RetireRefusesToCloseItsOwnTerminal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SYMPHONIA_RUNTIME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SYMPHONIA_RUNTIME", None)

        self.spawn = _load_spawn()
        self.spawn.STATE.write_text(json.dumps(RECORD))

        # Any orca traffic at all is a failure of the guard: it must refuse
        # before `stop_worker`, which is itself enough to kill the caller.
        self.adapter = mock.MagicMock()
        patcher = mock.patch.object(self.spawn, "_adapter", return_value=self.adapter)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _retire(self):
        return self.spawn.retire("GRE-900", "planner")

    def test_refuses_when_the_caller_is_the_role_being_retired(self):
        with mock.patch.dict(
            os.environ, {"ORCA_TERMINAL_HANDLE": "term_the_callers_own_pane"}
        ):
            with self.assertRaises(SystemExit) as caught:
                self._retire()

        self.assertIn("the role running this command", str(caught.exception))
        self.adapter.stop_worker.assert_not_called()
        self.adapter.close_terminal.assert_not_called()
        self.adapter.settle_task.assert_not_called()

    def test_the_registry_is_left_untouched(self):
        with mock.patch.dict(
            os.environ, {"ORCA_TERMINAL_HANDLE": "term_the_callers_own_pane"}
        ):
            with self.assertRaises(SystemExit):
                self._retire()

        # A refusal that half-retired the role would be worse than the bug.
        self.assertEqual(json.loads(self.spawn.STATE.read_text()), RECORD)

    def test_the_orchestrator_retiring_a_role_is_unaffected(self):
        """The Orchestrator runs from a terminal that is nobody's role, so
        `ORCA_TERMINAL_HANDLE` is unset or belongs to someone else. The
        guard must not fire there — that is the normal path."""

        self.adapter.dispatch_status.return_value = "completed"
        for handle in ("", "term_some_other_pane"):
            with self.subTest(handle=handle):
                self.adapter.reset_mock()
                with mock.patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": handle}):
                    result = self._retire()

                self.assertEqual(result["retired"], "GRE-900/planner")
                self.adapter.close_terminal.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
