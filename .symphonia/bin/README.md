# The spawn interface

**TLDR: `spawn` is the only way a role starts, `spawn wait` is the only way you hear back, and `spawn submit`/`spawn done` are the only way a role answers. Between those two, the Orchestrator makes no choice about models, permissions, worktrees or launch paths — the model/effort/permission grammar is decided in `src/adapters/harnesses/claude.py`, over a tier/access each role declares in its own frontmatter and `src/workflow/roles.py` reads. If you are about to type a raw `orca terminal create` or `orca orchestration worker-start`, stop: that is the failure this interface exists to prevent.**

## The Orchestrator's commands

```
.symphonia/bin/spawn plan             <TICKET>   # starts the ticket: creates its worktree
.symphonia/bin/spawn implement        <TICKET>
.symphonia/bin/spawn review-spec      <TICKET>
.symphonia/bin/spawn review-standards <TICKET>
.symphonia/bin/spawn status          [<TICKET>]
.symphonia/bin/spawn retire           <TICKET> <role>  # manual: same teardown a worker_done already ran for you
.symphonia/bin/spawn sweep           [<TICKET>]         # audits for a role whose world is already gone, tears it down
.symphonia/bin/spawn brief            <TICKET> --file <cut.md>  # posts a wave's coordination note to the ticket
.symphonia/bin/spawn wait            [--ack <delivery_id>] [--timeout-ms <ms>]
.symphonia/bin/spawn verdict          <TICKET> approved|revise [--notes <text>|--notes-file <path>]
```

One argument, the Ticket Key. No flags. `--tier` exists but is a human
override — never pass it yourself.

Every report a role sends back — `worker_done`, an escalation, an answer to
your questions — should be short: no preamble, no recap of what you already
know.

## The role's own two commands

Not yours. A ROLE runs these inside its own dispatched terminal — they are
the return half of the same interface, so no role ever types
`orca orchestration` by hand:

```
.symphonia/bin/spawn submit <TICKET> --file <body.md> [--max-wait-ms <ms>]
.symphonia/bin/spawn done   <TICKET> --outcome succeeded|failed --file <body.md> [--files-modified a,b]
```

`submit` sends a plan for a verdict and blocks until it arrives, printing it
parsed. `done` sends the single `worker_done` a dispatch allows — checking
the body first, because there is no second one.

`plan` refuses if the ticket already has a worktree; every other verb refuses
if it does not. The order of the workflow is enforced by the commands, so you
do not have to remember it.

## What each spawn does for you

Creates the ticket's worktree as a child of your own in Orca lineage, off the
repo default base in git (never your current branch), and runs
`.symphonia/bin/setup-worktree` on it — a fresh checkout has no `.env`,
because a `git worktree add` never brings a gitignored file. Launches the role at its
Capability Tier with permissions that never prompt. Labels the worktree, the
terminal and the board column with the phase. Creates the Task, injects the
dispatch, and records everything so `status` can report it.

## The loop

Spawn every ready role first, then wait once. Never wait between spawns.

```bash
.symphonia/bin/spawn plan SYM-5
.symphonia/bin/spawn plan SYM-7          # independent node, same wave
.symphonia/bin/spawn wait --timeout-ms 900000
```

`wait` wraps `orca orchestration check --wait`; you never call that raw. For
the planner it also drives the plan gate by itself: a plan submission
(`question`) lights the `human-gate` label, and a `worker_done` after an
approved verdict tears down the planner. Any other role's `worker_done` —
implementer, either reviewer, on either outcome — ends that role the same
way, directly: no gate state to transition, so there is nothing left for you
to retire by hand. Both are printed in `wait`'s output as `actions`
(`retire_planner`/`retire_role`), never something you decide by reading the
message.

Then, for every message `wait` reports that is not already a gate action:

| Message | What it means | What you do |
|---|---|---|
| `worker_done` + `succeeded` (non-planner role) | The role finished, wrote its handoff, and `wait` already ended it | Present the Human Gate if there is one, then spawn the next role |
| `worker_done` + `failed` | The role gave up; Orca marked the Task failed, and `wait` already ended it | Read its handoff, decide retry or escalate to the human |
| `question`, a real plan submission, from the planner | A plan is waiting on a verdict | `.symphonia/bin/spawn verdict <TICKET> approved\|revise [--notes ...]` — never `orca orchestration reply` by hand |
| `question`, anything else (a clarifying question from the planner, or any question from another role) | The gate does not apply — it is not a plan submission | `orca orchestration reply --id <message id> --body <answer>` directly; there is no `spawn` verb for this, and no raw `check --wait` is needed to see it — `spawn wait`'s `events` already carries it |
| `escalation` | The role has ownership but needs you to intervene | Read, act, and usually raise a Needs Attention flag |

`retire`/`sweep` are for what `wait` cannot see: a role that never got to
report at all — the app quit, a machine lost power, a worktree was deleted
by hand. `retire <TICKET> <role>` ends one you can name yourself, and redoes
its best-effort work even on a role already ended — it never assumes. `sweep
[<TICKET>]` finds every record whose terminal or worktree is already gone
and ends those without you having to name them; a record `wait` (or a prior
`sweep`) already ended is left alone and not reported.

The ack is automatic (GRE-187): every `wait` persists the Delivery id it saw
to a receipt on disk, and the next `wait` reads it back and sends it as the
ack, with no flag needed. `--ack <delivery_id>` still exists, and still wins
over the receipt, but it is now only for re-acking a specific id by hand:

