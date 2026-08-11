"""The package's `.env` loader.

TLDR: ``load()`` fills ``os.environ`` from the first `.env` it finds, and
never overrides a variable the shell already set — an export on the command
line has to keep winning over a file, or debugging becomes guesswork.

Where it looks, in order:

1. ``SYMPHONIA_ENV``, if set — an explicit path always wins.
2. ``~/.symphonia/.env`` — the shared one, and the only one that reaches
   every role. This mirrors the registry decision in ``bin/spawn``: a role
   runs inside the ticket's worktree, which is a DIFFERENT checkout of this
   repository, so a file living in the Orchestrator's checkout is invisible
   to it.
3. ``<repo>/.env`` — the checkout this package sits in, for a developer
   running commands from the repo itself.

Format: ``KEY=value`` per line. Blank lines and ``#`` comments are skipped, a
leading ``export`` is tolerated, and one layer of matching quotes is
stripped. Nothing else — no interpolation, no multi-line values. A secret
file deserves a parser you can read in one sitting.
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
REPO = PACKAGE.parent

SHARED_ENV = Path("~/.symphonia/.env").expanduser()


def candidates() -> list[Path]:
    explicit = os.environ.get("SYMPHONIA_ENV")
    found = [Path(explicit).expanduser()] if explicit else []
    return found + [SHARED_ENV, REPO / ".env"]


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue  # not an assignment; ignore rather than guess
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load(path: Path | None = None) -> dict[str, str]:
    """Fill ``os.environ`` from the first `.env` found. Returns what it set
    (not what the file contained: an entry already present in the
    environment is left alone and is not reported as set)."""

    files = [path] if path is not None else candidates()
    for candidate in files:
        if candidate is None or not candidate.is_file():
            continue
        applied = {}
        for key, value in parse(candidate.read_text()).items():
            if key not in os.environ:
                os.environ[key] = value
                applied[key] = value
        return applied
    return {}
