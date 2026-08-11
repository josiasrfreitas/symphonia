"""Runtime Adapter conformance suite.

TLDR: the 11-step walkthrough of the GRE-150 harness, promoted from printed
state to asserts. ``FakeConformance`` runs the reusable double directly;
``OrcaConformance`` runs the real ``OrcaRuntimeAdapter`` against the
scripted CLI — the same suite, so the double and the real adapter are held
to the same contract. Steps whose meaning depends on
``capabilities.tier_verification`` assert both branches: the strong
provider must reveal a silent downgrade, the weak one must at least never
claim it observed anything.

Run:  cd .symphonia/src && python3 -m unittest adapters.orca.conformance -v
"""
from __future__ import annotations

import json
import unittest

from ..attention import AttentionCode
from ..runtime_adapter import (
    Access,
    CapabilityTier,
    Liveness,
    RoleName,
    RoleSpec,
)
from ..runtime_adapter import AttemptRef, ContextRef, WorkspaceRef
from .adapter import MessageWorkerRefused, OrcaRuntimeAdapter, RunResult, _AttemptRecord
from .events import gate_events, parse_check_output
from .fake import FakeRuntime, FakeRuntimeAdapter, Rig, ScriptedOrcaCli

ALL_KINDS = ["result", "question", "escalation", "control-lost"]
TEN_MINUTES = 10 * 60 * 1000


def _spec(role: RoleName, tier: CapabilityTier, access: Access) -> RoleSpec:
    return RoleSpec(role=role, tier=tier, access=access, briefing=f"You are the {role.value}.")


