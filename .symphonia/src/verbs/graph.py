"""`map graph` — the map as a mermaid tree.

Read-only, and for a human: `flowchart TD` with the map at the root, one
node per ticket, and one edge per blocking relation drawn the way the
relation reads — `blocker --> blocked`.

Four classes, so the picture says what the frontier says: closed, takeable
now, claimed, blocked. A blocker that is not a ticket of this map gets its
own node marked external, because a dangling edge to nothing is the one
thing a diagram must not do.

Mermaid identifiers cannot carry the `-` of a ticket key, so ids are the
key with `_` in its place; the label keeps the key as written.
"""
from __future__ import annotations

from verbs import _shared as S

SPEC = {
    "name": "graph",
    "help": "the map and its tickets as a mermaid flowchart",
    "required": ("map",),
    "example": "map graph --map SYM-8",
}

CLASSES = (
    "    classDef closed fill:#e8e8e8,stroke:#9e9e9e,color:#616161",
    "    classDef ready fill:#e3f2e5,stroke:#43a047,color:#1b5e20",
    "    classDef claimed fill:#e7effb,stroke:#1e88e5,color:#0d47a1",
    "    classDef blocked fill:#fdeceb,stroke:#e53935,color:#b71c1c",
    "    classDef external fill:#fff8e1,stroke:#fb8c00,color:#e65100,stroke-dasharray:4 3",
)


def node_id(key: str) -> str:
    return key.replace("-", "_")


def _label(text: str) -> str:
    """Mermaid quotes a label with `"`, so the label may not carry one."""

    return text.replace('"', "'")


def _class_of(child, children, frontier_keys) -> str:
    if S.is_closed(child):
        return "closed"
    if child.key in frontier_keys:
        return "ready"
    if child.assignee:
        return "claimed"
    return "blocked"


def run(params: dict, *, tracker) -> str:
    key = params["map"].strip()
    children = tracker().list_children(key)
    frontier_keys = {c.key for c in S.takeable(children)}
    owned = {c.key for c in children}

    lines = ["```mermaid", "flowchart TD", f'    {node_id(key)}["{_label(key)} — the map"]']
    for child in children:
        label = f"{child.key} — {_label(child.title)}"
        if child.assignee and not S.is_closed(child):
            label += f" ({_label(child.assignee)})"
        lines.append(f'    {node_id(child.key)}["{label}"]')
        lines.append(f"    {node_id(key)} --- {node_id(child.key)}")

    external = []
    for child in children:
        for blocker in child.blocked_by:
            if blocker not in owned and blocker not in external:
                external.append(blocker)
    for blocker in external:
        lines.append(f'    {node_id(blocker)}["{blocker} — external to this map"]')

    for child in children:
        for blocker in child.blocked_by:
            lines.append(f"    {node_id(blocker)} --> {node_id(child.key)}")

    lines += list(CLASSES)
    lines.append(f"    class {node_id(key)} claimed")
    for child in children:
        lines.append(f"    class {node_id(child.key)} {_class_of(child, children, frontier_keys)}")
    for blocker in external:
        lines.append(f"    class {node_id(blocker)} external")
    lines.append("```")

    if not children:
        lines.append("")
        lines.append(f"Map {key} has no tickets yet — the tree is its root alone.")
    return "\n".join(lines)
