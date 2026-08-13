"""The single place that knows how to start an agent, and how to check
what actually answered.

One table maps Capability Tier to a model, one function builds the argv.
Nothing else in the package — and no agent — writes an agent command line
by hand: `orca orchestration worker-start` launches with Orca's own default
command and accepts no model or permission flag, and a hand-typed
`terminal create` stalls the worker on an invisible approval prompt
(both measured, GRE-179). Changing a model, an effort level or a
permission flag is an edit to this file only.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from roles import Access, CapabilityTier, RolePolicy

# `claude --model` takes an alias, so a model refresh does not need an edit
# here. Verified against the installed CLI on 2026-08-10.
TIER_MODELS: dict[CapabilityTier, str] = {
    CapabilityTier.FRONTIER: "fable",
    CapabilityTier.HIGH: "opus",
    CapabilityTier.STANDARD: "sonnet",
    CapabilityTier.FAST: "haiku",
}

# Effort is not a second capability dial: the tier already bought the model.
# Medium is the house default — the brief carries the thinking the role
# would otherwise rediscover. Sonnet is the one exception: it writes the
# code, and the extra reasoning keeps it from drifting from the plan
# (decision: 2026-08-11).
TIER_EFFORT: dict[CapabilityTier, str] = {
    CapabilityTier.FRONTIER: "medium",
    CapabilityTier.HIGH: "medium",
    CapabilityTier.STANDARD: "high",
    CapabilityTier.FAST: "low",
}

# Read access is enforced by the launch command, not by asking the model to
# behave: a reviewer that cannot reach Edit/Write cannot silently fix what
# it was asked to report.
WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")

# The skill that produces a role's handoff document — spawned roles are
# pointed here by the brief.
HANDOFF_SKILL = "~/.claude/skills/handoff/SKILL.md"


@dataclass(frozen=True)
class Launch:
    """Everything a spawn needs to start one agent session: the argv
    (unjoined — `orca.create_terminal` joins it), the session id minted for
    it, and where the evidence of what actually ran can be read back."""

    command: tuple[str, ...]
    session_id: str
    transcript: str


def prepare(*, workspace: str, policy: RolePolicy) -> Launch:
    """Build the launch for one role. `bypassPermissions`, not
    `acceptEdits`: acceptEdits still prompts on Bash, and a worker's very
    first act is a Bash call (`spawn done`), so anything less stalls every
    worker on its completion report (measured, GRE-179)."""

    session_id = str(uuid.uuid4())
    argv = [
        "claude",
        "--model", TIER_MODELS[policy.tier],
        "--effort", TIER_EFFORT[policy.tier],
        "--permission-mode", "bypassPermissions",
    ]
    if policy.access is Access.READ:
        argv += ["--disallowedTools", " ".join(WRITE_TOOLS)]
    argv += ["--session-id", session_id]
    return Launch(
        command=tuple(argv),
        session_id=session_id,
        transcript=str(transcript_path(workspace, session_id)),
    )


def handoff_hint() -> str:
    return f"{HANDOFF_SKILL} — the document half only (as with --doc-only)"


# --- deterministic tier evidence ------------------------------------------
#
# Orca records the launch command and never the answering model. The
# transcript does: every assistant turn carries `"model":"<name>"`. Pinning
# `--session-id` makes the transcript path predictable, which turns "we
# asked for Sonnet" into "Sonnet answered" — read from a file, never from
# the agent's word.


@dataclass(frozen=True)
class TierEvidence:
    """What is actually known about the tier a role ran at: `requested`
    (launch asked; no answer observed yet) or `observed` (the transcript
    answered — honest either way, only the tier differs)."""

    kind: str
    tier: CapabilityTier | None = None
    detail: str = ""


def transcript_path(workspace: str, session_id: str) -> Path:
    """Where the Claude CLI writes the session transcript for a workspace."""

    slug = str(Path(workspace).resolve()).replace("/", "-").replace(".", "-")
    return Path(os.path.expanduser("~/.claude/projects")) / slug / f"{session_id}.jsonl"


def observed_models(transcript: Path) -> list[str]:
    """Every distinct model that actually answered, oldest first. Empty when
    the session has not answered yet — which is not proof of failure."""

    if not transcript.exists():
        return []
    seen: list[str] = []
    for line in transcript.read_text(errors="replace").splitlines():
        if '"model"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        model = (record.get("message") or {}).get("model") or record.get("model")
        if isinstance(model, str) and model and model not in seen:
            seen.append(model)
    return seen


def observe(transcript: str, requested: CapabilityTier) -> TierEvidence:
    """Evidence from the transcript: not answered yet is `requested`, never
    a guess; answered is `observed`, match or mismatch."""

    models = observed_models(Path(transcript))
    if not models:
        return TierEvidence(
            kind="requested", tier=requested,
            detail="session has not answered yet; transcript is empty",
        )
    alias = TIER_MODELS[requested]
    if all(alias in m for m in models):
        return TierEvidence(
            kind="observed", tier=requested,
            detail=f"transcript reports {', '.join(models)}",
        )
    return TierEvidence(
        kind="observed", tier=None,
        detail=(
            f"tier mismatch: requested {requested.value} ({alias}), "
            f"transcript reports {', '.join(models)}"
        ),
    )
