"""Tests for `roles.load_policies` — the frontmatter-only role
policy loader that replaces `ROLE_TIERS`/`ROLE_ACCESS`/`ROLE_FILES`
(GRE-186 S1). Run either way:

    cd .symphonia/src && python3 -m unittest tests.test_roles
    python3 .symphonia/src/tests/test_roles.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

from roles import Access, CapabilityTier, RoleName
from roles import load_policies

ROLES_DIR = PACKAGE.parent / "roles"

# The matrix declared in each role file's own frontmatter, not in a second
# table here. Every role stands on `high` (opus) by the user's instruction of
# 2026-08-24, which replaces the spread decided on GRE-179: the planner and
# the standards reviewer came down from `frontier`, the implementer came up
# from `standard`. It also retires the 2026-08-12 rule that the two reviewers
# sit on different rungs — they now read the same diff with the same model,
# and the second perspective comes from the brief each one carries, not from
# a second model. The tier ladder itself never moves — only which rung a role
# stands on.
DECLARED = {
    RoleName.PLANNER: ("high", "write"),
    RoleName.IMPLEMENTER: ("high", "write"),
    RoleName.SPEC_REVIEWER: ("high", "read"),
    RoleName.STANDARDS_REVIEWER: ("high", "read"),
}


def _write_role(
    dirpath: Path, role: RoleName, *, role_name: str | None = None,
    tier: str = "standard", access: str = "write",
) -> None:
    role_name = role.value if role_name is None else role_name
    (dirpath / f"{role.value}.md").write_text(
        f"---\nrole: {role_name}\ncapability_tier: {tier}\naccess: {access}\n---\n\n# {role.value}\n"
    )


class TestLoadPoliciesHappyPath(unittest.TestCase):
    def test_loads_the_real_role_files(self):
        policies = load_policies(ROLES_DIR)
        for role, (tier, access) in DECLARED.items():
            with self.subTest(role=role.value):
                policy = policies[role]
                self.assertEqual(policy.role, role)
                self.assertEqual(policy.tier, CapabilityTier(tier))
                self.assertEqual(policy.access, Access(access))
                self.assertEqual(policy.role_file, f"{role.value}.md")


class TestLoadPoliciesFailsLoudly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dirpath = Path(self.tmp.name)
        for role, (tier, access) in DECLARED.items():
            _write_role(self.dirpath, role, tier=tier, access=access)

    def test_missing_access_fails(self):
        (self.dirpath / "implementer.md").write_text(
            "---\nrole: implementer\ncapability_tier: standard\n---\n\n# Implementer\n"
        )
        with self.assertRaises(SystemExit) as ctx:
            load_policies(self.dirpath)
        self.assertIn("access", str(ctx.exception))

    def test_invalid_tier_fails(self):
        _write_role(self.dirpath, RoleName.PLANNER, tier="legendary", access="write")
        with self.assertRaises(SystemExit) as ctx:
            load_policies(self.dirpath)
        self.assertIn("capability_tier", str(ctx.exception))

    def test_missing_file_fails(self):
        (self.dirpath / "planner.md").unlink()
        with self.assertRaises(SystemExit) as ctx:
            load_policies(self.dirpath)
        self.assertIn("planner.md", str(ctx.exception))

    def test_role_name_mismatch_fails(self):
        _write_role(self.dirpath, RoleName.PLANNER, role_name="implementer", tier="frontier", access="write")
        with self.assertRaises(SystemExit) as ctx:
            load_policies(self.dirpath)
        self.assertIn("role=", str(ctx.exception))

    def test_duplicate_key_fails(self):
        """F1: a repeated key inside the fences must not silently let the
        last occurrence win."""

        (self.dirpath / "spec-reviewer.md").write_text(
            "---\nrole: spec-reviewer\ncapability_tier: high\naccess: read\n"
            "access: write\n---\n\n# Spec Reviewer\n"
        )
        with self.assertRaises(SystemExit) as ctx:
            load_policies(self.dirpath)
        self.assertIn("access", str(ctx.exception))
        self.assertIn("spec-reviewer.md", str(ctx.exception))

    def test_non_empty_line_without_colon_fails(self):
        (self.dirpath / "spec-reviewer.md").write_text(
            "---\nrole: spec-reviewer\ncapability_tier: high\naccess: read\n"
            "not a key value line\n---\n\n# Spec Reviewer\n"
        )
        with self.assertRaises(SystemExit) as ctx:
            load_policies(self.dirpath)
        self.assertIn("spec-reviewer.md", str(ctx.exception))

    def test_unresolved_merge_conflict_fails_instead_of_loading_theirs(self):
        """The real-world shape of F1: a `spec-reviewer.md` left with an
        unresolved merge conflict must not hand `spawn` `access: write` in
        silence — it must fail, naming the file."""

        (self.dirpath / "spec-reviewer.md").write_text(
            "---\nrole: spec-reviewer\ncapability_tier: high\n"
            "<<<<<<< HEAD\naccess: read\n=======\naccess: write\n>>>>>>> other\n"
            "---\n\n# Spec Reviewer\n"
        )
        with self.assertRaises(SystemExit) as ctx:
            load_policies(self.dirpath)
        self.assertIn("spec-reviewer.md", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
