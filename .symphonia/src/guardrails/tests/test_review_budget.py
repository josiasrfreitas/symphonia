"""Tests for `src/guardrails/review_budget.py` (GRE-177).

TLDR: a real git repo in a temp dir for every counting rule GRE-156 closed —
no mocked `git`, since the whole point is what `numstat -z`/`check-attr -z`
actually emit. Run either way:

    cd .symphonia/src && python3 -m unittest guardrails.tests.test_review_budget
    python3 .symphonia/src/guardrails/tests/test_review_budget.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

import guardrails.review_budget as RB


def git(*argv: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def run(argv: list[str]) -> tuple[int, dict | None]:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = RB.main(argv)
    except SystemExit as exc:
        code = exc.code
    out = buf.getvalue()
    return code, (json.loads(out) if out.strip() else None)


class BudgetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("config", "user.email", "t@t", cwd=self.repo)
        git("config", "user.name", "t", cwd=self.repo)

    def commit(self, message: str) -> str:
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", message, "--allow-empty", cwd=self.repo)
        return git("rev-parse", "HEAD", cwd=self.repo)

    def args(self, base: str, **extra) -> list[str]:
        out = ["--base", base, "--repo", str(self.repo), "--budget", str(extra.pop("budget", 1000))]
        for key, value in extra.items():
            out += [f"--{key}", str(value)]
        return out


class TestCounting(BudgetCase):
    def test_simple_addition_counts_added_lines(self):
        (self.repo / "a.txt").write_text("a\nb\n")
        base = self.commit("init")
        (self.repo / "a.txt").write_text("a\nb\nc\n")
        self.commit("change")
        code, out = run(self.args(base))
        self.assertEqual(code, 0)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["files"], [{"path": "a.txt", "added": 1, "deleted": 0}])
        self.assertEqual(out["verdict"], "within")

    def test_whitespace_only_change_counts_without_dash_w(self):
        (self.repo / "a.txt").write_text("a\nb\n")
        base = self.commit("init")
        (self.repo / "a.txt").write_text("a\nb \n")
        self.commit("trailing space")
        code, out = run(self.args(base))
        self.assertEqual(out["total"], 2)

    def test_intact_rename_counts_zero(self):
        (self.repo / "a.txt").write_text("a\nb\nc\n")
        base = self.commit("init")
        git("mv", "a.txt", "b.txt", cwd=self.repo)
        self.commit("rename")
        code, out = run(self.args(base))
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["files"], [{"path": "b.txt", "added": 0, "deleted": 0}])

    def test_rename_with_edit_counts_only_the_edit(self):
        (self.repo / "a.txt").write_text("a\nb\nc\n")
        base = self.commit("init")
        git("mv", "a.txt", "b.txt", cwd=self.repo)
        (self.repo / "b.txt").write_text("a\nb\nc\nd\n")
        self.commit("rename and edit")
        code, out = run(self.args(base))
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["files"], [{"path": "b.txt", "added": 1, "deleted": 0}])

    def test_binary_counts_zero_but_is_listed(self):
        base = self.commit("init")
        (self.repo / "bin.dat").write_bytes(b"\x00\x01\x02\x03")
        self.commit("add binary")
        code, out = run(self.args(base))
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["binaries"], ["bin.dat"])
        self.assertEqual(out["files"], [])

    def test_overflow_yields_exit_1_and_verdict_over(self):
        (self.repo / "a.txt").write_text("a\n")
        base = self.commit("init")
        (self.repo / "a.txt").write_text("a\nb\n")
        self.commit("change")
        code, out = run(self.args(base, budget=0))
        self.assertEqual(code, 1)
        self.assertEqual(out["verdict"], "over")


class TestAttributeExclusions(BudgetCase):
    def test_linguist_generated_is_excluded_and_listed(self):
        (self.repo / ".gitattributes").write_text("gen.txt linguist-generated\n")
        (self.repo / "gen.txt").write_text("a\n")
        base = self.commit("init")
        (self.repo / "gen.txt").write_text("a\nb\n")
        self.commit("change generated file")
        code, out = run(self.args(base))
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["excluded_generated"], ["gen.txt"])
        self.assertEqual(out["files"], [])

    def test_budget_exempt_is_excluded_and_listed(self):
        (self.repo / ".gitattributes").write_text("mutants.toml symphonia-budget-exempt\n")
        (self.repo / "mutants.toml").write_text("a\n")
        base = self.commit("init")
        (self.repo / "mutants.toml").write_text("a\nb\n")
        self.commit("change exempt file")
        code, out = run(self.args(base))
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["excluded_exempt"], ["mutants.toml"])

    def test_unset_attribute_does_not_exclude(self):
        (self.repo / ".gitattributes").write_text("a.txt -linguist-generated\n")
        (self.repo / "a.txt").write_text("a\n")
        base = self.commit("init")
        (self.repo / "a.txt").write_text("a\nb\n")
        self.commit("change")
        code, out = run(self.args(base))
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["excluded_generated"], [])


class TestBaseResolution(BudgetCase):
    def setUp(self):
        super().setUp()
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)
        os.environ["SYMPHONIA_RUNTIME"] = self.runtime.name
        self.addCleanup(os.environ.pop, "SYMPHONIA_RUNTIME", None)

    def write_spawns(self, records: dict) -> None:
        Path(self.runtime.name, "spawns.json").write_text(json.dumps(records))

    def test_ticket_reads_head_first_dispatch(self):
        (self.repo / "a.txt").write_text("a\n")
        base = self.commit("init")
        (self.repo / "a.txt").write_text("a\nb\n")
        self.commit("change")
        self.write_spawns({"GRE-1/implementer": {"head_first_dispatch": base}})
        code, out = run(["--ticket", "GRE-1", "--repo", str(self.repo), "--budget", "1000"])
        self.assertEqual(out["base"], base)
        self.assertEqual(out["total"], 1)

    def test_ticket_without_head_first_dispatch_exits_2_never_falling_back(self):
        (self.repo / "a.txt").write_text("a\n")
        base = self.commit("init")
        (self.repo / "a.txt").write_text("a\nb\n")
        self.commit("change")
        # head_at_dispatch is present but must never be used as a fallback.
        self.write_spawns({"GRE-2/implementer": {"head_at_dispatch": base}})
        code, out = run(["--ticket", "GRE-2", "--repo", str(self.repo), "--budget", "1000"])
        self.assertEqual(code, 2)
        self.assertIsNone(out)

    def test_unknown_ticket_exits_2(self):
        self.write_spawns({})
        code, out = run(["--ticket", "GRE-9", "--repo", str(self.repo), "--budget", "1000"])
        self.assertEqual(code, 2)

    def test_explicit_base_wins_over_ticket(self):
        (self.repo / "a.txt").write_text("a\n")
        base = self.commit("init")
        (self.repo / "a.txt").write_text("a\nb\n")
        self.commit("change")
        self.write_spawns({"GRE-3/implementer": {"head_first_dispatch": "deadbeef"}})
        code, out = run(["--base", base, "--ticket", "GRE-3", "--repo", str(self.repo), "--budget", "1000"])
        self.assertEqual(out["base"], base)


if __name__ == "__main__":
    unittest.main()
