"""Tests for the two verbs a ROLE runs: `spawn submit` and `spawn done`.

TLDR: these are the return half of the spawn interface, and every check here
exists because of something measured against Orca 1.4.168, not because of a
style preference:

- `--payload` and the structured flags (`--task-id`, `--dispatch-id`,
  `--outcome`) are mutually exclusive — sending both is `invalid_argument`
  and no message goes out at all. `test_done_sends_exactly_one_payload_and_no_
  structured_flags` is the regression that pins this.
- an injected dispatch mints a capability, and a lifecycle message without it
  is refused with `dispatch_capability_invalid`.
- a dispatch grants exactly one `worker_done`, so a body that does not follow
  its contract has to fail BEFORE anything is sent — there is no second try.

No network: `SPAWN.orca` is replaced by a fake that records argv, and the
registry is redirected with SYMPHONIA_RUNTIME. Run either way:

    cd .symphonia/src && python3 -m unittest adapters.tests.test_role_verbs
    python3 .symphonia/src/adapters/tests/test_role_verbs.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src, the sys.path root since the move
sys.path.insert(0, str(PACKAGE))

from adapters.orca import adapter as _cli
from adapters.tests.test_brief import _load_spawn

SUBMISSION = "## Plan\nGRE-1 — comment abc\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
REPORT = "## Plan\nGRE-1 — comment abc\n\n## Approval\n1 rodada.\n\n## Deviations\nNone.\n"


class RoleVerbCase(unittest.TestCase):
    """One live planner record, a fake `orca`, and a scratch runtime dir."""

    gate_state = "verdict-approved"
    approval_rounds = 1

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SYMPHONIA_RUNTIME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SYMPHONIA_RUNTIME", None)
        os.environ["ORCA_TERMINAL_HANDLE"] = "term_planner"
        self.addCleanup(os.environ.pop, "ORCA_TERMINAL_HANDLE", None)

        # Reloaded per test so STATE picks up this test's runtime dir.
        self.spawn = _load_spawn()
        # The record's worktree is the checkout the suite actually runs in:
        # the handle-less fallback compares against `git rev-parse
        # --show-toplevel`, so a made-up path would make every fallback test
        # pass for the wrong reason (it would raise on "no match" instead of
        # on what the test is about).
        self.toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
        ).stdout.strip()
        self.record = {
            "ticket": "GRE-1",
            "role": "planner",
            "worktree": self.toplevel,
            "terminal": "term_planner",
            "task": "task_1",
            "dispatch": "ctx_1",
            "capability": "dcap_abc",
            "gate_state": self.gate_state,
            "approval_rounds": self.approval_rounds,
        }
        self.spawn.state_write({"GRE-1/planner": self.record})

        self.calls: list[tuple] = []
        self.answers: list[dict] = []

        def fake_orca(*argv, expect_lifecycle_ok=False):
            self.calls.append((argv, expect_lifecycle_ok))
            return self.answers.pop(0) if self.answers else {}

        self.spawn.orca = fake_orca

    def body_file(self, text: str) -> str:
        path = Path(self.tmp.name) / "body.md"
        path.write_text(text)
        return str(path)

    def argv_of(self, index: int = 0) -> list[str]:
        return list(self.calls[index][0])

    def payload_of(self, index: int = 0) -> dict:
        argv = self.argv_of(index)
        return json.loads(argv[argv.index("--payload") + 1])


class TestOwnRecord(RoleVerbCase):
    def test_finds_itself_by_terminal_handle(self):
        key, rec = self.spawn.own_record("GRE-1")
        self.assertEqual(key, "GRE-1/planner")
        self.assertEqual(rec["dispatch"], "ctx_1")

    def test_a_handle_that_is_not_a_recorded_role_fails_loudly(self):
        os.environ["ORCA_TERMINAL_HANDLE"] = "term_someone_else"
        with self.assertRaises(SystemExit) as ctx:
            self.spawn.own_record("GRE-1")
        self.assertIn("term_someone_else", str(ctx.exception))

    def test_two_live_roles_in_one_worktree_without_a_handle_is_refused(self):
        """The fallback matches on the worktree, and after the planner the
        implementer reuses that same checkout — ambiguity must never be
        resolved by picking one."""
        os.environ.pop("ORCA_TERMINAL_HANDLE")
        data = self.spawn.state_read()
        data["GRE-1/implementer"] = {**self.record, "role": "implementer", "terminal": "term_impl"}
        self.spawn.state_write(data)
        with self.assertRaises(SystemExit) as ctx:
            self.spawn.own_record("GRE-1")
        self.assertIn("cannot tell which role", str(ctx.exception))

    def test_a_retired_role_is_not_a_candidate(self):
        """A retired role's terminal is closed; reporting on its behalf would
        aim a lifecycle message at a dispatch that is already settled."""

        os.environ.pop("ORCA_TERMINAL_HANDLE")
        data = self.spawn.state_read()
        data["GRE-1/planner"] = {**self.record, "retired": True}
        self.spawn.state_write(data)
        with self.assertRaises(SystemExit) as ctx:
            self.spawn.own_record("GRE-1")
        self.assertIn("no live spawn", str(ctx.exception))

    def test_the_fallback_finds_a_single_role_by_worktree(self):
        """The fallback has to actually work, or the two tests around it
        would be passing on a path that never matches anything."""

        os.environ.pop("ORCA_TERMINAL_HANDLE")
        key, rec = self.spawn.own_record("GRE-1")
        self.assertEqual(key, "GRE-1/planner")
        self.assertEqual(rec["worktree"], self.toplevel)


class TestDone(RoleVerbCase):
    def test_sends_exactly_one_payload_and_no_structured_flags(self):
        """The regression that matters: `--payload` alongside `--task-id` /
        `--dispatch-id` / `--outcome` / `--files-modified` is refused by Orca
        outright, so the message must carry one payload and nothing else."""

        self.spawn.done("GRE-1", self.body_file(REPORT), outcome="succeeded",
                        files_modified="a.py,b.py")
        argv = self.argv_of()
        self.assertEqual(argv.count("--payload"), 1)
        for banned in ("--task-id", "--dispatch-id", "--outcome", "--files-modified"):
            self.assertNotIn(banned, argv)
        payload = self.payload_of()
        self.assertEqual(payload["taskId"], "task_1")
        self.assertEqual(payload["dispatchId"], "ctx_1")
        self.assertEqual(payload["outcome"], "succeeded")
        self.assertEqual(payload["filesModified"], ["a.py", "b.py"])

    def test_carries_the_dispatch_capability(self):
        self.spawn.done("GRE-1", self.body_file(REPORT), outcome="succeeded", files_modified="")
        argv = self.argv_of()
        self.assertIn("--dispatch-capability", argv)
        self.assertEqual(argv[argv.index("--dispatch-capability") + 1], "dcap_abc")

    def test_the_lifecycle_answer_is_checked(self):
        self.spawn.done("GRE-1", self.body_file(REPORT), outcome="succeeded", files_modified="")
        self.assertTrue(self.calls[0][1], "worker_done must be sent with expect_lifecycle_ok")

    def test_approval_facts_come_from_the_record_not_the_body(self):
        """The role no longer asserts `planApproved` / `approvalRounds`; the
        gate counted the rounds and recorded the approval."""

        data = self.spawn.state_read()
        data["GRE-1/planner"]["approval_rounds"] = 3
        self.spawn.state_write(data)
        self.spawn.done("GRE-1", self.body_file(REPORT), outcome="succeeded", files_modified="")
        payload = self.payload_of()
        self.assertTrue(payload["planApproved"])
        self.assertEqual(payload["approvalRounds"], 3)
        body = self.argv_of()[self.argv_of().index("--body") + 1]
        self.assertIn("3 rodadas.", body)

    def test_sections_the_package_does_not_know_are_passed_through(self):
        """Only `## Approval` is the gate's to rewrite. Anything else the
        planner wrote has to reach the ticket — there is no second
        worker_done to send it in."""

        body = REPORT.rstrip() + "\n\n## Risks\n- o retry pode duplicar\n"
        self.spawn.done("GRE-1", self.body_file(body), outcome="succeeded", files_modified="")
        sent = self.argv_of()[self.argv_of().index("--body") + 1]
        self.assertIn("## Risks\n- o retry pode duplicar", sent)

    def test_a_body_that_does_not_parse_sends_nothing(self):
        broken = "## Plan\npointer\n\n## Deviations\nNone.\n"  # no Approval
        with self.assertRaises(Exception):
            self.spawn.done("GRE-1", self.body_file(broken), outcome="succeeded", files_modified="")
        self.assertEqual(self.calls, [], "the one allowed worker_done must not be spent")

    def test_a_body_missing_a_section_the_rewriter_ignores_sends_nothing(self):
        """`set_approval_rounds` only needs `## Approval`, so a body missing
        `## Deviations` would sail past it — the parse is what catches it, and
        it has to run before the one allowed worker_done is spent."""

        half = "## Plan\nGRE-1 — c\n\n## Approval\n1 rodada.\n"
        with self.assertRaises(Exception):
            self.spawn.done("GRE-1", self.body_file(half), outcome="succeeded", files_modified="")
        self.assertEqual(self.calls, [])

    def test_an_unknown_outcome_sends_nothing(self):
        with self.assertRaises(SystemExit):
            self.spawn.done("GRE-1", self.body_file(REPORT), outcome="maybe", files_modified="")
        self.assertEqual(self.calls, [])


class TestDoneBeforeApproval(RoleVerbCase):
    gate_state = "submitted"

    def test_refuses_and_sends_nothing(self):
        with self.assertRaises(SystemExit) as ctx:
            self.spawn.done("GRE-1", self.body_file(REPORT), outcome="succeeded", files_modified="")
        self.assertIn("not approved", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_a_failed_outcome_is_always_allowed(self):
        """A planner that gives up must be able to say so before any verdict
        — the gate leaves a failed report to the human."""

        self.spawn.done("GRE-1", self.body_file("gave up"), outcome="failed", files_modified="")
        self.assertEqual(self.payload_of()["outcome"], "failed")


class TestDoneEmptySuccess(unittest.TestCase):
    """GRE-187 item 6: `done --outcome succeeded` from a write-access,
    non-planner role is refused before anything is sent if the worktree
    shows no change against `head_at_dispatch` — and, for every role and
    either outcome, an empty body is refused outright.

    A real temp git repo, not a fake: the comparison is `git -C <worktree>
    rev-parse HEAD` / `git status --porcelain`, and stubbing those out would
    test the stub, not the two commands that actually run.
    """

    REPORT_BODY = "Report body text.\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SYMPHONIA_RUNTIME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SYMPHONIA_RUNTIME", None)
        os.environ["ORCA_TERMINAL_HANDLE"] = "term_impl"
        self.addCleanup(os.environ.pop, "ORCA_TERMINAL_HANDLE", None)

        self.spawn = _load_spawn()

        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.repo, check=True)
        (self.repo / "a.txt").write_text("x")
        subprocess.run(["git", "add", "a.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo, check=True)
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()

        self.record = {
            "ticket": "GRE-1", "role": "implementer", "access": "write",
            "worktree": str(self.repo), "terminal": "term_impl",
            "task": "task_1", "dispatch": "ctx_1", "capability": "dcap_abc",
            "head_at_dispatch": head,
        }
        self.spawn.state_write({"GRE-1/implementer": self.record})

        self.calls: list[tuple] = []

        def fake_orca(*argv, expect_lifecycle_ok=False):
            self.calls.append((argv, expect_lifecycle_ok))
            return {}

        self.spawn.orca = fake_orca

    def body_file(self, text: str) -> str:
        path = Path(self.tmp.name) / "body.md"
        path.write_text(text)
        return str(path)

    def _set(self, **updates) -> None:
        data = self.spawn.state_read()
        data["GRE-1/implementer"].update(updates)
        self.spawn.state_write(data)

    def test_clean_worktree_same_head_refuses(self):
        with self.assertRaises(SystemExit) as ctx:
            self.spawn.done(
                "GRE-1", self.body_file(self.REPORT_BODY),
                outcome="succeeded", files_modified="",
            )
        self.assertIn("no change", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_a_new_commit_sends(self):
        (self.repo / "b.txt").write_text("y")
        subprocess.run(["git", "add", "b.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=self.repo, check=True)
        self.spawn.done(
            "GRE-1", self.body_file(self.REPORT_BODY), outcome="succeeded", files_modified="",
        )
        self.assertEqual(len(self.calls), 1)

    def test_an_uncommitted_change_sends_and_flags_the_record(self):
        """Real work, just never committed — accepted, not refused, but the
        record is flagged so the Orchestrator can check without opening the
        worktree by hand (correction round: implementer delivered good work
        and never ran `git commit`)."""

        (self.repo / "c.txt").write_text("z")
        self.spawn.done(
            "GRE-1", self.body_file(self.REPORT_BODY), outcome="succeeded", files_modified="",
        )
        self.assertEqual(len(self.calls), 1)
        rec = self.spawn.state_read()["GRE-1/implementer"]
        self.assertTrue(rec["uncommitted_work"])

    def test_a_new_commit_does_not_flag_the_record(self):
        (self.repo / "b.txt").write_text("y")
        subprocess.run(["git", "add", "b.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=self.repo, check=True)
        self.spawn.done(
            "GRE-1", self.body_file(self.REPORT_BODY), outcome="succeeded", files_modified="",
        )
        rec = self.spawn.state_read()["GRE-1/implementer"]
        self.assertNotIn("uncommitted_work", rec)

    def test_failed_outcome_with_clean_worktree_sends(self):
        self.spawn.done(
            "GRE-1", self.body_file(self.REPORT_BODY), outcome="failed", files_modified="",
        )
        self.assertEqual(len(self.calls), 1)

    def test_read_access_role_sends_even_if_clean(self):
        """A reviewer legitimately writes no code; its report IS the work."""

        self._set(access="read")
        self.spawn.done(
            "GRE-1", self.body_file(self.REPORT_BODY), outcome="succeeded", files_modified="",
        )
        self.assertEqual(len(self.calls), 1)

    def test_no_baseline_sends(self):
        """A record from before this round has no `head_at_dispatch`; the
        check is skipped rather than trapping a role that predates it."""

        data = self.spawn.state_read()
        data["GRE-1/implementer"].pop("head_at_dispatch")
        self.spawn.state_write(data)
        self.spawn.done(
            "GRE-1", self.body_file(self.REPORT_BODY), outcome="succeeded", files_modified="",
        )
        self.assertEqual(len(self.calls), 1)

    def test_a_measurement_failure_does_not_refuse(self):
        """The verdict's ressalva: if git itself cannot answer (worktree
        gone, broken repo), that is not proof of an empty success — a false
        positive here costs a whole round's work, a false negative costs a
        check the Orchestrator already does."""

        self._set(worktree=str(self.repo) + "-missing")
        self.spawn.done(
            "GRE-1", self.body_file(self.REPORT_BODY), outcome="succeeded", files_modified="",
        )
        self.assertEqual(len(self.calls), 1)

    def test_empty_body_refuses(self):
        """One case (implementer, failed) is enough: the check sits before
        any role/outcome branch in `done()`, so it never sees either — the
        guarantee comes from that position in the code, not from exercising
        every combination here."""

        with self.assertRaises(SystemExit) as ctx:
            self.spawn.done("GRE-1", self.body_file("   \n"), outcome="failed", files_modified="")
        self.assertIn("empty report", str(ctx.exception))
        self.assertEqual(self.calls, [])


class TestSubmit(RoleVerbCase):
    gate_state = "idle"
    approval_rounds = 0

    def test_parses_the_verdict_instead_of_returning_prose(self):
        self.answers = [{"answer": "APPROVED\n\n- cuide do retry", "messageId": "q-1",
                         "timedOut": False, "timeoutMs": 1000}]
        out = self.spawn.submit("GRE-1", self.body_file(SUBMISSION), max_wait_ms=60000)
        self.assertEqual(out["verdict"], "approved")
        self.assertEqual(out["notes"], ["cuide do retry"])
        self.assertEqual(out["question_id"], "q-1")

    def test_a_timeout_resumes_the_same_question(self):
        """`ask` clamps at 30 minutes and a human verdict takes longer; a
        second `ask` would be a second question for the gate to reconcile."""

        self.answers = [
            {"answer": None, "messageId": "q-1", "timedOut": True, "timeoutMs": 1000},
            {"answer": "REVISE\n\n- corrija o escopo", "messageId": "q-1",
             "timedOut": False, "timeoutMs": 1000},
        ]
        out = self.spawn.submit("GRE-1", self.body_file(SUBMISSION), max_wait_ms=60000)
        self.assertEqual(out["verdict"], "revise")
        first, second = self.argv_of(0), self.argv_of(1)
        self.assertIn("--question", first)
        self.assertNotIn("--question", second)
        self.assertEqual(second[second.index("--resume") + 1], "q-1")

    def test_gives_up_after_max_wait_without_asking_again(self):
        self.answers = [{"answer": None, "messageId": "q-1", "timedOut": True, "timeoutMs": 1000}]
        with self.assertRaises(SystemExit) as ctx:
            self.spawn.submit("GRE-1", self.body_file(SUBMISSION), max_wait_ms=1000)
        self.assertIn("q-1", str(ctx.exception))
        self.assertEqual(len(self.calls), 1)

    def test_a_verdict_that_does_not_parse_is_a_readable_error(self):
        """Someone answering by hand instead of through `spawn verdict` must
        not produce a traceback, and must not send the planner back to
        submit — that files a second question and costs another verdict."""

        self.answers = [{"answer": "LGTM, go", "messageId": "q-1",
                         "timedOut": False, "timeoutMs": 1000}]
        with self.assertRaises(SystemExit) as ctx:
            self.spawn.submit("GRE-1", self.body_file(SUBMISSION), max_wait_ms=60000)
        message = str(ctx.exception)
        self.assertIn("Do NOT submit again", message)
        self.assertIn("LGTM, go", message)

    def test_a_malformed_submission_asks_nothing(self):
        broken = "## Plan\nGRE-1 — pointer\n\n## Changes\nNone.\n"  # no Decisions
        with self.assertRaises(Exception):
            self.spawn.submit("GRE-1", self.body_file(broken), max_wait_ms=60000)
        self.assertEqual(self.calls, [])

    def test_a_submission_filed_under_another_ticket_asks_nothing(self):
        other = SUBMISSION.replace("GRE-1 —", "GRE-9 —")
        with self.assertRaises(SystemExit) as ctx:
            self.spawn.submit("GRE-1", self.body_file(other), max_wait_ms=60000)
        self.assertIn("GRE-9", str(ctx.exception))
        self.assertEqual(self.calls, [])


class TestCapabilityExtraction(RoleVerbCase):
    def test_reads_the_token_orca_only_prints_in_the_preamble(self):
        preamble = (
            "orca orchestration send --from term_x \\\n"
            "  --dispatch-capability dcap_CsezluWw_EVuCTHODwEPod--nDSh5BcFhdIKKDcVwUc \\\n"
            "  --type worker_done\n"
        )
        self.assertEqual(
            _cli._capability_of(preamble),
            "dcap_CsezluWw_EVuCTHODwEPod--nDSh5BcFhdIKKDcVwUc",
        )

    def test_no_token_is_not_an_error_here(self):
        self.assertIsNone(_cli._capability_of("no capability in this text"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
