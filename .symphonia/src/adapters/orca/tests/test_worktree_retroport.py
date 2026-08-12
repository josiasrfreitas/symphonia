"""GRE-184 M2: the adapter's worktree argv, held to the same measured shape
`test_spawn_characterization.py` freezes for `spawn.create_worktree` /
`spawn.find_worktree` / `spawn.default_base`. Run either way:

    cd .symphonia/src && python3 -m unittest adapters.orca.tests.test_worktree_retroport
    python3 .symphonia/src/adapters/orca/tests/test_worktree_retroport.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from adapters.orca.adapter import OrcaRuntimeAdapter, RunResult


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


class CreateWorkspace(unittest.TestCase):
    """Same argv `spawn.create_worktree` sends (test_spawn_characterization,
    SpawnPlanHappyPath), minus the `setup_worktree.setup` call, which is a
    separate subprocess boundary the caller owns (Decision, PR-B plan P1)."""

    def test_argv_matches_spawn_create_worktree(self):
        ticket, wt_id, path = "GRE-201", "wt-1", "/workspaces/gre-201"
        runner = RecordingRunner([
            _proc(_ok({"worktree": {"id": wt_id, "path": path}})),
            _proc(_ok({"terminals": [{"handle": "term-orphan", "title": "", "command": ""}]})),
            _proc(_ok({})),
        ])
        adapter = OrcaRuntimeAdapter(
            coordinator="orchestrator", run_id="run-1", runner=runner, bind_control=False,
        )

        ws = adapter.create_workspace(ticket, base_branch="main")

        self.assertEqual(runner.clean_calls(), [
            ["worktree", "create", "--name", "gre-201", "--parent-worktree", "active",
             "--base-branch", "main", "--setup", "run", "--linear-issue", ticket,
             "--comment", f"symphonia ticket {ticket}"],
            ["terminal", "list", "--worktree", f"id:{wt_id}"],
            ["terminal", "close", "--terminal", "term-orphan", "--tab"],
        ])
        self.assertEqual(ws.id, wt_id)
        self.assertEqual(ws.path, path)

    def test_titled_terminal_is_not_closed(self):
        """Only a blank title/command reads as the orphan fallback shell —
        a role's own terminal must never be swept up by this."""

        ticket, wt_id, path = "GRE-201", "wt-1", "/workspaces/gre-201"
        runner = RecordingRunner([
            _proc(_ok({"worktree": {"id": wt_id, "path": path}})),
            _proc(_ok({"terminals": [{"handle": "term-role", "title": "🧭 planning", "command": "claude"}]})),
        ])
        adapter = OrcaRuntimeAdapter(
            coordinator="orchestrator", run_id="run-1", runner=runner, bind_control=False,
        )

        adapter.create_workspace(ticket, base_branch="main")

        self.assertEqual(len(runner.clean_calls()), 2)  # no close call issued


class FindWorkspace(unittest.TestCase):
    """Same argv as `spawn.find_worktree`; match rule is GRE-187's own —
    exact `Path(path).name` wins, else exactly one `<ticket>-<digits>`
    suffix match, else anomaly."""

    def test_matches_by_path_basename(self):
        runner = RecordingRunner([
            _proc(_ok({"worktrees": [
                {"id": "wt-9", "path": "/workspaces/gre-1", "display_name": "🔨 GRE-1 · implementing"},
            ]})),
        ])
        adapter = OrcaRuntimeAdapter(
            coordinator="orchestrator", run_id="run-1", runner=runner, bind_control=False,
        )

        ws = adapter.find_workspace("GRE-1")

        self.assertEqual(runner.clean_calls(), [["worktree", "list"]])
        self.assertEqual(ws.id, "wt-9")
        self.assertEqual(ws.path, "/workspaces/gre-1")

    def test_no_match_is_none(self):
        runner = RecordingRunner([_proc(_ok({"worktrees": []}))])
        adapter = OrcaRuntimeAdapter(
            coordinator="orchestrator", run_id="run-1", runner=runner, bind_control=False,
        )
        self.assertIsNone(adapter.find_workspace("GRE-1"))

    def test_exact_name_wins_over_suffixed(self):
        runner = RecordingRunner([
            _proc(_ok({"worktrees": [
                {"id": "wt-old", "path": "/workspaces/gre-187-2"},
                {"id": "wt-exact", "path": "/workspaces/gre-187"},
            ]})),
        ])
        adapter = OrcaRuntimeAdapter(
            coordinator="orchestrator", run_id="run-1", runner=runner, bind_control=False,
        )

        ws = adapter.find_workspace("GRE-187")

        self.assertEqual(ws.id, "wt-exact")
        self.assertEqual(ws.path, "/workspaces/gre-187")

    def test_lone_suffixed_worktree_is_found(self):
        runner = RecordingRunner([
            _proc(_ok({"worktrees": [
                {"id": "wt-2", "path": "/workspaces/gre-187-2"},
            ]})),
        ])
        adapter = OrcaRuntimeAdapter(
            coordinator="orchestrator", run_id="run-1", runner=runner, bind_control=False,
        )

        ws = adapter.find_workspace("GRE-187")

        self.assertEqual(ws.id, "wt-2")
        self.assertEqual(ws.path, "/workspaces/gre-187-2")

    def test_non_digit_suffix_is_not_confused(self):
        runner = RecordingRunner([
            _proc(_ok({"worktrees": [
                {"id": "wt-a", "path": "/workspaces/gre-1870"},
                {"id": "wt-b", "path": "/workspaces/gre-187-foo"},
            ]})),
        ])
        adapter = OrcaRuntimeAdapter(
            coordinator="orchestrator", run_id="run-1", runner=runner, bind_control=False,
        )

        self.assertIsNone(adapter.find_workspace("GRE-187"))

    def test_two_suffixed_and_no_exact_raises_listing_both(self):
        runner = RecordingRunner([
            _proc(_ok({"worktrees": [
                {"id": "wt-2", "path": "/workspaces/gre-187-2"},
                {"id": "wt-3", "path": "/workspaces/gre-187-3"},
            ]})),
        ])
        adapter = OrcaRuntimeAdapter(
            coordinator="orchestrator", run_id="run-1", runner=runner, bind_control=False,
        )

        with self.assertRaises(RuntimeError) as ctx:
            adapter.find_workspace("GRE-187")

        self.assertIn("/workspaces/gre-187-2", str(ctx.exception))
        self.assertIn("/workspaces/gre-187-3", str(ctx.exception))


class DefaultBase(unittest.TestCase):
    """A git call, not an orca one — never goes through the injected Runner
    (matches `spawn.default_base` unedited)."""

    def test_reads_origin_head(self):
        import subprocess as _subprocess

        adapter = OrcaRuntimeAdapter(coordinator="orchestrator", run_id="run-1")
        with mock.patch.object(_subprocess, "run", return_value=_proc_ns("main\n")) as run:
            self.assertEqual(adapter.default_base(), "main")
        run.assert_called_once_with(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True,
        )

    def test_raises_without_origin_head(self):
        import subprocess as _subprocess

        adapter = OrcaRuntimeAdapter(coordinator="orchestrator", run_id="run-1")
        with mock.patch.object(_subprocess, "run", return_value=_proc_ns("")):
            with self.assertRaisesRegex(RuntimeError, "cannot resolve origin/HEAD"):
                adapter.default_base()


def _proc_ns(stdout: str):
    from types import SimpleNamespace
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
