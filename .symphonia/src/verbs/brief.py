"""`map brief` — the eighth verb, and the one door between the two halves.

TLDR: it reads a briefing off disk, refuses it unless it carries the door
record and one checkable success criterion, and only then creates the
construction card with the briefing as its body. With `--map` it also
refuses while the map it belongs to is still open.

The verb is thin on purpose: what a briefing *is* lives in `intake`, pure
and testable without a tracker, because the intake handoff is held to the
same field and there must be exactly one definition of it.
"""
from __future__ import annotations

import intake
from injection import REFUSED, Refusal, Refused

SPEC = {
    "name": "brief",
    "help": "validate a briefing and open the construction card it describes",
    "required": ("file", "parent"),
    "example": "map brief --file briefing.md --parent SYM-9",
}

CLOSED_STATES = ("completed", "canceled")
"""The two state categories that take a child off the frontier; a child in
any other one is still on it.

`state_type` and not the state *name*, which is a team setting a human can
rename. A ticket closed for being out of scope lands in `canceled` and so
counts as closed — which is right: it left the map by a decision, not by
being unfinished."""


def _map_is_closed(tracker, key: str) -> None:
    """Refuse while the map at `key` still has anything to do.

    The end of a map is the same rule the initial phase declares it with:
    the frontier is empty *and* the fog is empty. Both are read here, and
    the refusal names which of the two blocked it and how much is left —
    "the map is not finished" would send the reader looking in the wrong
    half."""

    open_children = [c.key for c in tracker.list_children(key)
                     if c.state_type not in CLOSED_STATES]
    if open_children:
        raise Refused(Refusal(
            blocked=(f"the map {key} still has {len(open_children)} open ticket(s) on its "
                     f"frontier: {', '.join(open_children)}"),
            accepted=("a map whose frontier is empty — every child closed — before a "
                      "briefing is written from it; or the same call without --map"),
            example=SPEC["example"],
            kind=REFUSED,
        ))
    fog = intake.fog_items(tracker.get_item(key).body)
    if fog:
        raise Refused(Refusal(
            blocked=(f"the map {key} still has {len(fog)} item(s) under "
                     f"{intake.FOG_HEADING!r}: {'; '.join(fog)}"),
            accepted=("a map whose fog is empty too — the frontier being empty is only "
                      "half of the end of a map; or the same call without --map"),
            example=SPEC["example"],
            kind=REFUSED,
        ))


def run(params: dict, *, tracker) -> str:
    """`map brief --file briefing.md --parent SYM-9 [--map SYM-8]`.

    The tracker is not touched until the briefing has passed: a refused
    briefing costs no network call and needs no `LINEAR_API_KEY`."""

    text = intake.read_document(params["file"], what="briefing", example=SPEC["example"])
    title, door, fact = intake.validate_briefing(text)

    key = params.get("map")
    if key is not None:
        if not isinstance(key, str) or not key.strip():
            raise Refused(Refusal(
                blocked="--map was given without a value",
                accepted="--map <key of the Decision Map>, or the call without --map at all",
                example=f"{SPEC['example']} --map SYM-8",
                kind=REFUSED,
            ))
        _map_is_closed(tracker(), key.strip())

    ref = tracker().create_item(title, text, parent=params["parent"].strip())
    return "\n".join([
        f"Card de construção criado: {ref.key}",
        f"URL: {ref.url}",
        f"Porta: {door}",
        f"Fato: {fact}",
        f"Título: {title}",
        "",
        "O briefing é o corpo do card. A próxima fase começa por ele.",
    ])
