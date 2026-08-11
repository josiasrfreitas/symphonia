"""Tests for `spawn.teardown` and `spawn.sweep` (GRE-189).

TLDR: `teardown` is `retire`'s body, extracted and made best-effort in a
fixed order and idempotent — a second call on an already-retired record
touches the adapter not at all. `sweep` audits the registry for a record
whose world (terminal, worktree) is already gone and tears it down without
being told which ticket/role died. `retire` itself keeps its own argv and
self-guard, covered unedited by `test_retire_self_guard.py`; this file
covers the two verbs that ticket added. Run either way:

    cd .symphonia/src && python3 -m unittest adapters.tests.test_teardown_sweep
    python3 .symphonia/src/adapters/tests/test_teardown_sweep.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

from adapters.tests.test_brief import _load_spawn


def _record(worktree: str, **over) -> dict:
    base = {
        "ticket": "GRE-9", "role": "implementer", "worktree": worktree,
        "terminal": "term-9", "task": "task-9", "dispatch": "disp-9",
        "capability": "dcap_xyz",
    }
    base.update(over)
    return base


class FakeAdapter:
    """Stands in for the four id-taking methods `teardown`/`sweep` use.
    `fail` names which of them raise the real adapter's `OrcaCliError`."""

    def __init__(self, error_cls):
        self.error_cls = error_cls
        self.calls: list[tuple] = []
        self.dispatch_status_value = "dispatched"
        self.terminals: set[str] = set()
        self.fail: set[str] = set()

    def _maybe_raise(self, name: str) -> None:
        if name in self.fail:
            raise self.error_cls(f"{name} failed")

    def stop_worker(self, dispatch_id):
        self.calls.append(("stop_worker", dispatch_id))
        self._maybe_raise("stop_worker")

    def close_terminal(self, handle, *, tab):
        self.calls.append(("close_terminal", handle, tab))
        self._maybe_raise("close_terminal")

    def dispatch_status(self, task_id, *, default="?"):
        self.calls.append(("dispatch_status", task_id))
        self._maybe_raise("dispatch_status")
        return self.dispatch_status_value

    def settle_task(self, task_id, reason):
        self.calls.append(("settle_task", task_id, reason))
        self._maybe_raise("settle_task")

    def list_terminals(self):
        self.calls.append(("list_terminals",))
        return set(self.terminals)


class SpawnRuntimeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SYMPHONIA_RUNTIME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SYMPHONIA_RUNTIME", None)

        self.spawn = _load_spawn()
        self.adapter = FakeAdapter(self.spawn._cli.OrcaCliError)
        self.spawn._adapter = lambda: self.adapter

    def worktree(self, name: str, *, exists: bool = True) -> str:
        path = Path(self.tmp.name) / name
        if exists:
            path.mkdir()
        return str(path)


class TeardownEffectsOrder(SpawnRuntimeCase):
    def _seed(self, **over) -> None:
        self.spawn.state_write({"GRE-9/implementer": _record(self.worktree("gre-9"), **over)})

    def test_effects_run_in_a_fixed_order_and_mark_retired(self):
        self._seed()
        result = self.spawn.teardown("GRE-9", "implementer")

        self.assertEqual(
            [c[0] for c in self.adapter.calls],
            ["stop_worker", "close_terminal", "dispatch_status", "settle_task"],
        )
        self.assertEqual(result["effects"], ["worker-stop", "terminal closed", "task settled as failed"])
        self.assertTrue(self.spawn.state_read()["GRE-9/implementer"]["retired"])

    def test_a_completed_dispatch_is_not_re_settled(self):
        self._seed()
        self.adapter.dispatch_status_value = "completed"
        result = self.spawn.teardown("GRE-9", "implementer")

        self.assertEqual([c[0] for c in self.adapter.calls], ["stop_worker", "close_terminal", "dispatch_status"])
        self.assertNotIn("task settled as failed", result["effects"])
        self.assertTrue(self.spawn.state_read()["GRE-9/implementer"]["retired"])