class ConformanceMixin:
    """The walkthrough. Subclasses provide ``self.adapter`` and ``self.rig``."""

    adapter: object
    rig: Rig

    def test_walkthrough(self):
        a, rig = self.adapter, self.rig

        # 1. a prepared workspace: setup ran and the stray shell was closed
        # before create resolved — no caller can name an unprepared workspace.
        with self.subTest(step=1):
            ws = a.create_workspace("GRE-201", base_branch="main")
            self.assertEqual(ws.ticket_key, "GRE-201")
            self.assertEqual(ws.branch, "gre-201")
            self.assertTrue(ws.id)
            self.assertTrue(ws.path)
            self.assertIsNone(a.find_workspace("GRE-999"))
            self.assertEqual(a.find_workspace("GRE-201"), ws)
            setup_done, live = rig.workspace_state("GRE-201")
            self.assertTrue(setup_done)
            self.assertEqual(live, 0)

        # 2. launch evidence is honest: nothing has answered at launch, so
        # the evidence is "requested"; the strong check is a second call.
        with self.subTest(step=2):
            planner = a.launch_role(ws, _spec(RoleName.PLANNER, CapabilityTier.STANDARD, Access.WRITE))
            self.assertEqual(planner.tier_evidence.kind, "requested")
            self.assertIs(planner.tier_evidence.tier, CapabilityTier.STANDARD)
            plan = a.dispatch(planner.context, "Write the Local Technical Plan.")
            rig.answer(planner.context)
            evidence = a.verify_tier(planner.context)
            if a.capabilities.tier_verification:
                self.assertEqual(evidence.kind, "observed")
                self.assertIs(evidence.tier, CapabilityTier.STANDARD)
            else:
                self.assertEqual(evidence.kind, "requested")

        # 3. the collision the runtime cannot see: a second writer is
        # refused; readers are unlimited.
        with self.subTest(step=3):
            with self.assertRaisesRegex(RuntimeError, "write access"):
                a.launch_role(ws, _spec(RoleName.IMPLEMENTER, CapabilityTier.STANDARD, Access.WRITE))
            reader = a.launch_role(ws, _spec(RoleName.SPEC_REVIEWER, CapabilityTier.FAST, Access.READ))
            a.close_role(reader.context)

        # 4. the result envelope: outcome is about the process, never the
        # product; drained once, gone after ack.
        with self.subTest(step=4):
            rig.post_result(plan, "succeeded", "Plan appended to the ticket.")
            batch = a.drain(ALL_KINDS)
            self.assertEqual([e.kind for e in batch.events], ["result"])
            self.assertEqual(batch.events[0].outcome, "succeeded")
            self.assertEqual(batch.events[0].attempt.attempt_id, plan.attempt_id)
            a.ack(batch.receipt)
            self.assertEqual(a.drain(ALL_KINDS).events, ())
            a.close_role(planner.context)

        # 5. a correction is a new attempt into the *same* context — there
        # is no argument that turns dispatch into launch_role.
        with self.subTest(step=5):
            impl = a.launch_role(ws, _spec(RoleName.IMPLEMENTER, CapabilityTier.STANDARD, Access.WRITE))
            build = a.dispatch(impl.context, "Implement the approved plan.")
            rig.answer(impl.context)
            spec_r = a.launch_role(ws, _spec(RoleName.SPEC_REVIEWER, CapabilityTier.STANDARD, Access.READ))
            std_r = a.launch_role(ws, _spec(RoleName.STANDARDS_REVIEWER, CapabilityTier.STANDARD, Access.READ))
            fix = a.dispatch(impl.context, "The spec reviewer found two gaps. Address them.")
            self.assertNotEqual(fix.attempt_id, build.attempt_id)
            self.assertEqual(fix.context.id, impl.context.id)
            self.assertEqual(build.context.id, impl.context.id)
            a.close_role(spec_r.context)
            a.close_role(std_r.context)

        # 6. the accepted debt: the provider silently serves another tier.
        # A strong provider reveals it; a weak one cannot — but must never
        # dress the gap up as an observation.
        with self.subTest(step=6):
            ws2 = a.create_workspace("GRE-202", base_branch="main")
            rig.stage(RoleName.PLANNER, CapabilityTier.FAST)
            planner2 = a.launch_role(ws2, _spec(RoleName.PLANNER, CapabilityTier.HIGH, Access.WRITE))
            plan2 = a.dispatch(planner2.context, "Plan it.")
            rig.answer(planner2.context)
            self.assertIs(rig.serving_truth(planner2.context), CapabilityTier.FAST)
            evidence = a.verify_tier(planner2.context)
            if a.capabilities.tier_verification:
                self.assertEqual(evidence.kind, "observed")
                self.assertIs(evidence.tier, CapabilityTier.FAST)  # downgrade visible
            else:
                self.assertEqual(evidence.kind, "requested")
                self.assertIs(evidence.tier, CapabilityTier.HIGH)  # and nothing can tell
            self.assertIsNotNone(plan2)

        # 7. the silent finisher: no event will ever come, because the
        # failure is the absence of one — sweep, not drain, finds it.
        with self.subTest(step=7):
            orphan = a.dispatch(planner2.context, "One more thing.")
            rig.go_quiet(planner2.context, 11 * 60 * 1000)
            self.assertEqual(a.drain(ALL_KINDS).events, ())
            statuses = {s.attempt_id: s for s in a.sweep()}
            status = statuses[orphan.attempt_id]
            self.assertIs(status.liveness, Liveness.IDLE)
            self.assertGreaterEqual(status.quiet_for_ms, TEN_MINUTES)

        # 8. the stolen control binding: reclaimed automatically, reported
        # loudly — a silent reclaim would hide the breach.
        with self.subTest(step=8):
            rig.steal_control("some-other-pane")
            batch = a.drain(ALL_KINDS)
            self.assertIn("control-lost", [e.kind for e in batch.events])
            self.assertEqual(rig.control_holder(), a.coordinator)
            a.ack(batch.receipt)

        # 9. no graceful stop, and the zombie's late result: a fenced
        # attempt cannot be completed by the zombie that was doing it.
        with self.subTest(step=9):
            receipt = a.kill(fix)
            self.assertTrue(receipt.confirmed_gone)
            rig.post_result(fix, "succeeded", "Done! (sent by a killed context)")
            batch = a.drain(["result"])
            self.assertEqual([e for e in batch.events if e.kind == "result"], [])
            a.ack(batch.receipt)

        # 10. the mailbox cap: a capped FIFO batch, replayed until acked —
        # the core has to be idempotent per attempt.
        with self.subTest(step=10):
            for i in range(5):
                rig.post_escalation(orphan, f"escalation {i}")
            first = a.drain(["escalation"])
            self.assertEqual([e.reason for e in first.events], ["escalation 0", "escalation 1", "escalation 2"])
            redelivered = a.drain(["escalation"])  # no ack in between
            self.assertEqual([e.reason for e in redelivered.events], [e.reason for e in first.events])
            a.ack(redelivered.receipt)
            rest = a.drain(["escalation"])
            self.assertEqual([e.reason for e in rest.events], ["escalation 3", "escalation 4"])
            a.ack(rest.receipt)
            self.assertEqual(a.drain(["escalation"]).events, ())

        # 11. teardown refuses while a context lives — there is no force
        # flag; a dropped worker may still be writing.
        with self.subTest(step=11):
            still = a.launch_role(ws, _spec(RoleName.IMPLEMENTER, CapabilityTier.STANDARD, Access.WRITE))
            with self.assertRaisesRegex(RuntimeError, "refusing to destroy"):
                a.destroy_workspace(ws)
            a.close_role(still.context)
            a.destroy_workspace(ws)
            self.assertTrue(rig.workspace_gone("GRE-201"))

    def test_local_event_survives_a_drain_that_filtered_it(self):
        # Review M1: a control-lost queued by _ensure_control must not be
        # consumed by the ack of a drain whose kinds filtered it out —
        # nothing is consumed until it was actually delivered and acked.
        a, rig = self.adapter, self.rig
        ws = a.create_workspace("GRE-211", base_branch="main")
        planner = a.launch_role(ws, _spec(RoleName.PLANNER, CapabilityTier.STANDARD, Access.WRITE))
        a.dispatch(planner.context, "Work.")
        rig.steal_control("some-other-pane")
        filtered = a.drain(["result"])  # control-lost not requested
        self.assertEqual(filtered.events, ())
        a.ack(filtered.receipt)
        delivered = a.drain(ALL_KINDS)
        self.assertIn("control-lost", [e.kind for e in delivered.events])
        a.ack(delivered.receipt)
        self.assertEqual(a.drain(ALL_KINDS).events, ())

    def test_question_token_is_single_use(self):
        a, rig = self.adapter, self.rig
        ws = a.create_workspace("GRE-210", base_branch="main")
        planner = a.launch_role(ws, _spec(RoleName.PLANNER, CapabilityTier.STANDARD, Access.WRITE))
        attempt = a.dispatch(planner.context, "Plan, then ask.")
        rig.post_question(attempt, "Approve the plan?")
        batch = a.drain(["question"])
        self.assertEqual([e.kind for e in batch.events], ["question"])
        token = batch.events[0].token
        a.ack(batch.receipt)
        a.respond(token, "Approved.")
        with self.assertRaisesRegex(RuntimeError, "spent or unknown"):
            a.respond(token, "Approved twice.")


