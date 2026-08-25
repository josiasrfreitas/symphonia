"""Tests for `build_brief` in `src/spawn.py` — the part that needs no
tracker.

What is left here after the faked-tracker tests were removed: every role
has a `io:brief-template` block, and a missing placeholder value fails
loudly instead of shipping a Brief with a hole in it. What a Brief looks
like when a real ticket and real comments fill it is no longer covered
anywhere. Run either way:

    cd .symphonia/src && python3 -m unittest tests.test_brief
    python3 .symphonia/src/tests/test_brief.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import gate as GATE
import spawn as SPAWN

RoleName = SPAWN.RoleName












class TestEveryRoleHasABriefTemplate(unittest.TestCase):
    """The four spawnable roles each declare their own `io:brief-template`
    block — none of them fall back to a role with no template of its own."""

    def test_every_policy_role_file_has_a_brief_template(self):
        for role, policy in SPAWN._policies().items():
            with self.subTest(role=role.value):
                role_path = SPAWN.ROLES_DIR / policy.role_file
                try:
                    GATE.extract_block(role_path.read_text(), "md io:brief-template")
                except LookupError:
                    self.fail(f"{role_path} has no 'md io:brief-template' block")


class TestExtractBlockFailureIsLoud(unittest.TestCase):
    def test_role_file_with_no_brief_template_raises(self):
        with self.assertRaises(LookupError):
            GATE.extract_block("# No I/O section here.", "md io:brief-template")


if __name__ == "__main__":
    unittest.main(verbosity=2)
