# The spawn interface

**TLDR: `spawn` is the only way a role starts, and `check --wait` is the only way you hear back. Between those two, the Orchestrator makes no choice about models, permissions, worktrees or launch paths — they are all decided in `adapters/orca/launcher.py`. If you are about to type a raw `orca terminal create` or `orca orchestration worker-start`, stop: that is the failure this interface exists to prevent.**

## The five commands

```
.symphonia/bin/spawn plan             <TICKET>   # starts the ticket: creates its worktree
.symphonia/bin/spawn implement        <TICKET>
.symphonia/bin/spawn review-spec      <TICKET>
.symphonia/bin/spawn review-standards <TICKET>
.symphonia/bin/spawn status          [<TICKET>]
.symphonia/bin/spawn retire           <TICKET> <role>
```

One argument, the Ticket Key. No flags. `--tier` exists but is a human
override — never pass it yourself.

`plan` refuses if the ticket already has a worktree; every other verb refuses
if it does not. The order of the workflow is enforced by the commands, so you
do not have to remember it.

## What each spawn does for you

Creates the ticket's worktree as a child of your own in Orca lineage, off the
repo default base in git (never your current branch). Launches the role at its
Capability Tier with permissions that never prompt. Labels the worktree, the
terminal and the board column with the phase. Creates the Task, injects the
dispatch, and records everything so `status` can report it.

## The loop

Spawn every ready role first, then wait once. Never wait between spawns.

```bash
.symphonia/bin/spawn plan SYM-5
.symphonia/bin/spawn plan SYM-7          # independent node, same wave
orca orchestration check --wait --types worker_done,escalation,question --timeout-ms 900000 --json
```

Then, for every message in the returned Delivery:

| Message | What it means | What you do |
|---|---|---|
| `worker_done` + `succeeded` | The role finished and wrote its handoff | Present the Human Gate, then `retire` it and spawn the next role |
| `worker_done` + `failed` | The role gave up; Orca marked the Task failed | Read its handoff, decide retry or escalate to the human |
| `question` | A role is blocked and waiting on you | `orca orchestration reply --id <msg_id> --body <answer>` — it stays blocked until you do |
| `escalation` | The role has ownership but needs you to intervene | Read, act, and usually raise a Needs Attention flag |

Acknowledge and keep waiting in one call:

```bash
orca orchestration check --ack <delivery_id> --wait --types worker_done,escalation,question --timeout-ms 900000 --json
```

## Things that will bite you

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
| `bin/spawn` | The verbs, worktree policy, phase labels, what a role is told at dispatch |
| `roles/*.md` | What each role does and never does |
| `config.json` | The calibration numbers |

Adding a provider is a `PROVIDERS` entry in `launcher.py`. Changing a model is
one line in the same file. Neither touches a caller, and neither is ever done
at the terminal.
