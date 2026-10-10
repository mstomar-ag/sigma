---
name: sigma-loop
description: Autonomously run backlog goals through phase agents, verified PRs, and park/continue until stopped or budgeted. Use for unattended runs or /sigma-loop.
allowed-tools: Bash(python3 *), Bash(gh issue view *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-loop

Detailed selection triggers: [selection](references/selection.md).

Drive the backlog. Python owns state, budget, and the configured source: local goal files by default,
or GitHub issues when `discovery.source: github` (requires `gh` authentication).

**Shared rules are here. Step procedures are under `${CLAUDE_SKILL_DIR}/references/`; open yours
before running it:**

- `references/picking.md` — 1-2: batch dispatch, `DONE`/`BUDGET`, dependency gate, `--feature`, pre-checks.
- `references/running.md` — 3: model tier, phase subagents, maker≠checker, lanes, worktree, slices.
- `references/filing.md` — 3: hand-offs, follow-up issues, kit findings.
- `references/progress.md` — 3-5: phase notes, banners and costs, QC, retro.
- `references/landing.md` — 6: verify, commit, PR, review, merge, record, pipeline card.
- `references/stopping.md` — 7: spare-iteration gap work, the STOP report.

Start: `g=$(python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" start .sdlc --session-pid "$PPID")`; exit: `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" session-end .sdlc --session-pid "$PPID" --session-generation "$g"`.
**Pass `--session-pid "$PPID"` every pick. Read `$PPID` from your shell each time; omission
duplicates claims.**

Then repeat until the helper says stop:

1. With subagents: `goals=$(python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" next-batch .sdlc
   --session-pid "$PPID")`. Without subagents: use `next` with the same arguments, run its one
   goal's steps 2–7 inline, then pick again. Both paths stop at `HANDOFF` after 20 goals by
   default; resume `/sigma-loop` in a fresh session. See `references/picking.md`.

   **1a. With subagents, dispatch every goal — one line or many** — each in a fresh subagent
   running steps 2–7 for that ONE goal, at its resolved tier (`references/picking.md`) (Codex:
   `fork_turns="none"`). **Never** dispatch with an unattended `claude -p` (uncapped spend,
   unmanaged worker) — and **never** hand a subagent's own scratch work a `cp`/`rsync` of another
   goal's live worktree. As EACH subagent finishes, immediately refill just that ONE freed slot:
   `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" next .sdlc --session-pid "$PPID" --skip
   <comma-separated other still-live goals>`. **The `--skip` list is not optional for a refill**,
   and neither is `--session-pid "$PPID"`. Omitting it can re-dispatch a goal a sibling slot already
   holds. Slot sizing, dispatch logging, releasing an undispatched goal: `references/picking.md`.
2. Dispatch `goal` lines before a terminal line. If output is `DONE` (nothing pickable), `BUDGET` (iterations, wall-clock
   minutes, or reported tokens — each only when `config.json` sets it), or `HANDOFF` (a goal-count
   hand-off, on by default) → STOP, and report the stderr `BUDGET (...)`/`HANDOFF (...)` line, which
   names which ceiling tripped.

   On a terminal line, stop claiming; record active slots before ending. `DONE` does not always mean the backlog is empty. `next`/`next-batch` never hand back a goal
   whose own body declares a prerequisite (`**Blocked by:** #N`) that is still OPEN. Report that
   line rather than "the backlog is drained", and do NOT `--skip` past it. Confining a whole run to
   one unit of work is `--feature <name>`, and it is *exclusive*:

   > **While a `--feature` run is active, no goal outside that unit may be picked.**

   Both in full: `references/picking.md`.
3. Otherwise, run the **opt-in pre-checks, in this order** — before spending a token. Each prints
   `OFF` (a no-op — feature disabled), `PROCEED`, or `PARKED <reason>`. On **`PARKED <reason>`**:
   **Do not research it: loop back to step 1** and take the next goal. (The park counts as one
   iteration.)

   ```
   result=$(python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" precheck .sdlc "$goal")        # duplicate  (backlog_check)
   result=$(python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" decompose-check .sdlc "$goal") # epic       (goal_decompose)
   result=$(python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" design-check .sdlc "$goal")    # undesigned (goal_design)
   ```

   That order is load-bearing. It, what each park writes, and how to dismiss a false positive
   (never restore `sdlc:goal` by hand — **run `/sigma-unpark`**): `references/picking.md`.

   Then **recall prior art** — if the knowledge graph is enabled, run the `sigma-context` pre-flight
   to pull a cited brief from the graph + past issues + conventions (no-op when the KG is off).

   **Match the model to the goal.** **Read the tier IT resolved from the pick's own stderr** — a
   line shaped `sigma: model tier for <goal> resolved to <tier> (signal=<signal>)` — rather
   than recomputing it yourself, **especially in github mode**: `"$goal"` there is the bare issue
   number, and a manual `predict.py resolve "$goal" .sdlc` classifies that literal digit string,
   not the issue's real text. **You DO still need to log this same tier+signal to the LOCAL
   action-log copy yourself**: `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" log .sdlc "$goal"
   model_choice --model <tier> --signal <signal>`. **Use the SAME tier+signal you just established
   above — never a fresh, separate `why "$goal"` call here.** Never run a step ABOVE the goal
   ceiling. The fallback when that stderr line is missing, and the per-step `resolve-step`
   downgrade: `references/running.md`.

   Then run **each phase as its own subagent** with that host's model override (see
   `references/running.md`). One subagent PER PHASE, **tier `off` included** (`record done` is REFUSED without each, #684).
   Artifacts pass between phases through the filesystem, not shared context. Then read the goal and
   run it through the full SDLC (research → plan → plan-review → implement → review) — each phase
   via its **executor**. `$goal` is a **file path** in local mode (read it) or a **GitHub
   issue number** in github mode (`gh issue view "$goal"`).

   **The maker must not be the checker (`config.review.independent`, default on); asked, not proved.**
   Every review gate (plan-review, pre-PR code review, post-PR review at step 6) is asked to run as a
   **fresh subagent that never saw the maker's context**, given the PROJECT, not the author: `review_context.py brief .sdlc
   "$goal" --for plan-review|code-review|pr-review [--artifact <path|PR#>] >
   "/tmp/brief-$(basename "$goal" .md).md"` writes the pack to that file; hand over **only that**.
   No subagents is **not** a degradation: use the mechanism `reviewer.py resolve .sdlc` names. Only
   an `inline` answer reviews inline, and says so. `work.py record-plan-review` records a
   plan-review verdict against the file's `Plan sha256:`.

   **No phase subagent commits — only `work.py commit` does, once, at step 6.** This binds every
   dispatched phase subagent (research, plan, plan-review, implement, review, retro, and each
   individual slice under 3b), and it does not travel from one dispatch prompt
   to the next on its own: **say it in each one**. A phase that wants a local checkpoint leaves the
   worktree DIRTY and says so in its own handoff; it never stages or commits anything itself.

   **Match the ceremony to the goal** — after Research, resolve the lane it measured. Either way an
   unsized goal is **`medium`** — unknown gets more rigour, not less. **Plan-Review runs in full at
   every lane.** **Park instead of forcing through** if you hit any of:
   - a hard checkpoint / a decision only the user can make,
   - an **irreversible or expensive action** (deploy, delete, overwrite, spend, migrate) — NEVER
     run one unattended unless `loop.py spend-approval` approves,
   - a failure you cannot resolve — record THIS one as `failed` (step 6): parked means
     "needs a human decision", failed means "needs a fix"; the queue separates the two.

   **"Budget" or "tier too small" is not a park reason.** Before any park after a review send-back
   (at the latest, the 2nd send-back at one tier) run
   `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" escalate .sdlc "$goal" <tier> --after plan-review`
   → `ESCALATE <next>`: re-dispatch at `<next>`. `CEILING`/`OFF`: keep fixing. `references/running.md`.

   **3a. Register this goal, THEN cut the worktree if enabled.** Regardless of
   `config.work.enabled`, first register its worker:
   `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" agent-start .sdlc "$goal" --pid $PPID`.
   **A nonzero exit from `agent-start` means task ownership was not established — another live
   agent may own this (goal, thread); STOP this goal before `work.py start` rather than proceed (#2527: this refusal is real for a plain Claude collision now, not only a Codex thread mismatch).** With `config.work.enabled` on: `python3 "${CLAUDE_SKILL_DIR}/scripts/work.py" start
   .sdlc "$goal" --session-pid "$PPID"`. **Pass `--session-pid "$PPID"` — the same value you just
   gave `agent-start --pid`, and the same one every `loop.py` call takes.** Read the line it prints — it
   names the base it actually cut from. **If this command exits 4, STOP this goal and do NOT
   `record` it** — it has already moved the goal out of the claimed state for you, and recording
   anything on top of that would undo a transition that is already correct.
   Do **every edit for this goal inside that worktree**; the human's checkout must never move, and
   never change branch (it would rewrite `.sdlc/goals/` underneath you). Bookkeeping is the
   exception and stays in the MAIN checkout. How `<base>` resolves: `references/running.md`.

   **3b. Independent slices?** Run `slices.py plan .sdlc "$goal"` for a declared slices manifest;
   on Codex add `--host codex --goal-worktree <path> --tier <tier-or-off> --effort <effort>`; before dispatch run `python3 "${CLAUDE_SKILL_DIR}/../sigma-model/scripts/predict.py"
   host-model codex "<tier-or-off>" .sdlc --effort "<effort>"` and use its returned pair. With work off,
   run one unit. Dispatch one wave at a time. Run Codex slices sharing a checkout
   sequentially; concurrent slices need real `isolation: worktree`. For `dispatch: session`, print
   the `claude --worktree` or Codex command and let the human start it. Never start unattended `claude -p` or
   `codex exec` for a slice. Without a manifest or parallelism, run one unit. See
   `references/running.md` for wave logging, death-watch, and commands.

   **Blocked on someone else's AREA? Hand it off before you park** — use `handoff.py open`, THEN
   park this goal as normal. **Found something worth tracking that isn't a cross-area blocker? Never
   call `gh issue create` directly.** Use `handoff.py track`, and **choose `--queue queued` — on an
   autonomous or overnight run that is the DEFAULT posture for a non-blocking finding, not the
   exception**; **`--blocks yes` is what drives `--queue actionable`.** Every flag, and how a kit
   finding is routed off this board: `references/filing.md`.

   As you complete each phase, **record it** so the issue timeline is the audit trail:
   `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" note .sdlc "$goal" -` (text on stdin, quoted heredoc; #713).
   Mark each phase boundary before dispatch and after return:

   ```
   python3 "${CLAUDE_SKILL_DIR}/scripts/phase_report.py" start .sdlc "$goal" <phase> --model <tier> --pid "$PPID"
   python3 "${CLAUDE_SKILL_DIR}/scripts/phase_report.py" end .sdlc "$goal" <phase> --agent-id <agentId> --pid "$PPID"
   # <phase>: goal|research|plan|plan_review|implement|review|retro
   ```

   For Claude dispatch, append `--requested-model <Task model>` to `start` (e.g. `sonnet`);
   its existing observed-tier check still runs. For Codex dispatch, append
   `--host-model <exact ID> --expect-agent-id` and use the child's `phase_report.py codex-agent-id`
   UUID for `end --agent-id`. Inline work omits dispatch flags and `--agent-id`, never `--pid
   "$PPID"`: `end` trusts a marker this same process started, or (its fallback) a pid-less marker
   still inside the operator's configured lease -- never a marker another live process owns
   (#2667). Details: `references/progress.md`.

   `end` measures and writes the `phase` event's REAL tokens; never invent usage. Log each file's
   first create/edit/delete per phase and record 🔒 Critical Insights. Banner and inline details:
   `references/progress.md`.
4. Entering the **review** phase? Move the board card to QC:
   `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" qc .sdlc "$goal"` (github-project board only — a no-op for local/issues).
5. **Retrospective (Learn)** — after Review, run the **`sigma-retro`** executor (advisory). Under
   `config.review.independent` this too runs through the resolver, not as an assertion. Autonomous
   mode → **write only the audit-trail notes**, and **park** any north-star / standing-rule proposal
   to the review queue for a human; never edit a standing doc unattended.
6. Record the outcome, including the retro grade from step 5 if Retrospective ran:
   `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" record .sdlc "$goal" done --retro-grade
   achieved|partial|diverged` (or `parked "reason" --qkind <kind>` / `failed "reason" ...`; omit the flag on a
   goal that never reached Retrospective). Passing it is also what refreshes the knowledge graph
   when `knowledge_graph.auto_refresh` is `true`. **Do not run a graph build by hand as well.**

   **REQUIRED PATTERN** — any blocking call (`loop.py verify`, `work.py merge`, a dispatched
   review-gate subagent, or any other long-running step a phase must wait on): issue it as ONE
   foreground call, same turn, then READ THE LITERAL RESPONSE before deciding what happened —
   never assume a timeout means "killed" or "still running", this environment does BOTH for the
   identical test. A response naming a tracked id may still be alive: wait on/check THAT id, never
   re-invoke. A plain error/exit with no id means nothing is left running: a clean re-run is
   correct. **Never** shell-background it yourself (`&`, `nohup`) and end your turn trusting a
   notification that will not come. The two response shapes and the do/never commands, in full:
   `references/landing.md`.

   With `config.verify.enforce` on, a `done` needs FRESH machine evidence first — `verify` runs the
   goal's proving command and records it; `record done` is REFUSED without a passing, this-run
   verify. **This call has no timeout of its own — size your tool-call timeout to the PROJECT's verify
   command, not to a Sigma default.** **Edit the worktree after a green verify and the next
   `record done` is REFUSED, naming the paths that moved.**

   **Landing the work** (`config.work.enabled` on) — once verify is green, and never with a bare
   `git` command: `work.py commit .sdlc "$goal" --message "<type: what changed>"` → `work.py pr
   .sdlc "$goal"`. **Put the plan on the branch before that first `commit`** — copy
   `.sdlc/plans/<goal-stem>.md` into the SAME relative path inside the worktree. **`commit` REFUSES
   when `git add -A` staged a secret-shaped file** — **do exactly what the refusal prints, character
   for character**, then re-run `commit`. Never `git add -f` past it.

   **Then REVIEW the PR you just opened, if `config.work.require_review` is set** — a real review AFTER
   the PR. Self-review before the PR is never enough. **Resolve the mechanism, never assert it** —
   `reviewer.py resolve .sdlc` and use what it names, fed the `--for pr-review --artifact <PR#>`
   brief. The skills ask that the maker not clear its own PR; code cannot stop it. **No human approves — the loop reviews and clears its
   own PR:**
   - **Evidence-bound:** generation → reviewer → `review-evidence` (`references/running.md`).
   - **No blocking issues** → `work.py post-review .sdlc "$goal" --evidence "$REVIEW_EVIDENCE" --verdict approve`.
   - **Blocking issues** → `--verdict block --reason "<the issues>"`, then **fix them in the
     worktree — fresh, never resumed** (back to Implement), re-run `loop.py verify`, then
     `work.py commit` **AND `work.py
     pr` — the push is not optional**, and **re-review, also fresh** (own verdict;
     `references/running.md`), until clean.
     **`commit` is LOCAL; only `pr` pushes.** **The cycle is hard-capped:** at
     `work.max_review_cycles` (default **3**) `post-review` returns `PARK: …` instead of asking for
     another fix → `record parked "<why>"`.
   - **Unblock (same revision)** → fresh generation, `--verdict unblock`.

   Then `work.py merge .sdlc "$goal"`. **This call can block for minutes — raise your tool-call
   timeout before running it, not after it times out**: the safe minimum is the full worst case,
   **~1,350,000ms (22.5 minutes)**. **Read its first word and record accordingly — never merge past
   it by hand:** `PARK: …` → `record parked` (but a **failing required check** is a fix → `record
   failed`); `PR #N merged …` → **`record done`**; any other line → **`record review`** (done
   means merged; `record done` is REFUSED until then). Every ending, and why **`record done`
   releases the checkout itself — do not run `work.py finish`**: `references/landing.md`. **Read it before the first merge of a run.**

   With `config.ledger.enabled` on, the claim and the outcome are mirrored to the **team ledger**
   automatically — never record those by hand. `loop.py next` prints a **LEDGER INBOX** block on
   stderr when a teammate needs you: read it and answer each item with `handoff.py ack` — the inbox
   spells out the exact command per item. Take a `P0` next rather than interrupting your goal.
7. Loop.

At STOP, print one machine-readable line FIRST — `LOOP STOP: backlog-empty`, `LOOP STOP: budget`, or
`LOOP STOP: handoff` — then report: N done, M parked, K failed. On a `handoff` stop, ALSO print the
literal resume command — `references/stopping.md`. If anything parked or failed, point the user to the items —
`.sdlc/state/review-queue.md` in local mode, or the issues labelled `sdlc:parked` in github mode.
Parking is always correct over forcing an irreversible action to "finish" a goal.
The self-improving path — closing a knowledge-graph gap instead of stopping when the backlog is
empty but budget remains — and `supervise_daemon.py`: `references/stopping.md`.

**Wake-and-work setup (optional).** `ledger.autowatch` is one-time operator setup, not something
you invoke mid-loop: [`AUTOWATCH.md`](AUTOWATCH.md).

**Inbound Slack commands (optional).** One-time setup for `slack_commands`:
[`SLACK_COMMANDS.md`](SLACK_COMMANDS.md).
