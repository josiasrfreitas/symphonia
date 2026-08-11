#!/usr/bin/env python3
"""The whole spawn interface the Orchestrator is allowed to use.

TLDR: the four role verbs take one argument each — a Ticket Key — plus
`status`, `retire`, and the plan gate pair `wait`/`verdict`. The Orchestrator
never picks a model, a permission flag, a worktree or a launch path; every
one of those is decided here. If a command below does not express what you
need, that is a package change, not an improvisation at the terminal.

    .symphonia/bin/spawn plan             GRE-181
    .symphonia/bin/spawn implement        GRE-181
    .symphonia/bin/spawn review-spec      GRE-181
    .symphonia/bin/spawn review-standards GRE-181
    .symphonia/bin/spawn status          [GRE-181]
    .symphonia/bin/spawn retire           GRE-181 planner
    .symphonia/bin/spawn wait             [--ack <delivery_id>] [--timeout-ms <ms>]
    .symphonia/bin/spawn verdict          GRE-181 approved|revise [--notes <text>|--notes-file <path>]

And two verbs a ROLE runs, inside its own dispatched terminal — the return
half of the same interface. No role ever types `orca orchestration` by hand:

    .symphonia/bin/spawn submit           GRE-181 --file plan.md
    .symphonia/bin/spawn done             GRE-181 --outcome succeeded --file report.md

`plan` derives a worktree from the repo's default base; every later role
reuses that same worktree, so one Ticket Key means one checkout from plan to
PR. Roles are Orca dispatches (attached), not child worktrees: they report
`worker_done` to the Orchestrator's Run.

`wait` is the one loop that hears back from every role — it also drives the
plan gate: it turns mailbox messages into typed events (`adapters/orca/events.py`),
runs each through `adapters/plan_gate.py`, and executes what comes back
(label the ticket, retire the planner, flag a divergence). `verdict` is how
the human's decision reaches the planner: it never comes from an agent
typing `APPROVED`/`REVISE` into a reply by hand.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from adapters import attention as _attention
from adapters import plan_gate as _gate
from adapters import reports as _reports
from adapters import runtime_adapter as _contract
from adapters.linear import adapter as _linear
from adapters.linear import client as _linear_client
from adapters.orca import adapter as _cli
from adapters.orca import events as _events
from adapters.orca import launcher as _launcher
import setup_worktree as _setup_worktree

# `.symphonia/`, the package root: two levels up from this file
# (`src/spawn.py`), one for `src/` and one for the package. Everything below
# that reads a Resource by path — `roles/`, `config.json` — anchors on this.
PACKAGE = Path(__file__).resolve().parents[1]

RoleName = _contract.RoleName
Access = _contract.Access
GATE_ROLE = RoleName.PLANNER
"""Only the planner's completion is a Human Gate today; `wait` only reacts
to gate events keyed to this role's dispatch."""

ASK_MAX_MS = 1_800_000
"""What `orca orchestration ask` will actually wait, measured on 1.4.168: a
larger `--timeout-ms` is clamped to this silently (asking for 99_999_999 came
back reporting `timeoutMs: 1800000`). A human verdict routinely takes longer
than 30 minutes, so `submit` resumes the same question by id instead of
asking a second time."""

# The registry of live spawns, deliberately OUTSIDE every checkout.
#
# Both sides of a dispatch read it: the Orchestrator, from its own checkout,
# and the role itself, from the ticket's worktree — two different copies of
# this repository. Keeping it under `PACKAGE/.runtime/` meant each side wrote
# a different file, so a role could not look up its own task id, dispatch id
# or capability. Override with SYMPHONIA_RUNTIME (the tests do).
#
# Writers: `spawn`, `wait`, `verdict`, `retire` — all Orchestrator-side. The
# role-side verbs (`submit`, `done`) only ever read, so two processes in two
# checkouts never race for this file.
RUNTIME_DIR = Path(os.environ.get("SYMPHONIA_RUNTIME", "~/.symphonia/runtime")).expanduser()
STATE = RUNTIME_DIR / "spawns.json"
# The baton between roles is the installed handoff skill's document, in the
# location that skill owns — not a format this package invents.
#
# It lives outside the repository on purpose: it is never committed, never
# travels with the branch, and outlives nothing. Its only job is carrying
# context from the role that just died to the one about to start. Moving it
# into the worktree would turn a disposable note into a versioned artifact
# that reviewers have to maintain — do not.
HANDOFF_SKILL = "~/.claude/skills/handoff/SKILL.md"
HANDOFF_DIR = "~/orca/.context"
ROLE_FILES = {
    RoleName.PLANNER: "planner.md",
    RoleName.IMPLEMENTER: "implementer.md",
    RoleName.SPEC_REVIEWER: "spec-reviewer.md",
    RoleName.STANDARDS_REVIEWER: "standards-reviewer.md",
}

# What the phase looks like in the Orca sidebar. Orca exposes no per-worktree
# colour, so the phase is carried by the three labels it does expose: the
# worktree display name, the terminal title, and the board column. A glance
# at the sidebar should answer "which phase is this ticket in" without
# opening anything.
ROLE_BADGE = {
    RoleName.PLANNER: ("🧭", "planning", "in-progress"),
    RoleName.IMPLEMENTER: ("🔨", "implementing", "in-progress"),
    RoleName.SPEC_REVIEWER: ("🔍", "spec review", "in-review"),
    RoleName.STANDARDS_REVIEWER: ("📐", "standards review", "in-review"),
}


