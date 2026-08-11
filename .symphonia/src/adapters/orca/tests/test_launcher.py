"""Conformance tests for the launch interface (GRE-179).

TLDR: locks the three things that are silent when they break — the role
matrix drifting away from what the role files declare, a worker launched in a
mode that can stop at a permission prompt, and a read-only role launched
somewhere it could write. Run either way:

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
    ROLE_ACCESS,
    ROLE_TIERS,
    TIER_MODELS,
    build_launch,
    observed_models,
    tier_matches,
)
from adapters.runtime_adapter import Access, CapabilityTier, RoleName

ROLES_DIR = Path(__file__).resolve().parents[4] / "roles"


def declared_tier(role: RoleName) -> str:
    """The `capability_tier` a role file declares in its frontmatter."""

    text = (ROLES_DIR / f"{role.value}.md").read_text()
    for line in text.splitlines():
        if line.startswith("capability_tier:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{role.value}.md declares no capability_tier")


class TestMatrixAgreesWithRoleFiles(unittest.TestCase):
    """A role file and the launcher are two statements of the same decision.
    When they disagree, the role file is the one humans read and the launcher
    is the one that runs — so the drift is invisible until a ticket is
    planned by the wrong model."""

    def test_every_role_declares_the_tier_it_launches_at(self):
        for role in RoleName:
            with self.subTest(role=role.value):
                self.assertEqual(declared_tier(role), ROLE_TIERS[role].value)

    def test_every_tier_has_a_model(self):
        for tier in CapabilityTier:
            self.assertIn(tier, TIER_MODELS, f"{tier.value} would launch nothing")


class TestNothingLaunchesIntoAPrompt(unittest.TestCase):
    """The failure this whole interface exists to prevent: an agent that
    stalls on an approval prompt no one can see, because a permission prompt
    produces no orchestration message and no dispatch state change."""

    def test_every_role_launches_unattended(self):
        for role in RoleName:
            plan = build_launch(role, session_id="s", workspace="/tmp/w")
            with self.subTest(role=role.value):
                self.assertIn("bypassPermissions", plan.command)

    def test_every_provider_declares_an_unattended_mode(self):
        for name, grammar in PROVIDERS.items():
            with self.subTest(provider=name):
                self.assertTrue(grammar.unattended, f"{name} has no unattended flag")


class TestReadOnlyIsStructural(unittest.TestCase):
    def test_read_roles_cannot_reach_write_tools(self):
        for role, access in ROLE_ACCESS.items():
            plan = build_launch(role, session_id="s", workspace="/tmp/w")
            with self.subTest(role=role.value):
                if access is Access.READ:
                    self.assertIn("--disallowedTools", plan.command)
                    for tool in ("Edit", "Write", "NotebookEdit"):
                        self.assertIn(tool, plan.command)
                else:
                    self.assertNotIn("--disallowedTools", plan.command)

    def test_a_provider_that_cannot_deny_refuses_read_roles(self):
        """Better to fail the launch than to start a reviewer that can edit
        the code it is judging."""

        read_role = next(r for r, a in ROLE_ACCESS.items() if a is Access.READ)
        with self.assertRaises(ValueError):
            build_launch(read_role, session_id="s", workspace="/tmp/w", provider="codex")

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(ValueError):
            build_launch(RoleName.PLANNER, session_id="s", workspace="/tmp/w", provider="nope")


class TestTierIsObservable(unittest.TestCase):
    """Orca records the launch command, never the answering model. The
    transcript records the model on every answer, and pinning the session id
    at launch is what makes that file addressable."""

    def test_transcript_path_is_derived_from_the_session_id(self):
        plan = build_launch(RoleName.PLANNER, session_id="abc", workspace="/tmp/w")
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

        plan = build_launch(RoleName.SPEC_REVIEWER, session_id="s", workspace="/tmp/w")
        self.assertIn("'Edit Write NotebookEdit'", plan.command)


if __name__ == "__main__":
    unittest.main()
