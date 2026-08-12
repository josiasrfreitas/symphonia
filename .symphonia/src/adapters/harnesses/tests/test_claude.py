"""Tests for the Claude launch interface (GRE-179) and its `HarnessAdapter`
wrapper, `ClaudeHarness` (GRE-186 S2).

TLDR: `build_launch` still locks the two things that are silent when they
break — a worker launched in a mode that can stop at a permission prompt,
and a read-only role launched somewhere it could write. `ClaudeHarness`
wraps the same argv behind the neutral Protocol; its own tests check the
argv it produces against the frozen `test_spawn_characterization.py` oracle,
that `observe()` reports both `TierEvidence` kinds this harness can produce,
and that a harness which cannot enforce read-only refuses rather than
silently launching with write access.
Run either way:

    cd .symphonia/src && python3 -m unittest adapters.harnesses.tests.test_claude
    python3 .symphonia/src/adapters/harnesses/tests/test_claude.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from adapters.harness_adapter import HarnessCapabilities, HarnessRefusal
from adapters.harnesses.claude import (
    PROVIDERS,
    ClaudeHarness,
    build_launch,
    observed_models,
    tier_matches,
)
from adapters.orca.adapter import _shell_join
from adapters.runtime_adapter import Access, CapabilityTier, RoleName, WorkspaceRef
from adapters.tests.test_spawn_characterization import (
    FIXED_UUID,
    PLAN_COMMAND_IMPLEMENTER,
    PLAN_COMMAND_PLANNER,
)
from workflow.roles import load_policies

ROLES_DIR = Path(__file__).resolve().parents[4] / "roles"
POLICIES = load_policies(ROLES_DIR)

WORKSPACE = WorkspaceRef(ticket_key="GRE-1", id="wt-1", path="/tmp/w", branch="b")


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
                self.assertIn("bypassPermissions", plan.argv)

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
                    self.assertIn("--disallowedTools", plan.argv)
                    self.assertIn("Edit Write NotebookEdit", plan.argv)
                else:
                    self.assertNotIn("--disallowedTools", plan.argv)

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
        spaces must not split into two arguments. Joining is the caller's
        job now, not `build_launch`'s (GRE-186 S3) — `_shell_join` lives in
        `adapters/orca/adapter.py`, the one place that actually calls that
        CLI; this test plays that caller's role."""

        policy = POLICIES[RoleName.SPEC_REVIEWER]
        plan = build_launch(
            RoleName.SPEC_REVIEWER, session_id="s", workspace="/tmp/w",
            tier=policy.tier, access=policy.access,
        )
        self.assertIn("'Edit Write NotebookEdit'", _shell_join(plan.argv))


class TestClaudeHarnessCapabilities(unittest.TestCase):
    def test_claude_can_enforce_read_only_and_observe_the_answering_model(self):
        harness = ClaudeHarness()
        self.assertEqual(
            harness.capabilities, HarnessCapabilities(read_only=True, tier_evidence="observed")
        )


