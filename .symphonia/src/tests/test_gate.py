"""Tests for `gate` — the state machine, the per-event decision
(`apply_gate_event`) and the attribution/execution loop (`run`), driven
with plain dicts and fakes. No CLI, no network. Run either way:

    cd .symphonia/src && python3 -m unittest tests.test_gate
    python3 .symphonia/src/tests/test_gate.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gate as GATE

SUBMISSION = "## Plan\nGRE-1 — pointer\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
DONE_BODY = "## Plan\nGRE-1 — comment abc\n\n## Approval\n1 rodada.\n\n## Deviations\nNone.\n"


def question(message_id: str = "q-1"):
    return SimpleNamespace(kind="plan-question", message_id=message_id)


def done(outcome: str):
    return SimpleNamespace(
        kind="worker-done", message_id="d-1", outcome=outcome, summary=DONE_BODY,
    )


# --- the pure state machine -------------------------------------------------


class TestHappyPath(unittest.TestCase):
    def test_submit_approve_done_retires(self):
        result = GATE.transition(GATE.IDLE, question("q-1"), last_question_id=None)
        self.assertEqual((result.state, result.actions), (GATE.SUBMITTED, (GATE.LABEL_ON,)))
        self.assertEqual(result.question_id, "q-1")

        # `spawn verdict` records the verdict; no event carries it.
        result = GATE.transition(GATE.VERDICT_APPROVED, done("succeeded"), report_ok=True)
        self.assertEqual((result.state, result.actions), (GATE.RETIRED, (GATE.RETIRE_PLANNER,)))


class TestReviseRoundTrip(unittest.TestCase):
    def test_revise_then_resubmit_relights_the_label(self):
        # A genuine resubmission carries a NEW question id.
        result = GATE.transition(GATE.VERDICT_REVISE, question("q-2"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (GATE.SUBMITTED, (GATE.LABEL_ON,)))
        self.assertEqual(result.question_id, "q-2")


class TestReplayIsIdempotent(unittest.TestCase):
    def test_repeated_submission_does_not_relight_the_label(self):
        result = GATE.transition(GATE.SUBMITTED, question("q-1"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (GATE.SUBMITTED, ()))

    def test_an_unknown_event_kind_is_inert(self):
        inert = SimpleNamespace(kind="approval-reply", body="APPROVED")
        result = GATE.transition(GATE.VERDICT_APPROVED, inert)
        self.assertEqual((result.state, result.actions), (GATE.VERDICT_APPROVED, ()))

    def test_repeated_worker_done_after_retirement_does_not_retire_twice(self):
        result = GATE.transition(GATE.RETIRED, done("succeeded"), report_ok=True)
        self.assertEqual((result.state, result.actions), (GATE.RETIRED, ()))

    def test_replay_of_the_submission_after_approval_does_not_relight_the_label(self):
        result = GATE.transition(GATE.VERDICT_APPROVED, question("q-1"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (GATE.VERDICT_APPROVED, ()))

    def test_replay_of_the_submission_after_revise_does_not_relight_the_label(self):
        result = GATE.transition(GATE.VERDICT_REVISE, question("q-1"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (GATE.VERDICT_REVISE, ()))


class TestDivergenceIsFlagged(unittest.TestCase):
    def test_succeeded_done_without_recorded_approval_is_flagged(self):
        result = GATE.transition(GATE.SUBMITTED, done("succeeded"), report_ok=True)
        self.assertEqual((result.state, result.actions), (GATE.SUBMITTED, (GATE.FLAG_MALFORMED,)))

    def test_failed_done_is_left_for_the_human(self):
        result = GATE.transition(GATE.SUBMITTED, done("failed"))
        self.assertEqual((result.state, result.actions), (GATE.SUBMITTED, ()))

    def test_approved_done_whose_body_did_not_parse_is_flagged_not_retired(self):
        result = GATE.transition(GATE.VERDICT_APPROVED, done("succeeded"), report_ok=False)
        self.assertEqual(
            (result.state, result.actions), (GATE.VERDICT_APPROVED, (GATE.FLAG_MALFORMED,))
        )


class TestRetiredPlannerCannotReopen(unittest.TestCase):
    def test_plan_question_after_retirement_is_ignored(self):
        result = GATE.transition(GATE.RETIRED, question("q-2"), last_question_id="q-1")
        self.assertEqual((result.state, result.actions), (GATE.RETIRED, ()))


# --- apply_gate_event: the per-event decision --------------------------------


class TestApplyGateEventQuestionFiltering(unittest.TestCase):
    """The gate must only react to a real plan submission — a clarifying
    question from the planner must not light the label."""

    def _question(self, body, message_id="q-1"):
        return SimpleNamespace(kind="plan-question", message_id=message_id, question=body)

    def test_non_submission_question_is_not_a_gate_event(self):
        rec = {"ticket": "GRE-1", "gate_state": GATE.IDLE}
        actions = GATE.apply_gate_event(rec, self._question("Should I use A or B?"), {})
        self.assertEqual(actions, [])
        self.assertEqual(rec["gate_state"], GATE.IDLE)
        self.assertNotIn("question_id", rec)

    def test_real_submission_lights_the_label(self):
        rec = {"ticket": "GRE-1", "gate_state": GATE.IDLE}
        actions = GATE.apply_gate_event(rec, self._question(SUBMISSION, "q-1"), {})
        self.assertEqual(actions, [(GATE.LABEL_ON, None)])
        self.assertEqual(rec["gate_state"], GATE.SUBMITTED)
        self.assertEqual(rec["question_id"], "q-1")
        self.assertEqual(rec["last_question_id"], "q-1")
        self.assertEqual(rec["approval_rounds"], 1)
        # the raw submission is recorded — `spawn verdict` republishes it
        # on approval, never on submission
        self.assertEqual(rec["plan_body"], SUBMISSION)

    def test_a_submission_filed_under_another_ticket_is_flagged(self):
        rec = {"ticket": "GRE-1", "gate_state": GATE.IDLE}
        body = "## Plan\nGRE-9 — pointer\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
        actions = GATE.apply_gate_event(rec, self._question(body), {})
        action, reason = actions[0]
        self.assertEqual(action, GATE.FLAG_MALFORMED)
        self.assertIn("GRE-9", reason)
        self.assertEqual(rec["gate_state"], GATE.IDLE)

    def test_body_that_starts_plan_but_fails_to_parse_is_flagged(self):
        rec = {"ticket": "GRE-1", "gate_state": GATE.IDLE}
        body = "## Plan\nGRE-1 — pointer\n\n## Changes\nNone.\n"  # missing Decisions
        actions = GATE.apply_gate_event(rec, self._question(body), {})
        action, reason = actions[0]
        self.assertEqual(action, GATE.FLAG_MALFORMED)
        self.assertIn("Decisions", reason)
        self.assertEqual(rec["gate_state"], GATE.IDLE)  # never toggled


class TestApplyGateEventReplayVsResubmission(unittest.TestCase):
    """Replay of an unacked Delivery must not relight the label or revert a
    decided verdict; a genuine resubmission after REVISE must."""

    def _question(self, message_id, body=SUBMISSION):
        return SimpleNamespace(kind="plan-question", message_id=message_id, question=body)

    def test_replay_after_approval_does_not_relight_the_label(self):
        rec = {"ticket": "GRE-1", "gate_state": GATE.VERDICT_APPROVED, "last_question_id": "q-1"}
        actions = GATE.apply_gate_event(rec, self._question("q-1"), {})
        self.assertEqual(actions, [])
        self.assertEqual(rec["gate_state"], GATE.VERDICT_APPROVED)

    def test_resubmission_after_revise_lights_the_label(self):
        rec = {"ticket": "GRE-1", "gate_state": GATE.VERDICT_REVISE, "last_question_id": "q-1"}
        actions = GATE.apply_gate_event(rec, self._question("q-2"), {})
        self.assertEqual(actions, [(GATE.LABEL_ON, None)])
        self.assertEqual(rec["gate_state"], GATE.SUBMITTED)
        self.assertEqual(rec["last_question_id"], "q-2")
        self.assertEqual(rec["approval_rounds"], 1)

    def test_resubmission_overwrites_the_previous_plan_body(self):
        old_body = "## Plan\nGRE-1 — pointer\n\n## Decisions\n1. old\n\n## Changes\nNone.\n"
        new_body = "## Plan\nGRE-1 — pointer\n\n## Decisions\n1. new\n\n## Changes\nFixed per REVISE.\n"
        rec = {
            "ticket": "GRE-1", "gate_state": GATE.VERDICT_REVISE,
            "last_question_id": "q-1", "plan_body": old_body,
        }
        GATE.apply_gate_event(rec, self._question("q-2", new_body), {})
        self.assertEqual(rec["plan_body"], new_body)


class TestApplyGateEventWorkerDone(unittest.TestCase):
    """The planner's worker_done: parsed here, and only retiring when the
    gate already recorded the approval."""

    SUMMARY = "## Plan\npointer\n\n## Approval\n1 rodada.\n\n## Deviations\nNone.\n"

    def _done(self, message_id="d-1", outcome="succeeded", summary=None):
        return SimpleNamespace(
            kind="worker-done", message_id=message_id, outcome=outcome,
            summary=self.SUMMARY if summary is None else summary,
        )

    def test_approved_and_wellformed_report_retires(self):
        rec = {"ticket": "GRE-1", "gate_state": GATE.VERDICT_APPROVED}
        raw = SimpleNamespace(payload={})
        actions = GATE.apply_gate_event(rec, self._done(), {"d-1": raw})
        self.assertEqual(actions, [(GATE.RETIRE_PLANNER, None)])
        self.assertEqual(rec["gate_state"], GATE.RETIRED)
        self.assertEqual(rec["deviations"], [])

    def test_report_that_does_not_parse_is_flagged_not_retired(self):
        rec = {"ticket": "GRE-1", "gate_state": GATE.VERDICT_APPROVED}
        raw = SimpleNamespace(payload={})
        broken = "## Plan\npointer\n\n## Deviations\nNone.\n"  # no Approval
        actions = GATE.apply_gate_event(rec, self._done(summary=broken), {"d-1": raw})
        action, reason = actions[0]
        self.assertEqual(action, GATE.FLAG_MALFORMED)
        self.assertIn("Approval", reason)
        self.assertEqual(rec["gate_state"], GATE.VERDICT_APPROVED)

    def test_a_lifecycle_rejection_from_orca_is_flagged(self):
        """Measured on Orca 1.4.168: a refused worker_done still arrives as
        a worker_done, marked in the payload. Without this it reads as a
        completion that completed nothing."""

        rec = {"ticket": "GRE-1", "gate_state": GATE.VERDICT_APPROVED}
        raw = SimpleNamespace(payload={"_orcaLifecycleRejection": {
            "code": "missing_dispatch_id", "reason": "worker_done requires dispatchId.",
        }})
        actions = GATE.apply_gate_event(rec, self._done(), {"d-1": raw})
        action, reason = actions[0]
        self.assertEqual(action, GATE.FLAG_MALFORMED)
        self.assertIn("missing_dispatch_id", reason)
        self.assertEqual(rec["gate_state"], GATE.VERDICT_APPROVED)


# --- run: attribution and execution ------------------------------------------


class FakeTracker:
    """Records what the loop asked of it; never touches a network."""

    def __init__(self):
        self.gate_calls, self.attention = [], []

    def set_gate(self, ticket, on):
        self.gate_calls.append((ticket, on))

    def set_attention(self, ticket, code, reason):
        self.attention.append((ticket, code, reason))


class GateLoopRunCase(unittest.TestCase):
    def setUp(self):
        self.record = {
            "ticket": "GRE-1", "role": "planner",
            "dispatch": "ctx_1", "task": "task_1",
            "gate_state": GATE.IDLE, "approval_rounds": 0,
        }
        self.data = {"GRE-1/planner": dict(self.record)}
        self.tracker = FakeTracker()
        self.teardown_calls = []

    def _teardown(self, ticket, role):
        self.teardown_calls.append((ticket, role))
        return {"retired": f"{ticket}/{role}", "effects": ["worker-stop", "terminal closed"]}

    def _run(self, events, raw_by_id=None, gate_role="planner"):
        return GATE.run(
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
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE.LABEL_ON}])
        self.assertEqual(self.tracker.gate_calls, [("GRE-1", True)])

    def test_falls_back_to_task_id_when_dispatch_id_is_absent(self):
        event = self.question(dispatch_id=None)
        actions, unattributed = self._run([event])
        self.assertEqual(unattributed, [])
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE.LABEL_ON}])

    def test_a_message_from_an_unknown_dispatch_is_reported_not_swallowed(self):
        event = self.question(dispatch_id="ctx_other", task_id="task_other")
        actions, unattributed = self._run([event])
        self.assertEqual(actions, [])
        self.assertEqual(len(unattributed), 1)
        self.assertEqual(unattributed[0]["task"], "task_other")

    def test_an_event_for_another_role_is_left_alone(self):
        actions, unattributed = self._run([self.question()], gate_role="implementer")
        self.assertEqual(actions, [])
        self.assertEqual(unattributed, [])
        self.assertEqual(self.data["GRE-1/planner"]["gate_state"], GATE.IDLE)
        self.assertEqual(self.teardown_calls, [])


class TestPlannerRetirement(GateLoopRunCase):
    def test_an_approved_worker_done_tears_down_the_planner(self):
        """`teardown()` re-reads and rewrites the registry on its own; `run`
        still has to carry the flag over so the caller's write does not
        resurrect the retired role."""

        self.data["GRE-1/planner"]["gate_state"] = GATE.VERDICT_APPROVED
        actions, _ = self._run([self.done()])
        self.assertEqual(self.teardown_calls, [("GRE-1", "planner")])
        self.assertTrue(self.data["GRE-1/planner"]["retired"])
        self.assertEqual(self.data["GRE-1/planner"]["gate_state"], GATE.RETIRED)
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE.RETIRE_PLANNER}])


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
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE.RETIRE_ROLE}])
        self.assertNotIn("retired", self.data["GRE-1/planner"], "only the reporting role's record changes")

    def test_fires_on_a_failed_outcome_too(self):
        actions, _ = self._run([self.implementer_done(outcome="failed")])
        self.assertEqual(self.teardown_calls, [("GRE-1", "implementer")])
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE.RETIRE_ROLE}])

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
        self.data["GRE-1/planner"]["gate_state"] = GATE.VERDICT_APPROVED
        actions, _ = self._run([self.done(body="not a done report")])
        self.assertEqual(actions, [{"ticket": "GRE-1", "action": GATE.FLAG_MALFORMED}])
        self.assertEqual(len(self.tracker.attention), 1)
        ticket, code, reason = self.tracker.attention[0]
        self.assertEqual(ticket, "GRE-1")
        self.assertEqual(code, GATE.AttentionCode.MALFORMED_REPORT.value)
        self.assertTrue(reason)
        self.assertEqual(self.teardown_calls, [], "a malformed report must not retire the role")


if __name__ == "__main__":
    unittest.main(verbosity=2)
