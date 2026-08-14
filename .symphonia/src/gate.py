"""The plan gate, whole: the report formats a role writes, the state
machine that judges the events, and the loop that executes the actions.

This is the one file a gate operation lives in. The verbs in `spawn.py`
carry the payload (the brief in, the report out); everything that *judges*
or *acts on* a role's message is here:

- The parsers (`parse_plan_submission`, `parse_approval_reply`,
  `parse_planner_done`) turn a message body into typed fields or raise
  `MalformedReport` naming exactly what is wrong — structural parsing only
  (first-line tokens, `## ` sections), never judgment about prose.
- `transition` is the pure state machine between the planner and the human:
  a submission turns the label on, a verdict turns it off, an approved
  `worker_done` retires the planner.
- `run` attributes each mailbox event to the role it belongs to, applies
  the machine, and executes what comes back — label the ticket, end the
  role, flag a divergence. The tracker and `teardown` arrive by injection,
  so a test drives this with a plain dict.

Payload × body rule (see `.symphonia/README.md`): a script-decided field
always travels in the message payload, never the body, even where the body
repeats it for a human to read.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

# --- structured Needs Attention codes --------------------------------------


class AttentionCode(enum.Enum):
    """Why an Implementation Ticket stopped and needs the user. Scripts
    branch on the code, never on substrings of the free-text reason."""

    MALFORMED_REPORT = "malformed-report"
    """A role's message did not follow its I/O contract; a script found the
    mismatch, never a model reading prose."""

    ROLE_REPORTED = "role-reported"
    """A role reported a blocker; the Orchestrator raised the flag for it."""


# --- report parsing ---------------------------------------------------------


class MalformedReport(Exception):
    """A role's message did not follow its I/O contract. The message names
    exactly what section, token, or field is missing or wrong — callers
    turn this into `AttentionCode.MALFORMED_REPORT`, never into a guess
    about what was meant."""


def extract_block(text: str, tag: str) -> str:
    """The content of the first fenced code block whose info string is
    exactly `tag` (e.g. `"md io:brief-template"`). Raises `LookupError` if
    no such block exists — a missing template is a package bug, not a
    role-report problem."""

    pattern = re.compile(r"```" + re.escape(tag) + r"\n(.*?)\n```", re.DOTALL)
    match = pattern.search(text)
    if match is None:
        raise LookupError(f"no fenced block tagged {tag!r}")
    return match.group(1)


def _sections(body: str) -> dict[str, str]:
    """Split a body into `{heading: content}` on lines starting `## `."""

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


@dataclass(frozen=True)
class PlanSubmission:
    ticket: str
    pointer: str
    decisions: tuple[str, ...]
    changes: str


def is_plan_submission(body: str) -> bool:
    """The cheap, structural test that recognizes a plan submission among
    other messages — the first line and nothing else."""

    lines = body.splitlines()
    return bool(lines) and lines[0].strip() == "## Plan"


def parse_plan_submission(body: str) -> PlanSubmission:
    if not is_plan_submission(body):
        raise MalformedReport("plan submission: first line must be exactly '## Plan'")
    sections = _sections(body)
    plan_line = _require_section(sections, "Plan", "plan submission")
    ticket, _, pointer = plan_line.partition(" — ")
    if not pointer:
        raise MalformedReport(
            f"plan submission: '## Plan' line must be 'TICKET — pointer', got {plan_line!r}"
        )
    decisions_text = _require_section(sections, "Decisions", "plan submission")
    decisions = tuple(line.strip() for line in decisions_text.splitlines() if line.strip())
    changes = _require_section(sections, "Changes", "plan submission")
    return PlanSubmission(
        ticket=ticket.strip(), pointer=pointer.strip(), decisions=decisions, changes=changes,
    )


@dataclass(frozen=True)
class ApprovalVerdict:
    approved: bool
    notes: tuple[str, ...]


_VALID_TOKENS = ("APPROVED", "REVISE")


def parse_approval_reply(body: str) -> ApprovalVerdict:
    lines = body.splitlines()
    token_line = next((line.strip() for line in lines if line.strip()), "")
    if token_line not in _VALID_TOKENS:
        raise MalformedReport(
            f"approval reply: first non-empty line must be exactly one of "
            f"{_VALID_TOKENS}, got {token_line!r}"
        )
    notes = tuple(
        line.strip()[2:].strip() for line in lines if line.strip().startswith("- ")
    )
    return ApprovalVerdict(approved=token_line == "APPROVED", notes=notes)


def format_approval_reply(token: str, notes: tuple[str, ...] | list[str] = ()) -> str:
    """The one place that writes the shape `parse_approval_reply` reads —
    script on both ends of the conversation, so `APPROVED`/`REVISE` never
    depends on a model reading prose."""

    if token not in _VALID_TOKENS:
        raise ValueError(f"unknown approval token {token!r}; use one of {_VALID_TOKENS}")
    lines = [line.strip() for line in notes if line.strip()]
    return token + ("\n\n" + "\n".join(f"- {line}" for line in lines) if lines else "")


@dataclass(frozen=True)
class PlannerReport:
    plan_pointer: str
    deviations: tuple[str, ...]


def parse_planner_done(body: str) -> PlannerReport:
    """The body only. `planApproved`/`approvalRounds` are derived from the
    recorded gate state by `spawn done` — a role asserting "the plan was
    approved" is a claim the script already has the answer to."""

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
    """Rewrite only the `## Approval` section, from the round count the
    gate observed. Only that section: the rest of the body is the role's,
    and a dispatch grants exactly one `worker_done`, so nothing dropped
    here could ever be sent again."""

    rounds = f"{approval_rounds} rodada." if approval_rounds == 1 else f"{approval_rounds} rodadas."
    lines = body.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "## Approval")
    except StopIteration:
        raise MalformedReport("planner worker_done: missing required section '## Approval'")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines)
    )
    tail = lines[end:]
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[: start + 1] + [rounds] + [""] * bool(tail) + tail)


