"""Loads `RolePolicy`, the single source for what a role runs at and can touch.

TLDR: `ROLE_TIERS`/`ROLE_ACCESS`/`ROLE_FILES` used to be three tables a
launcher kept in sync with the role files by hand (GRE-179's own conformance
test existed only to catch the drift). This module reads the frontmatter of
each `.symphonia/roles/*.md` instead — one declaration, not two — and fails
at bootstrap, loudly, if a role file is missing or silent on any field.

`RolePolicy` itself is core vocabulary and lives in `adapters.runtime_adapter`
(re-exported here) — reading one off disk is a `workflow` concern, but the
shape isn't, and `adapters/` code must never import `workflow/` to see it.
"""
from __future__ import annotations

from pathlib import Path

from adapters.runtime_adapter import Access, CapabilityTier, RoleName, RolePolicy

__all__ = ["RolePolicy", "load_policies"]


def _frontmatter(text: str, path: Path) -> dict[str, str]:
    """A minimal `key: value` parser between the two `---` fences — this
    package's role files carry nothing richer, and pulling in a YAML
    dependency for three scalar fields is not worth the import.

    Refuses rather than silently resolving: a repeated key (the shape an
    unresolved merge conflict's two sides take) and a non-empty line with no
    `:` (the shape a conflict marker takes) both fail loudly, naming the
    file and the line — an unresolved `<<<<<<<`/`=======`/`>>>>>>>` conflict
    in a role file must not quietly hand a reviewer `access: write`."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SystemExit(f"{path} has no frontmatter (expected a leading '---')")
    fields: dict[str, str] = {}
    for lineno, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            return fields
        if not line.strip():
            continue
        if ":" not in line:
            raise SystemExit(
                f"{path}:{lineno} is not a 'key: value' line and not blank: {line!r} "
                f"(an unresolved merge conflict marker looks like this)"
            )
        key, _, value = line.partition(":")
        key = key.strip()
        if key in fields:
            raise SystemExit(
                f"{path}:{lineno} redeclares {key!r}; "
                f"already set earlier in this frontmatter (an unresolved merge "
                f"conflict leaves both sides of a key in the file)"
            )
        fields[key] = value.strip()
    raise SystemExit(f"{path} frontmatter never closes with '---'")


def load_policies(roles_dir: Path) -> dict[RoleName, RolePolicy]:
    """Every `RoleName`'s policy, read from its own file.

    Iterates over `RoleName`, never the directory listing — a stray file in
    `roles/` cannot mint a role that doesn't exist in code, and a `RoleName`
    with no file fails instead of silently having no policy. No fallback:
    a missing file, a `role:` that disagrees with the filename, a
    `capability_tier` outside `CapabilityTier`, or an `access` missing or
    outside `write|read` all fail here, naming the file and the field.
    """

    policies: dict[RoleName, RolePolicy] = {}
    for role in RoleName:
        role_file = f"{role.value}.md"
        path = roles_dir / role_file
        if not path.exists():
            raise SystemExit(f"{path} is missing; every RoleName needs a role file")
        fields = _frontmatter(path.read_text(), path)

        declared_role = fields.get("role")
        if declared_role != role.value:
            raise SystemExit(
                f"{path} declares role={declared_role!r}, expected {role.value!r}"
            )

        tier_value = fields.get("capability_tier")
        try:
            tier = CapabilityTier(tier_value)
        except ValueError:
            raise SystemExit(
                f"{path} declares capability_tier={tier_value!r}; "
                f"known: {', '.join(t.value for t in CapabilityTier)}"
            )

        access_value = fields.get("access")
        try:
            access = Access(access_value)
        except ValueError:
            raise SystemExit(
                f"{path} declares access={access_value!r}; "
                f"known: {', '.join(a.value for a in Access)}"
            )

        policies[role] = RolePolicy(role=role, tier=tier, access=access, role_file=role_file)
    return policies
