#!/usr/bin/env python3
"""The whole spawn interface the Orchestrator is allowed to use.

TLDR: the four role verbs take one argument each — a Ticket Key — plus
`status`, `retire`, `sweep`, `brief`, and the plan gate pair `wait`/`verdict`.
The Orchestrator never picks a model, a permission flag, a worktree or a
launch path; every one of those is decided here. If a command below does not
express what you need, that is a package change, not an improvisation at
the terminal.

    .symphonia/bin/spawn plan             GRE-181
    .symphonia/bin/spawn implement        GRE-181
    .symphonia/bin/spawn review-spec      GRE-181
    .symphonia/bin/spawn review-standards GRE-181
    .symphonia/bin/spawn status          [GRE-181]
    .symphonia/bin/spawn retire           GRE-181 planner
    .symphonia/bin/spawn sweep           [GRE-181]
    .symphonia/bin/spawn brief            GRE-181 --file cut.md
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
plan gate: it turns mailbox messages into typed events (`adapters/orca/events.py`)
and hands them to `workflow.gate_loop.run`, which runs each through
`adapters/plan_gate.py` and executes what comes back (label the ticket,
retire the planner, flag a divergence). `verdict` is how the human's
decision reaches the planner: it never comes from an agent typing
`APPROVED`/`REVISE` into a reply by hand.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import os
import subprocess
import sys
# Session ids are minted inside `ClaudeHarness.prepare()` now
# (`adapters/harnesses/claude.py`, GRE-186 S3), not here — but `uuid` is
# still imported so the name resolves as `spawn.uuid`. `claude.py` imports
# the identical shared module object, so a test that patches `uuid4`
# through this attribute (`self.spawn.uuid`) still reaches the code that
# actually calls it.
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from adapters import harness_adapter as _harness_adapter
from adapters import plan_gate as _gate
from adapters import reports as _reports
from adapters import runtime_adapter as _contract
from adapters.linear import adapter as _linear
from adapters.linear import client as _linear_client
from adapters.orca import adapter as _cli
from adapters.orca import events as _events
from adapters.harnesses import claude as _claude
import setup_worktree as _setup_worktree
from workflow import gate_loop as _gate_loop
from workflow import roles as _roles

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
# Writers: `spawn`, `wait`, `verdict`, `retire`, `teardown`, `sweep` — all
# Orchestrator-side. The role-side verbs (`submit`, `done`) only ever read,
# so two processes in two checkouts never race for this file.
#
# The record shape below (`ticket`, `dispatch`, `task`, `gate_state`, ...) is
# internal to this package, not a public contract: nothing outside `spawn.py`
# and `workflow/gate_loop.py` is entitled to depend on it, and it can change
# shape between versions without notice.
RUNTIME_DIR = Path(os.environ.get("SYMPHONIA_RUNTIME", "~/.symphonia/runtime")).expanduser()
STATE = RUNTIME_DIR / "spawns.json"
# A sibling, not `STATE` itself: `state_write` swaps the inode via
# `os.replace`, so an flock held on the file being replaced protects
# nothing once the swap happens.
STATE_LOCK = STATE.with_name(STATE.name + ".lock")
# The baton between roles is a document dropped at a configured directory —
# not a format this package invents, and (GRE-186 S3) not a path this
# package hardcodes either: `"handoff_dir"` in `config.json` names it, this
# function is the one place that reads that key (C5 of the S3 verdict), and
# every caller below goes through it rather than reading the key itself.
#
# It lives outside the repository on purpose: it is never committed, never
# travels with the branch, and outlives nothing. Its only job is carrying
# context from the role that just died to the one about to start. Moving it
# into the worktree would turn a disposable note into a versioned artifact
# that reviewers have to maintain — do not.
_HANDOFF_DIR_DEFAULT = "~/orca/.context"


def _handoff_dir() -> str:
    config = json.loads((PACKAGE / "config.json").read_text())
    return config.get("handoff_dir", _HANDOFF_DIR_DEFAULT)


# The role matrix (tier, access, role file) lives only in each role file's
# own frontmatter now — `workflow.roles.load_policies` reads it, and fails
# at bootstrap rather than letting a missing/malformed declaration launch
# something undeclared (GRE-186 S1). No cache: every verb call re-reads the
# four small files, so an edit to a role file takes effect on the next spawn
# without restarting anything.
ROLES_DIR = PACKAGE / "roles"


def _policies() -> dict[RoleName, _roles.RolePolicy]:
    return _roles.load_policies(ROLES_DIR)


def _harness() -> _harness_adapter.HarnessAdapter:
    """The one harness this package composes over — hardcoded, not looked
    up from `config.json` (decision 6 / item 5 of GRE-186): a registry keyed
    by provider and a `config.harness` setting are deferred until a second
    harness exists to prove the seam is real, not speculative."""

    return _claude.ClaudeHarness()

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

    # GRE-184 M5: the transport is `_adapter()._orca`, not a direct
    # `subprocess_runner` call — the last piece of the seam. `wait`/
    # `verdict`/`submit`/`done` (GRE-185's boundary) still call this
    # function by the same name, signature, and error text; nothing below
    # this line changed.
    try:
        return _adapter()._orca(*argv, expect_lifecycle_ok=expect_lifecycle_ok)
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


@contextlib.contextmanager
def state_lock() -> Iterator[None]:
    """Serializes a read-modify-write cycle of the registry across
    processes: `wait` can sit on a snapshot for up to 15 minutes while a
    concurrent `verdict` writes `gate_state` elsewhere, and without this the
    read-modify-write in either one can silently revert the other's write.

    Hold this ONLY around a `state_read()`/`state_write()` pair, never
    around the blocking `check --wait` itself — that wait is the up-to-15-
    minute part, and holding the lock across it would starve every
    concurrent `verdict` for as long as `wait` sits idle.

    flock, not a POSIX record lock: two separate `os.open()` calls on
    `STATE_LOCK` conflict even from the SAME process, because a flock is
    held by the open file description, not the process. That means this
    lock does not nest — `teardown()` must never call `state_lock()` while
    it is invoked from inside `wait`'s critical section (which it is, via
    `workflow.gate_loop.run`'s `retire_planner`/`retire_role` actions): a
    second acquisition here would deadlock the one process holding the
    first. `teardown()` stays unlocked; `wait`, `verdict`, and `sweep`
    acquire this.
    """

    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE.parent, 0o700)  # a directory that already existed
    fd = os.open(STATE_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- the runtime adapter, composed -----------------------------------------


def _adapter() -> _cli.OrcaRuntimeAdapter:
    """The production adapter `spawn()` composes its verbs over (GRE-184
    M4): `find_workspace`/`create_workspace`/`default_base`/`set_phase`/
    `open_context`/`dispatch`/`snapshot`. `coordinator`/`run_id` are inert
    placeholders — `bind_control=False` (approval condition #3 of the PR-A
    plan) means `_ensure_control` never reads them, and none of the methods
    above reference `self.coordinator` either; only `message_worker`/
    `respond`/`sweep` do, and this package never calls those. Consequence,
    same as documented on the flag itself: `spawn`'s production path never
    exercises control-lost detection — the conformance suite (which always
    binds) is the only place that check runs."""

    return _cli.OrcaRuntimeAdapter(coordinator="spawn", run_id="spawn", bind_control=False)


# --- the spawn itself ----------------------------------------------------


# --- the Execution Brief (every role's input) ------------------------------


def _current_branch(workspace: str) -> str:
    proc = subprocess.run(
        ["git", "-C", workspace, "branch", "--show-current"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() or "(unknown)"


def _head(workspace: str) -> str:
    """The commit a worktree is on, or `""` if git could not answer — the
    same "empty/erro -> empty string" shape as `_current_branch`. Recorded
    at dispatch as `head_at_dispatch`, the baseline `done()` compares
    against to catch an empty `succeeded`."""

    proc = subprocess.run(
        ["git", "-C", workspace, "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip()


def _handoff_file(ticket: str) -> tuple[Path | None, int]:
    """The one handoff a role's Brief may point to, plus how many other
    files were passed over — item 7 of GRE-187: a role must never have to
    choose which of two documents to believe.

    The canonical path, `{handoff_dir}/{ticket_lower}.md`, always wins when
    it exists: it is the only file a role's own "How to finish" instructs
    it to write, so its presence alone proves it is current. Legacy files
    from before this round (`{ticket_lower}-*.md`) are a fallback, picked by
    newest mtime — the lexicographic name mixes role and date and would lie
    about which one is current.
    """

    directory = Path(os.path.expanduser(_handoff_dir()))
    canonical = directory / f"{ticket.lower()}.md"
    legacy = sorted(directory.glob(f"{ticket.lower()}-*.md"))
    if canonical.exists():
        return canonical, len(legacy)
    if not legacy:
        return None, 0
    newest = max(legacy, key=lambda p: p.stat().st_mtime)
    return newest, len(legacy) - 1


def build_brief(role: RoleName, ticket: str, workspace: str, *, tracker=None) -> str:
    """Assembles the Execution Brief injected at dispatch: extracts the
    ``io:brief-template`` block from the role's own file and fills it from
    the ticket. The role opens with the ticket already in hand — zero tool
    call needed to fetch it (`orca linear` may be disconnected; this reads
    the tracker adapter directly, same as GRE-174)."""

    ticket = ticket.upper()
    policy = _policies()[role]
    role_path = ROLES_DIR / policy.role_file
    template = _reports.extract_block(role_path.read_text(), "md io:brief-template")

    tracker = tracker or _linear.LinearTracker()

    item = tracker.get_item(ticket)
    comments = tracker.list_comments(item.ref.id)
    comment_text = "\n\n".join(
        f"**{c.author_name or 'unknown'} · {c.created_at[:10]}**\n\n{c.body}"
        for c in comments
    ) or "None."

    handoff_file, superseded = _handoff_file(ticket)
    if handoff_file is None:
        handoff_text = "None — this is the first role on this ticket."
    else:
        handoff_text = f"- {handoff_file}"
        if superseded:
            handoff_text += (
                f"\n\n({superseded} older handoff(s) superseded — "
                f"deliberately not part of your context.)"
            )

    values = {
        "ticket_key": ticket,
        "ticket_lower": ticket.lower(),
        "role": role.value,
        "role_file": f".symphonia/roles/{policy.role_file}",
        "workspace": workspace,
        "branch": _current_branch(workspace),
        "title": item.title,
        "url": item.ref.url,
        "description": item.body or "(no description)",
        "comments": comment_text,
        "handoff_files": handoff_text,
        "handoff_dir": _handoff_dir(),
        "handoff_hint": _harness().handoff_hint(),
    }
    try:
        return template.format(**values)
    except KeyError as exc:
        raise SystemExit(
            f"Brief template in {role_path} references unknown placeholder {exc}"
        )


# --- role identity: how a role finds its own record -----------------------


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
    """Composed over `OrcaRuntimeAdapter` and one `HarnessAdapter` (GRE-186
    S3) — every orca call below lives in `_adapter()`'s methods, the launch
    itself in `_harness().prepare()`, not here. Item 10 of GRE-181 ("failure
    mid-sequence leaves no orphan"): a fresh workspace is torn down if
    `set_phase`/`prepare`/`open_context` fails; `dispatch`'s one
    characterized failure (a dispatch that minted no capability) already
    rolls its own task/terminal back inside the adapter (GRE-184 M3) —
    unchanged from what M0 froze, so this wrapper only translates the
    error, it does not clean up a second time. A workspace is never
    destroyed once reused.
    """

    ticket = ticket.upper()
    adapter = _adapter()
    workspace = adapter.find_workspace(ticket)
    fresh = False
    if fresh_worktree:
        if workspace is not None:
            raise SystemExit(
                f"{ticket} already has a worktree at {workspace.path}. "
                f"Planning runs once per ticket; use `implement` to continue in it."
            )
        try:
            workspace = adapter.create_workspace(ticket, base_branch=adapter.default_base())
        except (RuntimeError, _cli.OrcaCliError) as exc:
            raise SystemExit(str(exc)) from exc
        fresh = True
        # The env files are gitignored, so the new checkout does not have them —
        # it looks complete and fails the first time something reads `.env`.
        # Called here rather than relying on Orca's repo setup hook, which is a
        # per-machine setting and is empty until someone fills it in: a spawn must
        # not depend on a checkbox. Running it twice is harmless.
        print(json.dumps(_setup_worktree.setup(Path(workspace.path))), file=sys.stderr)
    else:
        if workspace is None:
            raise SystemExit(
                f"{ticket} has no worktree yet. Run `spawn plan {ticket}` first — "
                f"every role after the planner reuses the checkout planning created."
            )

    # `--tier` is a human override, not a knob for the Orchestrator: the
    # policy is the default precisely so no agent has to choose a model.
    policy = _policies()[role]
    if tier:
        try:
            policy = dataclasses.replace(policy, tier=_contract.CapabilityTier(tier))
        except ValueError:
            raise SystemExit(
                f"unknown tier {tier!r}; known: "
                f"{', '.join(t.value for t in _contract.CapabilityTier)}"
            )

    harness = _harness()
    try:
        adapter.set_phase(workspace, role)
        prepared = harness.prepare(workspace=workspace, policy=policy)
        context = adapter.open_context(workspace, role=role, access=policy.access, launch=prepared)
    except (RuntimeError, _cli.OrcaCliError, _harness_adapter.HarnessRefusal) as exc:
        if fresh:
            try:
                adapter.destroy_workspace(workspace)
            except (RuntimeError, _cli.OrcaCliError):
                pass  # best-effort; the launch failure is what gets reported
        raise SystemExit(str(exc)) from exc

    # Every role's input is the Execution Brief, injected: the ticket is
    # read and formatted here, never left for the role to go fetch.
    spec = build_brief(role, ticket, workspace.path)
    try:
        attempt = adapter.dispatch(context, spec)
    except _cli.OrcaCliError as exc:
        raise SystemExit(str(exc)) from exc
    snap = adapter.snapshot(attempt)
    head_at_dispatch = _head(workspace.path)

    record = {
        "ticket": ticket,
        "role": role.value,
        "tier": policy.tier.value,
        "access": snap["access"],
        "worktree": workspace.path,
        "worktree_id": workspace.id,
        # The baseline `done()` compares HEAD against to catch a `succeeded`
        # that changed nothing (GRE-187 item 6). `""` on a git error — never
        # raised here, since a `spawn` that fails on a measurement it does
        # not need would be its own bug.
        "head_at_dispatch": head_at_dispatch,
        "terminal": snap["terminal"],
        "task": snap["task"],
        "dispatch": snap["dispatch"],
        "capability": snap["capability"],
        "gate_state": IDLE,
        "approval_rounds": 0,
        # Honest by construction (decision 3 of the GRE-186 S3 verdict):
        # `requested` is what launch itself can ever prove; nothing reads
        # this before `status()` calls `harness.observe()` for the strong
        # check. Deliberately never re-read afterwards either: this records
        # what was known at launch, and `observe()` is the live source of
        # what's known now — reading the record back in its place would
        # trade a live value for a stale one, the dishonesty this ticket
        # exists to kill.
        "tier_evidence": {
            "kind": "requested",
            "tier": policy.tier.value,
            "detail": "recorded at launch; no observation yet",
        },
        # The ContextRef <-> HarnessSession association (decision 5): the
        # loose `session_id`/`transcript` fields this replaces are gone.
        "session": {"id": snap["session_id"], "observation_ref": snap["observation_ref"]},
        "command": snap["command"],
    }
    data = state_read()
    data[f"{ticket}/{role.value}"] = record
    state_write(data)
    return record


# --- observation ---------------------------------------------------------


def status(ticket: str | None) -> list[dict]:
    """Deterministic state per spawn: what the dispatch says, and what tier
    evidence the harness can produce — `harness.observe()` reads it,
    never this function guessing from a transcript itself (GRE-186 S3: the
    model-alias-vs-transcript comparison left the core along with
    `verify_tier`).

    Tolerant of a record from before this round: one with no `session`/
    `tier_evidence` (the old `model_requested` shape) reads as
    `evidence_kind="unverifiable"`, named as a pre-GRE-186 record — nothing
    more elaborate than that; the record format is not a contract (decision
    5 of the S3 verdict)."""

    adapter = _adapter()
    harness = _harness()
    out = []
    for key, rec in sorted(state_read().items()):
        if ticket and not key.startswith(ticket.upper() + "/"):
            continue
        try:
            dispatch_status = adapter.dispatch_status(rec["task"])
        except _cli.OrcaCliError:
            dispatch_status = "unknown"
        if "session" in rec and "tier_evidence" in rec:
            session = _harness_adapter.HarnessSession(**rec["session"])
            requested = _contract.CapabilityTier(rec["tier"])
            evidence = harness.observe(session, requested)
            evidence_kind, evidence_detail = evidence.kind, evidence.detail
        else:
            evidence_kind = "unverifiable"
            evidence_detail = "pre-GRE-186 record; no session/tier_evidence to observe"
        out.append(
            {
                "key": key,
                "dispatch_status": dispatch_status,
                "tier": rec.get("tier"),
                "evidence_kind": evidence_kind,
                "evidence_detail": evidence_detail,
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
    role in the ticket needs it. The Orchestrator's own verb, typed by hand
    or by a script — not idempotent, unlike `teardown`: a human retiring an
    already-dead role a second time still deserves the same effects and the
    same answer, not a silent no-op. This is a deliberate choice, pinned by
    `RetireRerunsEffectsOnADeadRole` in `test_teardown_sweep.py` —
    `test_retire_self_guard` only pins the self-guard above, not this.

    `worker-stop` is tried but not relied on: it only knows dispatches that
    `worker-start` created, and this package launches through `terminal
    create` so it can set a model and permissions (GRE-179). Closing the
    terminal is what actually ends the role.
    """

    key = f"{ticket.upper()}/{role_value}"
    rec = state_read().get(key)
    if rec is None:
        raise SystemExit(f"no spawn recorded for {key}")

    # A role can name its own terminal here — `retire GRE-188 planner` run
    # from inside the planner's own pane. Nothing below guards against it:
    # `stop_worker` and `close_terminal` would kill the process running this
    # very function, so the caller loses its terminal mid-command and never
    # sees the task get settled, or any error explaining what happened.
    # Refuse instead. Retiring is the Orchestrator's verb, and it runs from
    # a terminal that is nobody's role.
    own_terminal = os.environ.get("ORCA_TERMINAL_HANDLE", "")
    if own_terminal and own_terminal == rec.get("terminal"):
        raise SystemExit(
            f"{key} is the role running this command; retiring it would close "
            f"this terminal mid-command and lose whatever it has not reported "
            f"yet. Report first and let the Orchestrator retire you."
        )

    return _run_teardown(ticket, role_value)


def teardown(ticket: str, role_value: str) -> dict:
    """Same effects as `retire`, minus the self-guard, plus idempotence: a
    record already marked `retired` returns immediately, without touching
    the adapter. Called by the gate loop when a non-planner's `worker_done`
    arrives, and by `sweep` for a record whose world is already gone — both
    a replayed Delivery and a repeated `sweep` must be a no-op, which is
    what the guard here is for. `retire` does not use it: a human retiring
    an already-dead role by hand still gets the same effects run again, not
    a silent no-op.
    """

    key = f"{ticket.upper()}/{role_value}"
    rec = state_read().get(key)
    if rec is None:
        raise SystemExit(f"no spawn recorded for {key}")
    if rec.get("retired"):
        return {"retired": key, "effects": ["already retired"], "worktree_kept": rec["worktree"]}
    return _run_teardown(ticket, role_value)


def _run_teardown(ticket: str, role_value: str) -> dict:
    """Every effect of ending one role's dispatch, best-effort, in a fixed
    order: `stop_worker` -> `close_terminal --tab` -> settle the Task ->
    mark `retired` in the registry. Nothing here raises on a failed effect:
    a dead terminal or an unreachable dispatch is exactly the situation
    this exists to clean up after, so each failure becomes an `effects`
    entry instead of aborting before `retired` is written.
    """

    adapter = _adapter()
    key = f"{ticket.upper()}/{role_value}"
    rec = state_read().get(key)
    if rec is None:
        raise SystemExit(f"no spawn recorded for {key}")

    effects = []
    try:
        adapter.stop_worker(rec["dispatch"])
        effects.append("worker-stop")
    except _cli.OrcaCliError:
        pass  # expected for terminal-created dispatches

    try:
        adapter.close_terminal(rec["terminal"], tab=True)
        effects.append("terminal closed")
    except _cli.OrcaCliError as exc:
        effects.append(f"terminal not closed ({exc})")

    # A killed role leaves its Task sitting in `dispatched` forever, which
    # Reconciliation would read as an attempt still in flight. Settle it —
    # best-effort like everything else here, so a CLI outage on this one
    # call cannot stop `retired` from ever being written.
    dispatch_status = ""
    try:
        dispatch_status = adapter.dispatch_status(rec["task"], default="")
    except _cli.OrcaCliError as exc:
        effects.append(f"task NOT settled ({exc})")
    else:
        if dispatch_status not in ("completed", "failed"):
            try:
                adapter.settle_task(rec["task"], f"{role_value} retired by the Orchestrator")
                effects.append("task settled as failed")
            except _cli.OrcaCliError as exc:
                effects.append(f"task NOT settled ({exc})")

    data = state_read()
    if key in data:
        data[key]["retired"] = True
        state_write(data)
    else:
        effects.append("registry record vanished before the retired write")
    return {
        "retired": key,
        "dispatch_was": dispatch_status,
        "effects": effects,
        "worktree_kept": rec["worktree"],
    }


def sweep(ticket: str | None) -> list[dict]:
    """Audit the registry for a record whose world is already gone — its
    terminal not among Orca's live handles, or its worktree missing from
    disk — and tear it down without being told which ticket/role died.
    What `retired` records are for `wait`'s gate on a `worker_done`, this is
    for everything that never reported at all: a role killed by the app
    quitting, a machine that lost power, a worktree deleted by hand.

    A live record is reported, not touched — `{"key": ..., "live": True}`.
    Already-retired records are not this verb's business; `teardown`'s own
    idempotence would no-op them anyway, but skipping them here keeps a
    `sweep` report about the records that still needed a decision.

    The whole read-decide-teardown loop runs under `state_lock()`: this is
    the only writer in the package that used to do its read-modify-write
    unlocked, which let a concurrent `wait`/`verdict` (holding the lock
    across its own snapshot) silently revert a `teardown` this loop had
    already run — after that teardown's effects (`close_terminal`,
    `settle_task`) were already irreversible. `teardown` itself stays
    unlocked, so this does not nest.

    A `list_terminals()` that comes back empty is refused rather than acted
    on: every unretired record would read as "terminal not live" and this
    loop would tear down every live role in the registry on the strength of
    one degraded CLI response. No retry, no heuristic — just refuse and say
    why; the caller can run `sweep` again once the CLI is answering.
    """

    adapter = _adapter()
    live_terminals = adapter.list_terminals()
    if not live_terminals:
        raise SystemExit(
            "list_terminals() returned no terminals at all; refusing to treat "
            "every unretired record as an orphan. Retry once the CLI is "
            "responding."
        )
    out = []
    with state_lock():
        for key, rec in sorted(state_read().items()):
            if ticket and not key.startswith(ticket.upper() + "/"):
                continue
            if rec.get("retired"):
                continue
            reasons = []
            if rec["terminal"] not in live_terminals:
                reasons.append("terminal not live")
            if not Path(rec["worktree"]).exists():
                reasons.append("worktree missing")
            if not reasons:
                out.append({"key": key, "live": True})
                continue
            out.append({
                "key": key, "live": False, "reason": reasons,
                "teardown": teardown(rec["ticket"], rec["role"]),
            })
    return out


# --- the plan gate ---------------------------------------------------------

IDLE = _gate.IDLE


def wait(*, ack: str | None, timeout_ms: int) -> dict:
    """The one loop the Orchestrator uses to hear back from every role.
    Wraps `orca orchestration check --wait`, then hands the batch to
    `workflow.gate_loop.run`, which runs each gate event through
    `apply_gate_event`/`plan_gate.transition` and executes what comes
    back — label the ticket, retire the planner, flag a divergence —
    never the Orchestrator reading a message and deciding by itself.

    Replay-safe: an unacked Delivery replays the same batch, but every
    action is guarded by the recorded `gate_state` (and, for plan-question,
    by the event's own id), so a repeat is a no-op (see `plan_gate.py`).

    The Linear tracker is built lazily, only when a gate action actually
    needs it — a `wait` that observes no gate action (the common case for
    every role but the planner) must work even without `LINEAR_API_KEY` set.

    The blocking `check --wait` runs BEFORE `state_lock()` is taken — it is
    the up-to-15-minute wait itself, and holding the lock across it would
    starve every concurrent `verdict` for as long as this call sits idle.
    Only the read-modify-write that follows is locked.
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

    with state_lock():
        data = state_read()
        actions_taken, unattributed = _gate_loop.run(
            events, raw_by_id, data,
            tracker=lambda: _linear.LinearTracker(),
            teardown=teardown,
            gate_role=GATE_ROLE.value,
        )
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
    `question_id`, and lifts the `human-gate` label. On `approved`, also
    posts the one and only copy of the plan the ticket ever gets — the
    recorded `plan_body` plus the `## Approval` verdict — never on
    submission (that would post an unapproved plan) and never on `revise`
    (that would post once per round). `retire` does not happen here: it
    happens in `wait`, when the planner's `worker_done` arrives with the
    approved gate_state already recorded — one path for both APPROVED and
    APPROVED-with-caveats, and it never kills a planner still blocked
    inside its `ask`.

    The read, both writes and the reply all happen inside one
    `state_lock()`: the reply is deliberately inside the lock, not just the
    registry writes around it. That preserves the two invariants below
    (state recorded before the reply, `question_id` retained until the
    reply lands) against a concurrent `wait`, and costs that `wait` only the
    duration of one local `orca reply` call, not the network round trip
    `wait` itself makes.
    """

    ticket = ticket.upper()
    if decision not in ("approved", "revise"):
        raise SystemExit(f"unknown decision {decision!r}; use 'approved' or 'revise'")

    key = f"{ticket}/{GATE_ROLE.value}"
    token = "APPROVED" if decision == "approved" else "REVISE"
    note_lines = [line.strip() for line in notes.splitlines() if line.strip()]
    body = _reports.format_approval_reply(token, note_lines)

    with state_lock():
        data = state_read()
        rec = data.get(key)
        if rec is None or not rec.get("question_id"):
            raise SystemExit(
                f"no pending plan submission recorded for {ticket}; "
                f"`spawn wait` must observe the submission before a verdict can be given"
            )
        if decision == "revise" and not notes.strip():
            raise SystemExit("REVISE with no --notes/--notes-file says nothing; name the correction")

        # Recorded BEFORE the reply goes out. The reply unblocks the planner,
        # which may run `spawn done` immediately; a registry that still said
        # `submitted` would refuse the very report the human just authorized.
        # `question_id` is kept until the reply lands, so a failed reply can
        # be retried instead of stranding the planner in its `ask`.
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
    tracker = None
    label = "cleared"
    try:
        tracker = _linear.LinearTracker()
        tracker.set_gate(ticket, False)
    except Exception as exc:  # noqa: BLE001 - any tracker failure, reported not raised
        label = f"NOT cleared ({exc}); clear the human-gate label by hand"

    result = {"ticket": ticket, "decision": token, "label": label}
    if decision == "approved":
        # Published here, on the approved path only — never on submission,
        # which would put an unapproved plan on the ticket, and never on a
        # REVISE, which would post one comment per round. `plan_body` is the
        # raw submission `wait` recorded off the genuine plan-question; a
        # record from before this round carries no such field. `body` is the
        # same `## Approval` text just sent in the reply above — reused
        # instead of recomputed from the same token/notes.
        plan_body = rec.get("plan_body")
        if plan_body is None:
            result["plan_copy"] = "NOT posted (no plan_body recorded)"
        else:
            comment = f"{plan_body.rstrip()}\n\n## Approval\n\n{body}\n"
            try:
                (tracker or _linear.LinearTracker()).post_comment(ticket, comment)
                result["plan_copy"] = "posted"
            except Exception as exc:  # noqa: BLE001 - any tracker failure, reported not raised
                result["plan_copy"] = f"NOT posted ({exc})"
    return result


def brief(ticket: str, body_path: str) -> dict:
    """Post a wave's coordination note as a ticket comment — the
    Orchestrator's sanctioned way to hand a role a cut of work, instead of
    typing into the tracker's own client by hand. `build_brief` already
    composes every ticket comment into the Execution Brief every role
    opens with, so posting the comment IS the whole job here — unlike
    `verdict`, where posting a comment is a side effect of a verdict
    already delivered (and a tracker failure there is reported, not
    raised), here nothing else happens if the post fails, so there is no
    try/except: `LinearError` (`linear_not_connected` included) propagates
    with its own traceback, never a silent fallback to another client.
    """

    ticket = ticket.upper()
    path = Path(body_path)
    if not path.is_file():
        raise SystemExit(f"{body_path} does not exist; nothing to post")
    body = path.read_text()
    if not body.strip():
        raise SystemExit(f"{body_path} is empty; an empty cut of work coordinates nothing")

    comment = _linear.LinearTracker().post_comment(ticket, body)
    return {"ticket": ticket, "posted": True, "comment": comment.id}


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


def _worktree_measurement(workspace: str) -> tuple[str, bool] | None:
    """`(HEAD, tree is dirty)`, or `None` if git itself could not answer —
    a worktree gone missing, a full disk, a repository in a broken state.
    `None` is not "clean": the caller must never read a failed measurement
    as proof that nothing changed (GRE-187 item 6 verdict ressalva)."""

    head_proc = subprocess.run(
        ["git", "-C", workspace, "rev-parse", "HEAD"], capture_output=True, text=True,
    )
    status_proc = subprocess.run(
        ["git", "-C", workspace, "status", "--porcelain"], capture_output=True, text=True,
    )
    if head_proc.returncode != 0 or status_proc.returncode != 0:
        return None
    return head_proc.stdout.strip(), bool(status_proc.stdout.strip())


def done(ticket: str, body_path: str, *, outcome: str, files_modified: str) -> dict:
    """The single `worker_done` a dispatch allows, built and checked here.

    For the planner it also refuses to fire before the gate recorded an
    approval — that check is what protects the one shot, since a `worker_done`
    that arrives without a recorded approval is flagged and cannot be resent.

    Two more refusals, both role-side and local (GRE-187 item 6): an empty
    body, for any role and either outcome — a `failed` with no explanation
    serves nobody either — and, for a write-access non-planner role
    reporting `succeeded`, a worktree that shows no change at all against
    `head_at_dispatch`. Both fire BEFORE anything is sent, same as the
    planner's parse checks above: the one shot a dispatch grants is never
    spent on an empty success.

    A third case never refuses: the same role reporting `succeeded` with a
    dirty tree but no new commit. That is real work, just not persisted —
    refusing it would destroy the report to save a five-second `git commit`.
    It is accepted and flagged `uncommitted_work: true` on the record
    instead, so the Orchestrator can check without opening the worktree.
    """

    ticket = ticket.upper()
    if outcome not in ("succeeded", "failed"):
        raise SystemExit(f"unknown outcome {outcome!r}; use 'succeeded' or 'failed'")
    key, rec = own_record(ticket)
    body = Path(body_path).read_text()
    if not body.strip():
        raise SystemExit(
            f"{ticket}/{rec['role']}: an empty report says nothing; `done` needs a body "
            f"even for --outcome failed"
        )
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

    if outcome == "succeeded" and rec.get("access") == "write" and rec["role"] != GATE_ROLE.value:
        baseline = rec.get("head_at_dispatch")
        if not baseline:
            # No baseline means the whole check is skipped, dirty-tree half
            # included — not just the HEAD comparison. A record from before
            # this round has no dirty-tree evidence to trust either, and
            # refusing on a partial check would trap an old record with a
            # clean tree, which is exactly what this skip exists to avoid.
            print(
                f"note: {ticket}/{rec['role']} has no head_at_dispatch recorded "
                f"(pre-GRE-187 dispatch); skipping the empty-success check entirely "
                f"(HEAD comparison and dirty-tree check both)", file=sys.stderr,
            )
        else:
            measured = _worktree_measurement(rec["worktree"])
            if measured is None:
                print(
                    f"note: could not measure {rec['worktree']} (git error); "
                    f"skipping the empty-success check", file=sys.stderr,
                )
            else:
                head_now, dirty = measured
                if head_now == baseline and not dirty:
                    raise SystemExit(
                        f"{ticket}/{rec['role']}: outcome=succeeded but the worktree shows no "
                        f"change — HEAD is still {head_now} (same as at dispatch) and `git "
                        f"status --porcelain` is empty. An empty success is exactly what this "
                        f"gate exists to catch. If there genuinely was nothing to do, report "
                        f"`--outcome failed --file <report explaining why>` instead, so the "
                        f"Orchestrator can decide."
                    )
                if head_now == baseline and dirty:
                    # The dirty tree is real work — this is not the empty
                    # success above — but it never made it into a commit, and
                    # uncommitted work does not survive the worktree. Accept,
                    # never refuse: flag it on the record so the Orchestrator
                    # can check without opening the worktree by hand.
                    print(
                        f"note: {ticket}/{rec['role']} outcome=succeeded but HEAD is still "
                        f"{head_now} (same as at dispatch) — the tree is dirty but nothing was "
                        f"committed; flagging uncommitted_work on the record", file=sys.stderr,
                    )
                    with state_lock():
                        data = state_read()
                        if key in data:
                            data[key]["uncommitted_work"] = True
                            state_write(data)

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
    p = sub.add_parser("sweep")
    p.add_argument("ticket", nargs="?")
    p = sub.add_parser("wait")
    p.add_argument("--ack", help="Delivery id to acknowledge before waiting again.")
    p.add_argument("--timeout-ms", type=int, default=900000)
    p = sub.add_parser("verdict")
    p.add_argument("ticket")
    p.add_argument("decision", choices=["approved", "revise"])
    p.add_argument("--notes", default="")
    p.add_argument("--notes-file")
    p = sub.add_parser("brief")
    p.add_argument("ticket")
    p.add_argument("--file", required=True, help="Path to the coordination note to post.")
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
    elif args.verb == "sweep":
        print(json.dumps(sweep(args.ticket), indent=2))
    elif args.verb == "wait":
        print(json.dumps(wait(ack=args.ack, timeout_ms=args.timeout_ms), indent=2))
    elif args.verb == "verdict":
        notes = args.notes
        if args.notes_file:
            notes = (notes + "\n" + Path(args.notes_file).read_text()).strip()
        print(json.dumps(verdict(args.ticket, args.decision, notes), indent=2))
    elif args.verb == "brief":
        print(json.dumps(brief(args.ticket, args.file), indent=2))
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
