# Picking a goal

The detail behind steps 1 and 2 of [`../SKILL.md`](../SKILL.md): resetting the per-run
budget, the batch-dispatch path when more than one goal comes back, what `DONE` and
`BUDGET` mean, confining a run to one unit of work, and the three opt-in pre-checks that
run before a token is spent.

---

Drive the backlog autonomously. The Python helpers own state/budget + the backlog source; you run
each goal. The source is config-selected (`.sdlc/config.json` → `discovery.source`): **local goal
files** (default) or **GitHub issues** (`source: github`, needs an authenticated `gh`). You run the
loop the same way either way — the helper handles where goals come from and how status is recorded.

First, reset the per-run budget: `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" start .sdlc
--session-pid "$PPID"` (mid-session, `start-run .sdlc --session-pid "$PPID"` resets your session's
and the checkout's budget for a fresh push, only when the user says so this session, never to continue past a stop, without `start`'s side effects or
editing `config.json`; bare: the only live session, or none.) **Always pass
`--session-pid "$PPID"`** on this call and on every
`next`/`next-batch` call below — `$PPID` here means literally that: read it fresh from YOUR OWN
shell each time you run one of these commands, not a value you captured once and are trying to
remember. It is your invoking shell's own parent process id, and it stays the SAME stable value
every time you read it during this one loop run — this identifies the caller's process. Codex
Desktop tasks can share it, so the registry also uses the validated `CODEX_THREAD_ID` supplied by
Codex; a missing or invalid Codex task ID refuses the pick. The session registry
(`loop.py session-active`/`session-end`, `docs/routines.md`) uses both to tell your own
prior picks apart from a sibling session's, and without it explicitly threaded through, a later
`next`/`next-batch` call can silently re-dispatch a goal THIS SAME run already claimed (confirmed:
omitting the flag lets that happen, because each separate command you run gets its own fresh,
short-lived process. Codex's task ID distinguishes tasks that share the same caller PID; the
explicit PID still binds all commands in one run and provides the liveness check).

Then repeat until the helper says stop:

