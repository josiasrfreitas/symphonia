"""What the seven verbs of the map share: the shape of the map's body, the
shape of the lines they write into it, and the two orderings they read out
of it.

Not a verb — `map.discover()` skips a module whose name starts with `_`, so
this file is importable by the verbs beside it without ever becoming
`map _shared`.

Nothing here talks to a tracker. Every function is pure over the values
`linear` returns (`Item`, `Child`), which keeps the verbs' own tests offline
and keeps the formats testable one round-trip at a time.
"""
from __future__ import annotations

import re

from injection import INCOMPLETE, REFUSED, Refusal, Refused

# --- the map's body ---------------------------------------------------------

# The five section headings, in English and literal, because the body is
# written by the upstream `wayfinder` skill and these verbs only read and
# patch what it wrote. Translating them would produce a map the skill no
# longer recognises.
DESTINATION = "Destination"
NOTES = "Notes"
DECISIONS = "Decisions so far"
FOG = "Not yet specified"
"""The fog. Literally `## Not yet specified` — not "fog", not a
translation. SYM-12 (V3) declares the twin of this constant in its own
`intake.py`, with the same string, because the two verticals are open on
parallel branches and neither may import the other. Consolidating the two
is declared debt of the V4 (SYM-13)."""
OUT_OF_SCOPE = "Out of scope"

SECTIONS = (DESTINATION, NOTES, DECISIONS, FOG, OUT_OF_SCOPE)

MAP_LABEL = "wayfinder:map"
TICKET_TYPES = ("research", "prototype", "grilling", "task")

RESOLUTION_HEADING = "## Resolution"
"""The fixed heading of a resolution comment. The first non-empty line
after it is the direct answer — the thing a reader gets before any
reasoning."""

KEY = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b")
"""A ticket key as it appears anywhere in a line — in the link text, in the
URL, or in a line a human typed by hand."""

LOW_RES_LINES = 12
"""How many lines of a section `low_res` shows before it says how many it
cut. Low resolution is the point: `claim` hands an agent the map's shape,
not the map."""


def _outside_fences(lines: list[str]) -> list[bool]:
    """One flag per line: is it outside every fenced code block? Same rule
    as `linear._outside_fences` — a `## ` inside a fence is quoted text,
    not structure. Repeated here because `linear.patch_section` only
    exposes writing, and reading a section is what these verbs do most."""

    flags = []
    fence = ""
    for line in lines:
        opener = line.lstrip()[:3]
        if fence:
            flags.append(False)
            if opener == fence:
                fence = ""
        elif opener in ("```", "~~~"):
            flags.append(False)
            fence = opener
        else:
            flags.append(True)
    return flags


def section(body: str, heading: str) -> str | None:
    """The text under `## <heading>`, or `None` when the section is absent
    — which is not the same as present and empty, and the two lead to
    different refusals."""

    lines = body.splitlines()
    outside = _outside_fences(lines)
    marker = f"## {heading}"
    start = next(
        (i for i, line in enumerate(lines) if outside[i] and line.strip() == marker),
        None,
    )
    if start is None:
        return None
    end = next(
        (i for i in range(start + 1, len(lines)) if outside[i] and lines[i].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start + 1:end]).strip("\n")


def empty_section(body: str, heading: str) -> bool:
    """True when the section exists and holds nothing but whitespace. An
    absent section is not empty — it is missing, and `validate` says so."""

    text = section(body, heading)
    return text is not None and not text.strip()


def blank_map_body(destination: str, notes: str = "") -> str:
    """A new map's body: the five sections, three of them empty because
    nothing has been decided, fogged, or ruled out yet."""

    filled = {DESTINATION: destination.strip(), NOTES: notes.strip()}
    return "\n\n".join(
        f"## {heading}\n{filled.get(heading, '')}".rstrip() for heading in SECTIONS
    ) + "\n"


# --- the index line ---------------------------------------------------------


def index_line(key: str, title: str, url: str, gist: str) -> str:
    """One line of the decisions index. The key leads the link text so the
    line names its ticket even when the URL is elided by a reader."""

    return f"- [{key} — {title}]({url}) — {gist.strip()}"


def index_keys(text: str) -> set[str]:
    """Every ticket key mentioned in a section. Deliberately generous: it
    matches the key in a line this module wrote and in a line a human typed
    into `## Out of scope`, because both mean the same thing — that ticket
    is already accounted for in the body."""

    return set(KEY.findall(text or ""))


def append_index(index: str, line: str) -> str:
    """The decisions section with `line` appended, keeping what was there."""

    kept = (index or "").strip("\n")
    return f"{kept}\n{line}" if kept.strip() else line


# --- the resolution comment -------------------------------------------------


def resolution_comment(answer: str) -> str:
    """The comment `resolve` posts: the fixed heading, then the answer with
    its direct reply on the first line."""

    return f"{RESOLUTION_HEADING}\n\n{answer.strip()}\n"


def resolution_answer(body: str) -> str | None:
    """The direct answer out of a resolution comment, or `None` when the
    comment is not one — the heading is the whole of the test."""

    lines = (body or "").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == RESOLUTION_HEADING),
        None,
    )
    if start is None:
        return None
    return next((line.strip() for line in lines[start + 1:] if line.strip()), None)


