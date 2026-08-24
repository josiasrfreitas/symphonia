#!/usr/bin/env python3
"""The gate of the intake — the pure half, with no tracker and no network.

TLDR: one module defines what a briefing is and what an intake handoff is,
because both are checked against the *same* field. `verbs/brief.py` calls
it before creating the construction card; `main()` calls it before writing
a handoff to disk. Nothing here reaches Linear or the filesystem except
`write_handoff`, and that one validates first.

The design (SYM-8, comment of 2026-08-19) puts one door between the two
halves of the flow: the initial phase ends by writing a briefing, and the
construction phase may not begin until that briefing carries the door
record and at least one checkable success criterion. A script cannot judge
prose, so "checkable" is a shape, not an opinion — see `CRITERION_PREFIX`.

Run the handoff validator directly; the file is its own entrypoint and
needs no `sys.path` bootstrap, because Python puts the script's directory
first:

    python3 .symphonia/src/intake.py handoff --ticket SYM-8 --file draft.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from injection import INCOMPLETE, REFUSED, Refusal, Refused, render

PACKAGE = Path(__file__).resolve().parents[1]  # .symphonia

# --- what the initial phase must have decided -------------------------------

DOOR_HEADING = "## Registro da porta"
"""The section every briefing and every intake handoff carries."""

DOORS = ("pequena", "meio", "grande")
"""The three doors of the intake (SYM-8). A fourth value is a typo, not a
new door, so it is refused instead of passed through."""

DOOR_FIELD = "- **Porta:**"
FACT_FIELD = "- **Fato:**"

CRITERIA_HEADING = "## Critérios de sucesso"

CRITERION_PREFIX = "Está pronto quando "
"""What makes a criterion checkable *by a script*: the fixed opening this
project's own cards already use. A keyword heuristic would be model
judgment wearing a script's clothes — this is a shape, and a shape either
matches or does not."""

FOG_HEADING = "## Not yet specified"
"""The fog section of a Decision Map body.

Public on purpose, and duplicated on purpose. The map body is written by
the `wayfinder` skill, which fixes five literal English section titles
(`## Destination`, `## Notes`, `## Decisions so far`, `## Not yet
specified`, `## Out of scope`) — the Portuguese names in the SYM-8 design
are descriptive prose, not the contract. SYM-11 declares the same string
as `verbs/_shared.FOG`: the two verticals are open in parallel, so neither
imports the other's literal. Consolidating them is declared debt of V4
(SYM-13).
"""

HANDOFF_HEADER = "# Handoff — intake {ticket}"
"""First line of an intake handoff. Distinct from the role handoff that
`spawn` already moves — same directory, different file, different shape."""

HANDOFF_EXAMPLE = "python3 .symphonia/src/intake.py handoff --ticket SYM-8 --file draft.md"


# --- reading sections -------------------------------------------------------


def section(text: str, heading: str) -> list[str]:
    """The lines under `heading`, up to the next `## ` heading. Empty when
    the heading is absent — an absent section and an empty one are the
    same thing to every caller here, and both get refused by name."""

    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if inside:
                break
            inside = stripped == heading
            continue
        if inside:
            lines.append(line)
    return lines


def _field(lines: list[str], label: str) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(label):
            return stripped[len(label):].strip()
    return ""


def door_record(text: str) -> tuple[str, str]:
    """The door and the fact that chose it, as `(porta, fato)`.

    Both fields are required: the door alone says which path was taken and
    the fact alone says what was found — only together do they say why the
    path was taken, which is the whole point of recording it."""

    lines = section(text, DOOR_HEADING)
    if not lines:
        raise Refused(Refusal(
            blocked=f"there is no {DOOR_HEADING!r} section",
            accepted=(f"a {DOOR_HEADING} section with two fields: "
                      f"{DOOR_FIELD} {'|'.join(DOORS)}, and {FACT_FIELD} <what made it that door>"),
            example=(f"{DOOR_HEADING} / {DOOR_FIELD} meio / "
                     f"{FACT_FIELD} o repo já tem o módulo, falta o verbo"),
            kind=INCOMPLETE,
        ))
    door, fact = _field(lines, DOOR_FIELD), _field(lines, FACT_FIELD)
    missing = [name for name, value in ((DOOR_FIELD, door), (FACT_FIELD, fact)) if not value]
    if missing:
        raise Refused(Refusal(
            blocked=f"{DOOR_HEADING} is missing: {', '.join(missing)}",
            accepted="both fields, each with a value on its own line",
            example=f"{DOOR_FIELD} meio / {FACT_FIELD} o repo já tem o módulo, falta o verbo",
            kind=INCOMPLETE,
        ))
    if door not in DOORS:
        raise Refused(Refusal(
            blocked=f"{door!r} is not one of the doors of the intake",
            accepted="one of: " + ", ".join(DOORS),
            example=f"{DOOR_FIELD} meio",
            kind=REFUSED,
        ))
    return door, fact


def checkable_criteria(text: str) -> list[str]:
    """Every success criterion a script can check, in the order written.

    Checkable means it opens with `CRITERION_PREFIX` and says something
    after it. A criterion that reads "Está pronto quando " and stops is
    not a criterion."""

    found = []
    for line in section(text, CRITERIA_HEADING):
        stripped = line.strip().lstrip("-*").strip()
        if stripped.startswith(CRITERION_PREFIX) and stripped[len(CRITERION_PREFIX):].strip():
            found.append(stripped)
    return found


def fog_items(body: str) -> list[str]:
    """The items still in the fog of a Decision Map body: the bullets
    under `FOG_HEADING`. Comments and blank lines are not items — the
    skill's own template leaves an HTML comment in the empty section, so
    counting it would make every fresh map look unfinished."""

    items = []
    for line in section(body, FOG_HEADING):
        stripped = line.strip()
        if stripped.startswith("<!--") or not stripped:
            continue
        if stripped.startswith(("- ", "* ")):
            items.append(stripped[2:].strip())
    return items


# --- the two documents ------------------------------------------------------


def title_of(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def validate_briefing(text: str) -> tuple[str, str, str]:
    """`(title, porta, fato)` for a briefing that may become a card.

    Refuses when the title, the door record, or a checkable criterion is
    missing — the three things the construction phase cannot start
    without."""

    title = title_of(text)
    if not title:
        raise Refused(Refusal(
            blocked="the briefing has no title",
            accepted="a first-level heading; its text becomes the card's title",
            example="# Portaria do intake: recomeço em brownfield",
            kind=INCOMPLETE,
        ))
    door, fact = door_record(text)
    if not checkable_criteria(text):
        raise Refused(Refusal(
            blocked=(f"{CRITERIA_HEADING} has no criterion a script can check"),
            accepted=(f"at least one item under {CRITERIA_HEADING} opening with "
                      f"{CRITERION_PREFIX!r} and saying what is true when it is done"),
            example=(f"{CRITERIA_HEADING} / - {CRITERION_PREFIX}"
                     "`map brief` recusa um briefing sem registro da porta"),
            kind=INCOMPLETE,
        ))
    return title, door, fact


def validate_handoff(text: str, ticket: str) -> tuple[str, str]:
    """The frame of an intake handoff: the fixed header line, then the
    same door record a briefing carries. The prose between them stays the
    agent's — the frame is what a script can hold it to."""

    header = HANDOFF_HEADER.format(ticket=ticket)
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first != header:
        raise Refused(Refusal(
            blocked=f"the handoff does not open with {header!r}",
            accepted="that exact first-level heading, with this ticket's key",
            example=header,
            kind=INCOMPLETE,
        ))
    return door_record(text)


