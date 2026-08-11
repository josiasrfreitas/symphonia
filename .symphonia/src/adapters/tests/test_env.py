"""Tests for the `.env` loader (GRE-178).

TLDR: the shell wins over the file, the search order is explicit, and the
parser handles exactly what it promises and nothing more. The last case is
the one that matters operationally: a role runs in the ticket's worktree, a
different checkout, so only the shared `.env` reaches it. Run either way:

    cd .symphonia/src && python3 -m unittest adapters.tests.test_env
    python3 .symphonia/src/adapters/tests/test_env.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src, the sys.path root since the move
sys.path.insert(0, str(PACKAGE))

from adapters import env as ENV


class TestParse(unittest.TestCase):
    def test_reads_assignments_and_skips_the_rest(self):
        parsed = ENV.parse(
            "# a comment\n"
            "\n"
            "LINEAR_API_KEY=lin_api_123\n"
            "export SYMPHONIA_RUNTIME=/tmp/r\n"
            "QUOTED=\"with spaces\"\n"
            "SINGLE='also fine'\n"
            "not an assignment\n"
        )
        self.assertEqual(parsed, {
            "LINEAR_API_KEY": "lin_api_123",
            "SYMPHONIA_RUNTIME": "/tmp/r",
            "QUOTED": "with spaces",
            "SINGLE": "also fine",
        })

    def test_only_one_layer_of_quotes_is_stripped(self):
        self.assertEqual(ENV.parse("K=\"'x'\"")["K"], "'x'")

    def test_an_inner_equals_stays_in_the_value(self):
        self.assertEqual(ENV.parse("K=a=b")["K"], "a=b")


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "PROBE_KEY", None)
        os.environ.pop("PROBE_KEY", None)

    def write(self, text: str, name: str = ".env") -> Path:
        path = Path(self.tmp.name) / name
        path.write_text(text)
        return path

    def test_fills_a_missing_variable(self):
        applied = ENV.load(self.write("PROBE_KEY=from_file"))
        self.assertEqual(applied, {"PROBE_KEY": "from_file"})
        self.assertEqual(os.environ["PROBE_KEY"], "from_file")

    def test_the_shell_wins_over_the_file(self):
        """An export on the command line must keep beating a file, or
        debugging which value is live becomes guesswork."""

        os.environ["PROBE_KEY"] = "from_shell"
        applied = ENV.load(self.write("PROBE_KEY=from_file"))
        self.assertEqual(applied, {})
        self.assertEqual(os.environ["PROBE_KEY"], "from_shell")

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(ENV.load(Path(self.tmp.name) / "absent.env"), {})

    def test_an_explicit_path_is_searched_first(self):
        explicit = self.write("PROBE_KEY=explicit", name="custom.env")
        os.environ["SYMPHONIA_ENV"] = str(explicit)
        self.addCleanup(os.environ.pop, "SYMPHONIA_ENV", None)
        self.assertEqual(ENV.candidates()[0], explicit)

    def test_the_shared_env_is_searched_before_the_repo_one(self):
        """The shared file is the only one a role can read: it runs in the
        ticket's worktree, which is a different checkout with no .env."""

        order = ENV.candidates()
        self.assertEqual(order[0], ENV.SHARED_ENV)
        self.assertEqual(order[1], ENV.REPO / ".env")

    def test_the_first_existing_file_wins(self):
        first = self.write("PROBE_KEY=first", name="first.env")
        self.write("PROBE_KEY=second", name="second.env")
        os.environ["SYMPHONIA_ENV"] = str(first)
        self.addCleanup(os.environ.pop, "SYMPHONIA_ENV", None)
        ENV.load()
        self.assertEqual(os.environ["PROBE_KEY"], "first")


if __name__ == "__main__":
    unittest.main(verbosity=2)
