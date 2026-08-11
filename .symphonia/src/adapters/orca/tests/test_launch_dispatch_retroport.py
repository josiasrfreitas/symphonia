"""GRE-184 M3: the adapter's launch/dispatch argv, held to the same
measured shape `test_spawn_characterization.py` freezes for
`spawn.spawn`/`spawn._require_capability`. Run either way:

    cd .symphonia/src && python3 -m unittest adapters.orca.tests.test_launch_dispatch_retroport
    python3 .symphonia/src/adapters/orca/tests/test_launch_dispatch_retroport.py
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest import mock

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from adapters.orca.adapter import OrcaCliError, OrcaRuntimeAdapter, RunResult
from adapters.runtime_adapter import Access, CapabilityTier, ContextRef, RoleName, RoleSpec, WorkspaceRef

FIXED_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _ok(result: dict) -> str:
    return json.dumps({"id": "req", "ok": True, "result": result})


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> RunResult:
    return RunResult(stdout=stdout, stderr=stderr, returncode=returncode)


class RecordingRunner:
    def __init__(self, responses: list[RunResult]):
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if not self._responses:
            raise AssertionError(f"no scripted response left for {argv!r}")
        return self._responses.pop(0)

    def clean_calls(self) -> list[list[str]]:
        out = []
        for argv in self.calls:
            if argv and argv[0] == "orca":
                argv = argv[1:]
                if argv and argv[-1] == "--json":
                    argv = argv[:-1]
            out.append(argv)
        return out


WORKSPACE = WorkspaceRef(ticket_key="GRE-201", id="wt-1", path="/workspaces/gre-201", branch="gre-201")
PLAN_COMMAND = (
    "claude --model fable --effort medium --permission-mode bypassPermissions "
    f"--session-id {FIXED_UUID}"
)


class SetPhase(unittest.TestCase):
    def test_argv_matches_spawn_badge(self):
        runner = RecordingRunner([_proc(_ok({}))])
        adapter = OrcaRuntimeAdapter(coordinator="orchestrator", run_id="run-1", runner=runner, bind_control=False)

        adapter.set_phase(WORKSPACE, RoleName.PLANNER)

        self.assertEqual(runner.clean_calls(), [[
            "worktree", "set", "--worktree", "id:wt-1",
            "--display-name", "🧭 GRE-201 · planning", "--workspace-status", "in-progress",
        ]])


class LaunchRole(unittest.TestCase):
    def test_argv_matches_spawn_launch(self):
        runner = RecordingRunner([
            _proc(_ok({"terminal": {"handle": "term-1"}})),
            _proc(_ok({})),
        ])
        adapter = OrcaRuntimeAdapter(coordinator="orchestrator", run_id="run-1", runner=runner, bind_control=False)
        spec = RoleSpec(role=RoleName.PLANNER, tier=CapabilityTier.STANDARD, access=Access.WRITE, briefing="brief")

        with mock.patch("adapters.orca.adapter.uuid.uuid4", return_value=FIXED_UUID):
            with mock.patch(
                "adapters.orca.adapter.build_launch",
                return_value=mock.Mock(command=PLAN_COMMAND, session_id=str(FIXED_UUID), transcript=None),
            ):
                result = adapter.launch_role(WORKSPACE, spec)

        self.assertEqual(runner.clean_calls(), [
            ["terminal", "create", "--worktree", "id:wt-1",
             "--title", "🧭 planning · GRE-201", "--command", PLAN_COMMAND],
            ["terminal", "wait", "--terminal", "term-1", "--for", "tui-idle", "--timeout-ms", "120000"],
        ])
        self.assertEqual(result.context.role, RoleName.PLANNER)


class Dispatch(unittest.TestCase):
    def _launched(self, runner: RecordingRunner) -> ContextRef:
        adapter = OrcaRuntimeAdapter(coordinator="orchestrator", run_id="run-1", runner=runner, bind_control=False)
        spec = RoleSpec(role=RoleName.PLANNER, tier=CapabilityTier.STANDARD, access=Access.WRITE, briefing="brief")
        with mock.patch(
            "adapters.orca.adapter.build_launch",
            return_value=mock.Mock(command=PLAN_COMMAND, session_id="s-1", transcript=None),
        ):
            result = adapter.launch_role(WORKSPACE, spec)
        return adapter, result.context

    def test_happy_path_argv(self):
        runner = RecordingRunner([
            _proc(_ok({"terminal": {"handle": "term-1"}})),
            _proc(_ok({})),
        ])
        adapter, context = self._launched(runner)
        runner._responses = [
            _proc(_ok({"task": {"id": "task-1"}})),
            _proc(_ok({
                "dispatch": {"id": "disp-1"},
                "preamble": "orca orchestration send --dispatch-capability dcap_ABC123 --type worker_done",
            })),
        ]

        ref = adapter.dispatch(context, "The Brief.")

        self.assertEqual(runner.clean_calls()[-2:], [
            ["orchestration", "task-create", "--spec", "The Brief."],
            ["orchestration", "dispatch", "--task", "task-1", "--to", "term-1",
             "--inject", "--return-preamble"],
        ])
        self.assertEqual(ref.attempt_id, "disp-1")
        snap = adapter.snapshot(ref)
        self.assertEqual(snap["capability"], "dcap_ABC123")
        self.assertEqual(snap["terminal"], "term-1")
        self.assertEqual(snap["task"], "task-1")
        self.assertEqual(snap["dispatch"], "disp-1")

    def test_missing_capability_rolls_back(self):
        runner = RecordingRunner([
            _proc(_ok({"terminal": {"handle": "term-1"}})),
            _proc(_ok({})),
        ])
        adapter, context = self._launched(runner)
        runner._responses = [
            _proc(_ok({"task": {"id": "task-1"}})),
            _proc(_ok({"dispatch": {"id": "disp-1"}, "preamble": "no capability token in this text"})),
            _proc(_ok({})),  # task-update --status failed
            _proc(_ok({})),  # terminal close
        ]

        with self.assertRaises(OrcaCliError) as ctx:
            adapter.dispatch(context, "The Brief.")
        self.assertIn("minted no capability", str(ctx.exception))

        self.assertEqual(runner.clean_calls()[-2:], [
            ["orchestration", "task-update", "--id", "task-1", "--status", "failed",
             "--result", json.dumps({"reason": "no dispatch capability in the preamble"})],
            ["terminal", "close", "--terminal", "term-1", "--tab"],
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
