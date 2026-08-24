"""`map resolve` — answer a ticket and leave the map able to prove it.

The success path is four writes in a fixed order, because each one is only
safe once the one before it landed:

1. post the resolution comment — the direct answer on its first line, under
   the fixed `## Resolution` heading, so a reader gets the answer before
   any reasoning;
2. close the ticket;
3. append its index line to the map's `## Decisions so far`, unless the map
   already accounts for the key — an already-indexed ticket, or one ruled
   out under `## Out of scope`, is not written twice;
4. read the children again and hand back the frontier as it now stands.

The four edge refusals happen before any of it: a ticket that is not on
this map, one nobody holds, one already closed, one still blocked. An empty
`--answer` never reaches here — `map.check` refuses it as incomplete,
because `answer` is in `required`.
"""
from __future__ import annotations

from injection import REFUSED
from verbs import _shared as S

SPEC = {
    "name": "resolve",
    "help": "answer a ticket, close it, and index the decision on the map",
    "required": ("map", "ticket", "answer"),
    "example": 'map resolve --map SYM-8 --ticket SYM-12 --answer "Three doors, one route."',
}


def run(params: dict, *, tracker) -> str:
    map_key = params["map"].strip()
    ticket_key = params["ticket"].strip()
    answer = params["answer"].strip()
    gist = S.optional(params, "gist", example=SPEC["example"] + ' --gist "three doors"')
    box = tracker()
    children = box.list_children(map_key)

    target = next((c for c in children if c.key == ticket_key), None)
    if target is None:
        S.refuse(
            blocked=f"{ticket_key} is not a ticket of map {map_key}",
            accepted="a ticket the map lists as its own",
            example=f"map frontier --map {map_key}",
            kind=REFUSED,
        )
    if S.is_closed(target):
        S.refuse(
            blocked=f"{ticket_key} is already closed ({target.state}); resolving it "
                    "again would post a second answer over the first",
            accepted="a ticket that is still open",
            example=f"map frontier --map {map_key}",
            kind=REFUSED,
        )
    if not target.assignee:
        S.refuse(
            blocked=f"{ticket_key} has no owner: nobody claimed it, so there is no "
                    "work to close",
            accepted="claim it first, then resolve it",
            example=f"map claim --map {map_key} --ticket {ticket_key} --assignee <who>",
            kind=REFUSED,
        )
    blocking = S.blocking_phrase(target, children)
    if blocking:
        S.refuse(
            blocked=f"{ticket_key} is still blocked by: {blocking}",
            accepted="resolve the blockers first, or drop the link with "
                     "map ticket --key",
            example=f"map frontier --map {map_key}",
            kind=REFUSED,
        )

    box.post_comment(ticket_key, S.resolution_comment(answer))
    box.close_item(ticket_key)

    map_item = box.get_item(map_key)
    index = S.section(map_item.body, S.DECISIONS) or ""
    ruled_out = S.index_keys(S.section(map_item.body, S.OUT_OF_SCOPE) or "")
    already = ticket_key in S.index_keys(index) or ticket_key in ruled_out
    line = S.index_line(ticket_key, target.title, target.url,
                        gist or answer.splitlines()[0])
    if not already:
        box.patch_body_section(map_key, S.DECISIONS, S.append_index(index, line))

    children = box.list_children(map_key)
    return "\n".join([
        f"{ticket_key} is resolved and closed: {answer.splitlines()[0]}",
        target.url,
        "",
        (f"Indexed on {map_key} under “{S.DECISIONS}”:\n{line}" if not already else
         f"Already accounted for on {map_key} (indexed, or ruled out under "
         f"“{S.OUT_OF_SCOPE}”) — the body was left as it was."),
        "",
        f"# Frontier of {map_key}, as it now stands",
        "",
        S.render_frontier(map_key, children),
    ])
