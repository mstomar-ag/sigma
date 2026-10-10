---
name: sigma-promote
description: Approve AI-filed issues for the loop and repair stuck confirmation labels. Use to promote goals or /sigma-promote.
allowed-tools: Bash(python3 *), Bash(gh issue *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-promote

Detailed selection triggers: [selection](references/selection.md).

`sdlc:needs-confirmation` is the human approval gate. An issue Sigma filed itself carries it
and deliberately does **not** carry `sdlc:goal`, so no loop picks it up until a person says so.

**The approval gesture is removing `sdlc:needs-confirmation`** — not adding `sdlc:goal`. Do the
intuitive thing in the GitHub UI and the issue ends up carrying both, which is not a state the model
has a name for, and which the two queue paths used to read differently. This skill is the way
across: one atomic label swap, the board card moved with it, and a worklist of everything already
stuck.

Engine: `${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/promote.py`.

## The three buckets

`python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/promote.py" list .sdlc [--assignee X] [--json]`

| Bucket | What it is | What to do |
|---|---|---|
| **deadlocked** | carries `sdlc:blocking`, no `sdlc:goal` | other work is blocked on it and **no queue can serve it** — approve it or the thing it blocks never resumes |
| **awaiting** | carries `sdlc:needs-confirmation` | the ordinary approval queue |
| **drift** | carries **both** labels | a half-finished approval; repair is one removal |

`deadlocked` is listed first because it is the only bucket where doing nothing is actively harmful.
`sdlc:blocking` re-ranks issues that are **already** eligible; it is not a queue of its own. So a
blocker without `sdlc:goal` is picked by nothing, while the auto-unpark sweep refuses to resume
whatever it blocks until it *closes*.

**Most of these now resolve themselves.** When a block is recorded, Sigma classifies each named
blocker and acts: its own unapproved follow-up is promoted, one belonging to someone else is granted
membership and recorded in the ledger for them, and only the cases that genuinely need a person are
left. So what lands in this bucket is the residue — a proposal a *human* filed, a parked blocker, or
a third-party issue that was never Sigma's to adopt. Each is a one-keystroke decision here, or
in `/sigma-unpark` for the parked ones.

Scope defaults to `discovery.github.assignee`, which a bare `/sigma-init` scaffold ships as `null`
(not yet decided) and `/sigma-setup` fills in with `@me` — either way, an install that never ran
`/sigma-setup` and never set it by hand reads as empty, so by default this is every open issue, not
"mine". The output says which scope it used.

## Promoting

```
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/promote.py" apply .sdlc 1284 1301 [--dry-run]
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/promote.py" demote .sdlc 1284
```

Per issue: add `sdlc:goal` and remove `sdlc:needs-confirmation` in **one** atomic swap, move the
board card to `Ready`, leave an audit comment. `demote` is the exact inverse and sends the card back
to `Backlog` — a human who promoted something by mistake now has an undo.

`--dry-run` prints the exact swap and writes nothing.

## What it refuses, and why

- **A closed issue.** Reopen it first.
- **A parked or blocked issue.** Unparking is a different decision from approving — it needs the
  question that caused the park answered, not a label flipped. Use **`/sigma-unpark`**.
- **An issue whose current state cannot be read.** Every refusal above is a safety check, and a
  check that fails open is not a check.
- **An issue naming a `Blocked by: #N` prerequisite that is not confirmed CLOSED.** Every such line
  is checked, not just the first — a sibling only just promoted in the same batch, or still
  carrying `sdlc:goal`/`sdlc:in-progress`, is not the same thing as *done*. `apply --dry-run` shows
  every blocker it checked and each one's resolved state.

One failure never stops the rest; each issue reports its own outcome.

## Flow

1. `list` — read the three buckets. Start with `deadlocked`.
2. For each candidate, look at the issue before approving it: an AI-filed proposal is a *proposal*.
   If it is wrong or stale, close it instead of promoting it.
3. `apply --dry-run` on the set, confirm the swaps, then `apply`.
4. Anything in `deadlocked` you do **not** want worked: close it, or park it deliberately and use
   `/sigma-unpark`'s keep-parked path so the record survives.

## Notes

- GitHub discovery mode only — a local-goals backlog has no label state, so there is no gate.
- Exit codes: `0` clean, `1` a read or write failed, `2` usage.
- A queue `list` could not read prints `UNKNOWN (read failed: <reason>)` — never `0` — with the
  reason in a `NOTE:` at the top; `--json` carries `read`/`errors` per bucket. `apply` reports a
  quota failure as `rate-limited` (the issue is fine; retry after reset), distinct from `failed`.
- `/sigma-doctor` reports the `deadlocked` set as a check row, so it surfaces without being looked for.

## Migrating the retired confirmation queue

`python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/promote.py" migrate-confirmation .sdlc [--dry-run | --apply]`

One-time move of every issue still carrying `sdlc:needs-confirmation`. **Dry run is the default and makes
zero writes**; only `--apply` writes (giving both flags is a usage error). It pages through every labelled
issue (past the 200-issue backlog window) and prints `covered N of M`; it exits non-zero when N is below M,
when the listing fails, or when the fetch hit its cap (`ai_filed.migration.max_issues`, default 10000).

| Population | How it is recognised | Result |
|---|---|---|
| ownership hold | the ownership flag comment | parked (`owner_hold`) |
| scope hold | the scope-expansion flag comment | parked (`scope_hold`) |
| follow-up | the `sdlc:followup` label | the rubric bucket priority; a lower existing priority is kept |
| human-typed | none of the above | armed at its own priority, parked when it has none |

Every arm goes through the same deny-list as filing, and a park results from any open `Blocked by:`
reference or gate hold (the predicates `apply` uses). Arm and park are each ONE atomic label swap that also
removes the confirmation label, so a parked issue never keeps it. Each migrated issue gets one comment saying
which and why. Closed issues are left untouched (and counted as covered). A second run changes nothing.
With `ai_filed.triage.enabled` false the verb refuses, because the queue is then still the intended state.
