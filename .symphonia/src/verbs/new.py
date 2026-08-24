"""`map new` — open a map.

One card, labelled `wayfinder:map`, with a body already shaped into the
five sections the `wayfinder` skill writes and the other six verbs read.
Three of them start empty: nothing has been decided, fogged, or ruled out
before the first ticket exists.

`--team` is required because `create_item` refuses without a team or a
parent, and a map has no parent. There is no workspace default to fall back
on — adding one is a `config.json` change, which this vertical does not own.
"""
from __future__ import annotations

from verbs import _shared as S

SPEC = {
    "name": "new",
    "help": "open a map: one card labelled wayfinder:map, with the five sections",
    "required": ("title", "destination", "team"),
    "example": 'map new --title "Intake v2" --destination "three doors, one route" --team SYM',
}


def run(params: dict, *, tracker) -> str:
    notes = S.optional(params, "notes", example=SPEC["example"] + ' --notes "brownfield"')
    body = S.blank_map_body(params["destination"], notes or "")
    ref = tracker().create_item(
        params["title"].strip(),
        body,
        labels=(S.MAP_LABEL,),
        team=params["team"].strip(),
    )
    return "\n".join([
        f"Map {ref.key} is open: {params['title'].strip()}",
        ref.url,
        "",
        f"Destination: {params['destination'].strip()}",
        f"Sections written: {', '.join(S.SECTIONS)} "
        f"({S.DECISIONS}, {S.FOG} and {S.OUT_OF_SCOPE} start empty).",
        "",
        f"Next: add the first ticket — map ticket --map {ref.key} "
        '--title "..." --question "..." --type research',
    ])
