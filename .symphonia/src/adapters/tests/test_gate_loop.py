"""Tests for the Orchestrator's side of the gate: `wait` and `verdict`.

TLDR: `workflow.gate_loop.apply_gate_event`/`.run` are covered in
`test_brief.py` and `workflow/tests/test_gate_loop.py`; what this file
covers is the shell around them — the lock that serializes `wait` and
`verdict`, how an event is attributed to a role, and the order in which
`verdict` records and delivers a decision. Most cases here are defects
found in review of earlier versions, so each of those is a regression, not
a hypothetical; the `TestBriefVerb` and `TestWaitPersistsDelivery` cases
cover the `brief` verb and the delivery journal/receipt respectively — both
new feature, not a regression.

No network: `SPAWN.orca` and `SPAWN._linear.LinearTracker` are replaced.
Run either way:

    cd .symphonia/src && python3 -m unittest adapters.tests.test_gate_loop
    python3 .symphonia/src/adapters/tests/test_gate_loop.py
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src, the sys.path root since the move
sys.path.insert(0, str(PACKAGE))

from adapters.tests.test_brief import _load_spawn

DONE_BODY = "## Plan\nGRE-1 — comment abc\n\n## Approval\n1 rodada.\n\n## Deviations\nNone.\n"


class FakeTracker:
    """Records what the gate asked of Linear; raises on demand."""

    def __init__(self, fail: bool = False, fail_comment: bool = False):
        self.gate_calls, self.attention, self.comments = [], [], []
        self.fail = fail
        self.fail_comment = fail_comment
        self.on_set_gate = None  # optional hook, fired after a real call is recorded

    def set_gate(self, ticket, on):
        if self.fail:
            raise RuntimeError("linear unreachable")
        self.gate_calls.append((ticket, on))
        if self.on_set_gate is not None:
            self.on_set_gate(ticket, on)

    def post_comment(self, ticket, body):
        if self.fail_comment:
            raise RuntimeError("linear unreachable")
        self.comments.append((ticket, body))
        return SimpleNamespace(id=f"comment-{len(self.comments)}")

    def set_attention(self, ticket, attention):
        self.attention.append((ticket, attention))


class _FakeStatusRetireAdapter:
    """`retire()`/`status()` compose over `OrcaRuntimeAdapter`'s concrete
    id-taking methods now (GRE-184 M4b), not `spawn.orca` — so a fixture
    that only replaces `spawn.orca` (this file's usual `fake_orca`) never
    sees those calls. This stands in for just the four methods `retire()`/
    `status()` use, routed through the same `orca_fn` double so `self.calls`
    still records everything in one place."""

    def __init__(self, orca_fn):
        self._orca_fn = orca_fn

    def dispatch_status(self, task_id, *, default="?"):
        shown = self._orca_fn("orchestration", "dispatch-show", "--task", task_id)
        return str((shown.get("dispatch") or {}).get("status", default))

    def stop_worker(self, dispatch_id):
        self._orca_fn("orchestration", "worker-stop", "--dispatch", dispatch_id)

    def close_terminal(self, handle, *, tab):
        argv = ["terminal", "close", "--terminal", handle]
        if tab:
            argv.append("--tab")
        self._orca_fn(*argv)

    def settle_task(self, task_id, reason):
        self._orca_fn(
            "orchestration", "task-update", "--id", task_id, "--status", "failed",
            "--result", json.dumps({"reason": reason}),
        )


class GateLoopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SYMPHONIA_RUNTIME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SYMPHONIA_RUNTIME", None)

        self.spawn = _load_spawn()
        self.record = {
            "ticket": "GRE-1",
            "role": "planner",
            "worktree": "/tmp/gre-1",
            "terminal": "term_planner",
            "task": "task_1",
            "dispatch": "ctx_1",
            "capability": "dcap_abc",
            "gate_state": self.spawn.IDLE,
            "approval_rounds": 0,
        }
        self.spawn.state_write({"GRE-1/planner": dict(self.record)})

        self.tracker = FakeTracker()
        self.spawn._linear.LinearTracker = lambda: self.tracker
        self.calls = []
        self.batch = {"deliveryId": "d1", "messages": []}

        def fake_orca(*argv, expect_lifecycle_ok=False):
            self.calls.append(list(argv))
            if argv[:2] == ("orchestration", "check"):
                return self.batch
            if argv[:2] == ("orchestration", "dispatch-show"):
                return {"dispatch": {"status": "completed"}}
            return {}

        self.spawn.orca = fake_orca
        self.spawn._adapter = lambda: _FakeStatusRetireAdapter(fake_orca)

    def message(self, **over):
        base = {
            "id": "m-1", "type": "worker_done", "subject": "s", "body": DONE_BODY,
            "payload": {"taskId": "task_1", "dispatchId": "ctx_1", "outcome": "succeeded"},
        }
        base.update(over)
        return base

    def set_state(self, **over):
        data = self.spawn.state_read()
        data["GRE-1/planner"].update(over)
        self.spawn.state_write(data)


class TestWaitPersistsRetirement(GateLoopCase):
    def test_the_retired_flag_survives_the_loops_own_write(self):
        """`retire()` writes the flag straight to the registry; `wait` then
        writes the copy it read before that. Without carrying the flag over,
        a dead planner stays 'live' and the next role in the same worktree
        cannot identify itself."""

        self.set_state(gate_state=self.spawn._gate.VERDICT_APPROVED)
        self.batch = {"deliveryId": "d1", "messages": [self.message()]}
        self.spawn.wait(ack=None, timeout_ms=1)
        rec = self.spawn.state_read()["GRE-1/planner"]
        self.assertTrue(rec.get("retired"), "wait must not resurrect a retired planner")
        self.assertEqual(rec["gate_state"], self.spawn._gate.RETIRED)


class TestWaitAttribution(GateLoopCase):
    def test_a_rejection_without_a_dispatch_id_is_still_flagged(self):
        """The refusal Orca sends back for a missing `dispatchId` is exactly
        the message that cannot be attributed by dispatch. It keeps `taskId`,
        so it is attributed by that instead of vanishing."""

        self.set_state(gate_state=self.spawn._gate.VERDICT_APPROVED)
        self.batch = {"deliveryId": "d1", "messages": [self.message(payload={
            "taskId": "task_1", "outcome": "succeeded",
            "_orcaLifecycleRejection": {
                "code": "missing_dispatch_id", "reason": "worker_done requires dispatchId.",
            },
        })]}
        out = self.spawn.wait(ack=None, timeout_ms=1)
        self.assertEqual(out["unattributed"], [])
        self.assertEqual(len(self.tracker.attention), 1)
        ticket, attention = self.tracker.attention[0]
        self.assertEqual(ticket, "GRE-1")
        self.assertIn("missing_dispatch_id", attention.reason)

    def test_a_message_from_an_unknown_dispatch_is_reported_not_swallowed(self):
        self.batch = {"deliveryId": "d1", "messages": [self.message(
            payload={"taskId": "task_other", "dispatchId": "ctx_other", "outcome": "succeeded"},
        )]}
        out = self.spawn.wait(ack=None, timeout_ms=1)
        self.assertEqual(len(out["unattributed"]), 1)
        self.assertEqual(out["unattributed"][0]["task"], "task_other")


class TestWaitCountsRounds(GateLoopCase):
    SUBMISSION = "## Plan\nGRE-1 — comment abc\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"

    def question(self, message_id):
        return self.message(id=message_id, type="question", body=self.SUBMISSION,
                            payload={"taskId": "task_1", "dispatchId": "ctx_1"})

    def test_a_second_question_before_a_verdict_moves_the_pending_id(self):
        """If the planner's first `submit` died, it is blocked on a NEW ask.
        The verdict must go to the thread it is actually waiting on — and
        that is not a new round, because no plan was decided on."""

        self.batch = {"deliveryId": "d1", "messages": [self.question("q-1")]}
        self.spawn.wait(ack=None, timeout_ms=1)
        first = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(first["question_id"], "q-1")
        self.assertEqual(first["approval_rounds"], 1)

        self.batch = {"deliveryId": "d2", "messages": [self.question("q-2")]}
        self.spawn.wait(ack=None, timeout_ms=1)
        second = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(second["question_id"], "q-2", "the verdict must follow the planner")
        self.assertEqual(second["approval_rounds"], 1, "re-asking is not a new round")
        self.assertEqual(self.tracker.gate_calls, [("GRE-1", True)], "label lit once")


class TestWaitPersistsDelivery(GateLoopCase):
    """GRE-187 stage A: the Delivery id no longer has to survive in a
    terminal's stdout to be ackable. New feature, not a regression — like
    `TestBriefVerb` above."""

    def test_wait_writes_the_receipt_for_the_delivery_it_saw(self):
        self.batch = {"deliveryId": "d1", "messages": [self.message()]}
        self.spawn.wait(ack=None, timeout_ms=1)

        self.assertEqual(self.spawn._journal.read_receipt(self.spawn.RUNTIME_DIR), "d1")

    def test_second_wait_without_ack_sends_the_persisted_id(self):
        """The normal-path half of the ack ordering: no crash, so the
        receipt the first call wrote (after its own `state_write`) is
        exactly what the second call reads back and sends as `--ack`. The
        crash half lives in
        `test_a_missing_receipt_after_a_crash_sends_no_ack_and_the_replay_is_a_no_op`."""

        self.batch = {"deliveryId": "d1", "messages": [self.message()]}
        self.spawn.wait(ack=None, timeout_ms=1)
        self.calls.clear()

        self.batch = {"deliveryId": "d2", "messages": []}
        self.spawn.wait(ack=None, timeout_ms=1)

        check_call = self.calls[0]
        self.assertIn("--ack", check_call)
        self.assertEqual(check_call[check_call.index("--ack") + 1], "d1")

    def test_explicit_ack_wins_over_the_persisted_receipt(self):
        self.batch = {"deliveryId": "d1", "messages": [self.message()]}
        self.spawn.wait(ack=None, timeout_ms=1)  # persists a receipt for d1
        self.calls.clear()

        self.batch = {"deliveryId": "d2", "messages": []}
        out = self.spawn.wait(ack="by-hand", timeout_ms=1)

        check_call = self.calls[0]
        self.assertEqual(check_call[check_call.index("--ack") + 1], "by-hand")
        self.assertEqual(out["acked"], "by-hand")

    def test_empty_delivery_id_persists_no_receipt_and_no_journal(self):
        self.batch = {"deliveryId": "", "messages": []}

        out = self.spawn.wait(ack=None, timeout_ms=1)

        self.assertIsNone(out["acked"])
        self.assertIsNone(self.spawn._journal.read_receipt(self.spawn.RUNTIME_DIR))
        self.assertFalse((self.spawn.RUNTIME_DIR / "events.jsonl").exists())

    def test_a_missing_receipt_after_a_crash_sends_no_ack_and_the_replay_is_a_no_op(self):
        """Simulates the crash the ordering in `wait` exists to survive: the
        registry write for the round already landed, but the receipt for
        the delivery that caused it never made it to disk (process died
        between `state_write` and `write_receipt`). The next `wait` must
        NOT send `--ack` for that delivery — nothing here proves Orca ever
        saw one — so it redelivers the identical batch, and the gate's own
        replay-safety (already proven in `TestWaitCountsRounds`) keeps
        `approval_rounds` from counting the same submission twice."""

        submission = (
            "## Plan\nGRE-1 — comment abc\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
        )
        question = self.message(
            id="q-1", type="question", body=submission,
            payload={"taskId": "task_1", "dispatchId": "ctx_1"},
        )
        self.batch = {"deliveryId": "d1", "messages": [question]}
        self.spawn.wait(ack=None, timeout_ms=1)
        first = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(first["approval_rounds"], 1)
        # The receipt this call just wrote never survived the crash.
        self.spawn._journal.write_receipt(self.spawn.RUNTIME_DIR, "")
        self.calls.clear()

        # Orca redelivers the same, still-unacked batch.
        out = self.spawn.wait(ack=None, timeout_ms=1)

        check_call = self.calls[0]
        self.assertNotIn("--ack", check_call)
        self.assertIsNone(out["acked"])
        second = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(second["approval_rounds"], 1, "replay must not double-count")

    def test_journal_line_carries_the_full_body(self):
        self.batch = {"deliveryId": "d1", "messages": [self.message()]}
        self.spawn.wait(ack=None, timeout_ms=1)

        lines = (self.spawn.RUNTIME_DIR / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["body"], DONE_BODY)

    def test_wait_return_value_carries_acked_and_elapsed_ms(self):
        self.batch = {"deliveryId": "d1", "messages": [self.message()]}

        out = self.spawn.wait(ack=None, timeout_ms=1)

        self.assertIsNone(out["acked"])
        self.assertIsInstance(out["elapsed_ms"], int)
        self.assertGreaterEqual(out["elapsed_ms"], 0)

    def test_status_without_a_ticket_reports_the_pending_delivery(self):
        """`status(None)` fanned out from a flat list into an object with a
        `pending_delivery` key read straight from the receipt — the only
        piece of this round without a test before now."""

        self.batch = {"deliveryId": "d1", "messages": [self.message()]}
        self.spawn.wait(ack=None, timeout_ms=1)

        out = self.spawn.status(None)

        self.assertEqual(out["pending_delivery"], "d1")
        self.assertEqual([rec["key"] for rec in out["spawns"]], ["GRE-1/planner"])


class TestVerdictOrdering(GateLoopCase):
    def test_the_approval_is_recorded_before_the_planner_is_told(self):
        """The reply unblocks the planner, which may run `spawn done` at
        once. A registry that still said `submitted` would refuse the report
        the human just authorized."""

        self.set_state(gate_state=self.spawn._gate.SUBMITTED, question_id="q-1")
        seen = {}

        def fake_orca(*argv, expect_lifecycle_ok=False):
            self.calls.append(list(argv))
            if argv[:2] == ("orchestration", "reply"):
                seen["state_at_reply"] = (
                    self.spawn.state_read()["GRE-1/planner"]["gate_state"]
                )
            return {}

        self.spawn.orca = fake_orca
        self.spawn.verdict("GRE-1", "approved", "")
        self.assertEqual(seen["state_at_reply"], self.spawn._gate.VERDICT_APPROVED)

    def test_a_tracker_failure_does_not_lose_a_delivered_verdict(self):
        self.set_state(gate_state=self.spawn._gate.SUBMITTED, question_id="q-1")
        self.tracker.fail = True
        out = self.spawn.verdict("GRE-1", "approved", "")
        self.assertIn("NOT cleared", out["label"])
        rec = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(rec["gate_state"], self.spawn._gate.VERDICT_APPROVED)
        self.assertNotIn("question_id", rec)

    def test_the_pending_question_is_only_cleared_after_the_reply_lands(self):
        self.set_state(gate_state=self.spawn._gate.SUBMITTED, question_id="q-1")

        def failing_orca(*argv, expect_lifecycle_ok=False):
            if argv[:2] == ("orchestration", "reply"):
                raise SystemExit("orca orchestration reply failed")
            return {}

        self.spawn.orca = failing_orca
        with self.assertRaises(SystemExit):
            self.spawn.verdict("GRE-1", "approved", "")
        rec = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(rec["question_id"], "q-1", "a failed reply must stay retryable")


class TestVerdictPublishesThePlanCopy(GateLoopCase):
    """GRE-187 item 4: the approved plan's one and only copy on the ticket
    is posted here — never on submission, never on REVISE — from the
    `plan_body` `wait` recorded off the genuine submission."""

    PLAN_BODY = "## Plan\nGRE-1 — full plan inline below\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"

    def test_approval_publishes_the_plan_and_the_notes_exactly_once(self):
        self.set_state(
            gate_state=self.spawn._gate.SUBMITTED, question_id="q-1", plan_body=self.PLAN_BODY,
        )
        out = self.spawn.verdict("GRE-1", "approved", "Ship it, but note the caveat.")
        self.assertEqual(out["plan_copy"], "posted")
        self.assertEqual(len(self.tracker.comments), 1)
        ticket, body = self.tracker.comments[0]
        self.assertEqual(ticket, "GRE-1")
        self.assertIn(self.PLAN_BODY.rstrip(), body)
        self.assertIn("## Approval", body)
        self.assertIn("APPROVED", body)
        self.assertIn("- Ship it, but note the caveat.", body)

    def test_revise_publishes_nothing(self):
        self.set_state(
            gate_state=self.spawn._gate.SUBMITTED, question_id="q-1", plan_body=self.PLAN_BODY,
        )
        out = self.spawn.verdict("GRE-1", "revise", "Fix the retry counter.")
        self.assertNotIn("plan_copy", out)
        self.assertEqual(self.tracker.comments, [])

    def test_a_post_comment_failure_is_visible_never_fatal(self):
        self.set_state(
            gate_state=self.spawn._gate.SUBMITTED, question_id="q-1", plan_body=self.PLAN_BODY,
        )
        self.tracker.fail_comment = True
        out = self.spawn.verdict("GRE-1", "approved", "")
        self.assertIn("NOT posted", out["plan_copy"])
        rec = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(rec["gate_state"], self.spawn._gate.VERDICT_APPROVED)

    def test_a_record_with_no_plan_body_is_reported_not_raised(self):
        """A record from before this round (or a submission the gate never
        saw, e.g. a hand-answered question) carries no `plan_body`."""

        self.set_state(gate_state=self.spawn._gate.SUBMITTED, question_id="q-1")
        out = self.spawn.verdict("GRE-1", "approved", "")
        self.assertEqual(out["plan_copy"], "NOT posted (no plan_body recorded)")
        self.assertEqual(self.tracker.comments, [])


class TestBriefVerb(GateLoopCase):
    """GRE-187 item 2: `spawn brief` is the Orchestrator's only sanctioned
    way to post a cut of work to the ticket — no try/except, because here
    posting the comment IS the work, unlike `verdict`'s cosmetic label/plan
    copy where a tracker failure is reported, not raised."""

    def _write(self, body):
        path = Path(self.tmp.name) / "cut.md"
        path.write_text(body)
        return str(path)

    def test_posts_the_file_body_verbatim_on_the_right_ticket(self):
        out = self.spawn.brief("GRE-1", self._write("Recorte da onda 10.\n"))
        self.assertEqual(out, {"ticket": "GRE-1", "posted": True, "comment": "comment-1"})
        self.assertEqual(self.tracker.comments, [("GRE-1", "Recorte da onda 10.\n")])

    def test_a_missing_file_is_refused_before_any_post(self):
        missing = str(Path(self.tmp.name) / "does-not-exist.md")
        with self.assertRaises(SystemExit):
            self.spawn.brief("GRE-1", missing)
        self.assertEqual(self.tracker.comments, [])

    def test_a_directory_is_refused_before_any_post(self):
        directory = Path(self.tmp.name) / "cut-dir"
        directory.mkdir()
        with self.assertRaises(SystemExit):
            self.spawn.brief("GRE-1", str(directory))
        self.assertEqual(self.tracker.comments, [])

    def test_an_empty_file_is_refused_before_any_post(self):
        with self.assertRaises(SystemExit):
            self.spawn.brief("GRE-1", self._write("   \n\n"))
        self.assertEqual(self.tracker.comments, [])

    def test_a_tracker_failure_propagates_never_swallowed(self):
        self.tracker.fail_comment = True
        with self.assertRaises(RuntimeError):
            self.spawn.brief("GRE-1", self._write("Recorte.\n"))


class TestRegistryLocation(GateLoopCase):
    def test_the_registry_and_its_directory_are_not_world_readable(self):
        """It holds Dispatch capability tokens, and a token is what
        authorizes a worker_done on someone else's dispatch."""

        self.spawn.state_write({"GRE-1/planner": dict(self.record)})
        self.assertEqual(self.spawn.STATE.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.spawn.STATE.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(list(self.spawn.STATE.parent.glob("*.tmp")), "no temp file left behind")


class TestStateLockClosesTheRace(GateLoopCase):
    """Acceptance criterion: `wait`/`verdict` concurrent must not lose an
    update. Two tests — the primitive directly, and the scenario from the
    issue body reproduced with a real thread."""

    def test_the_lock_primitive_blocks_a_concurrent_writer_then_lets_it_through(self):
        """Holding the lock via a SEPARATE fd simulates another process:
        flock conflicts across opens even within the same process (see
        `state_lock`'s docstring), so this is the deterministic version of
        the race — no timing dependent on `wait`'s own internals."""

        self.set_state(gate_state=self.spawn._gate.SUBMITTED, question_id="q-1")

        fd = os.open(self.spawn.STATE_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            result = {}

            def call_verdict():
                result["out"] = self.spawn.verdict("GRE-1", "approved", "")

            thread = threading.Thread(target=call_verdict)
            thread.start()
            thread.join(timeout=0.3)
            self.assertTrue(thread.is_alive(), "verdict must block while another opener holds the lock")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "verdict must complete once the lock is released")
        self.assertEqual(result["out"]["decision"], "APPROVED")

    def test_a_verdict_fired_mid_wait_is_not_reverted(self):
        """The regression this issue exists to close: `wait` reads a
        snapshot, a human's `verdict` lands on a DIFFERENT ticket while
        `wait` is still mid-flight, and `wait`'s own `state_write` — of the
        stale snapshot it read before the verdict — must not put GRE-1 back
        the way it found it.

        The verdict is fired from inside `FakeTracker.set_gate`, which
        `run()` calls from inside `wait`'s own critical section — as close
        to the real race as a single-process test gets. `on_set_gate` is
        cleared to a no-op before the verdict actually resolves the ticket
        gate label, so `verdict`'s own (cosmetic) `set_gate` call does not
        recurse into a second verdict."""

        self.set_state(gate_state=self.spawn._gate.SUBMITTED, question_id="q-1")
        data = self.spawn.state_read()
        data["GRE-2/planner"] = {
            "ticket": "GRE-2", "role": "planner", "worktree": "/tmp/gre-2",
            "terminal": "term_2", "task": "task_2", "dispatch": "ctx_2",
            "capability": "dcap_2", "gate_state": self.spawn.IDLE, "approval_rounds": 0,
        }
        self.spawn.state_write(data)

        submission = "## Plan\nGRE-2 — pointer\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
        self.batch = {"deliveryId": "d1", "messages": [self.message(
            id="q-2", type="question", body=submission,
            payload={"taskId": "task_2", "dispatchId": "ctx_2"},
        )]}

        box = {}

        def fire_verdict_once(ticket, on):
            if box.get("fired"):
                return
            box["fired"] = True
            self.tracker.on_set_gate = None  # do not recurse on the verdict's own cosmetic clear
            box["thread"] = threading.Thread(
                target=lambda: box.__setitem__("out", self.spawn.verdict("GRE-1", "approved", ""))
            )
            box["thread"].start()

        self.tracker.on_set_gate = fire_verdict_once
        self.spawn.wait(ack=None, timeout_ms=1)

        thread = box.get("thread")
        self.assertIsNotNone(thread, "the GRE-2 submission must have lit the label and fired the hook")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "the concurrent verdict must complete, not hang")

        rec = self.spawn.state_read()["GRE-1/planner"]
        self.assertEqual(rec["gate_state"], self.spawn._gate.VERDICT_APPROVED, "wait must not revert the verdict")
        self.assertNotIn("question_id", rec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
