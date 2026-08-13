"""Tests for the verbs that guard the one-shot `worker_done` and the
Orchestrator-side teardown refusals.

Every check here pins something measured against Orca 1.4.168, not a style
preference:

- `--payload` and the structured flags (`--task-id`, `--dispatch-id`,
  `--outcome`) are mutually exclusive — sending both is `invalid_argument`
  and nothing goes out, so `done` must send exactly one `--payload`.
- A dispatch grants exactly one `worker_done`, so every refusal (empty
  body, unapproved planner, empty success) must fire BEFORE anything is
  sent.
- `retire` run from inside the role's own pane would close the terminal
  mid-command; it must refuse.
- `sweep` on an empty `list_terminals()` would tear down every live role
  on one degraded CLI response; it must refuse.

The registry is redirected with SYMPHONIA_RUNTIME — read per call, so no
module reload is needed — and `orca.orca`, the one function that runs the
CLI, is replaced by an argv recorder: the argv asserted here is the exact
argv production would execute. No CLI, no network. Run either way:

    cd .symphonia/src && python3 -m unittest tests.test_role_verbs
    python3 .symphonia/src/tests/test_role_verbs.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry as REGISTRY
import spawn as SPAWN


class RoleVerbCase(unittest.TestCase):
    """A redirected registry, a recorded `orca.orca`, and one implementer
    record whose terminal is the caller's own (`ORCA_TERMINAL_HANDLE`)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SYMPHONIA_RUNTIME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SYMPHONIA_RUNTIME", None)

        self.calls: list[tuple[tuple[str, ...], bool]] = []

        def record(*argv, expect_lifecycle_ok=False):
            self.calls.append((argv, expect_lifecycle_ok))
            return {}

        patcher = mock.patch.object(SPAWN._orca, "orca", side_effect=record)
        patcher.start()
        self.addCleanup(patcher.stop)

        os.environ["ORCA_TERMINAL_HANDLE"] = "term-1"
        self.addCleanup(os.environ.pop, "ORCA_TERMINAL_HANDLE", None)

        self.record = {
            "ticket": "GRE-1", "role": "implementer", "tier": "standard",
            "access": "write", "worktree": "/tmp/gre-1", "terminal": "term-1",
            "task": "task-1", "dispatch": "disp-1", "capability": "dcap-1",
            "head_at_dispatch": "aaa111",
        }
        with REGISTRY.transaction() as data:
            data["GRE-1/implementer"] = dict(self.record)

        self.body = Path(self.tmp.name) / "report.md"
        self.body.write_text("Did the thing.\n")

    def _measure(self, head="bbb222", dirty=False):
        return mock.patch.object(
            SPAWN, "_worktree_measurement", return_value=(head, dirty)
        )


class TestDonePayloadShape(RoleVerbCase):
    def test_done_sends_exactly_one_payload_and_no_structured_flags(self):
        with self._measure():
            SPAWN.done("GRE-1", str(self.body), outcome="succeeded",
                       files_modified="a.py, b.py")
        (argv, lifecycle_ok) = self.calls[-1]
        self.assertTrue(lifecycle_ok, "worker_done must check the lifecycle answer")
        self.assertEqual(argv[:2], ("orchestration", "send"))
        self.assertEqual(argv.count("--payload"), 1)
        for forbidden in ("--task-id", "--dispatch-id", "--outcome", "--files-modified"):
            self.assertNotIn(forbidden, argv)
        payload = json.loads(argv[argv.index("--payload") + 1])
        self.assertEqual(payload["taskId"], "task-1")
        self.assertEqual(payload["dispatchId"], "disp-1")
        self.assertEqual(payload["outcome"], "succeeded")
        self.assertEqual(payload["filesModified"], ["a.py", "b.py"])
        self.assertIn("--dispatch-capability", argv)
        self.assertEqual(argv[argv.index("--dispatch-capability") + 1], "dcap-1")

    def test_empty_body_is_refused_before_anything_is_sent(self):
        self.body.write_text("   \n")
        with self.assertRaisesRegex(SPAWN.Refusal, "empty report"):
            SPAWN.done("GRE-1", str(self.body), outcome="failed", files_modified="")
        self.assertEqual(self.calls, [], "the one shot was not spent")


