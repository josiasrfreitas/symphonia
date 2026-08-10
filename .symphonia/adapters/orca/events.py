"""Typed gate events from the Orca orchestration mailbox.

TLDR: a pure function turns the JSON of ``orca orchestration check --peek
--json`` into typed gate events — plan question, approval reply,
worker_done — so gate recording is a script reading fields, never a model
reading a screen. ``record_gate`` (GRE-174) and the small piece in
``.symphonia/hooks/`` consume ``gate_events``; the hook itself is outside
this ticket.

Run standalone to print gate events as JSON lines:

    python3 events.py --terminal <handle>
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Union


@dataclass(frozen=True)
class OrchestrationMessage:
    """One mailbox message, field-for-field. The neutral layer both the
    adapter's ``drain`` and the gate reader are built on."""

    id: str
    type: str
    subject: str
    body: str
    payload: dict
    thread_id: str = ""
    sender: str = ""


@dataclass(frozen=True)
class CheckBatch:
    delivery_id: str
    messages: tuple[OrchestrationMessage, ...]


def _first(record: dict, *keys: str, default: str = "") -> Any:
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return default


def _payload_of(record: dict) -> dict:
    raw = _first(record, "payload", default=None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def parse_check_output(text: str) -> CheckBatch:
    """Parse ``check --peek --json`` output. Tolerates the two shapes the
    CLI emits: a bare message list, or ``{deliveryId, messages}``."""

    data = json.loads(text) if text.strip() else []
    if isinstance(data, dict):
        delivery = str(_first(data, "deliveryId", "delivery_id", "id"))
        raw_messages = _first(data, "messages", default=[])
    else:
        delivery, raw_messages = "", data
    messages = tuple(
        OrchestrationMessage(
            id=str(_first(m, "id", "messageId", "message_id")),
            type=str(_first(m, "type")),
            subject=str(_first(m, "subject")),
            body=str(_first(m, "body")),
            payload=_payload_of(m),
            thread_id=str(_first(m, "threadId", "thread_id")),
            sender=str(_first(m, "from", "sender")),
        )
        for m in raw_messages
        if isinstance(m, dict)
    )
    return CheckBatch(delivery_id=delivery, messages=messages)


# --- the three gate events ---------------------------------------------------


@dataclass(frozen=True)
class PlanQuestion:
    """A worker asked for plan approval (``ask`` / ``--type question``)."""

    kind: str
    message_id: str
    task_id: str
    dispatch_id: str
    question: str


@dataclass(frozen=True)
class ApprovalReply:
    """The coordinator's reply that resolves a pending question."""

    kind: str
    message_id: str
    thread_id: str
    body: str


@dataclass(frozen=True)
class WorkerDone:
    """A worker reported its terminal outcome."""

    kind: str
    message_id: str
    task_id: str
    dispatch_id: str
    outcome: str
    summary: str


GateEvent = Union[PlanQuestion, ApprovalReply, WorkerDone]


def gate_events(messages: tuple[OrchestrationMessage, ...]) -> tuple[GateEvent, ...]:
    """Map mailbox messages to gate events by their declared ``type`` field
    — never by reading prose. Non-gate types are skipped."""

    out: list[GateEvent] = []
    for m in messages:
        task_id = str(_first(m.payload, "taskId", "task_id"))
        dispatch_id = str(_first(m.payload, "dispatchId", "dispatch_id"))
        if m.type == "question":
            out.append(PlanQuestion("plan-question", m.id, task_id, dispatch_id, m.body))
        elif m.type == "reply":
            out.append(ApprovalReply("approval-reply", m.id, m.thread_id, m.body))
        elif m.type == "worker_done":
            outcome = str(_first(m.payload, "outcome"))
            out.append(WorkerDone("worker-done", m.id, task_id, dispatch_id, outcome, m.body))
    return tuple(out)


def main(argv: list[str]) -> int:
    import subprocess

    if len(argv) != 2 or argv[0] != "--terminal":
        print("usage: events.py --terminal <handle>")
        return 2
    proc = subprocess.run(
        ["orca", "orchestration", "check", "--terminal", argv[1], "--peek", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    for event in gate_events(parse_check_output(proc.stdout).messages):
        print(json.dumps(event.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
