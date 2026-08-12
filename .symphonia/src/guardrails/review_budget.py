#!/usr/bin/env python3
"""Review Budget guardrail.

Measures changed lines of a ticket's diff against `review_budget_lines` in
`.symphonia/config.json` (default 400) and reports a verdict — never a
waiver. Counting rules are closed by GRE-156, not decided here:

- `git diff -M --numstat <base>...<head>` (no `-w`; whitespace counts;
  rename detected explicitly; an intact rename counts 0).
- Binaries count 0 but are listed.
- Paths marked `linguist-generated` or `symphonia-budget-exempt` in
  `.gitattributes` are excluded and listed — read via `git check-attr`,
  never inferred.
- Overflow only flips `verdict`/exit code. A human approves a split or
  records a waiver by editing `.gitattributes`; this script never does it.

Base resolution: `--base <commit>` always wins. Without it, `--ticket
<KEY>` reads `head_first_dispatch` from the `<KEY>/implementer` record in
`$SYMPHONIA_RUNTIME/spawns.json` (default `~/.symphonia/runtime`) — the
commit at the ticket's *first* dispatch, write-once per ticket. That field
does not exist yet (owned by GRE-188); until it is written, `--ticket`
exits 2 naming the missing field. `head_at_dispatch` — the per-*round*
baseline `spawn.py` overwrites on every re-dispatch — is FORBIDDEN as a
base here, in code and in this docstring: it silently absolves overflow
whenever a ticket had a correction round, which is the common case, not
the rare one.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]  # .symphonia
ATTRS = ("linguist-generated", "symphonia-budget-exempt")


def _fail(message: str) -> "SystemExit":
    print(f"review_budget: {message}", file=sys.stderr)
    return SystemExit(2)


def default_budget() -> int:
    config = json.loads((PACKAGE / "config.json").read_text())
    return int(config.get("review_budget_lines", 400))


def resolve_base(ticket: str, runtime_dir: Path) -> str:
    """The commit at the ticket's first dispatch — never the current
    round's baseline. See the module docstring for why."""

    state_path = runtime_dir / "spawns.json"
    if not state_path.exists():
        raise _fail(f"no registry at {state_path}; pass --base explicitly")
    data = json.loads(state_path.read_text())
    key = f"{ticket}/implementer"
    record = data.get(key)
    if record is None:
        raise _fail(f"no record for {key!r} in {state_path}; pass --base explicitly")
    base = record.get("head_first_dispatch")
    if not base:
        raise _fail(
            f"{key!r} has no head_first_dispatch recorded (field owned by "
            "GRE-188, not yet written) — pass --base explicitly"
        )
    return base


def _run_git(repo: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise _fail(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def _numstat_entries(repo: str, base: str, head: str) -> list[dict]:
    out = _run_git(repo, "diff", "-M", "--numstat", "-z", f"{base}...{head}")
    tokens = out.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    entries = []
    i = 0
    while i < len(tokens):
        added_s, deleted_s, path_field = tokens[i].split("\t", 2)
        if path_field == "":
            path, i = tokens[i + 2], i + 3
        else:
            path, i = path_field, i + 1
        binary = added_s == "-" or deleted_s == "-"
        added = 0 if binary else int(added_s)
        deleted = 0 if binary else int(deleted_s)
        entries.append({"path": path, "added": added, "deleted": deleted, "binary": binary})
    return entries


def _check_attrs(repo: str, paths: list[str]) -> dict[str, dict[str, str]]:
    if not paths:
        return {}
    stdin_data = "".join(p + "\0" for p in paths)
    proc = subprocess.run(
        ["git", "-C", repo, "check-attr", "--stdin", "-z", *ATTRS],
        input=stdin_data, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise _fail(proc.stderr.strip() or "git check-attr failed")
    tokens = proc.stdout.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    result: dict[str, dict[str, str]] = {p: {} for p in paths}
    for j in range(0, len(tokens), 3):
        path, attr, value = tokens[j], tokens[j + 1], tokens[j + 2]
        result[path][attr] = value
    return result


def _excluded(value: str) -> bool:
    """"set" or any custom truthy value excludes; unspecified/unset/false
    do not — the exact reading GRE-156 closed, no model judgment."""

    return value not in ("unspecified", "unset", "false")


def measure(repo: str, base: str, head: str) -> dict:
    entries = _numstat_entries(repo, base, head)
    attrs = _check_attrs(repo, [e["path"] for e in entries if not e["binary"]])

    binaries, excluded_generated, excluded_exempt, files = [], [], [], []
    total = 0
    for e in entries:
        if e["binary"]:
            binaries.append(e["path"])
            continue
        seen = attrs.get(e["path"], {})
        if _excluded(seen.get("linguist-generated", "unspecified")):
            excluded_generated.append(e["path"])
            continue
        if _excluded(seen.get("symphonia-budget-exempt", "unspecified")):
            excluded_exempt.append(e["path"])
            continue
        files.append({"path": e["path"], "added": e["added"], "deleted": e["deleted"]})
        total += e["added"] + e["deleted"]

    return {
        "binaries": binaries,
        "excluded_generated": excluded_generated,
        "excluded_exempt": excluded_exempt,
        "files": files,
        "total": total,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Review budget guardrail")
    parser.add_argument("--base", help="base commit; wins over --ticket")
    parser.add_argument("--ticket", help="ticket key; reads head_first_dispatch")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--budget", type=int, default=None)
    args = parser.parse_args(argv)

    if not args.base and not args.ticket:
        raise _fail("pass --base <commit> or --ticket <KEY>")

    if args.base:
        base = args.base
    else:
        runtime_dir = Path(os.environ.get("SYMPHONIA_RUNTIME", "~/.symphonia/runtime")).expanduser()
        base = resolve_base(args.ticket, runtime_dir)

    budget = args.budget if args.budget is not None else default_budget()
    result = measure(args.repo, base, args.head)
    verdict = "within" if result["total"] <= budget else "over"

    output = {
        "base": base,
        "head": args.head,
        "budget": budget,
        "verdict": verdict,
        **result,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if verdict == "within" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