def handoff_path(ticket: str, *, handoff_dir: str | Path) -> Path:
    """`{handoff_dir}/{ticket_lower}-intake.md` — beside the role handoff
    `spawn` writes at `{ticket_lower}.md`, never on top of it."""

    return Path(str(handoff_dir)).expanduser() / f"{ticket.lower()}-intake.md"


def write_handoff(ticket: str, text: str, *, handoff_dir: str | Path) -> Path:
    """Validate, then write. In that order, and that is the whole point:
    a handoff missing the door record is refused at the keystroke that
    would create it, not discovered at the end of the chain by the role
    that inherited it."""

    validate_handoff(text, ticket)
    path = handoff_path(ticket, handoff_dir=handoff_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- entrypoint -------------------------------------------------------------


def _handoff_dir() -> str:
    config = json.loads((PACKAGE / "config.json").read_text())
    return config.get("handoff_dir", "~/orca/.context")


def read_document(path: str, *, what: str, example: str) -> str:
    """A document off disk, or a refusal. A missing file and an empty one
    are caller error, not a stack trace to read."""

    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as err:
        raise Refused(Refusal(
            blocked=f"cannot read the {what} at {path!r}: {err.strerror or err}",
            accepted="a path to a readable UTF-8 markdown file",
            example=example,
            kind=REFUSED,
        )) from err
    if not text.strip():
        raise Refused(Refusal(
            blocked=f"the {what} at {path!r} is empty",
            accepted=f"a {what} with a title and a {DOOR_HEADING} section",
            example=example,
            kind=INCOMPLETE,
        ))
    return text


def main(argv: list[str] | None = None, *, handoff_dir: str | Path | None = None) -> int:
    """`intake handoff --ticket SYM-8 --file draft.md`. Same refusal text
    and same exit code as `map.main`, because an agent reading one must
    not have to learn a second format.

    `map.parse` is imported here and not at module scope: `verbs/brief.py`
    imports this module and `map` imports the verbs package, so a
    top-level import would close the circle."""

    from map import REFUSED_EXIT, parse  # late: see the docstring

    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        verb, params = parse(argv)
        if verb != "handoff":
            raise Refused(Refusal(
                blocked=f"{verb!r} is not a verb of the intake validator" if verb
                        else "no verb was given",
                accepted="the only verb is: handoff",
                example=HANDOFF_EXAMPLE,
                kind=REFUSED if verb else INCOMPLETE,
            ))
        missing = [name for name in ("ticket", "file")
                   if not isinstance(params.get(name), str) or not params[name].strip()]
        if missing:
            raise Refused(Refusal(
                blocked="handoff was called without a value for: "
                        + ", ".join(f"--{name}" for name in missing),
                accepted="every required parameter in the same call, each with a value: "
                         "--ticket <key>, --file <path>",
                example=HANDOFF_EXAMPLE,
                kind=INCOMPLETE,
            ))
        ticket = params["ticket"].strip()
        text = read_document(params["file"], what="handoff", example=HANDOFF_EXAMPLE)
        path = write_handoff(
            ticket, text,
            handoff_dir=handoff_dir if handoff_dir is not None else _handoff_dir(),
        )
        print(f"Handoff do intake escrito: {path}")
        return 0
    except Refused as refusal:
        print(render(refusal.refusal), file=sys.stderr)
        return REFUSED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
