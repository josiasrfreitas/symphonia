"""The single place that knows how to start an agent.

TLDR: one table maps Capability Tier to a concrete launch command, and one
function builds the argv. Nothing else in the package — and no agent — may
write an agent command line by hand. Changing a model, an effort level or a
permission flag is an edit to this file only.

Why this file exists (GRE-179): ``orca orchestration worker-start`` launches
every agent with Orca's own default command and accepts no model, effort or
permission flag, so every worker in Leva 2 ran at the default model. The only
path that controls the command line is ``orca terminal create --command``,
which hands the whole argv to the caller — including the permission flags a
worker needs to run unattended. Leaving that argv to each caller is what
stalled a worker for ~15 minutes on an invisible approval prompt. So the argv
is built here, once, and the callers pass a role.

Provider expansion: ``PROVIDERS`` keys the argv grammar by provider. Adding
one means adding a ``LaunchGrammar`` entry, not touching any caller.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..harness_adapter import (
    HarnessCapabilities,
    HarnessRefusal,
    HarnessSession,
    PreparedLaunch,
    TierEvidence,
)
from ..runtime_adapter import Access, CapabilityTier, RoleName, RolePolicy, WorkspaceRef

# --- Capability Tier -> model -------------------------------------------
#
# The translation the Runtime Adapter contract promises: roles declare a
# tier, only this table knows a model name. Verified against the installed
# CLI on 2026-08-10 — `claude --model` takes an alias ("opus", "sonnet") or a
# full name ("claude-fable-5"); aliases are used so a model refresh does not
# need an edit here.

TIER_MODELS: dict[CapabilityTier, str] = {
    CapabilityTier.FRONTIER: "fable",
    CapabilityTier.HIGH: "opus",
    CapabilityTier.STANDARD: "sonnet",
    CapabilityTier.FAST: "haiku",
}

# Effort is not a second capability dial: the tier already bought the model.
# Medium is the house default for every role — the brief carries the thinking
# the role would otherwise have to rediscover. Sonnet is the one exception:
# it writes the code, and the extra reasoning is what keeps it from drifting
# from the plan (decision: 2026-08-11).

TIER_EFFORT: dict[CapabilityTier, str] = {
    CapabilityTier.FRONTIER: "medium",
    CapabilityTier.HIGH: "medium",
    CapabilityTier.STANDARD: "high",
    CapabilityTier.FAST: "low",
}

# --- tier/access -----------------------------------------------------------
#
# The matrix decided on GRE-179 (planner Fable, implementer Sonnet, reviewer
# Opus) now lives only in each role's `.symphonia/roles/*.md` frontmatter,
# read by `workflow.roles.load_policies` — no second table here to drift out
# of sync with it (GRE-186 S1). `build_launch` takes `tier`/`access` from the
# caller's `RolePolicy`; it no longer has a default of its own.

# Tools a read-access role may never call. Read access is enforced by the
# launch command, not by asking the model to behave: a reviewer that cannot
# reach Edit/Write cannot silently fix what it was asked to report.
WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")


@dataclass(frozen=True)
class LaunchGrammar:
    """How one provider spells model, effort, permissions and read-only."""

    executable: str
    model_flag: str
    effort_flag: str | None
    # The flag that makes the agent run unattended. Measured on GRE-179:
    # `acceptEdits` still prompts on Bash, and a worker's very first act is a
    # Bash call (`orca orchestration send`), so acceptEdits stalls every
    # worker on its completion report. Only a full bypass runs clean.
    unattended: tuple[str, ...]
    deny_flag: str | None
    session_flag: str | None


PROVIDERS: dict[str, LaunchGrammar] = {
    "claude": LaunchGrammar(
        executable="claude",
        model_flag="--model",
        effort_flag="--effort",
        unattended=("--permission-mode", "bypassPermissions"),
        deny_flag="--disallowedTools",
        session_flag="--session-id",
    ),
    # Present so the shape is proven to generalize; the Codex grammar is
    # read from `codex --help` but is NOT exercised by the GRE-179 bench.
    # Do not route a real worker here before running the same bench on it.
    "codex": LaunchGrammar(
        executable="codex",
        model_flag="--model",
        effort_flag=None,  # effort is `-c model_reasoning_effort=...`
        unattended=("--dangerously-bypass-approvals-and-sandbox",),
        deny_flag=None,
        session_flag=None,
    ),
}

DEFAULT_PROVIDER = "claude"


@dataclass(frozen=True)
class LaunchPlan:
    """Everything a spawn needs, decided before anything is created.

    ``argv`` is the unjoined command — turning that into one shell string
    for `orca terminal create --command` is the caller's job, not this
    module's (GRE-186 S3: `_shell_join` now lives in `adapters/orca/adapter.py`,
    the one place that actually calls that CLI). A field here named
    ``command`` holding a pre-joined string used to collide, in meaning,
    with `PreparedLaunch.command` (the *unjoined* argv tuple the Protocol
    hands the runtime) — dropped rather than renamed once nothing but a
    test read it."""

    role: RoleName
    tier: CapabilityTier
    access: Access
    provider: str
    session_id: str
    transcript: Path | None = None
    argv: tuple[str, ...] = field(default=())


def build_launch(
    role: RoleName,
    *,
    session_id: str,
    workspace: str,
    tier: CapabilityTier,
    access: Access,
    provider: str = DEFAULT_PROVIDER,
) -> LaunchPlan:
    """The one function that writes an agent command line.

    ``tier``/``access`` are always the caller's ``RolePolicy`` — no default
    here to drift from the role file frontmatter (GRE-186 S1). ``ClaudeHarness
    .prepare()`` is the one production caller, and it passes the
    ``RolePolicy`` values it was given directly, so a caller holding a
    policy stays authoritative over its own declaration."""

    grammar = PROVIDERS.get(provider)
    if grammar is None:
        raise ValueError(
            f"no launch grammar for provider {provider!r}; "
            f"known: {', '.join(sorted(PROVIDERS))}"
        )

    argv: list[str] = [grammar.executable, grammar.model_flag, TIER_MODELS[tier]]
    if grammar.effort_flag:
        argv += [grammar.effort_flag, TIER_EFFORT[tier]]
    argv += list(grammar.unattended)
    if access is Access.READ:
        if grammar.deny_flag is None:
            raise ValueError(
                f"provider {provider!r} cannot enforce read-only access at launch; "
                f"refusing to start {role.value} where it could write"
            )
        argv += [grammar.deny_flag, " ".join(WRITE_TOOLS)]
    if grammar.session_flag:
        argv += [grammar.session_flag, session_id]

    return LaunchPlan(
        role=role,
        tier=tier,
        access=access,
        provider=provider,
        session_id=session_id,
        transcript=transcript_path(workspace, session_id) if provider == "claude" else None,
        argv=tuple(argv),
    )


# --- Deterministic tier verification -------------------------------------
#
# Orca records the launch command and never the answering model — that gap
# is why the Runtime Adapter carries no tier evidence of its own (GRE-186
# S3). The transcript does: every assistant turn carries `"model":"<name>"`.
# Pinning `--session-id` makes the transcript path predictable, which turns
# "we asked for Sonnet" into "Sonnet answered" — read from a file, never
# from the agent's word.


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


def tier_matches(tier: CapabilityTier, models: list[str]) -> bool:
    """Whether the models that answered are the tier that was requested."""

    alias = TIER_MODELS[tier]
    return bool(models) and all(alias in m for m in models)


# --- HarnessAdapter -------------------------------------------------------
#
# `ClaudeHarness` wraps everything above behind `adapters.harness_adapter`'s
# neutral Protocol: `build_launch` is still the one function that writes an
# agent command line, `observed_models`/`tier_matches` are still what turns
# a transcript into evidence. `OrcaRuntimeAdapter.open_context` never calls
# `build_launch` — the inversion GRE-186 S3 closes: `ClaudeHarness.prepare()`
# is the one production caller now, and `open_context` only ever sees the
# `PreparedLaunch` it hands back.

# The path `handoff_hint()` names — the skill that produces a role's handoff
# document. The one place this string is spelled (GRE-186 S3): `spawn.py`
# owns the baton instructions' structure, but splices this harness's own
# hint in rather than knowing the skill's path itself.
HANDOFF_SKILL = "~/.claude/skills/handoff/SKILL.md"


@dataclass(frozen=True)
class ClaudeHarness:
    """`HarnessAdapter` for the ``claude`` CLI.

    ``capabilities`` is a stored field, not a computed property: it never
    varies for the real harness, and a plain field lets a test stub it on
    one instance with a single constructor kwarg — no second harness
    implementation, no monkeypatching a class-level descriptor."""

    capabilities: HarnessCapabilities = field(
        default_factory=lambda: HarnessCapabilities(read_only=True, tier_evidence="observed")
    )

    def prepare(self, *, workspace: WorkspaceRef, policy: RolePolicy) -> PreparedLaunch:
        if policy.access is Access.READ and not self.capabilities.read_only:
            raise HarnessRefusal(
                f"this harness cannot enforce read-only access at launch; "
                f"refusing to start {policy.role.value} where it could write"
            )
        session_id = str(uuid.uuid4())
        plan = build_launch(
            policy.role,
            session_id=session_id,
            workspace=workspace.path,
            tier=policy.tier,
            access=policy.access,
        )
        session = HarnessSession(
            id=session_id,
            observation_ref=str(plan.transcript) if plan.transcript else None,
        )
        return PreparedLaunch(command=plan.argv, session=session)

    def observe(self, session: HarnessSession, requested: CapabilityTier) -> TierEvidence:
        """The tier-checking semantics `OrcaRuntimeAdapter.verify_tier` used
        to own, before GRE-186 S3 moved them here: no transcript reference,
        or a transcript that has not answered yet, is `requested`, never a
        guess. Once it has answered, a match is `observed`; a divergence is
        `observed` too — the evidence is honest either way, only the tier
        differs."""

        if not session.observation_ref:
            return TierEvidence(
                kind="requested", tier=requested,
                detail="this harness exposes no transcript for this session",
            )
        models = observed_models(Path(session.observation_ref))
        if not models:
            return TierEvidence(
                kind="requested", tier=requested,
                detail="session has not answered yet; transcript is empty",
            )
        if tier_matches(requested, models):
            return TierEvidence(
                kind="observed", tier=requested,
                detail=f"transcript reports {', '.join(models)}",
            )
        return TierEvidence(
            kind="observed", tier=None,
            detail=(
                f"tier mismatch: requested {requested.value} "
                f"({TIER_MODELS[requested]}), transcript reports {', '.join(models)}"
            ),
        )

    def handoff_hint(self) -> str:
        return f"{HANDOFF_SKILL} — the document half only (as with --doc-only)"