class FakeConformance(ConformanceMixin, unittest.TestCase):
    """The double, on the provider that records answered tiers."""

    def setUp(self):
        rt = FakeRuntime()
        self.adapter = FakeRuntimeAdapter(rt, "orchestrator", records_answers=True)
        self.rig = Rig(rt, records_answers=True)


class OrcaConformance(ConformanceMixin, unittest.TestCase):
    """The real adapter, on the scripted orca CLI. Orca records only the
    launch command, so it runs the weak-evidence branches."""

    def setUp(self):
        self.rt = FakeRuntime()
        self.adapter = OrcaRuntimeAdapter(
            coordinator="orchestrator", run_id="run-1", runner=ScriptedOrcaCli(self.rt)
        )
        self.rig = Rig(self.rt, records_answers=False)

    def test_message_worker_validates_dispatch_state(self):
        a, rig = self.adapter, self.rig
        ws = a.create_workspace("GRE-203", base_branch="main")
        impl = a.launch_role(ws, _spec(RoleName.IMPLEMENTER, CapabilityTier.STANDARD, Access.WRITE))
        attempt = a.dispatch(impl.context, "Do the work.")

        a.message_worker(attempt.attempt_id, "reviewer feedback attached")
        self.assertEqual(self.rt.sent[-1]["to"], f"dispatch:{attempt.attempt_id}")

        rig.set_dispatch_status(attempt, "completed")
        with self.assertRaises(MessageWorkerRefused) as caught:
            a.message_worker(attempt.attempt_id, "are you there?")
        attention = caught.exception.attention
        self.assertTrue(attention.needs)
        self.assertIs(attention.code, AttentionCode.TICKET_WITHOUT_WORKER)
        self.assertIn("dispatch --inject", attention.reason)
        self.assertIn("terminal send", attention.reason)
        self.assertEqual(len(self.rt.sent), 1)  # nothing was sent

        with self.assertRaises(MessageWorkerRefused):
            a.message_worker("disp-nonexistent", "hello?")

        # Review M2: a killed worker's dispatch is "stopped" — just as dead
        # as completed/failed, and just as refused.
        impl2 = a.launch_role(ws, _spec(RoleName.STANDARDS_REVIEWER, CapabilityTier.FAST, Access.READ))
        killed = a.dispatch(impl2.context, "Doomed work.")
        a.kill(killed)
        with self.assertRaises(MessageWorkerRefused) as caught:
            a.message_worker(killed.attempt_id, "still there?")
        self.assertIs(caught.exception.attention.code, AttentionCode.TICKET_WITHOUT_WORKER)
        self.assertEqual(len(self.rt.sent), 1)  # still nothing new sent


