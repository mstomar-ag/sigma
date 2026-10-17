---
name: sigma-log
description: Read the local action log to see an active goal, thread, and recent activity. Use for live loop status or /sigma-log.
allowed-tools: Bash(python3 *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-log

Detailed selection triggers: [selection](references/selection.md).

Read side of the **local-only action log** (`skills/sigma-loop/scripts/actionlog.py` writes it,
opt-in via config `action_log.enabled`, default `false`) — a full-granularity trace of file
touches, model/effort choices, subagent dispatch, and every mechanically-guaranteed loop action
(claim/worktree/verify/gate/record), kept entirely separate from the team ledger and never
committed (`.sdlc/state/log/`, already gitignored via the existing `RUNTIME_IGNORES` mechanism).
This skill only ever READS `.sdlc/state/log/*.jsonl`; it shares no code with the writer.

For **"where are we right now"**, run
`python3 "${CLAUDE_SKILL_DIR}/scripts/log.py" status .sdlc` and relay the output. It lists every
ACTIVE goal (claimed, and not yet recorded done/parked/failed), newest activity first, one line per
`(goal, thread)` with its most recent action and how long ago it happened. A goal that has been
quiet a long time (`claimed, last activity 3h ago`) is a visible smell for a human to judge — this
tool makes no liveness claim of its own; it only reports what the log itself says.

For **"what's the status on THIS goal"** — especially a large, multi-thread one (parallel slices,
several subagent dispatches) — run
`python3 "${CLAUDE_SKILL_DIR}/scripts/log.py" goal .sdlc <goal>` and relay the full, oldest-first
history for that one goal, `[actor,thread]`-prefixed, with the distinct thread count in the header.

For **"what is in every slot right now"** — the several-goals-at-once view — run
`python3 "${CLAUDE_SKILL_DIR}/scripts/log.py" slots .sdlc` and **relay its output verbatim**. It
renders the SAME active-goal derivation as **Block A** of `docs/output-contract.md`: one two-line
slot per active goal, newest activity first, capped at six with the remainder counted in the tail.
The block is constructed by `render.py`, not written freehand — do not reword it, re-order it, or
add a seventh slot.

Three things about that command are deliberate, and relaying them straight is the point:

- **It says when it does not know.** `action log off -- slot detail unavailable` means the config
  says the log is off and nothing has been read; `action log state unknown` means the config could
  not be read at all (a goal worktree has no `.sdlc/config.json`). Those are different facts from
  "on, and nothing is in flight". Never summarise any of them as "no goals are running".
- **A goal whose log records no SDLC phase is withheld, not guessed at.** The tail counts them.
- **No description ends in `→`** — the action log records what happened, never what unblocks next.

If a run reports no entries, the feature is very likely just **off** — point the user at config
`action_log.enabled: true` (default `false`, matching every other opt-in feature in this kit)
rather than treating it as an error. This skill needs no `gh`, no network access, and spends no LLM
tokens beyond relaying its own plain-text output.

## How often merged work is reverted or quickly fixed

For **"how often does merged Sigma work get reverted or quickly fixed"** run
`python3 "${CLAUDE_SKILL_DIR}/scripts/outcomes.py" report --window 30` from the repository root and
relay its output verbatim. It is a **proxy**: line 1 says so, and it counts commits that look like
reverts or fixes and touch a file a merged goal changed within 14 days. It never prints a rate, and
prints `unmeasured` with the reason when the action log is off or unreadable, the clone is shallow,
the window is 14 days or less (no goal can be mature), or fewer than 10 mature goals fall in it. The
window cannot exceed the action log's own retention (`action_log.retention_days`, default 90).
It is read-only (writes nothing, no `gh`, no network). Goals merged by a merge commit or a rebase
carry no `(#PR)` marker, so they are counted as `unresolved`, never guessed at.
