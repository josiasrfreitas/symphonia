"""Tests for `workflow.gate_loop.run` — the attribution/execution loop.

TLDR: `apply_gate_event` (the per-event decision) is covered in
`adapters.tests.test_brief`; this file covers the loop AROUND it, driven
directly with a plain registry dict, a fake tracker and a fake `teardown` —
no CLI, no `importlib.reload`, no monkeypatching a module. Run either way:

    cd .symphonia/src && python3 -m unittest workflow.tests.test_gate_loop
    python3 .symphonia/src/workflow/tests/test_gate_loop.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

from workflow import gate_loop as GATE_LOOP

SUBMISSION = "## Plan\nGRE-1 — pointer\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
DONE_BODY = "## Plan\nGRE-1 — comment abc\n\n## Approval\n1 rodada.\n\n## Deviations\nNone.\n"


class FakeTracker:
    """Records what the loop asked of it; never touches a network."""

    def __init__(self):
        self.gate_calls, self.attention = [], []

    def set_gate(self, ticket, on):
        self.gate_calls.append((ticket, on))

    def set_attention(self, ticket, attention):
        self.attention.append((ticket, attention))


class GateLoopRunCase(unittest.TestCase):
    def setUp(self):
        self.record = {
            "ticket": "GRE-1", "role": "planner",
            "dispatch": "ctx_1", "task": "task_1",
            "gate_state": GATE_LOOP.IDLE, "approval_rounds": 0,
        }
        self.data = {"GRE-1/planner": dict(self.record)}
        self.tracker = FakeTracker()
        self.teardown_calls = []

    def _teardown(self, ticket, role):
        self.teardown_calls.append((ticket, role))
        return {"retired": f"{ticket}/{role}", "effects": ["worker-stop", "terminal closed"]}

    def _run(self, events, raw_by_id=None, gate_role="planner"):
        return GATE_LOOP.run(
            events, raw_by_id or {}, self.data,
            tracker=lambda: self.tracker,
            teardown=self._teardown,
            gate_role=gate_role,
        )

    def question(self, message_id="q-1", dispatch_id="ctx_1", task_id="task_1"):
        return SimpleNamespace(
            kind="plan-question", message_id=message_id, question=SUBMISSION,
            dispatch_id=dispatch_id, task_id=task_id,
        )

    def done(self, message_id="d-1", dispatch_id="ctx_1", task_id="task_1", body=DONE_BODY,
             outcome="succeeded"):
        return SimpleNamespace(
            kind="worker-done", message_id=message_id, summary=body, outcome=outcome,
            dispatch_id=dispatch_id, task_id=task_id,
        )


class TestAttribution(GateLoopRunCase):
    def test_attributed_by_dispatch_id(self):
        actions, unattributed = self._run([self.question()])
        self.assertEqual(unattributed, [])
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE_LOOP._gate.LABEL_ON}])
        self.assertEqual(self.tracker.gate_calls, [("GRE-1", True)])

    def test_falls_back_to_task_id_when_dispatch_id_is_absent(self):
        event = self.question(dispatch_id=None)
        actions, unattributed = self._run([event])
        self.assertEqual(unattributed, [])
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE_LOOP._gate.LABEL_ON}])

    def test_a_message_from_an_unknown_dispatch_is_reported_not_swallowed(self):
        event = self.question(dispatch_id="ctx_other", task_id="task_other")
        actions, unattributed = self._run([event])
        self.assertEqual(actions, [])
        self.assertEqual(len(unattributed), 1)
        self.assertEqual(unattributed[0]["task"], "task_other")

    def test_an_event_for_another_role_is_left_alone(self):
        """A non-`worker-done` event attributed to a record outside
        `gate_role` has nothing to do with the plan gate OR role
        termination — it is simply left alone."""

        actions, unattributed = self._run([self.question()], gate_role="implementer")
        self.assertEqual(actions, [])
        self.assertEqual(unattributed, [])
        self.assertEqual(self.data["GRE-1/planner"]["gate_state"], GATE_LOOP.IDLE)
        self.assertEqual(self.teardown_calls, [])


class TestPlannerRetirement(GateLoopRunCase):
    def test_an_approved_worker_done_tears_down_the_planner(self):
        """`teardown()` re-reads and rewrites the registry on its own,
        outside this `data` copy; `run` still has to carry the flag over so
        the caller's `state_write` does not resurrect the retired role."""

        self.data["GRE-1/planner"]["gate_state"] = GATE_LOOP._gate.VERDICT_APPROVED
        actions, _ = self._run([self.done()])
        self.assertEqual(self.teardown_calls, [("GRE-1", "planner")])
        self.assertTrue(self.data["GRE-1/planner"]["retired"])
        self.assertEqual(self.data["GRE-1/planner"]["gate_state"], GATE_LOOP._gate.RETIRED)
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE_LOOP._gate.RETIRE_PLANNER}])


