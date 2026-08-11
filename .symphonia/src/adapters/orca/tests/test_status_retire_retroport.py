"""GRE-184 M4b: the adapter's cross-process control methods, held to the
same argv `test_spawn_characterization.py`'s `SpawnStatusAndRetire` freezes
for `spawn.status`/`spawn.retire`. Run either way:

    cd .symphonia/src && python3 -m unittest adapters.orca.tests.test_status_retire_retroport
    python3 .symphonia/src/adapters/orca/tests/test_status_retire_retroport.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from adapters.orca.adapter import OrcaCliError, OrcaRuntimeAdapter, RunResult


def _ok(result: dict) -> str:
    return json.dumps({"id": "req", "ok": True, "result": result})


def _err(code: str, message: str) -> str:
    return json.dumps({"id": "req", "ok": False, "error": {"code": code, "message": message}})


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


def _adapter(runner: RecordingRunner) -> OrcaRuntimeAdapter:
    return OrcaRuntimeAdapter(coordinator="spawn", run_id="spawn", runner=runner, bind_control=False)


class DispatchStatus(unittest.TestCase):
    def test_argv_and_return(self):
        runner = RecordingRunner([_proc(_ok({"dispatch": {"status": "dispatched"}}))])
        adapter = _adapter(runner)
        self.assertEqual(adapter.dispatch_status("task-9"), "dispatched")
        self.assertEqual(runner.clean_calls(), [["orchestration", "dispatch-show", "--task", "task-9"]])

    def test_a_missing_dispatch_row_reads_as_unknown_marker(self):
        runner = RecordingRunner([_proc(_ok({}))])
        adapter = _adapter(runner)
        self.assertEqual(adapter.dispatch_status("task-9"), "?")


class StopWorker(unittest.TestCase):
    def test_argv(self):
        runner = RecordingRunner([_proc(_ok({"stopped": True}))])
        adapter = _adapter(runner)
        adapter.stop_worker("disp-9")
        self.assertEqual(runner.clean_calls(), [["orchestration", "worker-stop", "--dispatch", "disp-9"]])

    def test_raises_on_a_dispatch_worker_start_never_created(self):
        runner = RecordingRunner([_proc(_err("dispatch_not_found", "no such worker"))])
        adapter = _adapter(runner)
        with self.assertRaises(OrcaCliError):
            adapter.stop_worker("disp-9")


class CloseTerminal(unittest.TestCase):
    def test_with_tab(self):
        runner = RecordingRunner([_proc(_ok({}))])
        adapter = _adapter(runner)
        adapter.close_terminal("term-9", tab=True)
        self.assertEqual(runner.clean_calls(), [["terminal", "close", "--terminal", "term-9", "--tab"]])

    def test_without_tab(self):
        runner = RecordingRunner([_proc(_ok({}))])
        adapter = _adapter(runner)
        adapter.close_terminal("term-9", tab=False)
        self.assertEqual(runner.clean_calls(), [["terminal", "close", "--terminal", "term-9"]])


class SettleTask(unittest.TestCase):
    def test_argv_matches_spawn_retire(self):
        runner = RecordingRunner([_proc(_ok({}))])
        adapter = _adapter(runner)
        adapter.settle_task("task-9", "planner retired by the Orchestrator")
        self.assertEqual(runner.clean_calls(), [[
            "orchestration", "task-update", "--id", "task-9", "--status", "failed",
            "--result", json.dumps({"reason": "planner retired by the Orchestrator"}),
        ]])


class ListTerminals(unittest.TestCase):
    """`sweep`'s only way to tell a role's pane is gone: no `--worktree`
    selector at all, unlike `_close_orphan_shell`'s call (GRE-189)."""

    def test_argv_and_return(self):
        runner = RecordingRunner([_proc(_ok({"terminals": [
            {"handle": "term-1", "title": "x", "command": "y"},
            {"handle": "term-2", "title": "", "command": ""},
        ]}))])
        adapter = _adapter(runner)
        self.assertEqual(adapter.list_terminals(), {"term-1", "term-2"})
        self.assertEqual(runner.clean_calls(), [["terminal", "list"]])

    def test_a_handle_less_row_is_dropped_not_stringified_as_none(self):
        runner = RecordingRunner([_proc(_ok({"terminals": [{"title": "x", "command": "y"}]}))])
        adapter = _adapter(runner)
        self.assertEqual(adapter.list_terminals(), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