class TestDoneEmptySuccess(RoleVerbCase):
    def test_no_change_at_all_is_refused(self):
        with self._measure(head="aaa111", dirty=False):
            with self.assertRaisesRegex(SPAWN.Refusal, "no\\s+change"):
                SPAWN.done("GRE-1", str(self.body), outcome="succeeded",
                           files_modified="")
        self.assertEqual(self.calls, [], "an empty success never reaches Orca")

    def test_dirty_tree_is_accepted_and_flagged(self):
        with self._measure(head="aaa111", dirty=True):
            SPAWN.done("GRE-1", str(self.body), outcome="succeeded",
                       files_modified="")
        self.assertEqual(len(self.calls), 1, "the report was sent")
        rec = REGISTRY.read()["GRE-1/implementer"]
        self.assertTrue(rec["uncommitted_work"])

    def test_a_failed_outcome_skips_the_check(self):
        with self._measure(head="aaa111", dirty=False):
            SPAWN.done("GRE-1", str(self.body), outcome="failed", files_modified="")
        self.assertEqual(len(self.calls), 1)


class TestPlannerDoneNeedsApproval(RoleVerbCase):
    def setUp(self):
        super().setUp()
        planner = dict(self.record)
        planner.update(role="planner", terminal="term-2", dispatch="disp-2",
                       task="task-2", gate_state=SPAWN.IDLE)
        with REGISTRY.transaction() as data:
            data["GRE-1/planner"] = planner
        os.environ["ORCA_TERMINAL_HANDLE"] = "term-2"

    def test_refused_before_the_gate_recorded_an_approval(self):
        self.body.write_text("## Plan\np\n\n## Approval\n1 rodada.\n\n## Deviations\nNone.\n")
        with self.assertRaisesRegex(SPAWN.Refusal, "not approved"):
            SPAWN.done("GRE-1", str(self.body), outcome="succeeded", files_modified="")
        self.assertEqual(self.calls, [], "the one shot was not spent")

    def test_only_the_planner_may_submit(self):
        os.environ["ORCA_TERMINAL_HANDLE"] = "term-1"
        plan = Path(self.tmp.name) / "plan.md"
        plan.write_text("## Plan\nGRE-1 — p\n\n## Decisions\n1. x\n\n## Changes\nNone.\n")
        with self.assertRaisesRegex(SPAWN.Refusal, "only the planner submits"):
            SPAWN.submit("GRE-1", str(plan), max_wait_ms=1000)
        self.assertEqual(self.calls, [])


class TestRetireSelfGuard(RoleVerbCase):
    def test_retire_refuses_the_callers_own_terminal(self):
        with self.assertRaisesRegex(SPAWN.Refusal, "role running this command"):
            SPAWN.retire("GRE-1", "implementer")
        self.assertEqual(self.calls, [], "nothing was stopped or closed")

    def test_retire_proceeds_from_a_terminal_that_is_nobodys_role(self):
        os.environ["ORCA_TERMINAL_HANDLE"] = "term-orchestrator"
        with mock.patch.object(SPAWN._orca, "stop_worker") as stop, \
             mock.patch.object(SPAWN._orca, "close_terminal") as close, \
             mock.patch.object(SPAWN._orca, "dispatch_status", return_value="completed"):
            result = SPAWN.retire("GRE-1", "implementer")
        stop.assert_called_once_with("disp-1")
        close.assert_called_once_with("term-1", tab=True)
        self.assertTrue(REGISTRY.read()["GRE-1/implementer"]["retired"])
        self.assertEqual(result["worktree_kept"], "/tmp/gre-1")


class TestSweepRefusesADegradedCli(RoleVerbCase):
    def test_empty_terminal_list_is_refused(self):
        with mock.patch.object(SPAWN._orca, "list_terminals", return_value=set()):
            with self.assertRaisesRegex(SPAWN.Refusal, "refusing to treat"):
                SPAWN.sweep(None)
        rec = REGISTRY.read()["GRE-1/implementer"]
        self.assertNotIn("retired", rec, "no live role was torn down")


if __name__ == "__main__":
    unittest.main(verbosity=2)
