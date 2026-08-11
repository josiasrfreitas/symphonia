"""`ScriptedOrcaCli`'s handling of `terminal list` with no `--worktree`
selector (GRE-189).

TLDR: `terminal list --worktree id:...` was already taught to the fake;
`sweep` (`adapter.list_terminals`) calls it with no selector at all, which
the fake did not know how to answer until now. Verified against the real
CLI (2026-08-11): with no `--worktree`, it answers with every live
terminal, not just one workspace's — without this, `sweep`'s test would be
exercising a shape the real Orca CLI never produces. Run either way:

    cd .symphonia/src && python3 -m unittest adapters.orca.tests.test_fake_terminal_list
    python3 .symphonia/src/adapters/orca/tests/test_fake_terminal_list.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from adapters.orca.fake import FakeRuntime, ScriptedOrcaCli


class TerminalListWithoutASelector(unittest.TestCase):
    def setUp(self):
        self.rt = FakeRuntime()
        self.cli = ScriptedOrcaCli(self.rt)

    def _list(self, *argv) -> list[dict]:
        out = self.cli(["orca", "terminal", "list", *argv, "--json"])
        return json.loads(out.stdout)["result"]["terminals"]

    def test_every_live_terminal_across_every_workspace_comes_back(self):
        # `create_workspace` opens one stray fallback shell per workspace.
        ws1 = self.rt.create_workspace("GRE-1")
        ws2 = self.rt.create_workspace("GRE-2")
        (shell1,) = [p.id for p in self.rt.live_in(ws1)]
        (shell2,) = [p.id for p in self.rt.live_in(ws2)]

        handles = {t["handle"] for t in self._list()}
        self.assertEqual(handles, {shell1, shell2})

    def test_a_dead_terminal_is_excluded(self):
        ws1 = self.rt.create_workspace("GRE-1")
        (shell1,) = [p.id for p in self.rt.live_in(ws1)]
        self.rt.kill_process(shell1)

        self.assertEqual(self._list(), [])

    def test_the_worktree_selector_still_filters_to_one_workspace(self):
        ws1 = self.rt.create_workspace("GRE-1")
        self.rt.create_workspace("GRE-2")
        (shell1,) = [p.id for p in self.rt.live_in(ws1)]

        handles = {t["handle"] for t in self._list("--worktree", f"id:{ws1}")}
        self.assertEqual(handles, {shell1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
