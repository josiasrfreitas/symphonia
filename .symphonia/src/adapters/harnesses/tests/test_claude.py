"""Conformance tests for the launch interface (GRE-179).

TLDR: locks the two things that are silent when they break — a worker
launched in a mode that can stop at a permission prompt, and a read-only
role launched somewhere it could write. What tier/access a role launches at
is no longer a second table here to drift from the role files (GRE-186 S1):
these tests read the same `RolePolicy` catalog `spawn.py` does, via
`workflow.roles.load_policies`, and pass it to `build_launch` explicitly.
Run either way:

    cd .symphonia/src && python3 -m unittest adapters.orca.tests.test_launcher
    python3 .symphonia/src/adapters/orca/tests/test_launcher.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from adapters.orca.launcher import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    build_launch,
    observed_models,
    tier_matches,
)
from adapters.runtime_adapter import Access, CapabilityTier, RoleName
from workflow.roles import load_policies

ROLES_DIR = Path(__file__).resolve().parents[4] / "roles"
POLICIES = load_policies(ROLES_DIR)


class TestNothingLaunchesIntoAPrompt(unittest.TestCase):
    """The failure this whole interface exists to prevent: an agent that
    stalls on an approval prompt no one can see, because a permission prompt
    produces no orchestration message and no dispatch state change."""

    def test_every_role_launches_unattended(self):
        for role, policy in POLICIES.items():
            plan = build_launch(
                role, session_id="s", workspace="/tmp/w",
                tier=policy.tier, access=policy.access,
            )
            with self.subTest(role=role.value):
                self.assertIn("bypassPermissions", plan.command)

    def test_every_provider_declares_an_unattended_mode(self):
        for name, grammar in PROVIDERS.items():
            with self.subTest(provider=name):
                self.assertTrue(grammar.unattended, f"{name} has no unattended flag")


class TestReadOnlyIsStructural(unittest.TestCase):
    def test_read_roles_cannot_reach_write_tools(self):
        for role, policy in POLICIES.items():
            plan = build_launch(
                role, session_id="s", workspace="/tmp/w",
                tier=policy.tier, access=policy.access,
            )
            with self.subTest(role=role.value):
                if policy.access is Access.READ:
                    self.assertIn("--disallowedTools", plan.command)
                    for tool in ("Edit", "Write", "NotebookEdit"):
                        self.assertIn(tool, plan.command)
                else:
                    self.assertNotIn("--disallowedTools", plan.command)

    def test_a_provider_that_cannot_deny_refuses_read_roles(self):
        """Better to fail the launch than to start a reviewer that can edit
        the code it is judging."""

        read_role = next(r for r, p in POLICIES.items() if p.access is Access.READ)
        with self.assertRaises(ValueError):
            build_launch(
                read_role, session_id="s", workspace="/tmp/w",
                tier=POLICIES[read_role].tier, access=Access.READ, provider="codex",
            )

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(ValueError):
            build_launch(
                RoleName.PLANNER, session_id="s", workspace="/tmp/w",
                tier=POLICIES[RoleName.PLANNER].tier,
                access=POLICIES[RoleName.PLANNER].access,
                provider="nope",
            )


class TestTierIsObservable(unittest.TestCase):
    """Orca records the launch command, never the answering model. The
    transcript records the model on every answer, and pinning the session id
    at launch is what makes that file addressable."""

    def test_transcript_path_is_derived_from_the_session_id(self):
        policy = POLICIES[RoleName.PLANNER]
        plan = build_launch(
            RoleName.PLANNER, session_id="abc", workspace="/tmp/w",
            tier=policy.tier, access=policy.access,
        )
        self.assertTrue(str(plan.transcript).endswith("abc.jsonl"))

    def test_models_are_read_from_the_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(
                json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
                + json.dumps({"message": {"model": "claude-sonnet-5"}}) + "\n"
                + "{not json at all}\n"
                + json.dumps({"message": {"model": "claude-sonnet-5"}}) + "\n"
            )
            self.assertEqual(observed_models(path), ["claude-sonnet-5"])
            self.assertTrue(tier_matches(CapabilityTier.STANDARD, ["claude-sonnet-5"]))
            self.assertFalse(tier_matches(CapabilityTier.HIGH, ["claude-sonnet-5"]))

    def test_a_missing_transcript_is_not_a_wrong_tier(self):
        self.assertEqual(observed_models(Path("/nonexistent/none.jsonl")), [])
        self.assertFalse(tier_matches(CapabilityTier.HIGH, []))


class TestArgvIsShellSafe(unittest.TestCase):
    def test_multiword_values_survive_the_command_string(self):
        """`orca terminal create --command` takes one string, so a value with
        spaces must not split into two arguments."""

        policy = POLICIES[RoleName.SPEC_REVIEWER]
        plan = build_launch(
            RoleName.SPEC_REVIEWER, session_id="s", workspace="/tmp/w",
            tier=policy.tier, access=policy.access,
        )
        self.assertIn("'Edit Write NotebookEdit'", plan.command)


if __name__ == "__main__":
    unittest.main()