class GateEventReading(unittest.TestCase):
    """events.py: typed gate events from check --peek --json, no model."""

    CHECK_OUTPUT = """
    {"deliveryId": "del-9", "messages": [
      {"id": "msg-1", "type": "question", "subject": "plan", "body": "GATE 1: approve the plan?",
       "payload": {"taskId": "task-7", "dispatchId": "disp-3"}},
      {"id": "msg-2", "type": "reply", "threadId": "msg-1", "body": "Approved."},
      {"id": "msg-3", "type": "heartbeat", "body": "alive", "payload": {"taskId": "task-7"}},
      {"id": "msg-4", "type": "worker_done", "body": "Done in three sentences.",
       "payload": {"taskId": "task-7", "dispatchId": "disp-3", "outcome": "succeeded"}}
    ]}
    """

    def test_gate_events_are_typed_and_complete(self):
        batch = parse_check_output(self.CHECK_OUTPUT)
        self.assertEqual(batch.delivery_id, "del-9")
        events = gate_events(batch.messages)
        self.assertEqual([e.kind for e in events], ["plan-question", "approval-reply", "worker-done"])
        question, reply, done = events
        self.assertEqual((question.task_id, question.dispatch_id), ("task-7", "disp-3"))
        self.assertEqual(question.question, "GATE 1: approve the plan?")
        self.assertEqual(reply.thread_id, "msg-1")
        self.assertEqual((done.outcome, done.summary), ("succeeded", "Done in three sentences."))

    def test_parses_bare_list_and_string_payload(self):
        batch = parse_check_output('[{"id": 5, "type": "worker_done", "body": "ok", "payload": "{\\"outcome\\": \\"failed\\"}"}]')
        (done,) = gate_events(batch.messages)
        self.assertEqual((done.message_id, done.outcome), ("5", "failed"))


