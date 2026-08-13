"""Role vocabulary and the policy each role file declares.

`RoleName`/`CapabilityTier`/`Access` are the package's shared vocabulary.
`load_policies` reads each `.symphonia/roles/*.md` frontmatter — the single
declaration of what a role runs at and can touch — and fails at bootstrap,
loudly, if a role file is missing or silent on any field.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path


class RoleName(enum.Enum):
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    SPEC_REVIEWER = "spec-reviewer"
    STANDARDS_REVIEWER = "standards-reviewer"


class CapabilityTier(enum.Enum):
    """Abstract level of model capability a role declares. `claude.py` —
    never the role — translates it into a concrete model and effort."""

    FRONTIER = "frontier"
    HIGH = "high"
    STANDARD = "standard"
    FAST = "fast"


class Access(enum.Enum):
    WRITE = "write"
    READ = "read"


@dataclass(frozen=True)
class RolePolicy:
    """What one role is allowed to run at and touch."""

    role: RoleName
    tier: CapabilityTier
    access: Access
    role_file: str


def _frontmatter(text: str, path: Path) -> dict[str, str]:
    """A minimal `key: value` parser between the two `---` fences.

    Refuses rather than silently resolving: a repeated key and a non-empty
    line with no `:` both fail loudly — the two shapes an unresolved merge
    conflict takes. A conflict in a role file must not quietly hand a
    reviewer `access: write`."""

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
                f"{path}:{lineno} redeclares {key!r}; already set earlier in "
                f"this frontmatter"
            )
        fields[key] = value.strip()
    raise SystemExit(f"{path} frontmatter never closes with '---'")


def load_policies(roles_dir: Path) -> dict[RoleName, RolePolicy]:
    """Every `RoleName`'s policy, read from its own file.

    Iterates over `RoleName`, never the directory listing — a stray file in
    `roles/` cannot mint a role that doesn't exist in code, and a `RoleName`
    with no file fails instead of silently having no policy."""

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