# --- orca CLI ------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def orca(*argv: str, expect_lifecycle_ok: bool = False) -> dict:
    """One orca call, envelope unwrapped, failures loud. The CLI wraps every
    --json response in {id, ok, result, _meta}.

    Delegates to `adapters.orca.adapter.unwrap_envelope` — the same
    unwrapper `OrcaRuntimeAdapter._orca` uses (GRE-184 M1) — and turns
    an `OrcaCliError` into the identical `SystemExit` this function has
    always raised. Name, signature and error text are unchanged, so `wait`
    and `verdict` (GRE-185) keep calling this without editing a line.

    `expect_lifecycle_ok` is for the lifecycle messages a role sends
    (`worker_done`). Measured against Orca 1.4.168: a rejected `worker_done`
    still answers `ok: true` — the refusal lives in `result.lifecycle` and in
    the process exit code, not in the envelope. Without this flag a rejected
    completion reads as a success, which is exactly how a planner ends up
    never retiring. Note the asymmetry: `lifecycle` is present only on a
    rejection, so its absence must never be treated as failure.
    """

    result = _cli.subprocess_runner(["orca", *argv, "--json"])
    try:
        return _cli.unwrap_envelope(argv, result, expect_lifecycle_ok=expect_lifecycle_ok)
    except _cli.OrcaCliError as exc:
        raise SystemExit(str(exc)) from exc


def state_read() -> dict:
    """The live registry, from the shared runtime file."""

    if STATE.exists():
        return json.loads(STATE.read_text())
    return {}


def state_write(data: dict) -> None:
    """Atomic, and 0600 from the moment the bytes exist — the registry holds
    Dispatch capability tokens, and a token is what authorizes a
    `worker_done` on someone else's dispatch. Both the directory and the temp
    file are created narrow rather than widened afterwards: a chmod after the
    write leaves a window at the default umask."""

    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE.parent, 0o700)  # a directory that already existed
    tmp = STATE.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, STATE)


# --- worktree ------------------------------------------------------------


def worktree_name(ticket: str) -> str:
    return ticket.strip().lower()


def find_worktree(ticket: str) -> tuple[str, str] | None:
    """(id, path) of this ticket's worktree, or None."""

    # Matched on the path, not the display name: spawn rewrites the display
    # name with the current phase ("🧭 SYM-5 · planning"), so a `name:`
    # lookup stops finding the ticket the moment it starts. The directory
    # never changes.
    name = worktree_name(ticket)
    listed = orca("worktree", "list")
    items = listed.get("worktrees", listed.get("items", []))
    for wt in items:
        if Path(str(wt.get("path", ""))).name == name:
            return str(wt.get("id", "")), str(wt.get("path", ""))
    return None


def default_base() -> str:
    """The repo's default base, read from git rather than assumed.

    The Orchestrator may be sitting on any branch when it spawns, and a
    ticket must never be stacked on whatever that branch happens to be.
    """

    proc = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True,
    )
    base = proc.stdout.strip()
    if not base:
        raise SystemExit(
            "cannot resolve origin/HEAD; run `git remote set-head origin -a` "
            "so the ticket branch has a known base"
        )
    return base


def create_worktree(ticket: str) -> tuple[str, str]:
    """One checkout per Ticket Key: child of the Orchestrator in Orca
    lineage, off the repo default base in git.

    Those are two different axes and both matter. `--parent-worktree active`
    makes every role show up under the Orchestrator in the sidebar, so the
    fleet is visible as one tree no matter which branch the Orchestrator is
    on. `--base-branch` is passed explicitly for exactly that reason: child
    lineage must not drag the Orchestrator's current branch in as the base.
    """

    created = orca(
        "worktree", "create",
        "--name", worktree_name(ticket),
        "--parent-worktree", "active",
        "--base-branch", default_base(),
        "--setup", "run",
        "--linear-issue", ticket,
        "--comment", f"symphonia ticket {ticket}",
    )
    wt = created.get("worktree", created)
    wt_id, path = str(wt.get("id", "")), str(wt.get("path", ""))
    if not wt_id or not path:
        raise SystemExit(f"worktree create returned no id/path for {ticket}: {created}")
    # Bare create leaves one untitled fallback shell (measured on GRE-179).
    # It is closed here so a ticket's tab list is exactly its live roles.
    #
    # Best-effort on purpose: this is cosmetic, and the shell's tab can
    # already be gone by the time we ask (`tab_not_found`). A tidy sidebar is
    # never worth failing a spawn that otherwise succeeded.
    for term in orca("terminal", "list", "--worktree", f"id:{wt_id}").get("terminals", []):
        if term.get("title") or term.get("command"):
            continue
        try:
            orca("terminal", "close", "--terminal", str(term.get("handle")), "--tab")
        except SystemExit as exc:
            print(f"note: leftover shell not closed ({exc})", file=sys.stderr)

    # The env files are gitignored, so the new checkout does not have them —
    # it looks complete and fails the first time something reads `.env`.
    # Called here rather than relying on Orca's repo setup hook, which is a
    # per-machine setting and is empty until someone fills it in: a spawn must
    # not depend on a checkbox. Running it twice is harmless.
    print(json.dumps(_setup_worktree.setup(Path(path))), file=sys.stderr)
    return wt_id, path


# --- the spawn itself ----------------------------------------------------


