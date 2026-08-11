"""Tests for `bin/setup-worktree`.

TLDR: a real git repo plus a real linked worktree in a temp dir — the whole
point of the script is finding the main checkout from inside a worktree, and
a fake would be testing the mock. Run either way:

    cd .symphonia && python3 -m unittest adapters.tests.test_setup_worktree
    python3 .symphonia/adapters/tests/test_setup_worktree.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE))


def _load():
    path = PACKAGE / "bin" / "setup-worktree"
    loader = importlib.machinery.SourceFileLoader("setup_worktree_under_test", str(path))
    spec = importlib.util.spec_from_loader("setup_worktree_under_test", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


SETUP = _load()


def git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True)


class SetupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.main = root / "main"
        self.main.mkdir()
        git("init", "-q", "-b", "main", cwd=self.main)
        git("config", "user.email", "t@t", cwd=self.main)
        git("config", "user.name", "t", cwd=self.main)
        (self.main / "README.md").write_text("x\n")
        (self.main / ".gitignore").write_text(".env\n.env.local\n")
        git("add", "-A", cwd=self.main)
        git("commit", "-qm", "init", cwd=self.main)

        self.worktree = root / "ticket"
        # Redirect the shared fallback so a real ~/.symphonia/.env on the
        # machine running the suite cannot change the outcome.
        self.shared = root / "shared.env"
        SETUP.SHARED_ENV = self.shared
        self.addCleanup(setattr, SETUP, "SHARED_ENV", SETUP.SHARED_ENV)

    def add_worktree(self) -> Path:
        git("worktree", "add", "-q", "-b", "gre-1", str(self.worktree), cwd=self.main)
        return self.worktree


class TestCopies(SetupCase):
    def test_copies_the_env_files_a_worktree_never_gets(self):
        (self.main / ".env").write_text("LINEAR_API_KEY=lin_api_x\n")
        (self.main / ".env.local").write_text("LOCAL=1\n")
        result = SETUP.setup(self.add_worktree())
        self.assertEqual((self.worktree / ".env").read_text(), "LINEAR_API_KEY=lin_api_x\n")
        self.assertEqual((self.worktree / ".env.local").read_text(), "LOCAL=1\n")
        self.assertEqual(sorted(c["file"] for c in result["copied"]), [".env", ".env.local"])
        self.assertEqual(Path(result["source"]).resolve(), self.main.resolve())

    def test_the_copy_is_not_world_readable(self):
        (self.main / ".env").write_text("LINEAR_API_KEY=lin_api_x\n")
        SETUP.setup(self.add_worktree())
        self.assertEqual((self.worktree / ".env").stat().st_mode & 0o777, 0o600)

    def test_never_overwrites_what_is_already_there(self):
        (self.main / ".env").write_text("FROM=main\n")
        worktree = self.add_worktree()
        (worktree / ".env").write_text("FROM=worktree\n")
        result = SETUP.setup(worktree)
        self.assertEqual((worktree / ".env").read_text(), "FROM=worktree\n")
        self.assertIn(".env", result["kept"])
        self.assertEqual(result["copied"], [])

    def test_running_twice_changes_nothing(self):
        (self.main / ".env").write_text("K=1\n")
        worktree = self.add_worktree()
        SETUP.setup(worktree)
        second = SETUP.setup(worktree)
        self.assertEqual(second["copied"], [])
        self.assertEqual(second["kept"], [".env"])

    def test_a_missing_source_is_not_an_error(self):
        result = SETUP.setup(self.add_worktree())
        self.assertEqual(result["copied"], [])
        self.assertEqual(sorted(result["not_found"]), [".env", ".env.local"])

    def test_falls_back_to_the_shared_env(self):
        """A machine may keep its secrets outside every checkout, so the main
        checkout has no `.env` to copy."""

        self.shared.write_text("LINEAR_API_KEY=lin_api_shared\n")
        result = SETUP.setup(self.add_worktree())
        self.assertEqual((self.worktree / ".env").read_text(), "LINEAR_API_KEY=lin_api_shared\n")
        self.assertEqual(result["copied"][0]["from"], str(self.shared))

    def test_the_checkout_prefers_its_own_env_over_the_shared_one(self):
        (self.main / ".env").write_text("FROM=main\n")
        self.shared.write_text("FROM=shared\n")
        SETUP.setup(self.add_worktree())
        self.assertEqual((self.worktree / ".env").read_text(), "FROM=main\n")


class TestMainCheckout(SetupCase):
    def test_found_from_inside_a_linked_worktree(self):
        found = SETUP.main_checkout(self.add_worktree())
        self.assertEqual(found.resolve(), self.main.resolve())

    def test_the_main_checkout_is_not_its_own_source(self):
        """Run there and there is nothing to copy from — copying a file onto
        itself would truncate it."""

        self.assertIsNone(SETUP.main_checkout(self.main))

    def test_outside_a_repo_it_is_simply_unknown(self):
        loose = Path(self.tmp.name) / "loose"
        loose.mkdir()
        result = SETUP.setup(loose)
        self.assertIsNone(result["source"])
        self.assertEqual(result["copied"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
