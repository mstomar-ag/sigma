# Solving a whole dependency chain

`/sigma-chain` plans every open prerequisite behind one goal, asks the person every decision once,
arms the members, and drives them to done. The engine is `skills/sigma-loop/scripts/chain.py`; the skill
is a thin script over its verbs. Nothing here is model-driven: each verb is a plain command.

## Solving a whole dependency chain

Paste this block into the file your agents read (`AGENTS.md`, `CLAUDE.md`, or equivalent). It is the single source; a test pins it. `/sigma-init` writes it into the new `project.md` and Cursor rule (never into an existing file), and `/sigma-doctor` reports whether an agent file carries it.

```
## Solving a whole dependency chain

When a goal is blocked by other open goals, do not walk the blockers one by one. Run the chain:
use the sigma-chain skill, or `chain.py plan`, `sheet`, `answer`, `approve`, `arm`, `run` from
the sigma-loop scripts. It plans the whole chain, asks every decision together, and then solves it.
```

## Flow

| Step | Verb | Touches GitHub |
|---|---|---|
| Plan | `chain.py plan <sdlc_dir> <N> [--json]` | reads only; writes the local record |
| Offer end goals | `chain.py plan <sdlc_dir> --longest [--by freeing]` | nothing, from the stored map |
| Sheet | `chain.py sheet <sdlc_dir> <chain>` | nothing |
| Answer | `chain.py answer <sdlc_dir> <chain> --answers <file>` or `--defaults` | parked members only, through the unpark gesture |
| Approve | `chain.py approve <sdlc_dir> <chain>` | nothing |
| Arm | `chain.py arm <sdlc_dir> <chain> [--dry-run]` | the only write path: label `chain:<root>`, one fixed comment, arming |
| Run | `chain.py run <sdlc_dir> <chain> [--session-pid P]` | one cycle: the loop's label-scoped pick |
| Stop / resume | `chain.py stop`, `chain.py resume [--cap N]` | nothing; local record only |

No GitHub write exists in any verb before `approve`. `run` is one cycle and is called again until it
reports done; exit 3 means capped or stuck with one reason (decision, held-gate, claimed, external,
rate-limit, degraded-read, capped).

## The sheet

Every decision the chain needs, shown together: the label drain's questions over the member set, plus a
cross-unit edge, an end goal that cannot be armed, and the cap. Every answer is a bank option (the cap is a
positive whole number), never free text. `--defaults` takes each recommended option except where there is
none, which is the cap.

## Member states

Live verdicts: armed, unarmed, parked, upper-tier, assigned-elsewhere, held-by-gate. From the stored map
only the life is known: armed, unarmed, parked, unknown. An upper-tier, parked or gate-held member is
labelled, never armed.

## Growth

`run` reports members added to and closed in the label since the last cycle. It does not act on them. A
change in membership is bounded by `chain.max_added` (below).

## Cap and honest limits

The cap is checked before each claim, so it can be overshot by the cost of the goals in flight. With no cap
configured nothing reads the spend ledger; with one set, an unreadable ledger fails closed.

Not available yet: a `status` verb, `--parallel`, and a `--cap-usd` flag (set `chain.max_usd`).
`resume --cap N` caps the number of goals, not dollars. Not measured: a run against a live repository, and
two drivers on one chain (the record has no lock; run one).

## Configuration

Keys under `chain` in the project config.

| Key | Default | Meaning |
|---|---|---|
| `chain.max_members` | 100 | Largest closure `plan` accepts; over it the plan is refused with its count, never truncated. |
| `chain.max_goals` | members plus `chain.max_added` | Most goals the chain may hold. |
| `chain.max_added` | 5 | Goals the chain may grow by when `chain.max_goals` is unset. |
| `chain.freeze` | false | When true (or `run --no-grow`), nothing joins by itself: each new prerequisite is asked on a delta sheet. A fourth such pause (max_reasks, default 3) ends the run stuck. |
| `chain.parallel` | unset (width 1) | Whole number of 2 or more: members one run cycle may claim at once. Capped by `parallel.goals.max_concurrent` (default 3) and honoured only with `parallel.goals.enabled` true and `work.enabled` on; otherwise the width is 1. A wider width is refused when `chain.max_usd` is set and the remaining budget does not cover the goals in flight at the p90 cost per goal. |
| `chain.max_active` | 1 | Most chains that may run at the same time. Setting it, or `chain.parallel`, turns the active-chain lock on. |
| `chain.max_usd` | unset | Dollar cap on spend attributed to members since approval. |
| `chain.max_minutes` | unset | Minutes cap measured from the approval stamp. |
| `chain.auto_longest` | reserved | Documented only: nothing reads it yet. |

## The active-chain lock

When `chain.parallel` or `chain.max_active` is set, each run cycle records its chain in
`.sdlc/state/chain-active.json` (pid, heartbeat, claimed pool) under a `flock` on a sidecar file. A chain whose
pool overlaps another active chain's, or that would exceed `chain.max_active`, is refused with the reason.

An entry is reclaimed only when its pid is dead AND its heartbeat is older than 900 seconds (a default the owner
is to ratify). Either clause alone is not enough: a dead pid with a fresh heartbeat may be a driver between
cycles, and a live pid with an old heartbeat is a hung driver a person should look at. So a driver whose cycles
are further apart than 900 seconds can be reclaimed by another chain while it is still meant to be running.

Manual recovery: `chain.py stop` on the chain releases its entry; a chain that ended stuck keeps it until stopped or run again. A lock file that cannot
be read is named in the refusal; once no chain is running, delete that one file and run again. Without
`fcntl` the lock refuses loudly rather than run unlocked.
