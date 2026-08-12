"""GRE-184 P6: item 10 of GRE-181 ("failure mid-sequence leaves no orphan
worktree/terminal/task"), for the one stage `test_spawn_characterization.py`
does not already characterize — an `open_context` failure (as opposed to the
dispatch-stage "no capability" rollback, which M0 already froze and which
`spawn()` does not duplicate, see spawn.py's `spawn()` docstring).

Reuses `test_spawn_characterization.CharacterizationCase` rather than
copying its setup — this file is new, not an edit to the frozen one. Run
either way:

    cd .symphonia/src && python3 -m unittest adapters.tests.test_spawn_rollback
    python3 .symphonia/src/adapters/tests/test_spawn_rollback.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import unittest

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

from adapters.tests.test_spawn_characterization import CharacterizationCase, _ok, _proc


class FreshWorktreeTornDownOnLaunchFailure(CharacterizationCase):
    """`spawn plan`: `worktree create` succeeds, `terminal create` (inside
    `open_context`) fails — the freshly created worktree must not survive."""

    def test_worktree_rm_follows_a_launch_failure(self):
        ticket, wt_id, path = "GRE-301", "wt-9", "/workspaces/gre-301"
        self.stub_linear_tracker(ticket)
        runner = self.install_runner([
            _proc(_ok({"worktrees": []})),                          # find_workspace
            _proc("main\n"),                                        # default_base
            _proc(_ok({"worktree": {"id": wt_id, "path": path}})),  # worktree create
            _proc(_ok({"terminals": []})),                          # orphan-shell list, none
            _proc(_ok({})),                                         # worktree set (badge)
            _proc("", returncode=1, stderr="boom"),                 # terminal create FAILS
            _proc(_ok({})),                                         # worktree rm (rollback)
        ])

        with self.assertRaises(SystemExit):
            self.spawn.spawn(self.RoleName.PLANNER, ticket, fresh_worktree=True)

        self.assertEqual(runner.clean_calls()[-1], ["worktree", "rm", "--worktree", f"path:{path}"])
        self.assertEqual(self.spawn.state_read(), {}, "no registry entry for a spawn that never dispatched")


class ReusedWorktreeSurvivesALaunchFailure(CharacterizationCase):
    """`spawn implement`: reuses an existing worktree; `terminal create`
    fails — the worktree that failure happened in must never be destroyed
    (Decision 5 of the approved plan: fresh-only rollback)."""

    def test_no_worktree_rm_is_ever_sent(self):
        ticket, wt_id, path = "GRE-301", "wt-9", "/workspaces/gre-301"
        runner = self.install_runner([
            _proc(_ok({"worktrees": [{"id": wt_id, "path": path}]})),  # find_workspace: reused
            _proc(_ok({})),                                            # worktree set (badge)
            _proc("", returncode=1, stderr="boom"),                    # terminal create FAILS
        ])

        with self.assertRaises(SystemExit):
            self.spawn.spawn(self.RoleName.IMPLEMENTER, ticket, fresh_worktree=False)

        self.assertNotIn("rm", [c[1] if len(c) > 1 else "" for c in runner.clean_calls() if c[:1] == ["worktree"]])
        self.assertEqual(self.spawn.state_read(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