# --- the state machine ------------------------------------------------------

# The two event kinds the gate judges. Coined HERE and imported by
# `orca.gate_events`, which mints the events — one origin, because a
# renamed or mistyped kind on either side would not error: it would fall
# through `transition`'s final return and silently disconnect the gate.
PLAN_QUESTION = "plan-question"
WORKER_DONE = "worker-done"

IDLE = "idle"
SUBMITTED = "submitted"
VERDICT_APPROVED = "verdict-approved"
VERDICT_REVISE = "verdict-revise"
RETIRED = "retired"

STATES = frozenset({IDLE, SUBMITTED, VERDICT_APPROVED, VERDICT_REVISE, RETIRED})

# Actions are values, not calls: `run` executes them against the tracker
# and the runtime.
LABEL_ON = "label_on"
LABEL_OFF = "label_off"
RETIRE_PLANNER = "retire_planner"
RETIRE_ROLE = "retire_role"
FLAG_MALFORMED = "flag_malformed"


@dataclass(frozen=True)
class Transition:
    state: str
    actions: tuple[str, ...]
    # Set only on a fresh (non-replay) plan-question submission — the id
    # the caller remembers and passes back as `last_question_id` next time.
    question_id: str | None = None


def transition(
    state: str,
    event: Any,
    *,
    last_question_id: str | None = None,
    report_ok: bool = True,
) -> Transition:
    """One event in, one state out plus the actions to run.

    Replay-safe by construction: an unacked Delivery replays the same
    batch, so transitions are keyed off the current state — applying the
    same event twice produces the same state and an empty action list. A
    `plan-question` is the one exception that needs identity:
    `last_question_id` tells a replayed submission from a genuine
    resubmission after REVISE.

    It handles the two events a coordinator can observe. The verdict reply
    is not among them — the answer to an `ask` goes back to the worker that
    asked (measured on Orca 1.4.168) — so the state moves to
    `verdict-approved`/`verdict-revise` when `spawn verdict` writes it.
    """

    kind = getattr(event, "kind", None)

    if kind == PLAN_QUESTION:
        if event.message_id == last_question_id:
            return Transition(state, ())  # the same submission, replayed
        if state == RETIRED:
            return Transition(state, ())  # a dead planner cannot reopen the gate
        if state == SUBMITTED:
            # A second, distinct question before any verdict: the planner
            # abandoned the first `ask` and is now blocked on this one. The
            # pending question id must follow the planner, or the verdict
            # is replied into a thread nobody is listening to.
            return Transition(state, (), question_id=event.message_id)
        return Transition(SUBMITTED, (LABEL_ON,), question_id=event.message_id)

    if kind == WORKER_DONE:
        if state == RETIRED:
            return Transition(state, ())  # guarded: retire fires once
        if event.outcome != "succeeded":
            return Transition(state, ())  # a failed planner is for the human
        if state == VERDICT_APPROVED:
            if not report_ok:
                return Transition(state, (FLAG_MALFORMED,))
            return Transition(RETIRED, (RETIRE_PLANNER,))
        # succeeded without a recorded approval: the report and the gate
        # state disagree — never guess which one is right.
        return Transition(state, (FLAG_MALFORMED,))

    return Transition(state, ())


