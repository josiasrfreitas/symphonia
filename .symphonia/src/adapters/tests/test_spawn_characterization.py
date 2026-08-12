"""M0 of GRE-184: freeze the argv `spawn.py` emits today, byte for byte.

TLDR: every `orca`/`git` call `spawn.py` makes goes through
`subprocess.run` — this stub records the exact argv of each call, in
order, so the migration (Runner injection, composing the
`OrcaRuntimeAdapter`) can be proven to change nothing about what actually
reaches the CLI. These tests must not be edited during M1-M6; if a
refactor changes an argv here, that is the migration breaking its own
acceptance criterion, not a test to update.

The five scenarios are the ones cited in the approved Local Technical Plan
for GRE-184 (Linear comment a0950496): `spawn plan` happy path, `spawn
plan` with a dispatch that mints no capability (rollback), `spawn
implement` reusing a worktree, `status`/`retire`, and `spawn done`
approved — plus one scripted round of `spawn submit`'s `ask` (noted as a
risk in the plan: the resume loop itself is already covered by
`test_role_verbs`).

Run either way:

    cd .symphonia/src && python3 -m unittest adapters.tests.test_spawn_characterization
    python3 .symphonia/src/adapters/tests/test_spawn_characterization.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src, the sys.path root since the move
sys.path.insert(0, str(PACKAGE))

from adapters.tests.test_brief import _load_spawn
from adapters.tracker_adapter import Comment, Item, ItemKind, ItemRef, Openness

FIXED_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")

SUBMISSION = "## Plan\nGRE-1 — comment abc\n\n## Decisions\n1. x\n\n## Changes\nNone.\n"
REPORT = "## Plan\nGRE-1 — comment abc\n\n## Approval\n1 rodada.\n\n## Deviations\nNone.\n"


class FakeTracker:
    """Same double `test_brief.py` uses: `build_brief` never touches a real
    Linear tracker or network during characterization."""

    def __init__(self, item: Item, comments: tuple[Comment, ...] = ()):
        self._item = item
        self._comments = list(comments)

    def get_item(self, ticket, *, with_relations=False):
        return self._item

    def list_comments(self, id):
        return self._comments


def _item(ticket: str) -> Item:
    return Item(
        ref=ItemRef(id="uuid-1", key=ticket, url=f"https://linear.app/x/issue/{ticket}/t"),
        kind=ItemKind.IMPLEMENTATION_TICKET,
        title="Characterize the spawn argv",
        body="Full description here.",
        openness=Openness.OPEN,
    )


def _ok(result: dict) -> str:
    """One `orca --json` response, enveloped like the real CLI."""

    return json.dumps({"id": "req", "ok": True, "result": result})


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class RecordingRunner:
    """Replaces `spawn.subprocess.run`: records every argv verbatim, replays
    scripted responses in call order. A call past the end of the script is a
    test bug (an uncharacterized call), not a silent pass."""

    def __init__(self, responses: list[SimpleNamespace]):
        self._responses = list(responses)
        self.raw_calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.raw_calls.append(list(argv))
        if not self._responses:
            raise AssertionError(f"no scripted response left for {argv!r}")
        return self._responses.pop(0)

    def clean_calls(self) -> list[list[str]]:
        """Argv with the `orca .../--json` wrapper stripped, so expected
        lists read as what `spawn.py` is actually choosing to send."""

        out = []
        for argv in self.raw_calls:
            if argv and argv[0] == "orca":
                argv = argv[1:]
                if argv and argv[-1] == "--json":
                    argv = argv[:-1]
            out.append(argv)
        return out


class CharacterizationCase(unittest.TestCase):
    """One reloaded `spawn` module per test, a scratch runtime dir, a pinned
    session id (`uuid.uuid4` is the only source of nondeterminism in a
    launch command), and worktree setup stubbed out (it is a separate
    subprocess boundary — `setup_worktree.py` — not the one under test)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["SYMPHONIA_RUNTIME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "SYMPHONIA_RUNTIME", None)

        self.spawn = _load_spawn()
        self.RoleName = self.spawn.RoleName

        uuid_patch = mock.patch.object(self.spawn.uuid, "uuid4", return_value=FIXED_UUID)
        uuid_patch.start()
        self.addCleanup(uuid_patch.stop)

        setup_patch = mock.patch.object(
            self.spawn._setup_worktree, "setup",
            return_value={"copied": [], "skipped": [".env"], "missing": []},
        )
        setup_patch.start()
        self.addCleanup(setup_patch.stop)

    def install_runner(self, responses: list[SimpleNamespace]) -> RecordingRunner:
        runner = RecordingRunner(responses)
        patcher = mock.patch.object(self.spawn.subprocess, "run", runner)
        patcher.start()
        self.addCleanup(patcher.stop)
        return runner

    def fake_tracker_for(self, ticket: str) -> FakeTracker:
        return FakeTracker(_item(ticket))

    def stub_linear_tracker(self, ticket: str) -> None:
        """`spawn.spawn()` builds its own `LinearTracker()` with no tracker
        param to inject (unlike `build_brief`, called directly, in
        `expected_brief`) — the module-level class is swapped instead so the
        planner scenarios never reach the network."""

        tracker = self.fake_tracker_for(ticket)
        patcher = mock.patch.object(self.spawn._linear, "LinearTracker", return_value=tracker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def expected_brief(self, role, ticket: str, workspace: str, branch: str) -> str:
        """The Brief `spawn plan` will inject — computed once, outside the
        scenario's own recorded call sequence, so precomputing it does not
        eat one of the scripted responses. `_current_branch` is the only
        subprocess boundary `build_brief` crosses; stubbed here to the
        branch already scripted for the real call."""

        with mock.patch.object(self.spawn.subprocess, "run", return_value=_proc(f"{branch}\n")):
            return self.spawn.build_brief(
                role, ticket, workspace, tracker=self.fake_tracker_for(ticket),
            )


PLAN_COMMAND_PLANNER = (
    "claude --model fable --effort medium --permission-mode bypassPermissions "
    f"--session-id {FIXED_UUID}"
)
PLAN_COMMAND_IMPLEMENTER = (
    "claude --model sonnet --effort high --permission-mode bypassPermissions "
    f"--session-id {FIXED_UUID}"
)


class SpawnPlanHappyPath(CharacterizationCase):
    """1. `spawn plan`: worktree create with lineage -> orphan shell closed
    -> badge -> terminal create -> tui-idle wait -> task-create with the
    Brief as spec -> dispatch --inject --return-preamble -> capability
    captured from the preamble."""

    def test_argv_sequence(self):
        ticket, wt_id, path = "GRE-201", "wt-1", "/workspaces/gre-201"
        brief = self.expected_brief(
            self.RoleName.PLANNER, ticket, path, branch="gre-201-plan",
        )
        self.stub_linear_tracker(ticket)
        runner = self.install_runner([
            _proc(_ok({"worktrees": []})),                               # 1 find_worktree
            _proc("main\n"),                                             # 2 default_base
            _proc(_ok({"worktree": {"id": wt_id, "path": path}})),       # 3 worktree create
            _proc(_ok({"terminals": [{"handle": "term-orphan"}]})),      # 4 terminal list
            _proc(_ok({})),                                              # 5 terminal close (orphan)
            _proc(_ok({})),                                              # 6 worktree set (badge)
            _proc(_ok({"terminal": {"handle": "term-1"}})),              # 7 terminal create
            _proc(_ok({})),                                              # 8 terminal wait tui-idle
            _proc("gre-201-plan\n"),                                     # 9 _current_branch (in build_brief)
            _proc(_ok({"task": {"id": "task-1"}})),                      # 10 task-create
            _proc(_ok({                                                  # 11 dispatch
                "dispatch": {"id": "disp-1"},
                "preamble": "orca orchestration send --dispatch-capability dcap_ABC123 --type worker_done",
            })),
        ])

        record = self.spawn.spawn(self.RoleName.PLANNER, ticket, fresh_worktree=True)

        self.assertEqual(runner.clean_calls(), [
            ["worktree", "list"],
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            ["worktree", "create", "--name", "gre-201", "--parent-worktree", "active",
             "--base-branch", "main", "--setup", "run", "--linear-issue", ticket,
             "--comment", f"symphonia ticket {ticket}"],
            ["terminal", "list", "--worktree", f"id:{wt_id}"],
            ["terminal", "close", "--terminal", "term-orphan", "--tab"],
            ["worktree", "set", "--worktree", f"id:{wt_id}",
             "--display-name", f"🧭 {ticket} · planning", "--workspace-status", "in-progress"],
            ["terminal", "create", "--worktree", f"id:{wt_id}",
             "--title", f"🧭 planning · {ticket}", "--command", PLAN_COMMAND_PLANNER],
            ["terminal", "wait", "--terminal", "term-1", "--for", "tui-idle", "--timeout-ms", "120000"],
            ["git", "-C", path, "branch", "--show-current"],
            ["orchestration", "task-create", "--spec", brief],
            ["orchestration", "dispatch", "--task", "task-1", "--to", "term-1",
             "--inject", "--return-preamble"],
        ])
        self.assertEqual(record["capability"], "dcap_ABC123")
        self.assertEqual(record["worktree"], path)
        self.assertEqual(record["terminal"], "term-1")


class SpawnPlanNoCapability(CharacterizationCase):
    """2. A dispatch whose preamble mints no capability rolls the task and
    the terminal back and refuses the spawn — spawn.py:398-428."""

    def test_rollback_argv(self):
        ticket, wt_id, path = "GRE-201", "wt-1", "/workspaces/gre-201"
        self.expected_brief(self.RoleName.PLANNER, ticket, path, branch="gre-201-plan")
        self.stub_linear_tracker(ticket)
        runner = self.install_runner([
            _proc(_ok({"worktrees": []})),
            _proc("main\n"),
            _proc(_ok({"worktree": {"id": wt_id, "path": path}})),
            _proc(_ok({"terminals": [{"handle": "term-orphan"}]})),
            _proc(_ok({})),
            _proc(_ok({})),
            _proc(_ok({"terminal": {"handle": "term-1"}})),
            _proc(_ok({})),
            _proc("gre-201-plan\n"),
            _proc(_ok({"task": {"id": "task-1"}})),
            _proc(_ok({"dispatch": {"id": "disp-1"}, "preamble": "no capability token in this text"})),
            _proc(_ok({})),  # task-update --status failed
            _proc(_ok({})),  # terminal close
        ])

        with self.assertRaises(SystemExit) as ctx:
            self.spawn.spawn(self.RoleName.PLANNER, ticket, fresh_worktree=True)
        self.assertIn("minted no capability", str(ctx.exception))

        self.assertEqual(runner.clean_calls()[-2:], [
            ["orchestration", "task-update", "--id", "task-1", "--status", "failed",
             "--result", json.dumps({"reason": "no dispatch capability in the preamble"})],
            ["terminal", "close", "--terminal", "term-1", "--tab"],
        ])
        self.assertEqual(len(runner.clean_calls()), 13)


class SpawnImplementReusesWorktree(CharacterizationCase):
    """3. `spawn implement`: no worktree create, no orphan-shell close —
    `build_brief()` is the Brief now, same as the planner's, so one extra
    subprocess boundary (`_current_branch`) joins the sequence."""

    def test_argv_sequence(self):
        ticket, wt_id, path, branch = "GRE-201", "wt-1", "/workspaces/gre-201", "gre-201-implement"
        expected_spec = self.expected_brief(
            self.RoleName.IMPLEMENTER, ticket, path, branch=branch,
        )
        self.stub_linear_tracker(ticket)
        runner = self.install_runner([
            _proc(_ok({"worktrees": [{"id": wt_id, "path": path}]})),  # find_worktree
            _proc(_ok({})),                                            # worktree set (badge)
            _proc(_ok({"terminal": {"handle": "term-2"}})),            # terminal create
            _proc(_ok({})),                                            # terminal wait tui-idle
            _proc(f"{branch}\n"),                                      # _current_branch (in build_brief)
            _proc(_ok({"task": {"id": "task-2"}})),                    # task-create
            _proc(_ok({                                                # dispatch
                "dispatch": {"id": "disp-2"},
                "preamble": "orca orchestration send --dispatch-capability dcap_DEF456 --type worker_done",
            })),
        ])

        record = self.spawn.spawn(self.RoleName.IMPLEMENTER, ticket, fresh_worktree=False)

        self.assertEqual(runner.clean_calls(), [
            ["worktree", "list"],
            ["worktree", "set", "--worktree", f"id:{wt_id}",
             "--display-name", f"🔨 {ticket} · implementing", "--workspace-status", "in-progress"],
            ["terminal", "create", "--worktree", f"id:{wt_id}",
             "--title", f"🔨 implementing · {ticket}", "--command", PLAN_COMMAND_IMPLEMENTER],
            ["terminal", "wait", "--terminal", "term-2", "--for", "tui-idle", "--timeout-ms", "120000"],
            ["git", "-C", path, "branch", "--show-current"],
            ["orchestration", "task-create", "--spec", expected_spec],
            ["orchestration", "dispatch", "--task", "task-2", "--to", "term-2",
             "--inject", "--return-preamble"],
        ])
        self.assertEqual(record["capability"], "dcap_DEF456")


class SpawnStatusAndRetire(CharacterizationCase):
    """4. `status`: one `dispatch-show` per recorded spawn. `retire`:
    worker-stop tried, terminal closed, and a still-open dispatch settled
    as failed."""

    def _seed(self, extra=None):
        # Deliberately the pre-GRE-186 S3 record shape: `model_requested`,
        # no `session`/`tier_evidence`. `test_status_argv` doubles as the
        # tolerant-read test C1 of the S3 verdict asks for — this seed is
        # exactly the old shape `status()` must read without raising.
        self.spawn.state_write({
            "GRE-1/planner": {
                "ticket": "GRE-1", "role": "planner", "worktree": "/workspaces/gre-1",
                "terminal": "term-9", "task": "task-9", "dispatch": "disp-9",
                "capability": "dcap_xyz", "gate_state": "idle", "approval_rounds": 0,
                "model_requested": "sonnet", "transcript": None,
                **(extra or {}),
            }
        })

    def test_status_argv(self):
        self._seed()
        runner = self.install_runner([
            _proc(_ok({"dispatch": {"status": "dispatched"}})),
        ])
        out = self.spawn.status("GRE-1")
        self.assertEqual(runner.clean_calls(), [
            ["orchestration", "dispatch-show", "--task", "task-9"],
        ])
        self.assertEqual(out[0]["dispatch_status"], "dispatched")
        # A record with no `session`/`tier_evidence` (every record before
        # this round) reads as `unverifiable`, never a guess and never a
        # crash on a missing key.
        self.assertEqual(out[0]["evidence_kind"], "unverifiable")
        self.assertIn("pre-GRE-186", out[0]["evidence_detail"])

    def test_retire_argv(self):
        self._seed()
        runner = self.install_runner([
            _proc(_ok({"stopped": True})),                          # worker-stop
            _proc(_ok({})),                                          # terminal close
            _proc(_ok({"dispatch": {"status": "dispatched"}})),      # dispatch-show
            _proc(_ok({})),                                          # task-update (settle)
        ])
        result = self.spawn.retire("GRE-1", "planner")
        self.assertEqual(runner.clean_calls(), [
            ["orchestration", "worker-stop", "--dispatch", "disp-9"],
            ["terminal", "close", "--terminal", "term-9", "--tab"],
            ["orchestration", "dispatch-show", "--task", "task-9"],
            ["orchestration", "task-update", "--id", "task-9", "--status", "failed",
             "--result", json.dumps({"reason": "planner retired by the Orchestrator"})],
        ])
        self.assertEqual(result["retired"], "GRE-1/planner")


class SpawnDoneApproved(CharacterizationCase):
    """5. `spawn done` for an approved planner: exactly one `orchestration
    send`, one `--payload`, no structured flags, `expect_lifecycle_ok`."""

    def setUp(self):
        super().setUp()
        os.environ["ORCA_TERMINAL_HANDLE"] = "term_planner"
        self.addCleanup(os.environ.pop, "ORCA_TERMINAL_HANDLE", None)
        self.record = {
            "ticket": "GRE-1", "role": "planner", "worktree": "/workspaces/gre-1",
            "terminal": "term_planner", "task": "task_1", "dispatch": "ctx_1",
            "capability": "dcap_xyz", "gate_state": "verdict-approved", "approval_rounds": 1,
        }
        self.spawn.state_write({"GRE-1/planner": self.record})

    def body_file(self, text: str) -> str:
        path = Path(self.tmp.name) / "body.md"
        path.write_text(text)
        return str(path)

    def test_argv_sequence(self):
        expected_body = self.spawn._reports.set_approval_rounds(REPORT, 1)
        payload = json.loads(
            self.spawn._payload_for(self.record, "succeeded", {"planApproved": True, "approvalRounds": 1})
        )
        payload["filesModified"] = ["a.py"]

        runner = self.install_runner([_proc(_ok({}))])
        self.spawn.done("GRE-1", self.body_file(REPORT), outcome="succeeded", files_modified="a.py")

        self.assertEqual(runner.clean_calls(), [[
            "orchestration", "send", "--from", "term_planner", "--type", "worker_done",
            "--subject", "GRE-1 planner: succeeded", "--body", expected_body,
            "--payload", json.dumps(payload), "--dispatch-capability", "dcap_xyz",
        ]])


class SpawnSubmitSingleRound(CharacterizationCase):
    """One scripted round of `spawn submit`'s `ask` — the resume-after-
    timeout loop is already characterized by `test_role_verbs.TestSubmit`;
    this pins the argv of the call that loop is built from."""

    def setUp(self):
        super().setUp()
        os.environ["ORCA_TERMINAL_HANDLE"] = "term_planner"
        self.addCleanup(os.environ.pop, "ORCA_TERMINAL_HANDLE", None)
        self.spawn.state_write({"GRE-1/planner": {
            "ticket": "GRE-1", "role": "planner", "worktree": "/workspaces/gre-1",
            "terminal": "term_planner", "task": "task_1", "dispatch": "ctx_1",
            "capability": "dcap_xyz", "gate_state": "idle", "approval_rounds": 0,
        }})

    def body_file(self, text: str) -> str:
        path = Path(self.tmp.name) / "body.md"
        path.write_text(text)
        return str(path)

    def test_argv_sequence(self):
        runner = self.install_runner([
            _proc(json.dumps({
                "answer": "APPROVED\n\n- nota", "messageId": "q-1",
                "timedOut": False, "timeoutMs": 1000,
            })),
        ])
        out = self.spawn.submit("GRE-1", self.body_file(SUBMISSION), max_wait_ms=60000)

        self.assertEqual(runner.clean_calls(), [[
            "orchestration", "ask", "--from", "term_planner",
            "--dispatch-capability", "dcap_xyz",
            "--question", SUBMISSION, "--timeout-ms", "60000",
        ]])
        self.assertEqual(out["verdict"], "approved")


# `BriefBatonText` (the GRE-186 S3 baton-text scenarios) lived here until
# the GRE-187 correction round (C8/S6): it called `build_brief()` directly
# through `expected_brief`, exactly like `test_brief.py`'s
# `test_implementer_brief_carries_the_baton_rule` and
# `test_reviewer_briefs_carry_the_read_only_line_and_no_handoff` — same
# function, same fake tracker, no argv or dispatch path in between. Its own
# docstring's justification ("the text has no oracle anywhere else") stopped
# being true once `test_brief.py` grew those two tests, so it was a second
# oracle for the same claim rather than added coverage. Removed; the two
# tests in `test_brief.py` are the single house for this text now.


if __name__ == "__main__":
    unittest.main(verbosity=2)
