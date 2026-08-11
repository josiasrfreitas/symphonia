# The spawn interface

**TLDR: `spawn` is the only way a role starts, `spawn wait` is the only way you hear back, and `spawn submit`/`spawn done` are the only way a role answers. Between those two, the Orchestrator makes no choice about models, permissions, worktrees or launch paths — they are all decided in `adapters/orca/launcher.py`. If you are about to type a raw `orca terminal create` or `orca orchestration worker-start`, stop: that is the failure this interface exists to prevent.**

## The Orchestrator's commands

```
.symphonia/bin/spawn plan             <TICKET>   # starts the ticket: creates its worktree
.symphonia/bin/spawn implement        <TICKET>
.symphonia/bin/spawn review-spec      <TICKET>
.symphonia/bin/spawn review-standards <TICKET>
.symphonia/bin/spawn status          [<TICKET>]
.symphonia/bin/spawn retire           <TICKET> <role>
.symphonia/bin/spawn wait            [--ack <delivery_id>] [--timeout-ms <ms>]
.symphonia/bin/spawn verdict          <TICKET> approved|revise [--notes <text>|--notes-file <path>]
```

One argument, the Ticket Key. No flags. `--tier` exists but is a human
override — never pass it yourself.

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
approved verdict retires the planner — both are printed in `wait`'s output
as `actions`, never something you decide by reading the message.

Then, for every message `wait` reports that is not already a gate action:

| Message | What it means | What you do |
|---|---|---|
| `worker_done` + `succeeded` (non-planner role) | The role finished and wrote its handoff | Present the Human Gate if there is one, then `retire` it and spawn the next role |
| `worker_done` + `failed` | The role gave up; Orca marked the Task failed | Read its handoff, decide retry or escalate to the human |
| `question`, a real plan submission, from the planner | A plan is waiting on a verdict | `.symphonia/bin/spawn verdict <TICKET> approved\|revise [--notes ...]` — never `orca orchestration reply` by hand |
| `question`, anything else (a clarifying question from the planner, or any question from another role) | The gate does not apply — it is not a plan submission | `orca orchestration reply --id <message id> --body <answer>` directly; there is no `spawn` verb for this, and no raw `check --wait` is needed to see it — `spawn wait`'s `events` already carries it |
| `escalation` | The role has ownership but needs you to intervene | Read, act, and usually raise a Needs Attention flag |

Acknowledge and keep waiting in one call:

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
`check`. Delivery is FIFO and replays the same batch until you `--ack` it, so
an unacked old batch hides every newer message behind it.

**A timeout is not a failure.** `check --wait` returning `count: 0` is a
checkpoint. Planning and implementation routinely run 15–60 minutes. Keep
waiting unless the terminal is gone or the human tells you to stop.

**A silent worker is not necessarily working.** Use `spawn status <TICKET>`:
it reports the dispatch state and which model actually answered, both read
from files. If you need to see the session itself, `orca terminal read`.

**Never message a dead dispatch.** After `worker_done` the worker is asleep; a
send lands in its mailbox and never wakes it. Go through the Runtime Adapter's
`message_worker`, which checks the dispatch state first, or start the next
role with a fresh dispatch.

**You spawn; roles never do.** A role that finishes writes its handoff
document and dies. It does not launch its successor and it does not hand
ownership to anyone. Every transition goes through you.

## Where the decisions live

| File | Decides |
|---|---|
| `adapters/orca/launcher.py` | Tier → model, effort, permission flags, read-only enforcement, provider grammar |
| `bin/spawn` | The verbs, worktree policy, phase labels, what a role is told at dispatch, the Execution Brief (`build_brief`), the plan gate wiring (`wait`/`verdict`) |
| `adapters/plan_gate.py` | The plan gate's state machine — submission, verdict, retire — as a pure function |
| `adapters/reports.py` | Parses a role's message body into typed fields; raises when the body does not follow its contract |
| `roles/*.md` | What each role does and never does, and (for the planner) the I/O shapes it reads and writes |
| `bin/setup-worktree` | What a fresh checkout needs and git does not bring: the env files |
| `config.json` | The calibration numbers |

Adding a provider is a `PROVIDERS` entry in `launcher.py`. Changing a model is
one line in the same file. Neither touches a caller, and neither is ever done
at the terminal.
