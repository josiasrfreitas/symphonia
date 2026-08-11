"""HarnessAdapter: the neutral boundary to one agent CLI (GRE-186 S2).

TLDR: ``RuntimeAdapter`` (``runtime_adapter.py``) knows how to create
workspaces, open Role Contexts and drain events — it says nothing about how
one agent process is actually started, or how the tier it ran at can be
checked. That is this second, narrower contract: prepare an unattended
launch for one role, observe what tier actually answered, and report what
the harness can and cannot do. ``harnesses.claude.ClaudeHarness`` is the
first (and, until a second harness exists, the only) implementation.

Import direction is one-way: this module reads core vocabulary
(``CapabilityTier``, ``WorkspaceRef``, ``TierEvidence``) from
``runtime_adapter``; nothing in ``runtime_adapter`` imports from here.

``TierEvidence`` still lives in ``runtime_adapter.py``, not here — it is
genuinely harness vocabulary (GRE-186's approved plan says so), but
``runtime_adapter.LaunchResult``/``verify_tier`` are still its last callers
this round, and moving its definition is scoped to GRE-186 S3, alongside
their removal, not this round's declared Write Scope. Re-exported here so
every other module in this round reads it from one place.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .runtime_adapter import CapabilityTier, TierEvidence, WorkspaceRef
from workflow.roles import RolePolicy

TierEvidenceKind = Literal["requested", "observed", "unverifiable"]


@dataclass(frozen=True)
class HarnessSession:
    """Identity of one launched agent session, plus where the evidence of
    what it actually ran can be read back from later — for Claude, the
    transcript path; ``None`` for a harness that exposes no such record."""

    id: str
    observation_ref: str | None = None


@dataclass(frozen=True)
class PreparedLaunch:
    """Everything a runtime needs to actually start the process: the argv,
    unjoined — turning that into one shell string is the caller's job
    (whatever it hands to ``orca terminal create --command``), not the
    harness's — and the session prepare() minted for it."""

    command: tuple[str, ...]
    session: HarnessSession


@dataclass(frozen=True)
class HarnessCapabilities:
    """What one harness can promise, independent of any particular launch."""

    read_only: bool
    """Whether this harness can structurally deny write tools at launch —
    the same guarantee ``Access.READ`` requires. A harness that cannot must
    refuse rather than start a reviewer that could edit what it reviews."""

    tier_evidence: TierEvidenceKind
    """The strongest ``TierEvidence.kind`` this harness can ever produce.
    Claude's is ``"observed"`` — it can read a transcript."""


class HarnessRefusal(Exception):
    """Raised by ``prepare()`` instead of starting a launch it cannot honor
    — a policy asking for ``Access.READ`` from a harness whose
    ``capabilities.read_only`` is False, or any other declared policy the
    harness structurally cannot keep. Better a loud refusal at prepare time
    than a role running with more access than its policy grants."""


class HarnessAdapter(Protocol):
    """Everything the workflow may ask of one agent harness. Nothing else."""

    @property
    def capabilities(self) -> HarnessCapabilities: ...

    def prepare(self, *, workspace: WorkspaceRef, policy: RolePolicy) -> PreparedLaunch: ...

    def observe(self, session: HarnessSession, requested: CapabilityTier) -> TierEvidence: ...

    def handoff_hint(self) -> str:
        """The line naming which skill produces a role's handoff document —
        a fragment the core baton instructions splice in, so the core never
        has to know the skill's path is a Claude-specific detail."""
        ...
