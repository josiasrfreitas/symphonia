"""Pidfile primitives for `spawn watch` — visibility, not exclusion.

TLDR: `write_pidfile`/`read_pidfile`/`remove_pidfile` persist and read
`RUNTIME_DIR/watch.pid`; `alive` answers whether a pid is a live process.
No lock lives here, and none should be added: the real guarantee against a
watcher and an interactive `wait` consuming the mailbox together is the
replay-safety `registry.transaction()` already gives every gate action
(guarded by `gate_state` and `last_question_id`) — a double-consumed batch
degrades to a harmless replay, never corrupted state (GRE-187, decision 1).
The pidfile only makes that state visible: a live watcher makes `wait`
refuse to start a second one. A second exclusion mechanism (a
`mailbox.lock`) is exactly what this ticket's write scope ruled out.
"""
from __future__ import annotations

import os
from pathlib import Path

PIDFILE = "watch.pid"


def write_pidfile(runtime_dir: Path | str, pid: int) -> None:
    """Atomic, same replace-a-tmp pattern as the registry and the journal
    receipt — a crash mid-write must never leave a half-written pidfile."""

    directory = Path(runtime_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / PIDFILE
    tmp = path.with_suffix(".pid.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(str(pid) + "\n")
    os.replace(tmp, path)


def read_pidfile(runtime_dir: Path | str) -> int | None:
    """The recorded pid, or `None` if no pidfile exists or it does not
    parse as one — a corrupt pidfile is not a live watcher."""

    path = Path(runtime_dir) / PIDFILE
    if not path.exists():
        return None
    text = path.read_text().strip()
    try:
        return int(text)
    except ValueError:
        return None


def alive(pid: int) -> bool:
    """Whether `pid` is a running process, via a signal-0 probe. A
    `PermissionError` means the process exists but is owned by someone
    else — still alive, from this check's point of view."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remove_pidfile(runtime_dir: Path | str) -> None:
    Path(runtime_dir, PIDFILE).unlink(missing_ok=True)