class TestClaudeHarnessPrepare(unittest.TestCase):
    """`prepare()` wraps `build_launch` behind the Protocol — same argv,
    unjoined (joining into a shell string is the caller's job, not the
    harness's), keyed to a session id `prepare()` mints itself."""

    def test_prepare_matches_the_frozen_argv_oracle(self):
        """`test_spawn_characterization.py`'s `PLAN_COMMAND_PLANNER`/
        `PLAN_COMMAND_IMPLEMENTER` are the argv literals frozen for GRE-184
        M0 and untouchable by this ticket's own condition #6 — imported, not
        copied, so there is exactly one oracle for what these two roles'
        commands are, not two that could quietly diverge. `prepare()` mints
        its own session id via this module's `uuid.uuid4`, so it is patched
        to the oracle's `FIXED_UUID` rather than matched positionally — a
        `--session-id` that silently dropped from the argv would then leave
        the expected `--session-id {FIXED_UUID}` in the string and fail
        loudly, instead of both sides losing the slot together."""

        cases = {
            RoleName.PLANNER: PLAN_COMMAND_PLANNER,
            RoleName.IMPLEMENTER: PLAN_COMMAND_IMPLEMENTER,
        }
        for role, expected in cases.items():
            with self.subTest(role=role.value):
                with mock.patch("adapters.harnesses.claude.uuid.uuid4", return_value=FIXED_UUID):
                    prepared = ClaudeHarness().prepare(workspace=WORKSPACE, policy=POLICIES[role])
                self.assertEqual(_shell_join(list(prepared.command)), expected)

    def test_prepare_refuses_read_when_the_harness_cannot_enforce_it(self):
        """Condition 3 of the GRE-186 PR-A verdict: `HarnessRefusal` is
        exercised, not dead code. A one-line stub over this one instance —
        never a second harness implementation — claims it cannot deny write
        tools, and a READ policy must be refused rather than silently
        launched with write access."""

        harness = ClaudeHarness(capabilities=HarnessCapabilities(read_only=False, tier_evidence="unverifiable"))
        read_policy = next(p for p in POLICIES.values() if p.access is Access.READ)
        with self.assertRaises(HarnessRefusal):
            harness.prepare(workspace=WORKSPACE, policy=read_policy)

    def test_prepare_still_allows_write_when_the_harness_cannot_enforce_read_only(self):
        harness = ClaudeHarness(capabilities=HarnessCapabilities(read_only=False, tier_evidence="unverifiable"))
        write_policy = next(p for p in POLICIES.values() if p.access is Access.WRITE)
        prepared = harness.prepare(workspace=WORKSPACE, policy=write_policy)
        self.assertNotIn("--disallowedTools", prepared.command)


class TestClaudeHarnessObserve(unittest.TestCase):
    """Condition 4 of the GRE-186 PR-A verdict, satisfied with the two kinds
    this harness can actually emit: no/empty transcript -> `requested`; the
    requested tier answered -> `observed`; a different tier answered ->
    `observed` too, with the mismatch named in the detail. `unverifiable` has
    no analogue here — it names `OrcaRuntimeAdapter.verify_tier`'s
    "no such context" branch, a lookup failure that can only happen before a
    session exists to observe; `observe()` is handed the session directly and
    never does that lookup, so there is nothing for a third kind to report."""

    def _session(self, path):
        from adapters.harness_adapter import HarnessSession
        return HarnessSession(id="s", observation_ref=str(path))

    def test_no_transcript_is_requested(self):
        harness = ClaudeHarness()
        evidence = harness.observe(self._session("/nonexistent/none.jsonl"), CapabilityTier.STANDARD)
        self.assertEqual(evidence.kind, "requested")
        self.assertEqual(evidence.tier, CapabilityTier.STANDARD)

    def test_empty_transcript_is_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.jsonl"
            path.write_text("")
            harness = ClaudeHarness()
            evidence = harness.observe(self._session(path), CapabilityTier.STANDARD)
            self.assertEqual(evidence.kind, "requested")

    def test_matching_model_is_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(json.dumps({"message": {"model": "claude-sonnet-5"}}) + "\n")
            harness = ClaudeHarness()
            evidence = harness.observe(self._session(path), CapabilityTier.STANDARD)
            self.assertEqual(evidence.kind, "observed")
            self.assertEqual(evidence.tier, CapabilityTier.STANDARD)

    def test_diverging_model_is_observed_with_the_mismatch_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(json.dumps({"message": {"model": "claude-opus-5"}}) + "\n")
            harness = ClaudeHarness()
            evidence = harness.observe(self._session(path), CapabilityTier.STANDARD)
            self.assertEqual(evidence.kind, "observed")
            self.assertIsNone(evidence.tier)
            self.assertIn("mismatch", evidence.detail)
            self.assertIn("claude-opus-5", evidence.detail)


class TestClaudeHarnessHandoffHint(unittest.TestCase):
    def test_names_the_handoff_skill(self):
        self.assertIn("handoff/SKILL.md", ClaudeHarness().handoff_hint())


if __name__ == "__main__":
    unittest.main()
