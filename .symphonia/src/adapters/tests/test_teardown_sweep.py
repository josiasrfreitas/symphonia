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
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

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
        self.adapter.terminals = {"term-unrelated-live"}

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
        self.adapter.terminals = {"term-unrelated-live"}

        out = self.spawn.sweep("GRE-4")

        self.assertEqual([o["key"] for o in out], ["GRE-4/implementer"])
        self.assertTrue(self.spawn.state_read()["GRE-4/implementer"]["retired"])
        self.assertNotIn("retired", self.spawn.state_read()["GRE-5/implementer"])

    def test_an_already_retired_record_is_not_reported_or_touched(self):
        wt = self.worktree("gre-6")
        self.spawn.state_write({
            "GRE-6/implementer": _record(wt, ticket="GRE-6", role="implementer", terminal="term-dead", retired=True),
        })
        self.adapter.terminals = {"term-unrelated-live"}

        out = self.spawn.sweep(None)

        self.assertEqual(out, [])
        self.assertEqual([c for c in self.adapter.calls if c[0] != "list_terminals"], [])


class SweepRefusesAnEmptyTerminalList(SpawnRuntimeCase):
    """An empty `list_terminals()` response is indistinguishable, from
    inside the loop, from every role having died at once — the far more
    likely explanation is a degraded CLI. GRE-189 F4: refuse rather than
    tear down everything unretired on the strength of one bad response."""

    def test_refuses_and_touches_nothing(self):
        wt = self.worktree("gre-7")
        self.spawn.state_write({
            "GRE-7/implementer": _record(wt, ticket="GRE-7", role="implementer", terminal="term-live"),
        })
        self.adapter.terminals = set()

        with self.assertRaises(SystemExit) as caught:
            self.spawn.sweep(None)

        self.assertIn("list_terminals()", str(caught.exception))
        self.assertEqual([c for c in self.adapter.calls if c[0] != "list_terminals"], [])
        self.assertNotIn("retired", self.spawn.state_read()["GRE-7/implementer"])


class SweepLocksTheRegistry(SpawnRuntimeCase):
    """GRE-189 F1 regression. Before this fix `sweep` read and wrote the
    registry with no lock at all, so a concurrent `state_lock()` holder
    (`wait`/`verdict`, sitting on a stale snapshot) could write back over a
    `teardown` `sweep` had already run — after that teardown's effects
    (`close_terminal`, `settle_task`) were already irreversible. Reproduced
    by the standards reviewer with `repro_lock.py`; this pins the fix: a
    concurrent lock holder must block `sweep`, not race it."""

    def test_sweep_blocks_on_a_held_lock_and_does_not_lose_either_write(self):
        wt = self.worktree("gre-7")
        self.spawn.state_write({
            "GRE-7/implementer": _record(wt, ticket="GRE-7", role="implementer", terminal="term-dead"),
        })
        self.adapter.terminals = {"term-unrelated-live"}

        HOLD_SECONDS = 0.3
        acquired = threading.Event()

        def hold_the_lock():
            with self.spawn.state_lock():
                acquired.set()
                data = self.spawn.state_read()
                # Stands in for a concurrent `verdict` write elsewhere in
                # the registry — must survive `sweep`'s write, not be
                # clobbered by it.
                data["GRE-7/implementer"]["gate_state"] = "verdict-approved"
                time.sleep(HOLD_SECONDS)
                self.spawn.state_write(data)

        holder = threading.Thread(target=hold_the_lock)
        holder.start()
        self.assertTrue(acquired.wait(timeout=2), "lock holder never acquired state_lock()")

        started = time.monotonic()
        out = self.spawn.sweep(None)
        elapsed = time.monotonic() - started
        holder.join()

        self.assertGreaterEqual(
            elapsed, HOLD_SECONDS,
            "sweep did not block on the concurrently-held state_lock()",
        )
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["live"])
        rec = self.spawn.state_read()["GRE-7/implementer"]
        self.assertTrue(rec["retired"], "sweep's teardown write was lost")
        self.assertEqual(
            rec["gate_state"], "verdict-approved",
            "the lock holder's write was lost",
        )


class TeardownReportsWhenTheRecordVanished(SpawnRuntimeCase):
    """GRE-189 F7. `_run_teardown` guards the `retired` write with
    `if key in data`, but used to report success regardless. Only reachable
    under the same concurrent-write conditions as F1, so this drives it
    directly by deleting the record between the two reads `_run_teardown`
    does."""

    def test_effects_say_the_record_vanished_instead_of_claiming_success(self):
        self.spawn.state_write({"GRE-9/implementer": _record(self.worktree("gre-9"))})
        real_state_read = self.spawn.state_read
        calls = []

        def state_read_then_delete():
            data = real_state_read()
            calls.append(data)
            # 1st read: `teardown`'s own idempotence check. 2nd: the guard
            # read at the top of `_run_teardown`. 3rd: the read this test
            # targets — the one right before the guarded `retired` write.
            if len(calls) == 3:
                data = dict(data)
                del data["GRE-9/implementer"]
            return data

        with mock.patch.object(self.spawn, "state_read", side_effect=state_read_then_delete):
            result = self.spawn.teardown("GRE-9", "implementer")

        self.assertIn("registry record vanished before the retired write", result["effects"])


class RetireRerunsEffectsOnADeadRole(SpawnRuntimeCase):
    """GRE-189: `retire`'s non-idempotence is a deliberate product choice —
    a human retiring an already-dead role a second time is asking "try
    again, the world may have changed", not expecting a silent no-op. That
    intent used to be pinned only by accident, as a side effect of
    `test_retire_self_guard`'s `subTest` reusing state across iterations.
    This test names the intent directly, independent of that guard."""

    def test_retiring_an_already_retired_record_reruns_every_effect(self):
        self.spawn.state_write({
            "GRE-9/implementer": _record(self.worktree("gre-9"), retired=True),
        })

        with mock.patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": ""}):
            result = self.spawn.retire("GRE-9", "implementer")

        self.assertEqual(
            [c[0] for c in self.adapter.calls],
            ["stop_worker", "close_terminal", "dispatch_status", "settle_task"],
        )
        self.assertEqual(result["retired"], "GRE-9/implementer")
        self.assertTrue(self.spawn.state_read()["GRE-9/implementer"]["retired"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
