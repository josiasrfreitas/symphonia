#!/usr/bin/env python3
"""Symphonia workflow installer.

TLDR: installs the thin skill pointers for Claude Code and Codex, creates
AGENTS.md and the CLAUDE.md link, and reports third-party dependencies.
Idempotent: every operation writes a desired state, so running it twice
produces the same result. `--check` reports differences and writes nothing.

Run from the target repo root (the repo that contains `.symphonia/`):

    python3 install.py           # install / update
    python3 install.py --check   # dry run

Stdlib only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SKILLS = ("orchestrate", "wayfinder", "handoff")
THIRD_PARTY = ("grilling", "code-review", "tdd")

AGENTS_MD = """# Agents

This repository uses the Symphonia workflow. Start at
[.symphonia/README.md](.symphonia/README.md).

Entry point: the `/orchestrate` skill.
"""

CLAUDE_MD = "See [AGENTS.md](AGENTS.md).\n"


def ensure_file(path: Path, content: str, check: bool, report: list[str]) -> None:
    """Write `content` to `path` only if it differs. The whole installer is
    built on this one primitive, which is what makes it idempotent."""
    current = path.read_text() if path.exists() else None
    if current == content:
        report.append(f"ok        {path}")
        return
    verb = "would write" if check else "write"
    report.append(f"{verb:9} {path}")
    if not check:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def install(root: Path, check: bool) -> int:
    report: list[str] = []
    pkg = root / ".symphonia"
    if not pkg.is_dir():
        print(f"error: {pkg} not found — run from the repo root", file=sys.stderr)
        return 1

    # Skills: thin pointers per harness, content stays in .symphonia/skills/.
    for name in SKILLS:
        source = pkg / "skills" / name / "SKILL.md"
        if not source.is_file():
            print(f"error: missing skill source {source}", file=sys.stderr)
            return 1
        body = source.read_text()
        ensure_file(root / ".claude" / "skills" / name / "SKILL.md", body, check, report)
        ensure_file(root / ".codex" / "prompts" / f"{name}.md", body, check, report)

    # AGENTS.md: create if absent; never overwrite an existing one.
    agents = root / "AGENTS.md"
    if agents.exists():
        report.append(f"kept      {agents} (exists; not overwritten)")
    else:
        ensure_file(agents, AGENTS_MD, check, report)

    # CLAUDE.md is always only a link to AGENTS.md. Create or normalize when
    # absent or already the link; warn (and leave alone) if it has content.
    claude = root / "CLAUDE.md"
    if claude.exists() and claude.read_text() not in ("", CLAUDE_MD):
        report.append(f"WARNING   {claude} has its own content; move it to AGENTS.md "
                      "and rerun — CLAUDE.md must be only a link")
    else:
        ensure_file(claude, CLAUDE_MD, check, report)

    # Third-party dependencies: declared and checked, never installed.
    missing = []
    for dep in THIRD_PARTY:
        candidates = (
            root / ".claude" / "skills" / dep,
            Path.home() / ".claude" / "skills" / dep,
        )
        if any(c.is_dir() for c in candidates):
            report.append(f"dep ok    {dep}")
        else:
            missing.append(dep)
            report.append(f"dep MISSING  {dep}")

    print("\n".join(report))
    if missing:
        print(f"\nInstall the missing third-party skills yourself: {', '.join(missing)}"
              "\n(see .symphonia/DEPENDENCIES.md)")
    print("\ncheck complete — nothing written" if check else "\ninstall complete")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="report differences without writing")
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="target repo root (default: cwd)")
    args = parser.parse_args()
    return install(args.root.resolve(), args.check)


if __name__ == "__main__":
    sys.exit(main())