class RealCliContract(unittest.TestCase):
    """Frozen samples captured from the REAL orca CLI (2026-08-10, ad hoc
    test after the envelope bug). If the CLI's response contract changes,
    these fixtures — not the scripted fake — are what must disagree.

    Honesty note (GRE-184 PR-B review): this PR changed five response
    shapes in ``fake.py`` — ``worktree create``, ``worktree list``,
    ``terminal create``, ``task-create``, and ``dispatch`` — without adding
    a matching ``REAL_*`` fixture for any of them. No recorded sample backs
    those shapes; their only provenance is the argv observed from
    ``spawn.py`` running against the real CLI. A fixture copied out of the
    fake would be worse than having none, since it would read as authority
    echoing itself. The dispatch preamble deserves special mention: the
    ``_capability_of`` regex (``adapter.py``) is only ever exercised in this
    suite against a string the fake synthesizes, never against a real
    preamble. It does work in production — it minted a capability four
    times in one wave the day this note was written — but that fact lives
    nowhere but this comment, and the next reader deserves to know it."""

    # orca worktree list --json  (2026-08-11, during the GRE-188 smoke run;
    # trimmed to the keys this package reads plus enough neighbours to show
    # the shape). This is the first of the five shapes above to get a real
    # sample, and it immediately contradicted the fake: `branch` comes back
    # as a full ref, where `worktree create` answers with the bare name.
    REAL_WORKTREE_LIST = """
    {
      "id": "6e6f2c1a-4c1f-4a1e-9b7a-1d2f3a4b5c6d",
      "ok": true,
      "result": {
        "worktrees": [
          {
            "id": "52a38d04-79af-4956-b72d-cc075a975764::/Users/j/orca/workspaces/symphonia/gre-188",
            "repoId": "52a38d04-79af-4956-b72d-cc075a975764",
            "path": "/Users/j/orca/workspaces/symphonia/gre-188",
            "head": "586b01547e17db7185d21426911fb65f6f71b16c",
            "branch": "refs/heads/josiasrfreitas/gre-188",
            "isBare": false,
            "isMainWorktree": false,
            "displayName": "🧭 GRE-188 · planning",
            "comment": "symphonia ticket GRE-188"
          }
        ]
      }
    }
    """

    def test_worktree_list_answers_branch_as_a_full_ref(self):
        """The measured shape, and the reason `find_workspace` strips.

        `worktree create` answers `branch` as a bare name while `list`
        answers a full ref. Before this fixture the fake emitted no
        `branch` at all, so the fallback was the only value any test saw
        and the two sides could not be caught disagreeing."""

        payload = json.loads(self.REAL_WORKTREE_LIST)["result"]
        (wt,) = payload["worktrees"]
        self.assertEqual(wt["branch"], "refs/heads/josiasrfreitas/gre-188")
        self.assertEqual(
            wt["branch"].removeprefix("refs/heads/"), "josiasrfreitas/gre-188"
        )

    # orca orchestration dispatch-show --task task_75ed1fd7b0b9 --json
    REAL_DISPATCH_SHOW = """
    {
      "id": "c960bf36-0e1f-4c0c-bd15-3291781a3e8c",
      "ok": true,
      "result": {
        "dispatch": {
          "id": "ctx_9c7251a58edd",
          "run_id": "run_e3af18d66a3f",
          "task_id": "task_75ed1fd7b0b9",
          "contract_version": 1,
          "assignee_handle": "term_2930bf3f-05a0-464f-b891-ca3911247955",
          "status": "completed",
          "failure_count": 0,
          "last_failure": null,
          "dispatched_at": "2026-08-10 18:50:10",
          "completed_at": "2026-08-10 18:53:12",
          "created_at": "2026-08-10 18:50:10",
          "last_heartbeat_at": null
        }
      },
      "_meta": {"runtimeId": "01b52095-c4bf-4029-8931-9d8cdd8beb96"}
    }
    """

    # orca orchestration check --terminal term_2930bf3f-... --peek --json
    REAL_CHECK_PEEK = """
    {
      "id": "a0de4d0e-9b44-483b-85db-91b7f209e704",
      "ok": true,
      "result": {
        "runId": "run_e3af18d66a3f",
        "dispatchId": "ctx_e3d829a0b350",
        "messages": [],
        "count": 0
      },
      "_meta": {"runtimeId": "01b52095-c4bf-4029-8931-9d8cdd8beb96"}
    }
    """

    # orca orchestration worker-show --dispatch ctx_9c7251a58edd --json
    REAL_ERROR = """
    {
      "id": "b33bd19c-ba46-4f7f-abb2-547ff9e44eaa",
      "ok": false,
      "error": {"code": "dispatch_not_found", "message": "Worker Dispatch ctx_9c7251a58edd was not found."},
      "_meta": {"runtimeId": "01b52095-c4bf-4029-8931-9d8cdd8beb96"}
    }
    """

    def _adapter_answering(self, fixture: str) -> OrcaRuntimeAdapter:
        return OrcaRuntimeAdapter(
            coordinator="orchestrator", run_id="run-1",
            runner=lambda argv: RunResult(stdout=fixture, stderr="", returncode=0),
        )

    def test_check_peek_unwraps_envelope(self):
        batch = parse_check_output(self.REAL_CHECK_PEEK)
        # The envelope's top-level id is the request id, never a delivery id.
        self.assertEqual(batch.delivery_id, "")
        self.assertEqual(batch.messages, ())

    def test_error_envelope_raises_in_parser(self):
        with self.assertRaisesRegex(ValueError, "dispatch_not_found"):
            parse_check_output(self.REAL_ERROR)

    def test_error_envelope_raises_in_adapter(self):
        adapter = self._adapter_answering(self.REAL_ERROR)
        with self.assertRaisesRegex(RuntimeError, "dispatch_not_found"):
            adapter._orca("orchestration", "worker-show", "--dispatch", "ctx_9c7251a58edd")

    def test_message_worker_guard_fires_on_real_dispatch_show(self):
        # The bug this task fixes: reading status off the envelope gave ""
        # and the guard never fired — a raw send went to a dead worker.
        adapter = self._adapter_answering(self.REAL_DISPATCH_SHOW)
        workspace = WorkspaceRef(ticket_key="GRE-175", id="wt-175", path="/workspaces/gre-175", branch="b")
        context = ContextRef(id="ctx-1", role=RoleName.IMPLEMENTER, workspace=workspace)
        ref = AttemptRef(attempt_id="ctx_9c7251a58edd", ticket_key="GRE-175", context=context)
        adapter._attempts["ctx_9c7251a58edd"] = _AttemptRecord(ref=ref, task_id="task_75ed1fd7b0b9")
        with self.assertRaises(MessageWorkerRefused) as caught:
            adapter.message_worker("ctx_9c7251a58edd", "are you there?")
        self.assertIs(caught.exception.attention.code, AttentionCode.TICKET_WITHOUT_WORKER)
        self.assertIn("completed", caught.exception.attention.reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