def work_spec(role: RoleName, ticket: str, workspace: str) -> str:
    """What the role is told at dispatch. Deliberately a pointer, not a
    briefing: the role contract lives in a versioned file, so it can be
    reviewed and changed without touching this launcher (closes GRE-175 M3 —
    the briefing used to be discarded at spawn)."""

    role_file = f".symphonia/roles/{ROLE_FILES[role]}"
    lines = [
        f"Ticket: {ticket.upper()}. Your role: {role.value}.",
        f"1. Read {role_file} — it is your role contract; follow it exactly.",
        # The worktree is linked to the ticket at creation, so the brief is
        # one command away. Saying "read the Execution Brief" without saying
        # how is what makes a role improvise.
        "1b. The Execution Brief is the linked ticket. Read it with "
        "`orca linear issue --current --full --json`, and write back to it "
        "(plan, findings, status) with the `orca linear` commands — never "
        "invent another channel.",
        f"2. Read {HANDOFF_DIR}/{ticket.lower()}-*.md — if such a file exists it is the "
        f"handoff from the role before you, and it is all the context you get.",
        f"3. Do the work of your role for {ticket.upper()} in this worktree ({workspace}).",
    ]
    if _launcher.ROLE_ACCESS[role] is Access.READ:
        lines.append(
            "4. You are read-only by construction: Edit/Write are disabled at launch. "
            "Report findings; never fix them yourself."
        )
    else:
        # The dying agent writes the baton; it never launches the next role.
        # Spawning belongs to the Orchestrator, so only the document half of
        # the handoff skill applies here.
        lines.append(
            f"4. Before you finish, write your handoff document following "
            f"{HANDOFF_SKILL} — the document half only (as with --doc-only). "
            f"Save it as {HANDOFF_DIR}/{ticket.lower()}-{role.value}-<YYYY-MM-DD>.md. "
            f"Do NOT hand ownership to anyone and do NOT launch another agent: "
            f"the Orchestrator starts the next role. That document is the only "
            f"thing that survives you."
        )
    lines.append(
        "5. Then send worker_done exactly once with --outcome succeeded or failed, and stop."
    )
    return "\n".join(lines)


# --- the Execution Brief (planner input) ----------------------------------


