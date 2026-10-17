---
name: sigma-goal
description: Run one goal through all SDLC phases with approval gates. Use for supervised end-to-end work or /sigma-goal.
allowed-tools: Bash(python3 *), Bash(gh issue view *), Bash(gh api *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-goal

Detailed selection triggers: [selection](references/selection.md).

Drive a single goal through the SDLC, with the user in the loop (this is the interactive
counterpart to the autonomous `/sigma-loop`).

1. Identify the goal: a path under `.sdlc/goals/` (preferred — so it's tracked) or inline text the
   user gives. If inline, offer to save it as the next `.sdlc/goals/NNNN-*.md`. **If the invocation
   carried `--feature <unit>`, selection is confined to that unit** — see *`--feature`* below, and
   apply it before anything else in this step.

   Then **claim it** — `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" claim .sdlc
   "<goal>"` — so the goal carries a real `claimed` lifecycle event and its autonomy span is
   measurable. This skill NAMES its own goal rather than asking the picker for one, and `claimed`
   used to be emitted only as a side effect of `loop.py next` selecting a goal — so nothing on this
   path emitted it at all, and 197 of 440 terminal goals on this repo's own board reached `done`
   with no claim (#1962). Skipping the line does not break the run: `loop.py` self-arms the claim on
   the first goal-scoped verb this skill calls anyway (step 2's `note` is one). Typing it is simply
   earlier, and therefore truer.
2. **Recall first** — if the knowledge graph is enabled, run the `sigma-context` pre-flight to assemble
   a cited brief from the graph + past issues + conventions (no-op when the KG is off). If
   `model_selection` is `auto`, also surface the recommended tier. For a local goal file, run
   `python3 "${CLAUDE_SKILL_DIR}/../sigma-model/scripts/predict.py" resolve "<goal-path>" .sdlc`.
   For a GitHub issue number, PIPE its real text in, never paste it into a shell argument (issue
   text is untrusted data; `$(...)` or backticks inside quotes would run, #713):
   `gh api repos/{owner}/{repo}/issues/"<goal>" --jq '.title + "\n\n" + (.body // "")' | python3
   "${CLAUDE_SKILL_DIR}/../sigma-model/scripts/predict.py" resolve - .sdlc "<goal>"` (POSIX shell;
   `-` reads the text from stdin) — the TITLE on the first line, because a haiku signal counts only
   there (#2827); the final
   argument keeps the ledger event attributed to the issue number. Passing
   the bare issue number
   as the text classifies its digits and can silently choose the wrong tier. This is the
   same GitHub-mode distinction as `/sigma-loop`'s `../sigma-loop/references/running.md`. Surface the result so you and the user
   know the intended model. **`/sigma-goal` dispatches each phase as its own subagent with the resolved
   `--model` override, exactly the same mechanism `/sigma-loop` uses** — the only behavioral difference
   from `/sigma-loop` is that you approve at each gate here, instead of the loop auto-proceeding between
   dispatches. On Codex, run `python3 "${CLAUDE_SKILL_DIR}/../sigma-model/scripts/predict.py" host-model codex "<tier-or-off>" .sdlc --effort "<effort>"` before each dispatch and pass its exact model ID and the same selected reasoning effort as `../sigma-loop/references/running.md` describes; never pass the Claude tier as a Codex model ID. `off` resolves the versioned ordinary-work Codex default, never the parent session's model.
   Use `fork_turns="none"` for each phase so it receives the goal, current
   phase, worktree and relevant artifact paths without copying this interactive session's history.
   Keep full evidence in the plan, review and verification artifacts; return a concise verdict and
   paths, then inspect the artifacts needed for the next gate.
   Then drive the phases, pausing for the user at each gate:
   **Goal** (restate) → **Research** (blast radius) → **Plan** → **Plan-Review** (use the
   `sigma-plan-review` skill — never skip) → **Implement** (test-first) → **Review** (evidence before
   "done") → **Retrospective** (step 3). Each phase runs via Sigma's own **executor** (`sigma-brainstorm` → Goal, `sigma-research` → Research, `sigma-plan` → Plan,
   `sigma-implement` → Implement, `sigma-review` + `sigma-verify` → Review, `sigma-retro` → Retrospective).
   An installed companion (such as `/code-review`) changes who executes a step, never which gates bind:
   Plan-Review, `sigma-review` and `sigma-verify` run on every goal and may not be skipped.

   **Before Research or code, capture P1 acceptance for every goal:** run
   `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/acceptance.py" record .sdlc "<goal>"`.
   This records the issue/local `## Done when` section (3–7 checkable statements).
   If absent, draft that section in a file and rerun with `--draft <file>`; capture posts
   drafted GitHub criteria back as a comment. Add `--verify-command '<command>'` when a
   focused proving command is known (local frontmatter is inherited). Never include secrets.
   Keep the resulting `.sdlc/acceptance/<goal-stem>.md` unchanged through implementation;
   copy it into the goal worktree and commit it with plan/research. If existing criteria
   are malformed, repair the source before capture; do not silently drop criteria.
   Refusal stops P1 until repaired.
   **After Research, route the rest by the lane it measured** — `python3
   "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/discovery.py" lane "<goal-path>"` in local mode, or read it
   from Research's note on the issue timeline in github mode — see *Lane routing* below.
   Each executor's resolution header encodes this — so it works on any host.
   Record each phase as you go — `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" note .sdlc
   "<goal>" -` (text on stdin, quoted heredoc; #713) (and 🔒 Critical Insights for key decisions) — so the
   issue timeline (github mode) or `.sdlc/journey/` (local) holds the audit trail. Mark phase
   boundaries too, now with real console visibility (#1626) — before dispatching each phase's
   subagent: `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/phase_report.py" start .sdlc
   "<goal>" <goal|research|plan|plan_review|implement|review|retro> --model <tier> --pid "$PPID"` prints a
   two-line banner (`⚪ PHASE START · #<goal> <title>` / `   🟨 P2 RESEARCH · requested host model:
   <id|unrecorded> · predicted model tier: <tier> (agent-set)`) and stamps a marker — an
   announcement, not a boundary, so deliberately not an EVENT block (#2112). **Bracket the dispatch
   itself the same way `/sigma-loop` does** (`../sigma-loop/references/running.md`): `python3
   "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" log .sdlc "<goal>" agent_dispatch --role phase
   --phase <phase> --model <tier>` right after dispatching the phase's subagent — **`--model` is
   REQUIRED here (#2514); `loop.py log` refuses a `--role phase` dispatch with no `--model`, and
   `<tier>` is the same value already resolved above, never a fresh call** — and `agent_done --role
   phase --phase <phase> --result <...>` right when it returns. Once that phase's subagent
   returns its `agentId`, `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/phase_report.py" end
   .sdlc "<goal>" <phase> --agent-id <agentId> --pid "$PPID"` measures its real transcript and writes the `phase`
   ledger event with real `tokens_in`/`tokens_out`. Never invent `ms`/`tokens_in`/`tokens_out`
   yourself. On Codex, a model mismatch or a configured raw-token ceiling with unmeasured usage
   makes `end` fail; stop the goal rather than treating the phase as verified.

   For a dispatched Claude phase, add `--requested-model <Task-model-selector>` to `start`, using
   the exact Task `model` argument (such as `sonnet`). That value is for display; the existing
   observed-tier check still applies when Claude's transcript is readable. For a dispatched
   Codex phase, add `--host-model <actual-host-model-id> --expect-agent-id` to `start`, using the
   exact model ID passed to the host subagent call; `end` refuses an observed mismatch. For inline
   work, omit those flags unless the current model is known. Ask the Codex phase
   subagent to run
   `python3 <absolute-phase_report.py> codex-agent-id`, and pass its returned `CODEX_THREAD_ID`
   UUID to `end --agent-id`. A Codex task name is not a rollout ID. Inline work omits
   `--agent-id`, never `--pid "$PPID"` (#2667). Codex model/tokens are observed; dollar cost is unavailable. THAT one is an EVENT
   block, and it adds the phase's **elapsed wall time** and the phase that follows it
   (`… P2 RESEARCH → P3 PLAN — 8m11s · $4.98 · …`; `→ last phase` on P7) — and carries no ✅ or
   other status marker, because you call it the same way whether the phase passed or
   blocked, so it claims nothing about the outcome. The goal TITLE on both
   lines is resolved from what is already on this machine (#2100) — no network call on the
   phase-boundary path — and degrades to the bare id when nothing on this host has it; pass
   `--title "<title>"` when you already hold it. The end block is BUILT by `render.py`, shelled out
   (#2112); a refusal falls back to the pre-#2112 `PHASE END` banner with the reason on stderr,
   never to silence. The console print is
   unconditional; the ledger write still gates on `journal.enabled` (off by default) like every
   other EVENTS-stream write — a different flag from `action_log`. Running inline (a host
   without subagents only; elsewhere `record done` can refuse it)? Call `end` without `--agent-id` — it falls back to a windowed read of your
   own session transcript, printing `cost: unavailable on this host` only when that too is absent.
   During **Implement**, announce each plan step as you start it — `python3
   "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/phase_report.py" step .sdlc "<goal>" implement --num <n>
   --total <m> --phase-model --brief "<step>"` prints a `STEP <n>/<m>` banner with this phase's predicted tier and
   writes nothing — as you follow `sigma-implement` (details:
   `sigma-implement`'s `## Announce each step`).
3. **Retrospective (Learn)** — after Review, run the **`sigma-retro`** executor: reflect on the
   structural + product debt the fix left behind, grade intent-vs-shipped, and route durable lessons to
   the right store (audit trail / north-star / standing rule). It's **advisory** — it records the
   audit-trail notes and **proposes** any north-star or standing-rule change for you to approve; it
   never auto-writes your standing docs.
4. When the goal is genuinely complete (verified, not assumed), record it — including the retro
   grade from step 3 (`--retro-grade achieved|partial|diverged`):
   `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" record .sdlc "<goal-path>" done
   --retro-grade achieved|partial|diverged`
   so it shows as done in `/sigma-status`. If the user stops early, or it hits an irreversible action
   they don't approve, record `parked "reason" --retro-grade ...` instead (omit the flag only if the goal
   stopped before Retrospective).
   Passing `--retro-grade` is also what refreshes the knowledge graph when
   `knowledge_graph.auto_refresh` is `true` (issue #1562) — `record` calls `kg.py refresh` itself
   once the outcome is recorded, so there is no separate graph build to run.
5. Report what shipped + the evidence.

## `--feature <unit>` — confine the run to one unit of work

`/sigma-goal --feature voice-interview` is an ordinary interactive run with one constraint added to
step 1. **Without the flag none of this section applies** — selection is as before.

> **While a `--feature` run is active, no goal outside that unit may be picked.**

That sentence is the contract's: `docs/branching-model.md` §14 states it once, and
`/sigma-loop --feature <unit>` quotes the same bytes — **quote it, never reword it.** The flag exists
so a focused session cannot wander onto adjacent work.

`--label <name>`: run `label_drain.py plan`, ask every question in one pass, run `answer`, then loop
(`docs/label-drain.md`):

> **While a `--label` run is active, no goal outside that label may be picked, and an open prerequisite of a member is never skipped silently.**

Three commitments ride with it, and none is optional:

- **Exclusive, not preferential — so the run STOPS when the unit drains.** Not a fallback, not a
  "nothing else to do" convenience, not a reason to ask the user to name something else.
- **Membership is the §4 declaration pair, never the label alone** — the `feature:<unit>` label and
  the two-line body marker together, with the body winning a conflict.
- **An excluded goal is left exactly as it was found** — not labelled, not commented on, not
  parked, not partially run. It was never this run's to touch.

**The flag takes the UNIT's name** — `voice-interview` — not its branch and not its label. Whether
a name is legal is `features._is_unit_name`'s answer and only its
(`skills/sigma-loop/scripts/features.py`); nothing here restates that rule, and a name it refuses
stops the run before anything is selected.

**The §4 pair is `features.read`'s answer, never a re-reading of it.** Where the two declarations
disagree, **the body wins**. Do not settle membership from the label alone: the label is attached at
pick, so a goal that declares the unit in its body and has not been picked yet carries no label at
all. That exact shortcut is the defect `unit_completion._declared_open` was written to fix (#1570),
and it is the read that answers "what is open on this unit".

Then, in step 1:

- **A goal named on the line that is not a member: stop.** Say which unit was asked for and which
  unit the goal declares, and do nothing else — no worktree, no phases, no partial run, and nothing
  written to the goal itself: no label, no comment, no park. Offering to run it anyway is the
  wandering this flag was added to prevent, with a question mark in front of it.
- **No goal named: ask the loop's own picker, scoped** — `python3
  "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" next .sdlc --feature <unit> --session-pid
  "$PPID"` — rather than reading the backlog a second way. The guarantee above then has ONE
  implementation, shared with `/sigma-loop`, instead of two that are free to disagree; and the goal
  comes back claimed, so a concurrent `/sigma-loop` cannot take it out from under an interactive
  run. `$PPID` is read fresh from your own shell on each call, exactly as `/sigma-loop` requires.
- **Nothing pickable: stop, and say so.** `nothing pickable` is `loop.py`'s own phrase for this
  state — reuse it, and NAME THE UNIT, because "this unit is drained" and "the board is drained"
  are different facts and only one of them is about the run the user asked for. A drained unit does
  not widen the search, does not fall back to the rest of the backlog, and is not a reason to ask
  the user to name another goal. Ending the run with the unit empty is the correct outcome, not a
  failure to find work.
- **The flag never MAKES a goal a member.** Stamping a body marker or attaching a `feature:` label
  so that a goal passes the filter defeats the filter. Both halves are written at pick, under the
  branching model's own rules; a selection constraint does not get to write either.
- **An issue that contradicts itself has no honest unit.** `features.read` raises `AmbiguousUnit` on
  rival declarations. A *named* goal in that state stops the run — a human edits the issue. A
  *candidate* in that state is skipped, never guessed at.

Everything after step 1 is unchanged: the same seven phases, the same gates, the same recording. The
unit constrains which goal runs, never how it runs.

## Lane routing — ceremony proportional to the work

Research sizes each goal into a lane; this is what consumes it. Without this step the lane is a label
nobody reads, and a typo fix earns the same seven-phase treatment as a schema migration.

- **small** — plan in a few lines rather than a document, and keep the retro to one line unless
  something real surfaced. Don't open a design discussion for a goal that touches one file.
- **medium** — the full pass, unchanged. This is the default.
- **large** — before planning, work the design out explicitly: the new structure, the contract that
  changes, the callers affected. Then ask whether it should be *several* goals — a large lane is the
  signal to split, and splitting is usually the better answer.

**Plan-Review runs in full at every lane.** It is the gate that never gets skipped: small goals are
where an unreviewed plan actually ships, because nobody looks twice at a change that seemed obvious.

An unsized goal resolves to **medium**, so an unknown goal gets more rigour rather than less.

Unlike `/sigma-loop`, you do NOT auto-proceed past checkpoints — the user approves each gate.
(The `../sigma-loop/scripts/loop.py` path reaches the sibling skill's recorder — both ship in one
plugin under `skills/`, so the relative path is stable.)
