"""`map frontier` — what can be worked now, and why the rest cannot.

Read-only. Two lists: the frontier itself, ordered by priority with
creation order as the tie-break, and everything held back, each line saying
what holds it — claimed, blocked, or closed.

An empty frontier is said in words. Silence would read as "the tool broke",
and the difference between "nothing is takeable" and "everything is done"
is exactly what `map validate` exists to judge.

The rendering lives in `_shared.render_frontier`, because `map resolve`
prints the same two lists after closing a ticket and the two must not drift.
"""
from __future__ import annotations

from verbs import _shared as S

SPEC = {
    "name": "frontier",
    "help": "the ordered set of tickets that can be worked now",
    "required": ("map",),
    "example": "map frontier --map SYM-8",
}


def run(params: dict, *, tracker) -> str:
    key = params["map"].strip()
    children = tracker().list_children(key)
    return "\n".join([
        f"# Frontier of {key}",
        "",
        S.render_frontier(key, children),
        "",
        f"Next: map claim --map {key} --ticket <key> --assignee <who>",
    ])
