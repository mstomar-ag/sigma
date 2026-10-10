---
name: sigma-rebase
description: Human-attended feature-branch catch-up, conflict resolution, verification, and optional landing. Use for stale branches or /sigma-rebase.
allowed-tools: Bash(python3 *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-rebase

Detailed selection triggers: [selection](references/selection.md).

A rebase conflict is a question about intent: "why did the base decide this, and why does my
branch disagree?" A raw `git rebase` error answers neither. This skill does, before it moves
anything — and once the tree is clean, it is also where you verify the result and, if you choose,
land it.

*Temporary and authority-less by design* — any human may run it on any branch; there is no
authorization system yet (`docs/dossier-pipeline.md`'s own framing for a Dossier-scoped decision
deferred on purpose). It complements, and never replaces, `feature_rebase.py`'s automatic upkeep
pass (`docs/branching-model.md` §3): that pass runs unattended, once per pick, and stops cold at a
conflict. This one runs whenever you ask, explains itself, and — for a genuine conflict — walks you
through resolving it interactively rather than leaving you with a bare `git` error.

Three scripts, one flow, each usable on its own:

- `skills/sigma-rebase/scripts/rebase_brief.py` — the decision-context brief and the clean-path
  rebase (steps 1-4 below).
- `skills/sigma-rebase/scripts/conflict_walk.py` — the interactive conflict-options walker (step 5).
- `skills/sigma-rebase/scripts/verify_merge.py` — verify, then ask to merge (step 6).

## The flow

1. **Brief first, always** — never rebase blind:
   ```
   python3 "${CLAUDE_SKILL_DIR}/scripts/rebase_brief.py" brief .sdlc [branch]
   ```
   `branch` defaults to whatever is currently checked out; pass one explicitly to check on a
   branch you are not sitting on right now (read-only either way — this subcommand never mutates
   anything). Prints, per commit the base picked up since you diverged: its own CHANGELOG entry
   when one exists, else the landing PR's description, else a linked design write-up
   (`.sdlc/design/<n>.md`), else an honest "no written explanation exists for this change" — never
   a fabricated reason. Then lists every file both sides touched since the merge-base: where a
   conflict is possible, named before the rebase runs rather than discovered by it.

2. **Show the brief to the human and ask for confirmation before rebasing.** This is the whole
   point of doing it first — nobody approves a rebase blind.

3. **On confirmation, rebase:**
   ```
   python3 "${CLAUDE_SKILL_DIR}/scripts/rebase_brief.py" rebase .sdlc [branch]
   ```
   Prints the same brief again, then attempts it — but ONLY when `branch` is the currently
   checked-out one (checking on a branch you are not on gets the brief and nothing else, on
   purpose: this skill never mutates a tree you are not sitting in front of). Autostashes a dirty
   tree, rebases onto `work.base` (resolved exactly as `feature_rebase.py`'s own automatic upkeep
   resolves it — unconditionally, so this works whether or not the repo has adopted
   `.sdlc/features/`), then pushes with `--force-with-lease`.

4. **Clean:** reports the rebase and the push; done.

5. **Conflict:** `rebase_brief.py` prints, for every conflicted file, that file's own decision
   context — the base-side commit(s) that touched it, or, when the base DELETED it, specifically
   the commit that deleted it and why — then stops. Resolve it with the walker:
   ```
   python3 "${CLAUDE_SKILL_DIR}/scripts/conflict_walk.py" walk .sdlc [branch]
   ```
   For each conflicted file, in turn: its shape (`deleted-by-us` / `deleted-by-them` / a genuine
   content conflict, read from git's own porcelain status), the same decision context
   `rebase_brief.py` already assembled, and a symbol search (`git log -S"<symbol>" --all`) for
   where the code may have moved. Then offers named options — **Recreate here** (keep this branch's own version),
   **Follow the move** (only when the search found a destination that still exists), **Abandon
   this hunk** (accept the base's decision), **Resolve by hand** — applies whichever is chosen, and
   repeats if a later commit conflicts again. **Follow the move** never writes its destination file
   itself; the branch's own content to re-apply there by hand is always printed, plainly, once the
   walk ends — not just on a clean finish. `abort` at any point runs `git rebase --abort`, leaving
   the tree exactly as it was. Once every conflict is actually resolved, pushes the branch with the
   same `--force-with-lease` step 3 uses. If a resolution would leave a replayed commit's diff
   completely empty against its new parent — git's own default silently DROPS a commit like that,
   with no message on either stream — this refuses instead: the rebase is left stopped exactly
   there, the at-risk commit named, nothing rewritten. Re-running this command resumes a rebase
   left stopped from a prior run (by this refusal or anything else) rather than losing your place.

6. **Once clean — verify, then ask to merge:**
   ```
   python3 "${CLAUDE_SKILL_DIR}/scripts/verify_merge.py" land .sdlc [branch]
   ```
   Runs this repo's own `verify.command` (the same key `loop.py verify_goal` reads) directly
   against the current working tree and reports pass/fail plainly. **Only if it passes** does it
   ask whether to merge — always asked, never automatic; answering no leaves everything exactly as
   it is, rebased/resolved but not merged. Answering yes finds (or opens) the landing pull request
   — readying it first if it is a draft — and lands it with a plain `gh pr merge`, the same shape a
   person landing a unit by hand already uses (`docs/branching-model.md` §13 stays the human-owned
   default; this is that same act, just carried out through this skill when you explicitly ask for
   it). A landing pull request a human already closed without merging is never reopened.

7. **A refused push** (someone else moved the branch between your fetch and this push) is reported
   plainly, never silently retried or force-overwritten: re-run from step 1.

## What this is not

- **Not automatic merge.** Step 6 lands nothing without an explicit yes, every time — there is no
  flag or config that makes this unattended, matching `docs/branching-model.md` §13's "completion
  is human-only by default" and the Dossier's own `non_goals`.
- **Landing is not on the irreversible-actions list, on purpose.** The loop parks deploy, delete, overwrite, spend and
  migrate. Landing a unit is a different gesture: it needs this skill's explicit yes (step 6) and a head-pinned merge, so it
  is a user-requested action with its own gate, not an unattended one. `docs/enforcement.md` has a row for it.
- **Not automatic.** Nothing here runs unattended, on a schedule, or without the branch you name
  being the one you are actually sitting on.

See `docs/branching-model.md` §3 ("detection is the rebase upkeep pass — and this is its
manually-triggered companion") and `docs/how-branching-works.md` Stage 5 for where this sits
against the rest of the branching model.