# --- the loop ---------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def apply_gate_event(rec: dict, event, raw_by_id: dict) -> list[tuple[str, str | None]]:
    """The gate's decision for one event: mutates `rec`'s
    `gate_state`/`question_id` in place and returns the `(action, reason)`
    pairs to execute — `reason` is only set for `flag_malformed`. No
    network call happens here.

    A `plan-question` is only a gate event when its body is a real plan
    submission — a clarifying question from the planner does not light the
    label and is left for the caller to treat as an ordinary message. A
    body that starts `## Plan` but does not otherwise parse is flagged,
    never silently accepted or ignored."""

    state = rec.get("gate_state", IDLE)
    if state not in STATES:
        # The registry is an Artifact humans can edit and old versions
        # wrote; a state outside the vocabulary must stop the wait, not
        # transition as if it were IDLE — guessing could fire an action
        # (label, retire) a second time.
        raise ValueError(
            f"registry record for {rec.get('ticket')}/{rec.get('role')} carries "
            f"unknown gate_state {state!r} (known: {sorted(STATES)}); fix the "
            f"Spawn Registry record and run `wait` again"
        )
    raw = raw_by_id.get(event.message_id)

    # Orca does not drop a refused lifecycle message: it rewrites the
    # subject/body and marks the payload with `_orcaLifecycleRejection`
    # (measured on 1.4.168). Without this branch that arrival reads as a
    # completion that never completed anything.
    rejection = (raw.payload if raw else {}).get("_orcaLifecycleRejection")
    if rejection:
        return [(
            FLAG_MALFORMED,
            f"Orca refused this {event.kind}: "
            f"{rejection.get('code')}: {rejection.get('reason')}",
        )]

    if event.kind == PLAN_QUESTION:
        if not is_plan_submission(event.question):
            return []
        try:
            submission = parse_plan_submission(event.question)
        except MalformedReport as exc:
            return [(FLAG_MALFORMED, str(exc))]
        if submission.ticket.upper() != str(rec.get("ticket", "")).upper():
            return [(
                FLAG_MALFORMED,
                f"plan submission is filed under {submission.ticket!r} but this "
                f"dispatch is {rec.get('ticket')!r}",
            )]
        result = transition(state, event, last_question_id=rec.get("last_question_id"))
        if result.question_id:
            rec["question_id"] = result.question_id
            rec["last_question_id"] = result.question_id
            # Kept in the same registry write as gate_state because
            # `spawn verdict` republishes this exact string on approval — a
            # sidecar file would let the two drift apart.
            rec["plan_body"] = event.question
            if result.state == SUBMITTED and state != SUBMITTED:
                # A round is a plan put in front of the human, counted here
                # so `spawn done` never asks the planner how many there
                # were. Re-asking the same pending question is not a round.
                rec["approval_rounds"] = int(rec.get("approval_rounds", 0)) + 1
    elif event.kind == WORKER_DONE:
        report_ok = True
        try:
            report = parse_planner_done(event.summary)
        except MalformedReport as exc:
            report_ok, report = False, None
            malformed_reason = str(exc)
        result = transition(state, event, report_ok=report_ok)
        if report is not None:
            rec["deviations"] = list(report.deviations)
        if not report_ok and FLAG_MALFORMED in result.actions:
            rec["gate_state"] = result.state
            rec["last_event_at"] = _now()
            return [(FLAG_MALFORMED, malformed_reason)]
    else:
        result = transition(state, event)

    rec["gate_state"] = result.state
    rec["last_event_at"] = _now()
    divergence = f"gate event {event.kind!r} did not match the recorded gate_state {state!r}"
    return [
        (action, divergence if action == FLAG_MALFORMED else None)
        for action in result.actions
    ]