class TestNonPlannerWorkerDone(GateLoopRunCase):
    """The gate governs only the planner's own dispatch, but a `worker_done`
    from anyone else's still has to end that role — there is no gate state
    for it to transition, so `run` handles it directly."""

    def setUp(self):
        super().setUp()
        self.data["GRE-1/implementer"] = {
            "ticket": "GRE-1", "role": "implementer",
            "dispatch": "ctx_2", "task": "task_2",
        }

    def implementer_done(self, message_id="d-2", outcome="succeeded"):
        return SimpleNamespace(
            kind="worker-done", message_id=message_id, summary=DONE_BODY, outcome=outcome,
            dispatch_id="ctx_2", task_id="task_2",
        )

    def test_worker_done_tears_down_a_non_gate_role(self):
        actions, unattributed = self._run([self.implementer_done()])
        self.assertEqual(unattributed, [])
        self.assertEqual(self.teardown_calls, [("GRE-1", "implementer")])
        self.assertTrue(self.data["GRE-1/implementer"]["retired"])
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE_LOOP._gate.RETIRE_ROLE}])
        self.assertNotIn("retired", self.data["GRE-1/planner"], "only the reporting role's record changes")

    def test_fires_on_a_failed_outcome_too(self):
        actions, _ = self._run([self.implementer_done(outcome="failed")])
        self.assertEqual(self.teardown_calls, [("GRE-1", "implementer")])
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE_LOOP._gate.RETIRE_ROLE}])

    def test_a_replayed_worker_done_is_a_no_op(self):
        self._run([self.implementer_done()])
        self.teardown_calls.clear()
        actions, _ = self._run([self.implementer_done()])
        self.assertEqual(self.teardown_calls, [], "an already-retired record is not torn down twice")
        self.assertEqual(actions, [])

    def test_a_non_worker_done_event_for_a_non_gate_role_is_left_alone(self):
        event = SimpleNamespace(
            kind="plan-question", message_id="q-9", question=SUBMISSION,
            dispatch_id="ctx_2", task_id="task_2",
        )
        actions, unattributed = self._run([event])
        self.assertEqual(actions, [])
        self.assertEqual(unattributed, [])
        self.assertEqual(self.teardown_calls, [])
        self.assertNotIn("retired", self.data["GRE-1/implementer"])


class TestApprovalRounds(GateLoopRunCase):
    def test_a_replayed_question_does_not_recount_the_round(self):
        self._run([self.question("q-1")])
        self.assertEqual(self.data["GRE-1/planner"]["approval_rounds"], 1)

        self._run([self.question("q-1")])
        self.assertEqual(self.data["GRE-1/planner"]["approval_rounds"], 1, "a replay is not a new round")
        self.assertEqual(self.tracker.gate_calls, [("GRE-1", True)], "label lit once")


class TestFlagMalformed(GateLoopRunCase):
    def test_a_malformed_done_report_is_flagged_with_a_reason(self):
        self.data["GRE-1/planner"]["gate_state"] = GATE_LOOP._gate.VERDICT_APPROVED
        actions, _ = self._run([self.done(body="not a done report")])
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE_LOOP._gate.FLAG_MALFORMED}])
        self.assertEqual(len(self.tracker.attention), 1)
        ticket, attention = self.tracker.attention[0]
        self.assertEqual(ticket, "GRE-1")
        self.assertTrue(attention.reason)
        self.assertEqual(self.teardown_calls, [], "a malformed report must not retire the role")


if __name__ == "__main__":
    unittest.main(verbosity=2)
