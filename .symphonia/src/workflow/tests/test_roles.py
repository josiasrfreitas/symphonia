"""Tests for `workflow.roles.load_policies` — the frontmatter-only role
policy loader that replaces `ROLE_TIERS`/`ROLE_ACCESS`/`ROLE_FILES`
(GRE-186 S1). Run either way:

    cd .symphonia/src && python3 -m unittest workflow.tests.test_roles
    python3 .symphonia/src/workflow/tests/test_roles.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia/src
sys.path.insert(0, str(PACKAGE))

from adapters.runtime_adapter import Access, CapabilityTier, RoleName
from workflow.roles import load_policies

ROLES_DIR = PACKAGE.parent / "roles"

# The matrix decided on GRE-179, now declared in each role file's own
# frontmatter instead of a second table here.
DECLARED = {
    RoleName.PLANNER: ("frontier", "write"),
    RoleName.IMPLEMENTER: ("standard", "write"),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
