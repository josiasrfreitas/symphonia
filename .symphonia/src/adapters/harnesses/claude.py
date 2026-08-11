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
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..runtime_adapter import Access, CapabilityTier, RoleName

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
    """Everything a spawn needs, decided before anything is created."""

    role: RoleName
    tier: CapabilityTier
    access: Access
    provider: str
    session_id: str
    command: str
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
    here to drift from the role file frontmatter (GRE-186 S1). The Runtime
    Adapter passes the ``RoleSpec`` values it was given, so a caller holding
    a spec stays authoritative over its own declaration."""

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
        command=_shell_join(argv),
        transcript=transcript_path(workspace, session_id) if provider == "claude" else None,
        argv=tuple(argv),
    )


def _shell_join(argv: list[str]) -> str:
    """`orca terminal create --command` takes one string, so the argv is
    joined here — quoting anything the shell would otherwise split."""

    out = []
    for part in argv:
        out.append(f"'{part}'" if re.search(r"[\s()*?\"$]", part) else part)
    return " ".join(out)


# --- Deterministic tier verification -------------------------------------
#
# The Runtime Adapter reports `tier_verification=False` because Orca records
# the launch command and never the answering model. The transcript does:
# every assistant turn carries `"model":"<name>"`. Pinning `--session-id`
# makes the transcript path predictable, which turns "we asked for Sonnet"
# into "Sonnet answered" — read from a file, never from the agent's word.


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
