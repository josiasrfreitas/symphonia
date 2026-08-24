"""`map claim` — take one ticket, and get everything needed to work it.

One call answers with both halves of the context: the ticket's own body,
and the map in low resolution — where the route is going, what has been
decided, what is still fogged, how much frontier is left. An agent that has
to make a second call to find out why its ticket exists will often not make
it.

Every reason a claim cannot happen is checked before anything is assigned,
against the map's own children: a ticket that is not on this map, one
already closed, one someone already holds, one still blocked. A blocker
this map cannot see counts as blocking, and the refusal says the blocker is
external — refusing too much is recoverable, claiming work that is not
ready is not.
"""
from __future__ import annotations

from injection import REFUSED
from verbs import _shared as S

SPEC = {
    "name": "claim",
    "help": "take a ticket off the frontier and get the ticket plus the map",
    "required": ("map", "ticket", "assignee"),
    "example": "map claim --map SYM-8 --ticket SYM-12 --assignee ana@example.com",
}


def run(params: dict, *, tracker) -> str:
    map_key = params["map"].strip()
    ticket_key = params["ticket"].strip()
    assignee = params["assignee"].strip()
    box = tracker()
    children = box.list_children(map_key)

    target = next((c for c in children if c.key == ticket_key), None)
    if target is None:
        S.refuse(
            blocked=f"{ticket_key} is not a ticket of map {map_key}",
            accepted="a ticket the map lists as its own; "
                     f"map frontier --map {map_key} names them",
            example=f"map frontier --map {map_key}",
            kind=REFUSED,
        )
    if S.is_closed(target):
        S.refuse(
            blocked=f"{ticket_key} is already closed ({target.state})",
            accepted="a ticket that is still open",
            example=f"map frontier --map {map_key}",
            kind=REFUSED,
        )
    if target.assignee:
        S.refuse(
            blocked=f"{ticket_key} is already claimed by {target.assignee}",
            accepted="a ticket nobody holds yet",
            example=f"map frontier --map {map_key}",
            kind=REFUSED,
        )
    blocking = S.open_blockers(target, children)
    if blocking:
        external = set(S.external_blockers(target, children))
        named = ", ".join(
            f"{key} (external to this map, so its state is unknown here)"
            if key in external else key
            for key in blocking
        )
        S.refuse(
            blocked=f"{ticket_key} is still blocked by: {named}",
            accepted="a ticket whose blockers are all closed — the frontier holds "
                     "exactly those",
            example=f"map frontier --map {map_key}",
            kind=REFUSED,
        )

    box.assign(ticket_key, assignee)
    ticket = box.get_item(ticket_key)
    map_item = box.get_item(map_key)

    return "\n".join([
        f"{ticket_key} is yours, {assignee}: {ticket.title}",
        ticket.ref.url,
        "",
        "## The ticket",
        ticket.body.strip() or "(the ticket has an empty body)",
        "",
        "## The map, in low resolution",
        S.low_res(map_item, children),
        "",
        f"Next, when it is answered: map resolve --map {map_key} "
        f'--ticket {ticket_key} --answer "..."',
    ])
