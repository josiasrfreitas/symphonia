"""Tests for `claude` — the launch interface.

TLDR: locks the two things that are silent when they break — a worker
launched in a mode that can stop at a permission prompt, and a read-only
role launched somewhere it could write — plus the transcript-based tier
evidence. Run either way:

    cd .symphonia/src && python3 -m unittest tests.test_claude
    python3 .symphonia/src/tests/test_claude.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claude as CLAUDE
from orca import shell_join
from roles import Access, CapabilityTier, RoleName, load_policies

ROLES_DIR = Path(__file__).resolve().parents[2] / "roles"
POLICIES = load_policies(ROLES_DIR)

FIXED_UUID = "a7c8e5f0-0000-4000-8000-000000000000"


def _prepare(role: RoleName) -> CLAUDE.Launch:
    with mock.patch.object(CLAUDE.uuid, "uuid4", return_value=FIXED_UUID):
        return CLAUDE.prepare(workspace="/tmp/w", policy=POLICIES[role])


class TestNothingLaunchesIntoAPrompt(unittest.TestCase):
    """The failure this whole interface exists to prevent: an agent that
    stalls on an approval prompt no one can see, because a permission
    prompt produces no orchestration message and no dispatch state
    change."""

    def test_every_role_launches_unattended(self):
        for role in RoleName:
            with self.subTest(role=role.value):
                launch = _prepare(role)
                self.assertIn("bypassPermissions", launch.command)


class TestArgvIsDecidedByThePolicy(unittest.TestCase):
    def test_planner_argv(self):
        self.assertEqual(
            _prepare(RoleName.PLANNER).command,
            ("claude", "--model", "fable", "--effort", "medium",
             "--permission-mode", "bypassPermissions", "--session-id", FIXED_UUID),
        )

    def test_implementer_argv(self):
        self.assertEqual(
            _prepare(RoleName.IMPLEMENTER).command,
            ("claude", "--model", "sonnet", "--effort", "high",
             "--permission-mode", "bypassPermissions", "--session-id", FIXED_UUID),
        )


class TestReadOnlyIsStructural(unittest.TestCase):
    def test_read_roles_cannot_reach_write_tools(self):
        for role, policy in POLICIES.items():
            with self.subTest(role=role.value):
                launch = _prepare(role)
                if policy.access is Access.READ:
                    self.assertIn("--disallowedTools", launch.command)
                    self.assertIn("Edit Write NotebookEdit", launch.command)
                else:
                    self.assertNotIn("--disallowedTools", launch.command)

    def test_multiword_values_survive_the_command_string(self):
        """`orca terminal create --command` takes one string, so a value
        with spaces must not split into two arguments."""

        launch = _prepare(RoleName.SPEC_REVIEWER)
        self.assertIn("'Edit Write NotebookEdit'", shell_join(launch.command))


class TestTierIsObservable(unittest.TestCase):
    """Orca records the launch command, never the answering model. The
    transcript records the model on every answer, and pinning the session
    id at launch is what makes that file addressable."""

    def test_transcript_path_is_derived_from_the_session_id(self):
        launch = _prepare(RoleName.PLANNER)
        self.assertTrue(launch.transcript.endswith(f"{FIXED_UUID}.jsonl"))

    def test_models_are_read_from_the_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(
                json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
                + json.dumps({"message": {"model": "claude-sonnet-5"}}) + "\n"
                + "{not json at all}\n"
                + json.dumps({"message": {"model": "claude-sonnet-5"}}) + "\n"
            )
            self.assertEqual(CLAUDE.observed_models(path), ["claude-sonnet-5"])

    def test_a_missing_transcript_is_not_a_wrong_tier(self):
        self.assertEqual(CLAUDE.observed_models(Path("/nonexistent/none.jsonl")), [])


class TestObserve(unittest.TestCase):
    """No/empty transcript -> `requested`, never a guess; the requested
    tier answered -> `observed`; a different tier answered -> `observed`
    too, with the mismatch named in the detail."""

    def test_no_transcript_is_requested(self):
        evidence = CLAUDE.observe("/nonexistent/none.jsonl", CapabilityTier.STANDARD)
        self.assertEqual(evidence.kind, "requested")
        self.assertEqual(evidence.tier, CapabilityTier.STANDARD)

    def test_empty_transcript_is_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.jsonl"
            path.write_text("")
            evidence = CLAUDE.observe(str(path), CapabilityTier.STANDARD)
            self.assertEqual(evidence.kind, "requested")

    def test_matching_model_is_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(json.dumps({"message": {"model": "claude-sonnet-5"}}) + "\n")
            evidence = CLAUDE.observe(str(path), CapabilityTier.STANDARD)
            self.assertEqual(evidence.kind, "observed")
            self.assertEqual(evidence.tier, CapabilityTier.STANDARD)

    def test_diverging_model_is_observed_with_the_mismatch_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(json.dumps({"message": {"model": "claude-opus-5"}}) + "\n")
            evidence = CLAUDE.observe(str(path), CapabilityTier.STANDARD)
            self.assertEqual(evidence.kind, "observed")
            self.assertIsNone(evidence.tier)
            self.assertIn("mismatch", evidence.detail)
            self.assertIn("claude-opus-5", evidence.detail)


class TestHandoffHint(unittest.TestCase):
    def test_names_the_handoff_skill(self):
        self.assertIn("handoff/SKILL.md", CLAUDE.handoff_hint())


if __name__ == "__main__":
    unittest.main(verbosity=2)