# --- reading the frontier ---------------------------------------------------

PRIORITY_LEVELS = ("high", "medium", "low")
"""The neutral vocabulary, same three names `linear.PRIORITY` maps to
provider numbers. Named here so a verb can refuse a bad level without
importing `linear`, which a refusal must never need."""

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2, "": 3}
"""Unset sorts last, after `low`: a card nobody prioritised is not more
urgent than one somebody deliberately called low."""

CLOSED = ("completed", "canceled")
"""The provider's state categories that mean "no longer on the route"."""


def is_closed(child) -> bool:
    return child.state_type in CLOSED


def order_frontier(children) -> list:
    """Priority first, creation order as the tie-break. `sorted` is stable,
    so the fallback is simply the order `list_children` returned."""

    return sorted(children, key=lambda c: PRIORITY_ORDER.get(c.priority, 3))


def open_blockers(child, children) -> list[str]:
    """The keys still blocking `child`, plus every blocker whose state this
    map cannot see. A blocker that is not a child of this map is external:
    refusing too much beats refusing too little, so it counts as blocking
    and the text says why."""

    state = {c.key: c for c in children}
    return [
        key for key in child.blocked_by
        if key not in state or not is_closed(state[key])
    ]


def external_blockers(child, children) -> list[str]:
    keys = {c.key for c in children}
    return [key for key in child.blocked_by if key not in keys]


def takeable(children) -> list:
    """Open, unclaimed, and nothing left blocking it — the frontier."""

    return order_frontier([
        c for c in children
        if not is_closed(c) and not c.assignee and not open_blockers(c, children)
    ])


def held_back(children) -> list:
    """Everything the frontier does not hold, in the order it came."""

    frontier = {c.key for c in takeable(children)}
    return [c for c in children if c.key not in frontier]


def describe(child, children) -> str:
    """One line of state for a child that is not on the frontier."""

    if is_closed(child):
        return f"{child.key} — {child.title} — closed ({child.state})"
    marks = []
    if child.assignee:
        marks.append(f"claimed by {child.assignee}")
    blocking = open_blockers(child, children)
    if blocking:
        external = set(external_blockers(child, children))
        named = ", ".join(
            f"{key} (external to this map, state unknown)" if key in external else key
            for key in blocking
        )
        marks.append(f"blocked by {named}")
    if not marks:
        marks.append(f"open ({child.state})")
    return f"{child.key} — {child.title} — " + "; ".join(marks)


# --- the map in low resolution ----------------------------------------------


def _trimmed(text: str) -> str:
    lines = [line for line in (text or "").splitlines()]
    if len(lines) <= LOW_RES_LINES:
        return "\n".join(lines).strip() or "(empty)"
    cut = len(lines) - LOW_RES_LINES
    return "\n".join(lines[:LOW_RES_LINES] + [f"… {cut} more line(s) in the map itself"])


def low_res(item, children) -> str:
    """The map's shape without the map: the five sections trimmed, and how
    much frontier is left. What `claim` hands back beside the ticket body,
    so an agent picking up work knows where the route stands."""

    parts = [f"# Map {item.ref.key} — {item.title}", item.ref.url, ""]
    for heading in SECTIONS:
        text = section(item.body, heading)
        parts.append(f"## {heading}")
        parts.append("(section missing from the map body)" if text is None else _trimmed(text))
        parts.append("")
    frontier = takeable(children)
    open_count = len([c for c in children if not is_closed(c)])
    parts.append(
        f"Frontier: {len(frontier)} takeable now, "
        f"{open_count - len(frontier)} open but held back, "
        f"{len(children) - open_count} closed."
    )
    return "\n".join(parts)


# --- refusing ---------------------------------------------------------------


def refuse(blocked: str, accepted: str, example: str, kind: str):
    """One line at the call site instead of three. Always `raise`s."""

    raise Refused(Refusal(blocked=blocked, accepted=accepted, example=example, kind=kind))


# --- optional parameters ----------------------------------------------------
#
# `map.check` validates everything in `required` and nothing else, by the
# contract in `verbs/__init__.py`. An optional parameter is therefore the
# verb's own job, and it fails in exactly one way worth naming: `--gist`
# with nothing after it, which `map.parse` hands over as `True`.


def flag(params: dict, name: str) -> bool:
    """A bare `--flag`. Present with any value counts as set — a caller who
    wrote `--user-requested yes` meant yes."""

    return name in params


def optional(params: dict, name: str, *, example: str) -> str | None:
    """The value of an optional parameter, or `None` when it was not
    passed. Valueless is refused rather than read as `True`."""

    if name not in params:
        return None
    value = params[name]
    if not isinstance(value, str) or not value.strip():
        refuse(
            blocked=f"--{name} was given no value",
            accepted=f"--{name} takes a value, or leave it out entirely",
            example=example,
            kind=INCOMPLETE,
        )
    return value.strip()


def keys(value: str) -> list[str]:
    """`"SYM-1, SYM-2"` as a list. Empty entries are dropped, order and
    repetition are the caller's."""

    return [part.strip() for part in value.split(",") if part.strip()]
