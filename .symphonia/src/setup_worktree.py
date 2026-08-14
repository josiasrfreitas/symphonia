#!/usr/bin/env python3
"""What a fresh worktree needs before a role can work in it.

TLDR: copies the untracked env files from the main checkout into the new
worktree. They are gitignored, so a `git worktree add` never brings them —
the checkout looks complete and fails the first time something reads
`.env`.

Run by `spawn` right after it creates a worktree, so it happens whether or
not Orca's own setup hook is configured. Safe to run again: it never
overwrites a file that is already there, and it is not an error for a source
to be missing.

    .symphonia/bin/setup-worktree [<worktree path>]   # default: cwd

To have Orca run it for worktrees created by hand (Settings → the repo →
setup script):

    .symphonia/bin/setup-worktree

Note what this does NOT solve: `LINEAR_API_KEY` already reaches every
worktree through `~/.symphonia/.env` (see `env.py`), by design —
a role reads it from a different checkout. The copy is for everything else
that reads a `.env` from the directory it runs in.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# Untracked by policy, needed by whatever runs in the worktree. Extend here,
# not at the call site: the list is the contract.
ENV_FILES = (".env", ".env.local")

SHARED_ENV = Path("~/.symphonia/.env").expanduser()


def main_checkout(worktree: Path) -> Path | None:
    """The repository this worktree was cut from.

    `--git-common-dir` points at the ORIGINAL checkout's `.git` even from
    inside a linked worktree, which is exactly the question being asked. In
    the main checkout itself it answers `.git` relatively, so it is resolved
    against the worktree before taking the parent.
    """

    proc = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True,
    )
    common = proc.stdout.strip()
    if not common:
        return None
    path = Path(common)
    if not path.is_absolute():
        path = (worktree / path).resolve()
    source = path.parent
    return None if source == worktree.resolve() else source


def setup(worktree: Path) -> dict:
    worktree = worktree.resolve()
    if not worktree.is_dir():
        raise SystemExit(f"{worktree} is not a directory")

    source = main_checkout(worktree)
    copied, skipped, missing = [], [], []

    for name in ENV_FILES:
        target = worktree / name
        if target.exists():
            skipped.append(name)  # never clobber what is already there
            continue
        candidates = [source / name] if source else []
        if name == ".env":
            # The shared one is the fallback: a machine may keep its secrets
            # outside every checkout, in which case the main checkout has no
            # `.env` to copy.
            candidates.append(SHARED_ENV)
        origin = next((c for c in candidates if c.is_file()), None)
        if origin is None:
            missing.append(name)
            continue
        shutil.copy2(origin, target)
        target.chmod(0o600)
        copied.append({"file": name, "from": str(origin)})

    return {
        "worktree": str(worktree),
        "source": str(source) if source else None,
        "copied": copied,
        "kept": skipped,
        "not_found": missing,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="setup-worktree", description=__doc__)
    parser.add_argument("worktree", nargs="?", default=".")
    args = parser.parse_args(argv)
    print(json.dumps(setup(Path(args.worktree)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
