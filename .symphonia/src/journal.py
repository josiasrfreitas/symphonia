"""Persists what `wait` consumes, so the mailbox stops being the only place
the Delivery lives.

TLDR: two pure, parametrized-by-directory pieces of state. `events.jsonl` is
an append-only record of every message a `wait` call has ever seen — the
Orchestrator's way to read a plan body, an escalation or a question after
the fact, without re-fetching from the mailbox. `delivery.json` is the
receipt for the one Delivery still unacked, so a `wait` that reopens after
the terminal died can auto-ack instead of re-delivering everything.

Neither function takes a lock: `append_events` is called from inside the
registry transaction `spawn.wait` holds, and `write_receipt` right after it
commits — the receipt only marks a delivery acked once the outcome it acks
is durable. A second exclusion mechanism here is exactly what GRE-187's
write scope ruled out — this module composes with the transaction that
exists, it does not invent one.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

EVENTS_FILE = "events.jsonl"
RECEIPT_FILE = "delivery.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_events(runtime_dir: Path | str, delivery_id: str, messages: Sequence[dict]) -> None:
    """Appends one JSON line per message — `{received_at, delivery_id, id,
    type, subject, body, payload, sender}` — in a single append `write()`
    for the whole batch. An empty batch writes nothing: an empty-and-instant
    `check --wait` return (the timeout anomaly measured in onda 9-10) must
    not leave a phantom line in the journal."""

    if not messages:
        return
    directory = Path(runtime_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    received_at = _now()
    lines = "".join(
        json.dumps({"received_at": received_at, "delivery_id": delivery_id, **message}) + "\n"
        for message in messages
    )
    path = directory / EVENTS_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(lines)


def read_events(runtime_dir: Path | str, limit: int) -> list[dict]:
    """The last `limit` journaled events, oldest first — `wait`'s only read
    path while a watcher is alive (GRE-187 stage B): the watcher already
    owns the mailbox, so this is a plain tail of what it already appended,
    never a re-check. No cursor, by design: every call re-reads the tail of
    the file, so there is no cursor file to lose or let drift from what was
    actually shown.

    `append_events` writes a batch in one `write()`, but that write is not
    atomic against a concurrent reader — a line caught mid-write does not
    parse. With a live watcher this is the only path `wait` has, so one bad
    line must not fail the whole read: it is skipped, not raised."""

    path = Path(runtime_dir) / EVENTS_FILE
    if not path.exists() or limit <= 0:
        return []
    lines = path.read_text().splitlines()[-limit:]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def read_receipt(runtime_dir: Path | str) -> str | None:
    """The pending Delivery id, or None if there isn't one — read from
    disk, never from a stdout a terminal may have lost."""

    path = Path(runtime_dir) / RECEIPT_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    delivery_id = data.get("delivery_id")
    return delivery_id or None


def write_receipt(runtime_dir: Path | str, delivery_id: str) -> None:
    """Records the Delivery still open, atomically (same replace-a-tmp
    pattern as the registry write, so a crash mid-write never leaves a
    half-written receipt). An empty `delivery_id` — the batch that carried
    nothing to ack — removes the receipt instead of writing one."""

    directory = Path(runtime_dir)
    path = directory / RECEIPT_FILE
    if not delivery_id:
        path.unlink(missing_ok=True)
        return
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps({"delivery_id": delivery_id, "received_at": _now()}) + "\n")
    os.replace(tmp, path)