```bash
.symphonia/bin/spawn wait --ack <delivery_id> --timeout-ms 900000
```

## Things that will bite you

**`--payload` and the structured flags are mutually exclusive.** Measured on
Orca 1.4.168: `orca orchestration send --type worker_done --task-id ...
--dispatch-id ... --outcome ... --payload '{"x":1}'` is refused with
`invalid_argument` and nothing is sent. The injected preamble teaches the
structured form, so anything the gate needs beyond those three fields forces
one single `--payload` carrying all of them. `spawn done` is where that shape
lives; a role following the preamble literally reports nothing.

**A refused `worker_done` still arrives as a `worker_done`.** Orca rewrites
the subject and body and adds `_orcaLifecycleRejection: {code, reason}` to the
payload — and answers `ok: true` in the envelope, with a non-zero exit code.
`wait` flags it as Needs Attention; a hand-rolled reader would count it as a
completion that completed nothing.

**An injected dispatch mints a capability, printed only in the preamble.**
Without `--dispatch-capability`, a lifecycle message is refused with
`dispatch_capability_invalid` and the dispatch stays open forever. `spawn`
captures the token at dispatch; the dispatch row's `capability_hash` is null
and cannot be read back later.

**Nothing pushes.** `worker_done` is mail into the Run mailbox. A finished
worker changes nothing on your screen; the message sits there until you
`check`. Delivery is FIFO and replays the same batch until it is acked, so
an unacked old batch hides every newer message behind it. Losing the
terminal's stdout no longer loses the ack: the pending Delivery id is
persisted to disk (`workflow/journal`'s receipt) only AFTER it has been
processed and the registry write is durable, so reopening the terminal and
calling `wait` again either sees the receipt and acks it, or — if the
process died before that write — sees no receipt, sends no `--ack`, and
lets Orca redeliver the identical batch for a harmless replay. Either way:
no id to recover from old output, and `spawn status` (no ticket) shows it
under `pending_delivery` if you want to check without waiting. That same
no-ticket call is now an object, not the flat list you get with a ticket —
`{"pending_delivery": ..., "spawns": [...]}` — so `spawn status | jq '.[0]'`
breaks; index into `.spawns` instead.

**A timeout is not a failure — but an instant empty return might not be one
either.** `check --wait` returning `count: 0` after the full `--timeout-ms`
is a checkpoint; keep waiting unless the terminal is gone or the human tells
you to stop. But an empty, `delivery_id`-less batch that comes back in a
couple of seconds instead of blocking (measured live, ondas 9–10) is
content-identical to a real timeout — the only tell is `wait`'s
`elapsed_ms`, now returned alongside `delivery_id`. A tiny `elapsed_ms` with
nothing in it means reconnect and call `wait` again, not that there is
nothing to wait for.

**A silent worker is not necessarily working.** Use `spawn status <TICKET>`:
it reports the dispatch state and the declared tier's evidence — what kind
of evidence exists (`requested` vs `observed`) and the detail behind it, not
a comparison against a model alias — both read from files. If you need to
see the session itself, `orca terminal read`.

**Never message a dead dispatch.** After `worker_done` the worker is asleep; a
send lands in its mailbox and never wakes it. Go through the Runtime Adapter's
`message_worker`, which checks the dispatch state first, or start the next
role with a fresh dispatch.

**You spawn; roles never do.** A role that finishes writes its handoff
document and dies. It does not launch its successor and it does not hand
ownership to anyone. Every transition goes through you. The handoff is one
current document per ticket — each role overwrites the same path, so the
next role never has two files to choose between. A wave's cut of work goes
to the ticket through `spawn brief`, never through a tracker client typed
by hand — `build_brief` already composes every ticket comment into the
next role's Execution Brief, so posting through the package is the whole
job. `build_brief` runs at dispatch, not on a schedule: a comment posted
after a role is already standing does not reach it — there is no channel
to a live role. Posting and re-dispatching are a pair, in that order; the
cut lands in the brief of the *next* dispatch, never the current one.

**An empty `succeeded` is refused, not registered.** `spawn done <TICKET>
--outcome succeeded` from a write-access, non-planner role is refused
outright if the worktree shows no change — same HEAD as at dispatch and a
clean `git status`. An empty body is refused for every role, on either
outcome. Both checks run before anything is sent, so an empty success never
reaches the registry to begin with.

## Where the decisions live

| File | Decides |
|---|---|
| `src/workflow/roles.py` | Which role runs at which tier and access — read from each role file's own frontmatter, one declaration, not a table to keep in sync |
| `src/adapters/harnesses/claude.py` | Tier → model, effort, permission flags, read-only enforcement, provider grammar |
| `src/spawn.py` (behind the `bin/spawn` entrypoint) | The verbs, worktree policy, phase labels, what a role is told at dispatch, the Execution Brief (`build_brief`), the plan gate wiring (`wait`/`verdict`) |
| `src/adapters/plan_gate.py` | The plan gate's state machine — submission, verdict, retire — as a pure function |
| `src/adapters/reports.py` | Parses a role's message body into typed fields; raises when the body does not follow its contract |
| `roles/*.md` | What each role does and never does, and the I/O shapes it reads and writes |
| `src/setup_worktree.py` (behind the `bin/setup-worktree` entrypoint) | What a fresh checkout needs and git does not bring: the env files |
| `config.json` | The calibration numbers, plus the `handoff_dir` a role's baton document is written to |

Adding a provider is a `PROVIDERS` entry in `claude.py`. Changing a model is
one line in the same file. Neither touches a caller, and neither is ever done
at the terminal.
