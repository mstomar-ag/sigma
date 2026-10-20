# Running the loop

How Sigma runs a goal: the two modes, the seven phases, and model selection. Back to the [README](../README.md).

## Two ways to run: interactive or autonomous

Both modes drive the **same seven phases** per goal — they differ in who's in the loop and what
happens at a checkpoint. The repo-scoped prompt hook underpins both.

### `/sigma-goal <goal>` — interactive

One goal through the engine, **pausing for your approval at each gate**. Take a goal from
`.sdlc/goals/` (preferred — so it's tracked) or inline text, then walk Goal → Research → Plan →
**Plan-Review** (via `sigma-plan-review`; recorded, and the loop refuses `done` without it) → Implement (test-first) → Review (evidence
before "done"). It does **not** auto-proceed past checkpoints — you approve each one. The outcome is
recorded to `.sdlc/` (`done`, or `parked` with a reason) so it shows in `/sigma-status`.

### `/sigma-loop` — autonomous

Pulls the backlog — local `.sdlc/goals/` files or [GitHub issues](backlog.md#your-backlog-local-files-or-github-issues) —
and runs **each goal autonomously** through the same phases, one dispatched subagent per phase, each bracketed
by `phase_report.py` (banner and cost line) and recorded in the action log; `loop.py phases <dir> <goal>` shows
what is recorded and the next step. A trivial goal can waive research and retro only
(`loop.py waive-phases`, recorded and visible); more phases mean more spend, so measure it on your own goals. Anything
that needs a human is **parked to `.sdlc/state/review-queue.md`** and the loop continues — it parks,
it does not force. It parks on:

- a hard checkpoint / a decision only you can make,
- an **irreversible or expensive action** (deploy, delete, overwrite, spend, migrate) — its
  instructions ask the agent to park it rather than run it unattended (advice: no code reads `gates.irreversible_actions` unless `rulebook.hard_stops.enabled` is true, default false, where it is the legacy alias for the class toggles;
  that advice is about the agent, and two things Sigma's own code does are outside it because each is bounded by
  construction: the lease-protected force-push of a unit branch, and, only once you enable upkeep, the removal of old
  backup refs under Sigma's own backup namespace; see `docs/branching-model.md` section 13b),
  unless you opted in (`spend_approval`, off by default) and wrote a single-use
  `sigma:spend-approved=<label>` line as the first line of that goal's issue body, which
  `loop.py spend-approval` checks, audits and then honours once (a per-use go-ahead, not a spend cap; the check is on who opened the issue, so any repo writer could add the marker to it),
- a hard failure it cannot resolve — recorded as **`failed`** (needs a fix), distinct from
  parked (needs a decision), so the review queue separates the two.

It halts on the **per-run budgets** (`config.json` → `budget`) — `max_iterations`, `max_minutes`
(wall-clock from the session's own `start`), `max_tokens` (cost-equivalent tokens from fully priced
Claude phase reports or explicit `loop.py spend`), and `max_codex_raw_tokens` (measured Codex phase
input + output tokens, including cached input; the reported cached and reasoning subsets are
already inside those two and are never added again). The two token counters use different units and never
mix. Codex's token ceiling is a **phase-boundary admission stop**, not a quota or billing cap: it
checks the next pick after measured phases finish and does not include orchestrator or unmeasured
turns. Keep `max_minutes` and a goal-count ceiling for those gaps. A run, and so every budget, belongs
to the session that started it: the token ceilings count the phases (and `loop.py spend <dir>
<tokens> <goal>` reports) of goals that session holds, and spend that names no goal, or a goal no
live session holds, is charged to every live session, so it over-counts rather than escapes a
ceiling. Another session's `start` in the same checkout neither resets nor inherits them, so **N
concurrent sessions may together spend N x each ceiling** — the same as N sequential runs. Size the
ceilings with that in mind if you run sessions side by side. Each key enforces only when set;
an absent/zero key enforces nothing. When a Claude model is absent from the rate card, every phase
end warns that `max_tokens` did not count its turns and `/sigma-doctor` reports the same coverage
gap. Independently,
a NEW `handoff.after_goals` ceiling (default 20, ON even when the
key is absent — see `config.json.tmpl`) stops the ORCHESTRATING session itself after that many
goals and hands off to a fresh one, so its own context never keeps growing across an unattended
drain; `enabled: false` opts back into the old behaviour. Goal-count limits retain their count
throughout one session, including refills; a new session resets them. A stopped run is
resume-safe (a budget stop, re-run, picks up
where it left off).
**Overnight without babysitting:** `python3 <installed-sigma>/skills/sigma-loop/scripts/supervise_daemon.py .sdlc` wraps the
loop in a zero-polling supervisor — blocked while a session runs, and on exit it classifies the
tail: loop finished → stop; per-run budget → relaunch; **usage-limit exhaustion → sleeps until the
stated reset time (+ jitter) and relaunches**; unknown crash → capped escalating backoff. Stop it
any time with `touch .sdlc/state/supervisor.stop`. (Sleeping *machine* ≠ sleeping process — on a
macOS laptop run it under `caffeinate -is`.)
For Codex, follow the [Codex-only opt-in migration](codex-autonomy.md) first; it verifies the enabled
Codex plugin and launches a fresh Codex session without changing Claude's installation.
Run **`/sigma-status`** any time for backlog counts (pending / in-progress / done / parked / failed) + whether the
review queue needs attention.

| | `/sigma-goal` (interactive) | `/sigma-loop` (autonomous) |
|---|---|---|
| Scope | one goal | the whole `.sdlc/goals/` backlog |
| At a checkpoint | pauses for you | parks to the review queue, continues |
| Approval | every gate | only what it parks |
| Stops on | goal complete / you stop | backlog empty, per-run budget, or a context hand-off (then resumes fresh) |
| Context bounded across goals | not yet — see issue tracker for the follow-up | goal count capped at 20 per session by default (`handoff.after_goals`); post-change token measurement pending |
| Irreversible action | asks you | asks the agent to park it (advice; a code check only when `rulebook.hard_stops.enabled` is true) |

`/sigma-goal` claims its own named goal directly and is not driven by `loop.py next`/`next-batch`,
so the hand-off ceiling above does not reach it — a human re-running `/sigma-goal` many times in one
long conversation can still grow that conversation's context unbounded. Tracked separately, since
`/sigma-goal`'s own approval-gated, single-goal design needs a different answer than an autonomous
loop's does, not merely the same mechanism wired in.

## The seven phases

Each phase runs via one of Sigma's own `sigma-*` skills, on every host.

1. **Goal** — restate the objective as one concrete, checkable goal. For feature/creative work, this
   is where you explore intent and requirements first.
   → *executor:* `sigma-brainstorm`.
2. **Research** — map the blast radius: affected files, existing patterns, constraints, prior art —
   then size the goal into a lane (`small`/`medium`/`large`) from what was actually measured, so small
   goals skip the ceremony they don't earn.
   → *executor:* **`sigma-research`** (Sigma's own).
3. **Plan** — write the plan: steps, files, tests, and a definition-of-done. Size it against real
   throughput with **`/sigma-velocity`** (measured git pace), not "this feels like weeks."
   → *executor:* `sigma-plan`.
4. **Plan-Review** — adversarially review the plan **before** any edit: verify each claim against the
   real code, stress-test what breaks after it ships, check scope/fit, and (vision-first) check it
   against your strategy. The skills ask the agent never to skip it. In the loop it is also checked: `gates.phase_record` (on in a fresh `/sigma-init`) makes `loop.py record done` and `work.py merge` refuse a goal with no approved plan-review (recorded through `work.py record-plan-review`, which binds it to the exact plan bytes; the gate needs `action_log.enabled`), and the opt-in `gates.plan_review.enabled` (off by default; needs `work.enabled`) additionally refuses the push in `work.py pr`. Interactive work that never calls `record done` or `work.py merge` is asked, not gated; the record is agent-written, so it proves a verdict was recorded, not who reviewed. Sigma owns this step.
   → *owned by* **`sigma-plan-review`** (Sigma's own).
5. **Implement** — build test-first and execute the plan step by step.
   Record the red with `loop.py red <sdlc> <goal>` once the planned tests fail (selector-only, no full verify), then code, then `loop.py verify` for green, then `work.py pr`. Red must be on the final test bytes: edit a test and record red again. `red` exits 1 when nothing is red, no assertion failure was recorded, or only some planned nodes are red (the PR gate needs every node), 2 when refused, 6 while a verify runs. It writes no verify evidence; the full verify is still required for green.
   → *executor:* `sigma-implement`.
6. **Review** — code-review the diff for real findings, then verify every claim with evidence before
   declaring anything done.
   → *executor:* `sigma-review` + `sigma-verify` (optional extra: `/code-review`).
7. **Retrospective** — surface the structural + product debt the fix left behind, grade
   intent-vs-shipped, and route each durable lesson to the right store (advisory).
   → *executor:* **`sigma-retro`** (Sigma's own).

## Match the model to the goal (optional)

A one-line rename doesn't need Opus; a schema migration shouldn't run on Haiku. Set
**`model_selection: auto`** in `.sdlc/config.json` and `/sigma-loop` **predicts a tier per goal** —
`haiku · sonnet · opus · fable` (`fable` only if `model_selection_max_tier` is raised; the default is `opus`) — from the goal text (deterministic regex, zero-dep), then runs that
goal's phases at it inside a subagent (the session can't switch its own model). Conflicts resolve
**upward**, so a hard goal is never under-powered, and a review send-back a tier cannot converge raises it one
rung (`loop.py escalate`) instead of parking. Off by default; run **`/sigma-model "<goal>"`** any
time to see the recommended tier. `/sigma-goal` also dispatches each phase at the selected tier
when subagents are available. Claude accepts the tier alias; Codex resolves it to an exact model ID
and reasoning effort before dispatch, checks the observed phase rollout, and uses a plugin-versioned
map. `model_host_overrides.codex` can choose a release-approved ID; a changed catalog needs a plugin update, never an arbitrary model string or portable tier such as `sonnet`.

### The loop session's own model

The session that runs `/sigma-loop` spends its model on every orchestration step, however cheap the
goal's phases are. Choose it with **`loop.session_model`** in `.sdlc/config.json` (letters, digits, `.`,
`_`, `-` only; anything else is refused with exit 2). The supervisor (`supervise_daemon.py`) passes it as
`--model` when `SIGMA_CLAUDE_CMD` is not set; with `model_selection: auto` and no key it defaults to
`sonnet`, and with neither it adds no flag. An explicit `SIGMA_CLAUDE_CMD` is never rewritten.
`/sigma-doctor` shows a "loop session model" row read from the session transcript and warns when that
model is priced above every tier the session dispatched; a host that exposes no transcript says so.
Measured: temp-dir tests only, not a live session.
