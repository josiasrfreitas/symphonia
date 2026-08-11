"""Neutral parser for role I/O, and the block-extraction helper the package
shares with ``bin/spawn``.

TLDR: ``extract_block`` pulls one tagged fenced code block out of a role
file's markdown — the same helper fills the Execution Brief template in
``spawn`` and reads the golden examples in the tests. The ``parse_*``
functions turn a role's message body into a typed, frozen dataclass or raise
``MalformedReport`` naming exactly what is missing or wrong — structural
parsing only (first-line tokens, ``^## `` sections), never judgment about
prose. See ``.symphonia/README.md``, "Rules the package encodes", for the
payload × body boundary these types assume: a script-decided field always
comes from the payload argument, never from the body, even where the body
repeats it for a human to read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class MalformedReport(Exception):
    """A role's message did not follow its I/O contract. The message names
    exactly what section, token, or payload field is missing or wrong —
    callers turn this into ``AttentionCode.MALFORMED_REPORT``, never into a
    guess about what was meant."""


# --- block extraction, shared by spawn and the golden tests ---------------


def extract_block(text: str, tag: str) -> str:
    """The content of the first fenced code block whose info string is
    exactly ``tag`` (e.g. ``"md io:brief-template"``). Raises ``LookupError``
    if no such block exists — a missing template is a package bug, not a
    role-report problem, so it is not a ``MalformedReport``."""

    pattern = re.compile(r"```" + re.escape(tag) + r"\n(.*?)\n```", re.DOTALL)
    match = pattern.search(text)
    if match is None:
        raise LookupError(f"no fenced block tagged {tag!r}")
    return match.group(1)


# --- section splitting, shared by the three parsers below -----------------


def _sections(body: str) -> dict[str, str]:
    """Split a body into ``{heading: content}`` on lines starting with
    ``## ``. Content is the text between one heading and the next, stripped
    of leading/trailing blank lines."""

    out: dict[str, str] = {}
    heading = None
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if heading is not None:
                out[heading] = "\n".join(buf).strip()
            heading = line[3:].strip()
            buf = []
        elif heading is not None:
            buf.append(line)
    if heading is not None:
        out[heading] = "\n".join(buf).strip()
    return out


def _require_section(sections: dict[str, str], name: str, what: str) -> str:
    if name not in sections:
        raise MalformedReport(f"{what}: missing required section '## {name}'")
    return sections[name]


# --- (a) plan submission ---------------------------------------------------


@dataclass(frozen=True)
class PlanSubmission:
    ticket: str
    pointer: str
    decisions: tuple[str, ...]
    changes: str


def is_plan_submission(body: str) -> bool:
    """The cheap, structural test a gate script uses to recognize a plan
    submission among other messages — the first line and nothing else."""

    lines = body.splitlines()
    return bool(lines) and lines[0].strip() == "## Plan"


def parse_plan_submission(body: str) -> PlanSubmission:
    if not is_plan_submission(body):
        raise MalformedReport(
            "plan submission: first line must be exactly '## Plan'"
        )
    sections = _sections(body)
    plan_line = _require_section(sections, "Plan", "plan submission")
    ticket, _, pointer = plan_line.partition(" — ")
    if not pointer:
        raise MalformedReport(
            "plan submission: '## Plan' line must be 'TICKET — pointer', "
            f"got {plan_line!r}"
        )
    decisions_text = _require_section(sections, "Decisions", "plan submission")
    decisions = tuple(
        line.strip() for line in decisions_text.splitlines() if line.strip()
    )
    changes = _require_section(sections, "Changes", "plan submission")
    return PlanSubmission(
        ticket=ticket.strip(), pointer=pointer.strip(),
        decisions=decisions, changes=changes,
    )


# --- (b) approval reply -----------------------------------------------------


@dataclass(frozen=True)
class ApprovalVerdict:
    approved: bool
    notes: tuple[str, ...]


_VALID_TOKENS = ("APPROVED", "REVISE")


def parse_approval_reply(body: str) -> ApprovalVerdict:
    lines = [line for line in body.splitlines()]
    token_line = next((line.strip() for line in lines if line.strip()), "")
    if token_line not in _VALID_TOKENS:
        raise MalformedReport(
            f"approval reply: first non-empty line must be exactly one of "
            f"{_VALID_TOKENS}, got {token_line!r}"
        )
    notes = tuple(
        line.strip()[2:].strip()
        for line in lines
        if line.strip().startswith("- ")
    )
    return ApprovalVerdict(approved=token_line == "APPROVED", notes=notes)


def format_approval_reply(token: str, notes: tuple[str, ...] | list[str] = ()) -> str:
    """The one place that writes the approval-reply shape ``parse_approval_reply``
    reads — ``spawn verdict`` calls this instead of assembling the token and
    the note list by hand, so the contract has a single house instead of one
    per caller."""

    if token not in _VALID_TOKENS:
        raise ValueError(f"unknown approval token {token!r}; use one of {_VALID_TOKENS}")
    lines = [line.strip() for line in notes if line.strip()]
    return token + ("\n\n" + "\n".join(f"- {line}" for line in lines) if lines else "")


# --- (c) planner worker_done -------------------------------------------------


@dataclass(frozen=True)
class PlannerReport:
    plan_pointer: str
    deviations: tuple[str, ...]


def parse_planner_done(body: str) -> PlannerReport:
    """The body only. ``planApproved`` and ``approvalRounds`` used to be read
    from the payload and are now derived from the recorded gate state by
    ``spawn done`` — a role asserting "the plan was approved" was a claim the
    script already had the answer to, and the only thing that claim could add
    was a disagreement."""

    sections = _sections(body)
    plan_pointer = _require_section(sections, "Plan", "planner worker_done")
    _require_section(sections, "Approval", "planner worker_done")
    deviations_text = _require_section(sections, "Deviations", "planner worker_done")
    deviations = (
        ()
        if deviations_text.strip() == "None."
        else tuple(
            line.strip()[2:].strip()
            for line in deviations_text.splitlines()
            if line.strip().startswith("- ")
        )
    )
    return PlannerReport(plan_pointer=plan_pointer.strip(), deviations=deviations)


def set_approval_rounds(body: str, approval_rounds: int) -> str:
    """Rewrite only the ``## Approval`` section, in place, from the round
    count the gate observed.

    Only that section: the rest of the body is the role's, and a report may
    carry sections this module never heard of (``## Risks``, ``## Handoff``).
    Rebuilding the whole body from the parsed fields would drop them, and a
    dispatch grants exactly one ``worker_done``, so nothing dropped here can
    be sent again."""

    rounds = f"{approval_rounds} rodada." if approval_rounds == 1 else f"{approval_rounds} rodadas."
    lines = body.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "## Approval")
    except StopIteration:
        raise MalformedReport("planner worker_done: missing required section '## Approval'")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines)
    )
    # Blank lines before the next heading are separators, not content.
    tail = lines[end:]
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[: start + 1] + [rounds] + [""] * bool(tail) + tail)
