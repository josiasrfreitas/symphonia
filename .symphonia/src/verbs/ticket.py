"""`map ticket` — put a step on the route, or wire one that already exists.

Two forms in one verb, because they answer the same question ("what has to
happen next?") and differ only in whether the card exists yet:

    create:  --map SYM-8 --title ... --question ... --type research
    rewire:  --map SYM-8 --key SYM-12 [--blocked-by ...] [--priority ...]

`map.check` cannot pick between two forms — it validates one fixed
`required` list — so `--map` is all it guarantees and the fork below is this
module's own work. Every branch of it refuses in the one Context Injection
format, never with a question and never with a guess.

Priority is the user's field (SYM-8). `--priority` without
`--user-requested` is refused here, before the tracker is ever built, so the
refusal reads the same whether or not `LINEAR_API_KEY` is set — and so an
agent cannot promote its own work by deciding it matters.
"""
from __future__ import annotations

from injection import INCOMPLETE, REFUSED
from verbs import _shared as S

SPEC = {
    "name": "ticket",
    "help": "add a step to the map, or rewire one that already exists",
    "required": ("map",),
    "example": 'map ticket --map SYM-8 --title "Read the skill" '
               '--question "which headings does it fix?" --type research',
}

_REWIRE_EXAMPLE = "map ticket --map SYM-8 --key SYM-12 --blocked-by SYM-11"

_CREATE_PARAMS = ("title", "question", "type")


def _create(params: dict, tracker, blocked_by, priority, user_requested) -> str:
    missing = [name for name in _CREATE_PARAMS if name not in params]
    if missing:
        S.refuse(
            blocked="ticket was called with neither form complete: creating needs "
                    + ", ".join(f"--{name}" for name in _CREATE_PARAMS)
                    + " (missing: " + ", ".join(f"--{name}" for name in missing)
                    + "), rewiring needs --key",
            accepted="one whole form: --title --question --type to create, "
                     "or --key to rewire an existing card",
            example=SPEC["example"],
            kind=INCOMPLETE,
        )
    values = {}
    for name in _CREATE_PARAMS:
        value = params[name]
        if not isinstance(value, str) or not value.strip():
            S.refuse(
                blocked=f"--{name} was given no value",
                accepted=f"--{name} takes a value",
                example=SPEC["example"],
                kind=INCOMPLETE,
            )
        values[name] = value.strip()
    if values["type"] not in S.TICKET_TYPES:
        S.refuse(
            blocked=f"{values['type']!r} is not a kind of step this map knows",
            accepted="--type is one of: " + ", ".join(S.TICKET_TYPES),
            example=SPEC["example"],
            kind=REFUSED,
        )

    box = tracker()
    ref = box.create_item(
        values["title"],
        f"## Question\n{values['question']}\n",
        parent=params["map"].strip(),
        labels=(f"wayfinder:{values['type']}",),
    )
    for key in blocked_by:
        box.add_blocker(ref.key, key)
    if priority:
        box.set_priority(ref.key, priority, user_requested=user_requested)

    lines = [
        f"{ref.key} is on the map, under {params['map'].strip()}: {values['title']}",
        ref.url,
        "",
        f"Type: {values['type']}",
        f"Question: {values['question']}",
    ]
    if blocked_by:
        lines.append("Blocked by: " + ", ".join(blocked_by))
    if priority:
        lines.append(f"Priority: {priority} (set because --user-requested was passed)")
    lines += ["", f"Next: map frontier --map {params['map'].strip()}"]
    return "\n".join(lines)


def _rewire(params: dict, tracker, key, blocked_by, priority, user_requested) -> str:
    collided = [name for name in _CREATE_PARAMS if name in params]
    if collided:
        S.refuse(
            blocked="ticket was called with both forms at once: --key rewires a card "
                    "that exists, " + ", ".join(f"--{name}" for name in collided)
                    + " would create a new one",
            accepted="one form per call: --key alone to rewire, "
                     "or --title --question --type alone to create",
            example=_REWIRE_EXAMPLE,
            kind=REFUSED,
        )
    if not blocked_by and not priority:
        S.refuse(
            blocked=f"--key {key} says which card to rewire but not what to change",
            accepted="--blocked-by <keys> or --priority <level> alongside --key",
            example=_REWIRE_EXAMPLE,
            kind=INCOMPLETE,
        )

    box = tracker()
    for blocker in blocked_by:
        box.add_blocker(key, blocker)
    if priority:
        box.set_priority(key, priority, user_requested=user_requested)

    changed = []
    if blocked_by:
        changed.append("blocked by " + ", ".join(blocked_by))
    if priority:
        changed.append(f"priority {priority} (set because --user-requested was passed)")
    return "\n".join([
        f"{key} rewired on map {params['map'].strip()}: " + "; ".join(changed),
        "",
        f"Next: map frontier --map {params['map'].strip()}",
    ])


def run(params: dict, *, tracker) -> str:
    key = S.optional(params, "key", example=_REWIRE_EXAMPLE)
    raw_blockers = S.optional(params, "blocked-by", example=_REWIRE_EXAMPLE)
    blocked_by = S.keys(raw_blockers) if raw_blockers else []
    priority = S.optional(params, "priority", example=SPEC["example"] + " --priority high --user-requested")
    user_requested = S.flag(params, "user-requested")

    # Both priority checks run before anything is created or fetched: a call
    # that will be refused must not leave a card behind.
    if priority and not user_requested:
        S.refuse(
            blocked="--priority was passed without --user-requested, and priority is "
                    "the user's field: an agent does not rank its own work",
            accepted="--priority only when the user asked for it, said with "
                     "--user-requested in the same call; otherwise leave it out and "
                     "let the frontier fall back to creation order",
            example=SPEC["example"] + " --priority high --user-requested",
            kind=REFUSED,
        )
    if priority and priority not in S.PRIORITY_LEVELS:
        S.refuse(
            blocked=f"{priority!r} is not a priority level this map knows",
            accepted="--priority is one of: high, medium, low",
            example=SPEC["example"] + " --priority high --user-requested",
            kind=REFUSED,
        )

    if key:
        return _rewire(params, tracker, key, blocked_by, priority, user_requested)
    return _create(params, tracker, blocked_by, priority, user_requested)
