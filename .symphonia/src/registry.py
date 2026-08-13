"""The Spawn Registry: every Role Context this package started — who runs
where, under which Attempt — one JSON file deliberately OUTSIDE every
checkout, because the Orchestrator and the role read it from two different
copies of this repository.

TLDR: `read()` for a lock-free snapshot, `transaction()` for every write.
Writers are all Orchestrator-side (`spawn`, `wait`, `verdict`, `retire`,
`sweep`); the role-side verbs (`submit`, `done`) only ever read. The record
shape is the contract between `spawn.py` (which writes records) and
`gate.py` (which judges them); the key is `"{TICKET}/{role}"`, built by
`key()` and never parsed back — a record already carries `ticket` and
`role` as fields.

Paths resolve on every call, never at import: `SYMPHONIA_RUNTIME` must
work when exported by the process running the verb, not the one that
happened to import the module first.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
from typing import Iterator


def runtime_dir() -> Path:
    return Path(os.environ.get("SYMPHONIA_RUNTIME", "~/.symphonia/runtime")).expanduser()


def key(ticket: str, role_value: str) -> str:
    return f"{ticket.upper()}/{role_value}"


def _state() -> Path:
    return runtime_dir() / "spawns.json"


def _prepare_dir(state: Path) -> None:
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state.parent, 0o700)  # a directory that already existed


def read() -> dict:
    """A snapshot without the lock — for observation (`status`) and for
    the role-side verbs, which never write."""

    state = _state()
    if state.exists():
        return json.loads(state.read_text())
    return {}


def _write(data: dict) -> None:
    # Atomic, and 0600 from the moment the bytes exist — the registry
    # holds Dispatch capability tokens, and a token is what authorizes a
    # `worker_done` on someone else's dispatch.
    state = _state()
    _prepare_dir(state)
    tmp = state.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, state)


def commit(data: dict) -> None:
    """A durable write in the middle of a `transaction()` — for the one
    caller (`verdict`) that must persist state BEFORE an external effect
    fires. Never call this outside a transaction: alone it is exactly the
    unlocked read-modify-write the transaction exists to prevent."""

    _write(data)


@contextlib.contextmanager
def transaction() -> Iterator[dict]:
    """Every write goes through here: an exclusive flock, the current
    records as a dict to mutate, one atomic write on a clean exit — and NO
    write when the block raises, so a half-applied batch is replayed
    rather than half-recorded.

    flock does not nest. Anything called from inside a transaction takes
    the open dict as an argument (`teardown(..., data=...)`) instead of
    opening its own. Hold this only around the read-modify-write itself,
    never around a blocking `check --wait`."""

    state = _state()
    _prepare_dir(state)
    # The lock is a sibling, not the state file itself: `_write` swaps the
    # inode via `os.replace`, so an flock on the file being replaced
    # protects nothing.
    lock = state.with_name(state.name + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = read()
        yield data
        _write(data)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