class TeardownEffectsAreBestEffort(SpawnRuntimeCase):
    """The condition the plan named: a failure that used to raise `SystemExit`
    and abort before `retired` was written now becomes an `effects` entry,
    and the chain keeps going."""

    def _seed(self) -> None:
        self.spawn.state_write({"GRE-9/implementer": _record(self.worktree("gre-9"))})

    def test_close_terminal_failure_does_not_block_settle_or_retired(self):
        self._seed()
        self.adapter.fail = {"close_terminal"}
        result = self.spawn.teardown("GRE-9", "implementer")

        self.assertIn("terminal not closed", result["effects"][1])
        self.assertEqual(
            [c[0] for c in self.adapter.calls],
            ["stop_worker", "close_terminal", "dispatch_status", "settle_task"],
        )
        self.assertTrue(self.spawn.state_read()["GRE-9/implementer"]["retired"])

    def test_settle_task_failure_still_marks_retired(self):
        self._seed()
        self.adapter.fail = {"settle_task"}
        result = self.spawn.teardown("GRE-9", "implementer")

        self.assertTrue(any("task NOT settled" in e for e in result["effects"]))
        self.assertTrue(self.spawn.state_read()["GRE-9/implementer"]["retired"])

    def test_dispatch_status_failure_skips_settle_but_still_marks_retired(self):
        self._seed()
        self.adapter.fail = {"dispatch_status"}
        result = self.spawn.teardown("GRE-9", "implementer")

        self.assertTrue(any("task NOT settled" in e for e in result["effects"]))
        self.assertEqual([c[0] for c in self.adapter.calls], ["stop_worker", "close_terminal", "dispatch_status"])
        self.assertTrue(self.spawn.state_read()["GRE-9/implementer"]["retired"])


class TeardownIsIdempotent(SpawnRuntimeCase):
    def test_an_already_retired_record_is_a_no_op(self):
        self.spawn.state_write({
            "GRE-9/implementer": _record(self.worktree("gre-9"), retired=True),
        })
        result = self.spawn.teardown("GRE-9", "implementer")

        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(result, {
            "retired": "GRE-9/implementer", "effects": ["already retired"],
            "worktree_kept": self.spawn.state_read()["GRE-9/implementer"]["worktree"],
        })

    def test_no_record_raises(self):
        with self.assertRaises(SystemExit):
            self.spawn.teardown("GRE-404", "implementer")


class SweepFindsOrphans(SpawnRuntimeCase):
    def test_orphan_by_dead_terminal(self):
        wt = self.worktree("gre-1")
        self.spawn.state_write({
            "GRE-1/implementer": _record(wt, ticket="GRE-1", role="implementer", terminal="term-dead"),
        })
        self.adapter.terminals = set()

        out = self.spawn.sweep(None)

        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["live"])
        self.assertIn("terminal not live", out[0]["reason"])
        self.assertTrue(self.spawn.state_read()["GRE-1/implementer"]["retired"])

    def test_orphan_by_missing_worktree(self):
        wt = self.worktree("gre-2", exists=False)
        self.spawn.state_write({
            "GRE-2/implementer": _record(wt, ticket="GRE-2", role="implementer", terminal="term-live"),
        })
        self.adapter.terminals = {"term-live"}

        out = self.spawn.sweep(None)

        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["live"])
        self.assertIn("worktree missing", out[0]["reason"])
        self.assertTrue(self.spawn.state_read()["GRE-2/implementer"]["retired"])

    def test_a_live_record_is_reported_but_not_torn_down(self):
        wt = self.worktree("gre-3")
        self.spawn.state_write({
            "GRE-3/implementer": _record(wt, ticket="GRE-3", role="implementer", terminal="term-live"),
        })
        self.adapter.terminals = {"term-live"}

        out = self.spawn.sweep(None)

        self.assertEqual(out, [{"key": "GRE-3/implementer", "live": True}])
        self.assertEqual(self.adapter.calls, [("list_terminals",)])
        self.assertNotIn("retired", self.spawn.state_read()["GRE-3/implementer"])

    def test_filters_by_ticket(self):
        wt4, wt5 = self.worktree("gre-4"), self.worktree("gre-5")
        self.spawn.state_write({
            "GRE-4/implementer": _record(wt4, ticket="GRE-4", role="implementer", terminal="term-dead-4"),
            "GRE-5/implementer": _record(wt5, ticket="GRE-5", role="implementer", terminal="term-dead-5"),
        })
        self.adapter.terminals = set()

        out = self.spawn.sweep("GRE-4")

        self.assertEqual([o["key"] for o in out], ["GRE-4/implementer"])
        self.assertTrue(self.spawn.state_read()["GRE-4/implementer"]["retired"])
        self.assertNotIn("retired", self.spawn.state_read()["GRE-5/implementer"])

    def test_an_already_retired_record_is_not_reported_or_touched(self):
        wt = self.worktree("gre-6")
        self.spawn.state_write({
            "GRE-6/implementer": _record(wt, ticket="GRE-6", role="implementer", terminal="term-dead", retired=True),
        })
        self.adapter.terminals = set()

        out = self.spawn.sweep(None)

        self.assertEqual(out, [])
        self.assertEqual([c for c in self.adapter.calls if c[0] != "list_terminals"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