def run(
    events,
    raw_by_id: dict,
    data: dict,
    *,
    tracker: Callable[[], Any],
    teardown: Callable[[str, str], dict],
    gate_role: str,
) -> tuple[list[dict], list[dict]]:
    """Attribute each event to the registry record it belongs to, apply
    `apply_gate_event`, and execute the actions it returns. Mutates `data`
    in place and returns `(actions_taken, unattributed)`; the caller reads
    and writes the registry, under its own lock.

    `tracker` is a zero-argument factory: a `wait` that observes no gate
    action (the common case) must keep working without `LINEAR_API_KEY`
    set, so the tracker is built at most once, only if an action needs it.
    A `worker_done` from any non-`gate_role` dispatch still ends that role,
    on both outcomes — it just carries no gate state to transition."""

    by_dispatch = {rec["dispatch"]: key for key, rec in data.items()}
    # Attribution falls back to the task id because a REFUSED lifecycle
    # message is exactly the one whose `dispatchId` may be missing; the
    # refusal keeps the rest of the payload, `taskId` included.
    by_task = {rec["task"]: key for key, rec in data.items()}
    actions_taken = []

    _tracker: list = []

    def resolved_tracker():
        if not _tracker:
            _tracker.append(tracker())
        return _tracker[0]

    unattributed = []
    for event in events:
        dispatch_id = getattr(event, "dispatch_id", None)
        key = by_dispatch.get(dispatch_id) or by_task.get(getattr(event, "task_id", None))
        if key is None:
            # Neither id maps to a spawn this package started: it completes
            # nothing and would otherwise vanish. Reported, never swallowed.
            unattributed.append({
                "message": event.message_id, "kind": event.kind,
                "dispatch": dispatch_id, "task": getattr(event, "task_id", None),
            })
            continue
        rec = data[key]
        ticket = rec["ticket"]
        if rec.get("role") != gate_role:
            if event.kind == WORKER_DONE and not rec.get("retired"):
                teardown(ticket, rec["role"])
                rec["retired"] = True
                actions_taken.append({"ticket": ticket, "action": RETIRE_ROLE})
            continue
        for action, reason in apply_gate_event(rec, event, raw_by_id):
            if action == LABEL_ON:
                resolved_tracker().set_gate(ticket, True)
            elif action == LABEL_OFF:
                resolved_tracker().set_gate(ticket, False)
            elif action == RETIRE_PLANNER:
                teardown(ticket, gate_role)
                # Set here as well as inside `teardown`: this loop only
                # promises its caller a mutated dict, not a teardown that
                # mutates — a test's injected fake may not.
                rec["retired"] = True
            elif action == FLAG_MALFORMED:
                resolved_tracker().set_attention(
                    ticket, AttentionCode.MALFORMED_REPORT.value,
                    reason or "malformed gate report",
                )
            actions_taken.append({"ticket": ticket, "action": action})
    return actions_taken, unattributed
