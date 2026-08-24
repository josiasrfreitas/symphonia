"""`map validate` — is the map finished, and is it well formed?

Two questions, answered separately because they fail for different reasons.

The verdict: a map is at its destination when the frontier is empty *and*
`## Not yet specified` is empty. Either one alone is not the end — an empty
frontier with fog left means the route stopped short of what nobody has
decided yet, and empty fog with a live frontier means the work is known and
simply not done. The answer names which of the two is not met.

The lint: everything about the map's *form* that a script can see and a
reader cannot — a closed ticket with no line in the decisions index, an
index line pointing at a card this map does not own, a closed ticket whose
resolution comment is missing or wears the wrong heading, a section the
body has lost. It lists; it never raises. A malformed map is still a map,
and refusing to describe it would leave the reader with nothing to fix.

A ticket ruled out under `## Out of scope` counts as accounted for: leaving
scope is an act of scope, not a step on the route, so its line lives there
and not in the decisions index.
"""
from __future__ import annotations

from verbs import _shared as S

SPEC = {
    "name": "validate",
    "help": "say whether the map is finished, and list what is malformed",
    "required": ("map",),
    "example": "map validate --map SYM-8",
}


def _verdict(fog: str | None, frontier) -> list[str]:
    unmet = []
    if frontier:
        unmet.append(
            f"the frontier still holds {len(frontier)} takeable ticket(s): "
            + ", ".join(c.key for c in frontier)
        )
    if fog is None:
        unmet.append(f"the body has no “{S.FOG}” section, so the fog cannot be read")
    elif fog.strip():
        unmet.append(f"“{S.FOG}” is not empty: {len(fog.strip().splitlines())} line(s) of fog")
    if unmet:
        return ["Not at the destination:"] + [f"- {reason}" for reason in unmet]
    return [
        "At the destination: the frontier is empty and "
        f"“{S.FOG}” holds nothing. Every step has been walked and nothing is "
        "left unspecified."
    ]


def _lint(body: str, children, comments_of) -> list[str]:
    pending = []

    for heading in S.SECTIONS:
        if S.section(body, heading) is None:
            pending.append(f"the body has no “{heading}” section")

    index = S.section(body, S.DECISIONS) or ""
    indexed = S.index_keys(index)
    ruled_out = S.index_keys(S.section(body, S.OUT_OF_SCOPE) or "")
    owned = {c.key for c in children}

    for key in sorted(indexed - owned):
        pending.append(
            f"“{S.DECISIONS}” indexes {key}, which is not a ticket of this map"
        )

    for child in children:
        if not S.is_closed(child):
            continue
        if child.key not in indexed and child.key not in ruled_out:
            pending.append(
                f"{child.key} is closed but has no line in “{S.DECISIONS}” "
                f"(nor in “{S.OUT_OF_SCOPE}”)"
            )
        answers = [
            S.resolution_answer(comment.body) for comment in comments_of(child.key)
        ]
        if not any(answers):
            pending.append(
                f"{child.key} is closed but no comment on it carries the "
                f"“{S.RESOLUTION_HEADING}” heading with an answer under it"
            )

    return pending


def run(params: dict, *, tracker) -> str:
    key = params["map"].strip()
    box = tracker()
    map_item = box.get_item(key)
    children = box.list_children(key)

    lines = [f"# Validation of {key}", ""]
    lines += _verdict(S.section(map_item.body, S.FOG), S.takeable(children))

    pending = _lint(map_item.body, children, box.list_comments)
    lines += ["", f"Format ({len(pending)} pending):" if pending else "Format: nothing pending."]
    lines += [f"- {item}" for item in pending]
    return "\n".join(lines)