def _current_branch(workspace: str) -> str:
    proc = subprocess.run(
        ["git", "-C", workspace, "branch", "--show-current"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() or "(unknown)"


def _handoff_files(ticket: str) -> list[Path]:
    return sorted(Path(os.path.expanduser(HANDOFF_DIR)).glob(f"{ticket.lower()}-*.md"))


def build_brief(role: RoleName, ticket: str, workspace: str, *, tracker=None) -> str:
    """Assembles the Execution Brief injected at dispatch: extracts the
    ``io:brief-template`` block from the role's own file and fills it from
    the ticket. The role opens with the ticket already in hand — zero tool
    call needed to fetch it (`orca linear` may be disconnected; this reads
    the tracker adapter directly, same as GRE-174)."""

    ticket = ticket.upper()
    role_path = PACKAGE / "roles" / ROLE_FILES[role]
    template = _reports.extract_block(role_path.read_text(), "md io:brief-template")

    tracker = tracker or _linear.LinearTracker()

    item = tracker.get_item(ticket)
    comments = tracker.list_comments(item.ref.id)
    comment_text = "\n\n".join(
        f"**{c.author_name or 'unknown'} · {c.created_at[:10]}**\n\n{c.body}"
        for c in comments
    ) or "None."

    handoff_files = _handoff_files(ticket)
    handoff_text = "\n".join(f"- {p}" for p in handoff_files) or (
        "None — this is the first role on this ticket."
    )

    values = {
        "ticket_key": ticket,
        "role": role.value,
        "role_file": f".symphonia/roles/{ROLE_FILES[role]}",
        "workspace": workspace,
        "branch": _current_branch(workspace),
        "title": item.title,
        "url": item.ref.url,
        "description": item.body or "(no description)",
        "comments": comment_text,
        "handoff_files": handoff_text,
    }
    try:
        return template.format(**values)
    except KeyError as exc:
        raise SystemExit(
            f"Brief template in {role_path} references unknown placeholder {exc}"
        )


# --- role identity: how a role finds its own record -----------------------

_CAPABILITY = re.compile(r"--dispatch-capability\s+(\S+)")


def _capability_of(preamble: str) -> str | None:
    """The Dispatch capability token Orca mints for an injected dispatch.

    It exists only as text inside the preamble (the dispatch row's
    `capability_hash` comes back null), so it is read with a regex — the
    preamble repeats it once per lifecycle command and they are the same
    token."""

    found = _CAPABILITY.search(preamble)
    return found.group(1) if found else None


def _require_capability(preamble: str, ticket: str, role_value: str, *, task: str, terminal: str) -> str:
    """The capability, or no spawn at all.

    Measured on Orca 1.4.168: an injected dispatch mints a capability and
    every lifecycle message is checked against it — a `worker_done` sent
    without it comes back `dispatch_capability_invalid` and the dispatch
    stays open forever. The token is printed only inside the preamble text
    (`capability_hash` on the dispatch row is null), so this is the one
    moment it can be captured.

    Fatal rather than a warning: a role launched without it would work for
    hours and only then be unable to report. Failing here costs a spawn;
    failing there costs the work. The task and terminal are rolled back so a
    retry starts clean.
    """

    capability = _capability_of(preamble)
    if capability:
        return capability
    orca(
        "orchestration", "task-update", "--id", task, "--status", "failed",
        "--result", json.dumps({"reason": "no dispatch capability in the preamble"}),
    )
    try:
        orca("terminal", "close", "--terminal", terminal, "--tab")
    except SystemExit as exc:
        print(f"note: terminal not closed ({exc})", file=sys.stderr)
    raise SystemExit(
        f"dispatch for {ticket}/{role_value} minted no capability, so the role could "
        f"never report; the terminal and task were rolled back. Retry the spawn."
    )


def own_record(ticket: str, data: dict | None = None) -> tuple[str, dict]:
    """The record of the role calling this, from inside its own terminal.

    Identity comes from `ORCA_TERMINAL_HANDLE`, which Orca exports into the
    pane and `spawn` already stored as `terminal` — an exact match, never a
    guess. The fallback (the worktree's git toplevel) exists for a pane whose
    environment did not carry the handle; if it matches more than one live
    role, this fails loudly rather than reporting on someone else's behalf.
    """

    ticket = ticket.upper()
    data = state_read() if data is None else data
    live = {
        key: rec for key, rec in data.items()
        if rec.get("ticket") == ticket and not rec.get("retired")
    }
    if not live:
        raise SystemExit(f"no live spawn recorded for {ticket}; is this the right Ticket Key?")

    handle = os.environ.get("ORCA_TERMINAL_HANDLE", "")
    if handle:
        for key, rec in live.items():
            if rec.get("terminal") == handle:
                return key, rec
        raise SystemExit(
            f"terminal {handle} is not a recorded role of {ticket}; "
            f"this command is run by a role, from inside its own dispatched terminal"
        )

    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    ).stdout.strip()
    matches = [(key, rec) for key, rec in live.items() if rec.get("worktree") == toplevel]
    if len(matches) == 1:
        return matches[0]
    raise SystemExit(
        f"cannot tell which role is calling: ORCA_TERMINAL_HANDLE is unset and "
        f"{len(matches)} live roles of {ticket} share the worktree {toplevel or '(unknown)'}"
    )


def spawn(role: RoleName, ticket: str, *, fresh_worktree: bool, tier: str | None = None) -> dict:
    ticket = ticket.upper()
    found = find_worktree(ticket)
    if fresh_worktree:
        if found:
            raise SystemExit(
                f"{ticket} already has a worktree at {found[1]}. "
                f"Planning runs once per ticket; use `implement` to continue in it."
            )
        wt_id, path = create_worktree(ticket)
    else:
        if not found:
            raise SystemExit(
                f"{ticket} has no worktree yet. Run `spawn plan {ticket}` first — "
                f"every role after the planner reuses the checkout planning created."
            )
        wt_id, path = found

    # `--tier` is a human override, not a knob for the Orchestrator: the
    # matrix is the default precisely so no agent has to choose a model.
    override = None
    if tier:
        try:
            override = _contract.CapabilityTier(tier)
        except ValueError:
            raise SystemExit(
                f"unknown tier {tier!r}; known: "
                f"{', '.join(t.value for t in _contract.CapabilityTier)}"
            )
    plan = _launcher.build_launch(
        role, session_id=str(uuid.uuid4()), workspace=path, tier=override
    )
    emoji, phase, board = ROLE_BADGE[role]

    # The ticket's worktree carries the phase it is currently in, so the
    # sidebar reads as a board without opening a single terminal.
    orca(
        "worktree", "set",
        "--worktree", f"id:{wt_id}",
        "--display-name", f"{emoji} {ticket} · {phase}",
        "--workspace-status", board,
    )

    terminal = orca(
        "terminal", "create",
        "--worktree", f"id:{wt_id}",
        "--title", f"{emoji} {phase} · {ticket}",
        "--command", plan.command,
    )["terminal"]["handle"]

    # Losing the first prompt costs a whole spawn, so readiness is waited on,
    # never assumed.
    orca("terminal", "wait", "--terminal", terminal, "--for", "tui-idle", "--timeout-ms", "120000")

    # The planner's input is the Execution Brief, injected: the issue is
    # read and formatted here, never left for the role to go fetch. Every
    # other role still gets the `work_spec()` pointer until its own cycle
    # brings it a Brief (legacy, migrated one role at a time).
    spec = build_brief(role, ticket, path) if role is RoleName.PLANNER else work_spec(role, ticket, path)
    task = orca(
        "orchestration", "task-create",
        "--spec", spec,
    )["task"]["id"]
    dispatched = orca(
        "orchestration", "dispatch", "--task", task, "--to", terminal, "--inject",
        "--return-preamble",
    )
    dispatch = dispatched["dispatch"]["id"]
    capability = _require_capability(
        dispatched.get("preamble") or "", ticket, role.value, task=task, terminal=terminal
    )

    record = {
        "ticket": ticket,
        "role": role.value,
        "tier": plan.tier.value,
        "model_requested": _launcher.TIER_MODELS[plan.tier],
        "access": plan.access.value,
        "worktree": path,
        "worktree_id": wt_id,
        "terminal": terminal,
        "task": task,
        "dispatch": dispatch,
        "capability": capability,
        "gate_state": IDLE,
        "approval_rounds": 0,
        "session_id": plan.session_id,
        "transcript": str(plan.transcript) if plan.transcript else None,
        "command": plan.command,
    }
    data = state_read()
    data[f"{ticket}/{role.value}"] = record
    state_write(data)
    return record


# --- observation ---------------------------------------------------------


def status(ticket: str | None) -> list[dict]:
    """Deterministic state per spawn: what the dispatch says, and which model
    actually answered — read from the transcript, never asked of the agent."""

    out = []
    for key, rec in sorted(state_read().items()):
        if ticket and not key.startswith(ticket.upper() + "/"):
            continue
        try:
            shown = orca("orchestration", "dispatch-show", "--task", rec["task"])
            dispatch_status = str((shown.get("dispatch") or {}).get("status", "?"))
        except SystemExit:
            dispatch_status = "unknown"
        transcript = Path(rec["transcript"]) if rec.get("transcript") else None
        models = _launcher.observed_models(transcript) if transcript else []
        out.append(
            {
                "key": key,
                "dispatch_status": dispatch_status,
                "model_requested": rec["model_requested"],
                "model_observed": ",".join(models) or "(sem resposta ainda)",
                "tier_ok": bool(models) and all(rec["model_requested"] in m for m in models),
                # A role stuck at `verdict-approved` with an old
                # `last_event_at` is one whose worker_done never landed —
                # visible here rather than needing a verb of its own.
                "gate_state": rec.get("gate_state", IDLE),
                "approval_rounds": rec.get("approval_rounds", 0),
                "last_event_at": rec.get("last_event_at"),
                "worktree": rec["worktree"],
                "terminal": rec["terminal"],
            }
        )
    return out


def retire(ticket: str, role_value: str) -> dict:
    """Stop one role and close its pane. The worktree survives — the next
    role in the ticket needs it.

    `worker-stop` is tried but not relied on: it only knows dispatches that
    `worker-start` created, and this package launches through `terminal
    create` so it can set a model and permissions (GRE-179). Closing the
    terminal is what actually ends the role.
    """

    key = f"{ticket.upper()}/{role_value}"
    rec = state_read().get(key)
    if rec is None:
        raise SystemExit(f"no spawn recorded for {key}")

    effects = []
    try:
        orca("orchestration", "worker-stop", "--dispatch", rec["dispatch"])
        effects.append("worker-stop")
    except SystemExit:
        pass  # expected for terminal-created dispatches

    try:
        orca("terminal", "close", "--terminal", rec["terminal"], "--tab")
        effects.append("terminal closed")
    except SystemExit as exc:
        effects.append(f"terminal not closed ({exc})")

    # A killed role leaves its Task sitting in `dispatched` forever, which
    # Reconciliation would read as an attempt still in flight. Settle it.
    shown = orca("orchestration", "dispatch-show", "--task", rec["task"])
    status = str((shown.get("dispatch") or {}).get("status", ""))
    if status not in ("completed", "failed"):
        orca(
            "orchestration", "task-update", "--id", rec["task"], "--status", "failed",
            "--result", json.dumps({"reason": f"{role_value} retired by the Orchestrator"}),
        )
        effects.append("task settled as failed")

    data = state_read()
    data[key]["retired"] = True
    state_write(data)
    return {
        "retired": key,
        "dispatch_was": status,
        "effects": effects,
        "worktree_kept": rec["worktree"],
    }


# --- the plan gate ---------------------------------------------------------

IDLE = _gate.IDLE


def _flag_malformed(tracker, ticket: str, reason: str) -> None:
    tracker.set_attention(
        ticket,
        _attention.Attention(
            needs=True, code=_attention.AttentionCode.MALFORMED_REPORT, reason=reason,
        ),
    )


def _apply_gate_event(rec: dict, event, raw_by_id: dict) -> list[tuple[str, str | None]]:
    """The gate's decision for one event, isolated from the tracker/Linear
    calls `wait` makes: mutates `rec`'s `gate_state`/`question_id` in place
    and returns the `(action, reason)` pairs to execute — `reason` is only
    set for `flag_malformed`. No network call happens here, so this is what
    a unit test drives.

    A `plan-question` is only a gate event when its body is a real plan
    submission (`reports.is_plan_submission`) — a clarifying question from
    the planner is not a submission, does not light the label, and is left
    for the caller to treat as an ordinary message instead. A body that
    starts `## Plan` but does not otherwise parse is flagged rather than
    silently accepted or silently ignored.

    What the parsers return is kept, not discarded: the plan's pointer (the
    comment the next role has to read) and the round count both come from
    here, and the Ticket Key on the `## Plan` line is checked against the
    record so a report filed under the wrong ticket is caught rather than
    counted.
    """

    state = rec.get("gate_state", IDLE)
    raw = raw_by_id.get(event.message_id)

    # Orca does not drop a refused lifecycle message: it rewrites the subject
    # and body and marks the payload. Measured on 1.4.168 — a `worker_done`
    # without `dispatchId` comes back as a `worker_done` carrying
    # `_orcaLifecycleRejection`. Without this branch that arrival is a
    # completion that never completed anything.
    rejection = (raw.payload if raw else {}).get("_orcaLifecycleRejection")
    if rejection:
        return [(
            _gate.FLAG_MALFORMED,
            f"Orca refused this {event.kind}: "
            f"{rejection.get('code')}: {rejection.get('reason')}",
        )]

    if event.kind == "plan-question":
        if not _reports.is_plan_submission(event.question):
            return []
        try:
            submission = _reports.parse_plan_submission(event.question)
        except _reports.MalformedReport as exc:
            return [(_gate.FLAG_MALFORMED, str(exc))]
        if submission.ticket.upper() != str(rec.get("ticket", "")).upper():
            return [(
                _gate.FLAG_MALFORMED,
                f"plan submission is filed under {submission.ticket!r} but this "
                f"dispatch is {rec.get('ticket')!r}",
            )]
        result = _gate.transition(state, event, last_question_id=rec.get("last_question_id"))
        if result.question_id:
            rec["question_id"] = result.question_id
            rec["last_question_id"] = result.question_id
            rec["plan_pointer"] = submission.pointer
            rec["plan_decisions"] = list(submission.decisions)
            if result.state == _gate.SUBMITTED and state != _gate.SUBMITTED:
                # A round is a plan put in front of the human, counted here
                # so `spawn done` never asks the planner how many there were.
                # Re-asking the same pending question is not a new round.
                rec["approval_rounds"] = int(rec.get("approval_rounds", 0)) + 1
    elif event.kind == "worker-done":
        report_ok = True
        try:
            report = _reports.parse_planner_done(event.summary)
        except _reports.MalformedReport as exc:
            report_ok, report = False, None
            malformed_reason = str(exc)
        result = _gate.transition(state, event, report_ok=report_ok)
        if report is not None:
            rec["plan_pointer_final"] = report.plan_pointer
            rec["deviations"] = list(report.deviations)
        if not report_ok and _gate.FLAG_MALFORMED in result.actions:
            rec["gate_state"] = result.state
            rec["last_event_at"] = _now()
            return [(_gate.FLAG_MALFORMED, malformed_reason)]
    else:
        result = _gate.transition(state, event)

    rec["gate_state"] = result.state
    rec["last_event_at"] = _now()
    divergence = (
        f"gate event {event.kind!r} did not match the recorded gate_state {state!r}"
    )
    return [
        (action, divergence if action == _gate.FLAG_MALFORMED else None)
        for action in result.actions
    ]


def wait(*, ack: str | None, timeout_ms: int) -> dict:
    """The one loop the Orchestrator uses to hear back from every role.
    Wraps `orca orchestration check --wait`, then runs each gate event
    through `_apply_gate_event`/`plan_gate.transition` and executes the
    actions it returns — label the ticket, retire the planner, flag a
    divergence — never the Orchestrator reading a message and deciding by
    itself.

    Replay-safe: an unacked Delivery replays the same batch, but every
    action is guarded by the recorded `gate_state` (and, for plan-question,
    by the event's own id), so a repeat is a no-op (see `plan_gate.py`).

    The Linear tracker is built lazily, only when a gate action actually
    needs it — a `wait` that observes no gate action (the common case for
    every role but the planner) must work even without `LINEAR_API_KEY` set.
    """

    argv = [
        "orchestration", "check", "--wait",
        "--types", "worker_done,escalation,question",
        "--timeout-ms", str(timeout_ms),
    ]
    if ack:
        argv += ["--ack", ack]
    batch_raw = orca(*argv)
    # `orca()` already unwraps the envelope; `parse_check_output` expects the
    # raw JSON text of `check --peek/--wait --json`, so it is fed back in —
    # the same helper serves both call sites without a second parser.
    batch = _events.parse_check_output(json.dumps(batch_raw))
    events = _events.gate_events(batch.messages)
    raw_by_id = {m.id: m for m in batch.messages}

    data = state_read()
    by_dispatch = {rec["dispatch"]: key for key, rec in data.items()}
    # Attribution falls back to the task id because a REFUSED lifecycle
    # message is exactly the one whose `dispatchId` may be missing — that is
    # what it was refused for. Measured: the refusal keeps the rest of the
    # payload, `taskId` included, so it can still be traced to its role.
    by_task = {rec["task"]: key for key, rec in data.items()}
    actions_taken = []

    _tracker: list = []

    def tracker():
        if not _tracker:
            _tracker.append(_linear.LinearTracker())
        return _tracker[0]

    unattributed = []
    for event in events:
        dispatch_id = getattr(event, "dispatch_id", None)
        key = by_dispatch.get(dispatch_id) or by_task.get(getattr(event, "task_id", None))
        if key is None:
            # Neither id maps to a spawn this package started: it completes
            # nothing and would otherwise vanish. Reported, never swallowed.
            unattributed.append({
                "message": event.message_id, "kind": event.kind,
                "dispatch": dispatch_id, "task": getattr(event, "task_id", None),
            })
            continue
        if not key.endswith(f"/{GATE_ROLE.value}"):
            continue  # the gate governs only the planner's own dispatch
        rec = data[key]
        ticket = rec["ticket"]
        for action, reason in _apply_gate_event(rec, event, raw_by_id):
            if action == _gate.LABEL_ON:
                tracker().set_gate(ticket, True)
            elif action == _gate.LABEL_OFF:
                tracker().set_gate(ticket, False)
            elif action == _gate.RETIRE_PLANNER:
                retire(ticket, GATE_ROLE.value)
                # `retire` re-reads and rewrites the registry; without this
                # the `state_write` at the end of the loop would put this
                # stale copy back and resurrect the planner.
                rec["retired"] = True
            elif action == _gate.FLAG_MALFORMED:
                _flag_malformed(tracker(), ticket, reason or "malformed gate report")
            actions_taken.append({"ticket": ticket, "action": action})
    state_write(data)
    return {
        "delivery_id": batch.delivery_id,
        # Every mailbox message, not just the ones typed as gate events —
        # an escalation or a non-planner question must stay visible to the
        # Orchestrator even though the gate itself has nothing to do with it.
        "events": [
            {
                "id": m.id, "type": m.type, "subject": m.subject,
                "body": m.body, "payload": m.payload, "sender": m.sender,
            }
            for m in batch.messages
        ],
        "actions": actions_taken,
        "unattributed": unattributed,
    }


def verdict(ticket: str, decision: str, notes: str) -> dict:
    """The human's decision, as argv — never typed by an agent. Formats the
    response in the contract's format (b), replies to the recorded
    `question_id`, and lifts the `human-gate` label. `retire` does not
    happen here: it happens in `wait`, when the planner's `worker_done`
    arrives with the approved gate_state already recorded — one path for
    both APPROVED and APPROVED-with-caveats, and it never kills a planner
    still blocked inside its `ask`."""

    ticket = ticket.upper()
    if decision not in ("approved", "revise"):
        raise SystemExit(f"unknown decision {decision!r}; use 'approved' or 'revise'")

    key = f"{ticket}/{GATE_ROLE.value}"
    data = state_read()
    rec = data.get(key)
    if rec is None or not rec.get("question_id"):
        raise SystemExit(
            f"no pending plan submission recorded for {ticket}; "
            f"`spawn wait` must observe the submission before a verdict can be given"
        )
    if decision == "revise" and not notes.strip():
        raise SystemExit("REVISE with no --notes/--notes-file says nothing; name the correction")

    token = "APPROVED" if decision == "approved" else "REVISE"
    note_lines = [line.strip() for line in notes.splitlines() if line.strip()]
    body = _reports.format_approval_reply(token, note_lines)

    # Recorded BEFORE the reply goes out. The reply unblocks the planner,
    # which may run `spawn done` immediately; a registry that still said
    # `submitted` would refuse the very report the human just authorized.
    # `question_id` is kept until the reply lands, so a failed reply can be
    # retried instead of stranding the planner in its `ask`.
    rec["gate_state"] = _gate.VERDICT_APPROVED if decision == "approved" else _gate.VERDICT_REVISE
    rec["last_event_at"] = _now()
    data[key] = rec
    state_write(data)

    orca("orchestration", "reply", "--id", rec["question_id"], "--body", body)
    rec.pop("question_id", None)
    data[key] = rec
    state_write(data)

    # The label is cosmetic next to the verdict; losing Linear must not make
    # a delivered verdict look like a failure.
    label = "cleared"
    try:
        _linear.LinearTracker().set_gate(ticket, False)
    except Exception as exc:  # noqa: BLE001 - any tracker failure, reported not raised
        label = f"NOT cleared ({exc}); clear the human-gate label by hand"
    return {"ticket": ticket, "decision": token, "label": label}


# --- the role's own two verbs ---------------------------------------------
#
# Everything above is run by the Orchestrator. The two below are run BY A
# ROLE, inside its own dispatched terminal, and they are the return half of
# what `spawn plan` is on the way in: the role writes a body, the script
# builds and checks the message. Three things measured on Orca 1.4.168 are
# why this cannot be left to a role typing `orca orchestration` by hand:
#
#   1. `--payload` and the structured flags (`--task-id`, `--dispatch-id`,
#      `--outcome`) are MUTUALLY EXCLUSIVE — using both is `invalid_argument`
#      and no message is sent at all. Anything the gate needs beyond those
#      three fields therefore forces one single `--payload` carrying all of
#      them, which is not the shape Orca's own preamble teaches.
#   2. An injected dispatch mints a capability, and a lifecycle message
#      without it is refused with `dispatch_capability_invalid`.
#   3. A dispatch grants exactly one `worker_done`. A second is refused, so a
#      malformed report cannot be corrected — the body has to be checked
#      BEFORE the single shot is spent, which is what these verbs do.


def _payload_for(rec: dict, outcome: str, extra: dict | None = None) -> str:
    """The one `--payload` a lifecycle message is allowed to carry: the three
    fields Orca reconciles on, plus whatever the gate needs."""

    payload = {
        "taskId": rec["task"],
        "dispatchId": rec["dispatch"],
        "outcome": outcome,
        **(extra or {}),
    }
    return json.dumps(payload)


def submit(ticket: str, body_path: str, *, max_wait_ms: int) -> dict:
    """Send a Local Technical Plan for a verdict and block until it arrives.

    The verdict is parsed here and returned as a field. That is the other
    half of `spawn verdict`: the reply is written by a script on the
    coordinator's side and read by a script on the planner's side, so
    `APPROVED`/`REVISE` never depends on a model reading prose.

    `ask` caps its own wait at 30 minutes, and a human verdict routinely
    takes longer, so a timeout is resumed by message id rather than asked
    again — a second `ask` would be a second question, and the gate would
    have two submissions to reconcile.
    """

    ticket = ticket.upper()
    key, rec = own_record(ticket)
    if rec["role"] != GATE_ROLE.value:
        raise SystemExit(f"{key} is not the planner; only the planner submits a plan for a verdict")

    body = Path(body_path).read_text()
    submission = _reports.parse_plan_submission(body)  # loud, before anything is sent
    if submission.ticket.upper() != ticket:
        raise SystemExit(
            f"the '## Plan' line says {submission.ticket!r} but you are dispatched on {ticket!r}"
        )

    ask = ["orchestration", "ask", "--from", rec["terminal"]]
    if rec.get("capability"):
        ask += ["--dispatch-capability", rec["capability"]]

    waited, message_id, answer = 0, None, None
    while answer is None:
        if waited >= max_wait_ms:
            raise SystemExit(
                f"no verdict after {max_wait_ms}ms; the question is still pending as "
                f"{message_id} — resume it with a longer --max-wait-ms, never ask again"
            )
        argv = list(ask)
        argv += ["--resume", message_id] if message_id else ["--question", body]
        slice_ms = min(ASK_MAX_MS, max_wait_ms - waited)
        result = orca(*argv, "--timeout-ms", str(slice_ms))
        # `ask` answers with its own object and no {id, ok, result} envelope:
        # {answer, messageId, answerMessageId, threadId, timedOut, timeoutMs}.
        waited += int(result.get("timeoutMs") or slice_ms)
        message_id = str(result.get("messageId") or message_id or "")
        answer = result.get("answer")
        if answer is None and not result.get("timedOut"):
            raise SystemExit(
                f"ask ended without an answer and without a timeout "
                f"(cancelled={result.get('cancelled')}, "
                f"connectionLost={result.get('connectionLost')}); question {message_id} is pending"
            )

    try:
        parsed = _reports.parse_approval_reply(str(answer))
    except _reports.MalformedReport as exc:
        raise SystemExit(
            f"the verdict on question {message_id} does not follow the approval format "
            f"({exc}). It was answered by hand instead of by `spawn verdict`. Do NOT "
            f"submit again — that files a second question and costs another verdict; "
            f"escalate and ask for the verdict to be re-sent. Raw answer:\n{answer}"
        )
    return {
        "ticket": ticket,
        "question_id": message_id,
        "verdict": "approved" if parsed.approved else "revise",
        "notes": list(parsed.notes),
    }


def done(ticket: str, body_path: str, *, outcome: str, files_modified: str) -> dict:
    """The single `worker_done` a dispatch allows, built and checked here.

    For the planner it also refuses to fire before the gate recorded an
    approval — that check is what protects the one shot, since a `worker_done`
    that arrives without a recorded approval is flagged and cannot be resent.
    """

    ticket = ticket.upper()
    if outcome not in ("succeeded", "failed"):
        raise SystemExit(f"unknown outcome {outcome!r}; use 'succeeded' or 'failed'")
    key, rec = own_record(ticket)
    body = Path(body_path).read_text()
    extra: dict = {}

    if rec["role"] == GATE_ROLE.value and outcome == "succeeded":
        state = rec.get("gate_state", IDLE)
        if state != _gate.VERDICT_APPROVED:
            raise SystemExit(
                f"the plan for {ticket} is not approved (gate state {state!r}); a planner's "
                f"worker_done is only valid after APPROVED, and you get exactly one"
            )
        _reports.parse_planner_done(body)  # loud, before the shot is spent
        rounds = int(rec.get("approval_rounds", 1)) or 1
        # Only `## Approval` is rewritten, and only to state what the gate
        # counted. Everything else in the body is the planner's and is passed
        # through untouched — a report may carry sections this package does
        # not know about, and there is no second worker_done to resend them.
        body = _reports.set_approval_rounds(body, rounds)
        extra = {"planApproved": True, "approvalRounds": rounds}

    argv = [
        "orchestration", "send",
        "--from", rec["terminal"],
        "--type", "worker_done",
        "--subject", f"{ticket} {rec['role']}: {outcome}",
        "--body", body,
        "--payload", _payload_for(rec, outcome, extra),
    ]
    if rec.get("capability"):
        argv += ["--dispatch-capability", rec["capability"]]
    if files_modified.strip():
        # Never as `--files-modified`: that is a structured flag, and one of
        # those alongside `--payload` is refused outright.
        payload = json.loads(argv[argv.index("--payload") + 1])
        payload["filesModified"] = [f.strip() for f in files_modified.split(",") if f.strip()]
        argv[argv.index("--payload") + 1] = json.dumps(payload)

    orca(*argv, expect_lifecycle_ok=True)
    return {"ticket": ticket, "role": rec["role"], "outcome": outcome, "reported": key}


# --- CLI -----------------------------------------------------------------

VERBS = {
    "plan": (RoleName.PLANNER, True),
    "implement": (RoleName.IMPLEMENTER, False),
    "review-spec": (RoleName.SPEC_REVIEWER, False),
    "review-standards": (RoleName.STANDARDS_REVIEWER, False),
}


def main() -> int:
    parser = argparse.ArgumentParser(prog="spawn", description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb in VERBS:
        p = sub.add_parser(verb)
        p.add_argument("ticket")
        p.add_argument(
            "--tier",
            help="HUMAN OVERRIDE ONLY. Run this role at another Capability Tier "
            "than the matrix says. The Orchestrator never passes this.",
        )
    p = sub.add_parser("status")
    p.add_argument("ticket", nargs="?")
    p = sub.add_parser("retire")
    p.add_argument("ticket")
    p.add_argument("role")
    p = sub.add_parser("wait")
    p.add_argument("--ack", help="Delivery id to acknowledge before waiting again.")
    p.add_argument("--timeout-ms", type=int, default=900000)
    p = sub.add_parser("verdict")
    p.add_argument("ticket")
    p.add_argument("decision", choices=["approved", "revise"])
    p.add_argument("--notes", default="")
    p.add_argument("--notes-file")
    # The role's own two verbs. Run inside a dispatched terminal, by the role
    # itself — the Orchestrator never calls these.
    p = sub.add_parser("submit")
    p.add_argument("ticket")
    p.add_argument("--file", required=True, help="Path to the plan submission body.")
    p.add_argument("--max-wait-ms", type=int, default=6 * ASK_MAX_MS)
    p = sub.add_parser("done")
    p.add_argument("ticket")
    p.add_argument("--file", required=True, help="Path to the report body.")
    p.add_argument("--outcome", choices=["succeeded", "failed"], required=True)
    p.add_argument("--files-modified", default="")

    args = parser.parse_args()
    if args.verb in VERBS:
        role, fresh = VERBS[args.verb]
        print(json.dumps(
            spawn(role, args.ticket, fresh_worktree=fresh, tier=args.tier), indent=2
        ))
    elif args.verb == "status":
        print(json.dumps(status(args.ticket), indent=2))
    elif args.verb == "retire":
        print(json.dumps(retire(args.ticket, args.role), indent=2))
    elif args.verb == "wait":
        print(json.dumps(wait(ack=args.ack, timeout_ms=args.timeout_ms), indent=2))
    elif args.verb == "verdict":
        notes = args.notes
        if args.notes_file:
            notes = (notes + "\n" + Path(args.notes_file).read_text()).strip()
        print(json.dumps(verdict(args.ticket, args.decision, notes), indent=2))
    elif args.verb == "submit":
        print(json.dumps(
            submit(args.ticket, args.file, max_wait_ms=args.max_wait_ms), indent=2
        ))
    elif args.verb == "done":
        print(json.dumps(done(
            args.ticket, args.file,
            outcome=args.outcome, files_modified=args.files_modified,
        ), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