1. `goals=$(python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" next-batch .sdlc --session-pid "$PPID")` —
one goal per line, or a lone `DONE`/`BUDGET`/`HANDOFF`. `parallel.goals.enabled` (off by default,
config-gated) decides ONLY how many lines can come back in one call — off, or only one goal
available, `next-batch` still returns exactly one line, byte-identical to plain `next`
(`goal=$goals`). It does **not** decide whether that one goal is dispatched: **every goal
`next-batch` returns, one line or many, is dispatched to its own fresh subagent below — none run
inline on hosts with subagents** (#2521). A host without subagents calls `next` for one goal and
runs its steps 2-7 inline, then repeats in a fresh session after the code-level `HANDOFF` (20
completed goals by default). Before this, an orchestrating session that ran every goal's steps 2-7 in
its own context, one after another, was the whole reason it grew unbounded across a long backlog
drain — measured on this repo's own transcripts: 13 of 18 sessions that peaked past 800k tokens of
context were `/sigma-loop`/`/sigma-goal` sessions, auto-compacting at 930k-998k. Dispatching even a
single goal keeps this session's own job to picking, dispatching, and recording one short result per
goal — the dispatch prompt and the subagent's own returned summary are what land in THIS session's
context, never the phase-by-phase transcript underneath (see `references/running.md`: "Artifacts
pass between phases through the filesystem... not shared context" — already true of a dispatched
goal's OWN internal phase subagents; #2521 is what stops the orchestrator from re-absorbing that
same transcript one level up, goal after goal).

**1a. Dispatch every goal when subagents are available — one line or many** (#2521, extends F10.5-3/#375's own batch shape to a
batch of one) — mirrors 3b's slice-wave shape one level up: instead of ONE goal's implementation
slices running concurrently, one or more WHOLE GOALS run — concurrently when `parallel.goals.enabled`
allows more than one at a time and `work.enabled` is on (else `next-batch` clamps to one), one at a time otherwise, but ALWAYS each in its own fresh subagent,
never inline in the orchestrating session.

**Capture the goal's resolved tier before you dispatch it — the SAME `next`/`next-batch` call that
just returned this goal already resolved it, strictly before returning.** `_next()` (the shared
chokepoint behind both `next` and `next-batch`) prints it to stderr, the identical line
`references/running.md` already documents for phase dispatch: `sigma: model tier for <goal>
resolved to <tier> (signal=<signal>)`. Read it from that same call's own output rather than
resolving it again — matched to THIS goal by the goal id the line names, never by position: a
`next-batch` call returning several goals prints one such line per goal, interleaved with other
pick-time diagnostics (cross-repo checks, the LEDGER INBOX block), so the line naming goal `b` is
not necessarily adjacent to `b`'s own entry in `$goals`. The later solo refill below (`loop.py next
... --skip ...`) resolves and prints exactly one goal's line the same way — one convention covers
both shapes. If the line is missing (`model_selection` isn't `"auto"`, or `journal.enabled` is
off — the shipped default), **fall back to resolving it yourself**, the exact fallback
`references/running.md` already documents for phase dispatch: in local mode `predict.py resolve
"$goal" .sdlc`; in github mode, fetch the issue's real text first, title on the first line
(`gh api repos/{owner}/{repo}/issues/"$goal" --jq '.title + "\n\n" + (.body // "")'`)
and pass that text, never the bare `"$goal"`. Under the default selection
setting that fallback prints `off`; retain it as the explicit no-auto-selection state and give it to
Codex's host resolver, which selects its versioned ordinary-work mapping.

For each goal in the batch, dispatch a **subagent** (fresh context) that runs this skill's steps 2
through 7 for that ONE goal. Claude receives the tier you just captured as its Task `model` value.
Before a Codex goal-slot dispatch, resolve that tier with its canonical effort: `python3 "${CLAUDE_SKILL_DIR}/../sigma-model/scripts/predict.py" host-model codex "<tier-or-off>" .sdlc --effort "<effort>"`; pass the printed `model=<id>` and the same `effort=<effort>` to Codex's subagent parameters. If resolution refuses, do not dispatch; select an approved current Codex ID in `model_host_overrides.codex`, or update the plugin for a changed catalog. Keep the portable tier for the ledger:
log the dispatch with that same tier: `python3
"${CLAUDE_SKILL_DIR}/scripts/loop.py" log .sdlc "$goal" agent_dispatch --role goal-slot --model
<tier>` — `actionlog.py` now requires `--model` for `--role goal-slot` unconditionally (#2514,
extended by #2544/#2555, consolidated alongside `phase`/`slice`'s own check — see that file's own
comments) — exactly as documented: it cuts its own worktree (3a already isolates goals from each
other: separate worktree + branch + PR per goal, so no extra `isolation: worktree` bookkeeping is
needed beyond what `work.py start` already does for a single goal), runs its own review, and
records its own outcome, **never above the tier you handed it** — the same ceiling
`references/running.md` has that subagent read back out of ITS OWN pick's stderr for its own phase
dispatches a moment later; passing it in here does not change what the ceiling IS, only who
resolves it first. The one exception is a review send-back that tier cannot converge: the slot
runs `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" escalate .sdlc "$goal" <tier> --after plan-review`
and re-dispatches that phase at the tier its `ESCALATE` answer names — never a park for "budget" or
"tier too small" (#2828; `references/running.md`). **Say in the slot's prompt** that each phase (research, plan, plan-review, implement, review, retro) is its own dispatched subagent, never inline, tier `off` included: `loop.py record done` is REFUSED otherwise (#684); `loop.py phases` names the next step. **Never** dispatch with an unattended `claude -p` (same reason as 3b: uncapped spend,
and a second unmanaged worker on one `.sdlc` breaks state) — and **never** hand a subagent's own
scratch/comparison work a `cp`/`rsync` of another goal's live worktree; a worktree's `.git` is a file
pointing at shared metadata, and copying it can corrupt the real one.

On Codex, dispatch the goal slot with `fork_turns="none"`. The default copies the parent's full
history into the child and defeats the context bound. Give the fresh child the project path,
goal ID, Sigma skill path, and instruction to run steps 2–7; artifacts and review briefs are
read from disk. If the host cannot provide fresh context, use the no-subagent inline path below.
Ask each slot to return a short outcome containing the goal ID, done/parked/failed verdict, PR or
worktree path, verification result, and any blocker. Keep the detailed evidence in the plan,
review artifact, test output file or issue timeline; send paths to those artifacts rather than
copying the phase transcript, full diff, or test log into this orchestrator's conversation. Before
accepting a `done` outcome, inspect that goal's diff or PR and the raw output of its configured
verification run. The proof must be fresh in the turn where you report `done`; if the child's run
is older, run the configured proof again. A child's success summary alone is never proof. Open any
other named artifact needed to
resolve a blocker or uncertain verdict. Wait for a live slot with the
host's blocking/event wait; do not issue short repeated status polls that each add a model turn.

**Claim ownership across this exact boundary needs no new mechanism, and nothing is "handed off".**
The orchestrator's own `next`/`next-batch` call already wrote the goal's claim under ITS
`--session-pid`; the dispatched subagent then reaches step 3a in its OWN fresh context and captures
its own `$PPID` for `agent-start --pid`/`work.py start --session-pid` — MEASURED (§2 of
the plan for #2521) to be the SAME value the orchestrator's own `$PPID` capture already used to
write the claim, within one continuous session, not a distinct, fresh pid. So there is no cross-pid
comparison to make at all on THIS boundary. The genuine cross-process boundary `work.py start`'s
resume guard was built for is CROSS-SESSION instead: a dead picker pid left by an ENDED orchestrating
session (a crash, or a HANDOFF/BUDGET relaunch) corroborated against a genuinely live worker marker —
and ships tested: `#1687`/`#1197`/`#1284`/`#2394`, extended by #2521's own claim-ownership regression
tests for this exact shape. A goal picked for the first time never even reaches the guard — there is
no worktree yet to resume, only to cut.

Each dispatched subagent is a full agent session, not a lightweight thread — that's the real cost of
`max_concurrent`, not anything sigma itself holds onto (see the "Memory & sizing"
section of `docs/parallelism.md`). Size the number of goal slots you actually run to what your machine can sustain that many
*active* sessions at once, not to how many issues happen to be open.

Track which goals are currently live in your other slots — you already know this, you dispatched
them. The same view, answered by a machine rather than your memory, is
`python3 "${CLAUDE_SKILL_DIR}/../sigma-log/scripts/log.py" slots .sdlc` — a Block A over the action
log, relayed verbatim — but the `--skip` list below is still built from what you dispatched, because
the action log is off by default (`action_log.enabled`) and `slots` then says so rather than listing
anything. As EACH subagent finishes (done/parked/failed) — not the whole batch — log it first:
`python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" log .sdlc "$goal" agent_done --role goal-slot --result
<done|parked|failed>` for the goal that just finished, THEN immediately refill
just that ONE freed slot: `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" next .sdlc --session-pid
"$PPID" --skip <comma-separated other still-live goals>`. **The `--skip` list is not optional for
a refill**, and neither is `--session-pid "$PPID"` — see the note above the first `start` call.
`next`/`next-batch`'s own claim-liveness check (F10.5/#374) can only tell whether the SHORT-LIVED
`loop.py` process that wrote a claim is still literally running — and that process has already
exited by the time it returns the goal to you, regardless of whether a subagent is still actively
working it minutes later. Under the GitHub label queue, a goal's own `sdlc:in-progress` label
(`mark_in_progress`, written synchronously as part of the pick that gave you the goal) DOES now
exclude it from a later pick (#1198) — but that write is best-effort: a transient `gh` failure is
swallowed silently, leaving the goal unexcluded, and a local-source install's own in-progress
marker is not this same durable, cross-session signal at all. `--skip` has no such gap — it is
certain the instant a pick returns, with no dependency on a separate GitHub read landing
correctly — so **it is still not optional for a refill.** Omitting it on a refill can re-dispatch
a goal a sibling slot already holds — two subagents doing the same work, worse than not
parallelizing at all. Continue refilling one slot at a time until a call
returns `DONE`, `BUDGET`, or `HANDOFF`. A terminal line stops new claims; finish and record
the other live slots before retiring the orchestrating session. If a `next-batch` response
contains goal lines and a terminal line, dispatch those goals first, then wait for their
outcomes without refilling. A goal claimed this way but never actually dispatched — a batch pick
that never got a free slot this run, or one you deliberately leave out of a refill — is not
cleaned up on its own: release its stale claim explicitly with `python3
"${CLAUDE_SKILL_DIR}/scripts/loop.py" release .sdlc "$goal" "<why>"`, the sanctioned counterpart
to `mark_in_progress` (leaves an audit comment,
never touches budget). Goals carry no file-conflict graph — each has its own worktree and PR, so overlap
surfaces as a plain PR-rebase. With multiple slots
live, it is the ORCHESTRATING pass — the one calling `next`/`next-batch` for refills — that sees any
**LEDGER INBOX** block (SKILL.md step 6), not a subagent mid-goal; handle it there, between refills,
the same way you would between goals in the single-goal path.

**No subagent capability:** call `loop.py next` instead of `next-batch`, run steps 2-7 for that one
goal inline, then call `next` again. Do not log a `goal-slot` dispatch or refill a nonexistent
slot. The `HANDOFF` response stops the run before the next claim and prints `/sigma-loop` for a
fresh-session continuation; the default 20-goal ceiling bounds the inline session. Phase reviews
still use `reviewer.py resolve .sdlc` as step 3 requires. If that resolver refuses an independent
review mechanism, park the goal instead of self-approving it.

2. If output is `DONE` (nothing pickable), `BUDGET` (a per-run budget hit: iterations, wall-clock
minutes, or reported tokens — each only when `config.json` sets it), or `HANDOFF` (the per-session
goal-count ceiling) → STOP new claims, finish any live goal slots, and report its stderr reason.

`HOLD` (no room) is not a stop, never `DONE`; nothing is claimed, no `budget` spent. Run
`resource_lease.py wait --max 540`, then pick again.

`DONE` does not always mean the backlog is empty. `next`/`next-batch` never hand back a goal
whose own body declares a prerequisite (`**Blocked by:** #N`) that is still OPEN — it is left
untouched, still `sdlc:goal`, never claimed and never parked, and becomes pickable by itself when
the blocker closes (`discovery.dependency_gate`, on by default; local goals: `#N`, or a stem on the `**Blocked by:**`
line, held until that file is `done`; an unresolvable ref holds too). When that is
why a pass came back empty, stderr says so: `nothing pickable — N goal(s) are waiting on an open
prerequisite (...)`. Report that line rather than "the backlog is drained", and do NOT `--skip`
past it.

Once someone closes the blocker, the very next `next`/`next-batch` releases its dependents: the
gate re-reads the blocker's live state rather than trusting the board mirror's cache. Nothing to
wait out, nothing to unpark. If the board cannot be reached the hold stands, and stderr says how
old the evidence is.
**Confining a whole run to one unit of work: `--feature <name>`.** Both `next` and `next-batch`
take it, and it is *exclusive*, in the words `docs/branching-model.md` §14 settles:

> **While a `--feature` run is active, no goal outside that unit may be picked.**

`--label <name>` (`next`, `next-batch`) does the same for a run label, see `docs/label-drain.md`:

> **While a `--label` run is active, no goal outside that label may be picked, and an open prerequisite of a member is never skipped silently.**

No fallback: when the unit has nothing pickable the run says so and **stops**, distinguishing a
drained unit from a drained board. Membership is the issue's `Feature:`/`Branch:` declaration pair
(§4), never the `feature:<name>` label alone — the label is attached at pick. A goal outside the
unit is left untouched: not claimed, labelled, commented on or parked. An impossible unit name, and
a bare `--feature`, are refused before any query. Open one with `/sigma-define`.

3. Otherwise: first **cross-check the pick** (opt-in, `backlog_check.enabled`) — before spending a
token, run `result=$(python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" precheck .sdlc "$goal")`. It
prints `OFF` (a no-op — feature disabled) or, when on, refreshes the board mirror and cross-checks the
goal against the rest of the backlog + the team ledger at **zero LLM cost** (also checking the
goal's own recent comments for a human-authored dependency marker your loop never wrote), then
either:
- prints **`PARKED <reason>`** — the goal was a confident DUPLICATE / OBSOLETED-BY-completed-work /
  BLOCKED-BY item and has already been parked-with-proof (the evidence is on the issue). **Do not
  research it: loop back to step 1** and take the next goal. (The park counts as one iteration.)
  If a human reviews the park and rules the finding a false positive, dismiss that EXACT match
  (not just the park) so the next precheck doesn't re-park on it identically: post
  `python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" note .sdlc "$goal" "$(python3
  "${CLAUDE_SKILL_DIR}/scripts/backlog_check.py" dismiss-text <kind> <ref> "<why>")"` — `<kind>` is
  the finding's own internal identifier, hyphenated: `duplicate` | `obsoleted-by` | `blocked-by` |
  `in-flight-elsewhere`. This is NOT the human-phrased wording the park comment renders it as —
  e.g. the rendered text "blocked by #821" is kind `blocked-by`, ref `821` (a stray leading `#` on
  `<ref>` is tolerated). THEN un-park it — restoring `sdlc:goal` on its own is **not** enough: the park added `sdlc:parked`, and that label is what keeps the issue out
  of the queue, so adding membership back beside it leaves two membership labels at once (drift
  `/sigma-doctor` reports) on an issue that is still unpickable. Run `/sigma-unpark` instead — it
  drops `sdlc:parked` and restores `sdlc:goal` in one atomic swap. **GitHub discovery mode only**: dismissal reads
  back from the goal's own issue comments, which don't exist for local goal files. In local
  (non-github) discovery, a posted dismissal note is never silently dropped — the next precheck's
  pack carries `degraded: ["dismissal_local_mode_unsupported"]` and the park/advisory line itself
  says the dismissal was not applied.
- prints **`PROCEED`** (optionally `(advisory)`, having annotated a weak match) → carry on below.

Then **check for an oversized goal** (opt-in, `goal_decompose.enabled`) — before spending a
token, run `result=$(python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" decompose-check .sdlc
"$goal")`. It prints `OFF` (a no-op — feature disabled) or, when on, classifies the goal's own
body at **zero LLM cost** (a goal already marked as a decomposition child or meta-goal is exempt
by construction), then either:
- prints **`PARKED <reason>`** — the goal reads like an epic (oversized per the classifier) and
  has already been parked for a human to split it (`mode: "file"` also files one
  idempotency-guarded "Decompose #N" meta-issue before parking, so the split has its own tracked
  home). **Do not research it: loop back to step 1** and take the next goal. (The park counts as
  one iteration.)
- prints **`PROCEED`** (optionally `(flagged: <reason>)`, `log` mode having only annotated) →
  carry on below.

Placed AFTER the cross-check above so a duplicate parks as a duplicate — a goal that is both a
dup and an epic never reaches this check.

Then **check the goal is designed** (opt-in, `goal_design.enabled`) — before spending a token,
run `result=$(python3 "${CLAUDE_SKILL_DIR}/scripts/loop.py" design-check .sdlc "$goal")`. This
is the Tech-side RETROFIT path of `goal-design` (`docs/dossier-pipeline.md` §6) — coverage for everything that reaches the loop WITHOUT having gone through the Product-path
front door (`/sigma-goal-design`, invoked directly against an approved Dossier, never through
this pick sequence). It prints `OFF` (a no-op — feature disabled) or, when on, checks the goal's
own labels at **zero LLM cost** (a goal already carrying `sdlc:designed`, or one that is itself a
decompose/design bookkeeping meta-goal, is exempt by construction), then either:
- prints **`PARKED <reason>`** — the goal has not been through a codebase-mapping design pass,
  and one "Design #N" meta-issue has been filed (idempotency-guarded, same shape as
  decompose-check's own `file` mode) instructing `/sigma-goal-design` to run against it.
  **Do not research it: loop back to step 1** and take the next goal. (The park counts as one
  iteration.)
- prints **`PROCEED`** — already `sdlc:designed`, or exempt.

Placed AFTER decompose-check, for the identical ordering reason decompose-check is placed after
the cross-check: a goal that is BOTH oversized AND undesigned must be split first, so each
eventual child gets its own design check individually, rather than one design pass being filed
against a goal about to be decomposed out from under it. `mode` (`"full"` default | `"lane"`)
only changes the DEPTH instructions written into the filed meta-issue, never whether this check
acts — unlike `goal_decompose.mode`, there is no log-only rung here. `sdlc:designed` itself is
written only by `goal-review` (contract §7) confirming a design. Where `sigma-goal-review` is not
installed, a human applies the label by hand once a filed design pass is confirmed.

**Measuring a pick (opt-in, #1607).** When a pick is slow, turn the trace on for it:
`SIGMA_PICK_TRACE=1` in the environment, or `loop.py next .sdlc --trace`. Off by default, and
off it writes nothing and adds no `gh` call. On, each pick appends one JSON line to
`.sdlc/state/pick-trace.jsonl`: per named stage (reconcile sweep, auto-unpark sweep, stale-claim
reclaim, release of unstarted claims, pending fetch, dependency hold, the unit/scope/owner gates,
claim writes, and `other` for the rest) the number of `gh` calls by class (list, read, write,
graphql, project) and the seconds. Calls are counted once, at the single transport seam, retries
included; no issue text is recorded. The file rotates at 1 MB to `pick-trace.jsonl.1` (one
generation, so about 2 MB at most). `loop.py pick-trace-report .sdlc` prints each stage's median and
p90 calls and seconds across the recorded picks. The trace measures; it does not change a pick.
