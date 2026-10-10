---
name: sigma-scope
description: Turn an idea into prioritized, deduplicated, dependency-linked GitHub issues. Use before issue creation or /sigma-scope.
allowed-tools: Bash(python3 *), Bash(gh issue *), Read, Grep
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-scope

Detailed selection triggers: [selection](references/selection.md).

`/sigma-triage` schedules and orders work that's already filed and understood. **sigma-scope**
solves the earlier problem: turning a rough, not-yet-scoped idea into a fully-planned, ready-to-
execute body of work — real issues, a real epic when one is warranted, real priorities, a real
assignment decision, and (if the user wants) an immediate, sequenced start. The mechanical pieces
(target resolution, board dedup, issue compilation, assignment + execution) already exist as
scripts; this skill is the reasoning layer that drives them and makes the judgment calls a script
can't — what the idea actually is, what's genuinely unclear about it, and what a sane plan for it
looks like.

The skill is seven steps:

1. **Resolve the target.** `python3 "${CLAUDE_SKILL_DIR}/scripts/brainstorm.py" .sdlc "<raw
   invocation text>"` — handles all four invocation forms (inline free text, a local `.md` file, a
   fuzzy reference to an existing issue, a direct `#N`) and returns a `brainstorm-target/v1` pack.
   - `form` in `text` / `file` / `issue_number` / `issue_reference`: a candidate target — confirm
     it at step 4 regardless of `confident`, a confident resolution can still be confidently wrong.
   - `form == "ambiguous"`: genuine, scored overlap between "this is new" and "this paraphrases an
     existing issue" — carries both `text` and `candidates`. Don't guess; resolve this at step 4.
   - `form == "unresolved"`: hard stop. Report `error` verbatim and ask the user to fix the
     invocation (a real file path, a real issue number) before anything else runs.

2. **Load the whole repo as context.** Normal tool use, not a script — `Read`/`Grep` the areas the
   resolved target actually touches (the relevant skill/module, its README section, its own
   conventions) so the eventual plan is grounded in what's really there, not a generic response.
   Skipping this step is the single most common way this skill would produce a plan that doesn't
   fit the codebase — always do it, even for a target that looks self-explanatory.

3. **Dedup against the board.** `python3 "${CLAUDE_SKILL_DIR}/scripts/dedup.py" .sdlc "<resolved
   text>"` (or `@<file path>` when the target came from a file) returns ranked `candidates` with a
   `strength` of `"duplicate"` (score ≥ 0.45) or `"related"` (0.18–0.45). When the resolved target
   is itself an existing issue (`form` was `issue_number`/`issue_reference`), that issue's own ref
   will legitimately appear as its own top hit — that's not a signal, discard it before reading the
   rest of the list. Never act on these candidates directly; they're surfaced as questions at step
   5, never silently treated as blocking or silently dropped.
   - A candidate's own pack carries `ref`/`score`/`strength`/`evidence`, deliberately no title —
     `dedup.py` never makes a live `gh` call itself. Before presenting a candidate at step 5, fetch
     its real title with `gh issue view <ref> --json title -q .title` (covered by this skill's own
     `Bash(gh issue *)` grant) so the question names the issue, not just a bare number — *"this looks
     related to #N: '\<real title\>' — same thing, related-but-distinct, or unrelated?"* is a
     question a user can actually answer at a glance; a bare `#N` isn't.

4. **Confirm the target explicitly.** Restate what's about to be scoped and get an explicit yes
   before planning anything: for an `ambiguous` step-1 result, ask directly which reading is
   correct ("is this a new idea, or the same thing as #N: *title*?"); otherwise, a short "scoping
   *this* — right?" is enough. This is specifically about WHETHER the right thing was resolved, not
   about the plan's content — that's step 5.

5. **Ask clarifying questions** — see "Clarifying-questions judgment" below for the exact criteria.
   Batch every genuine question into one round (mirroring `/sigma-triage`'s own question round),
   including step 3's dedup hits, each phrased as a question, never as a silent block or a silent
   skip: *"this looks related to #N — same thing, related-but-distinct, or unrelated?"*

6. **Plan thoroughly**, grounded in steps 2's context and step 5's answers:
   - Decide whether an epic wrapper is warranted (a real shared parent across several issues) — a
     single-issue plan never needs one; `compile_plan.compile_plan()` ignores epic data for a
     single-issue plan regardless, so decide this deliberately rather than defaulting to "always".
   - Draft each issue: a real title, a body grounded in step 2's actual file/module names (not
     placeholder text), a real `P0`–`P4` priority (default `P1` if nothing about the work argues
     for a different tier), and `blocked_by` edges to sibling keys where a genuine build-order
     dependency exists.
   - Write the plan as JSON in the exact shape `compile_plan.compile_plan()` documents
     (`{"epic": {...} | None, "issues": [{"key", "title", "body", "priority", "blocked_by"}, ...]}`)
     to `.sdlc/plans/scope/<slug>.plan.json` — durable and inspectable, the same general idea as
     `/sigma-triage`'s own plan artifacts (JSON left on disk under `.sdlc/plans/` so a later session
     can see what was decided), though without triage's date-prefixed slug, human-readable `.md`
     twin, or `active.json` pointer — this is a plain `<slug>.plan.json`, nothing more.
   - **Show the drafted plan to the user and get explicit confirmation before compiling it** — the
     next step creates real, live GitHub issues; there is no dry-run mode for it, so this
     confirmation is the equivalent gate `/sigma-triage`'s own dry-run step gives its plan.
   - Once confirmed: `python3 "${CLAUDE_SKILL_DIR}/scripts/scope.py" .sdlc --plan
     .sdlc/plans/scope/<slug>.plan.json --report .sdlc/plans/scope/<slug>.report.json` — this is
     the ONLY supported way to invoke `compile_plan.compile_plan()` from here (see "Why
     `scope.py`" below); never pass `--actionable` at this step, issues are armed at their plan priority (or parked if
     the deny-list matches); with `ai_filed.triage.enabled` false they file with the legacy `sdlc:needs-confirmation`.

7. **Resolve assignment and the execution path.** Infer `area` from the plan's own primary
   touched path (the directory/file step 2 and step 6 centered on); if the plan genuinely spans
   several unrelated areas with no shared owner, ask which one to resolve against rather than
   guessing.
   - `python3 "${CLAUDE_SKILL_DIR}/scripts/assign.py" resolve .sdlc "<area>"` returns `options`
     (self / CODEOWNERS owner / up to 3 active members) — present them and let the user pick one
     login, or explicitly choose to leave the plan unassigned.
   - Ask the same run-now-vs-file-and-stop question `/sigma-triage`'s own step 5 already uses. The
     issues themselves are already created and real, as of step 6 — this choice only decides what
     happens NEXT: **file-and-stop** (apply the assignment decided above if any, record it, stop —
     the issues stay exactly as step 6 left them, nothing further happens), or
     **start now**. If "start now" and the picked login is the invoking user, that's the
     self-assigned path; any other login is the hand-off path.
   - `python3 "${CLAUDE_SKILL_DIR}/scripts/assign.py" execute .sdlc --plan
     .sdlc/plans/scope/<slug>.plan.json --report .sdlc/plans/scope/<slug>.report.json --area
     "<area>" [--assignee <login>] --path file-and-stop|start-now-handoff|start-now-self` — report
     back what it did: `anchor_issue`, `assigned`, `promoted`, the computed `workflow` (waves),
     whether a hand-off comment was posted, the self-assigned `plan_file` if one was written, and
     any `warnings`.

## Clarifying-questions judgment

This is the one place in the skill that is pure judgment, not a checklist — over-asking (stopping
for confirmation on everything) defeats the point as thoroughly as under-asking (silently guessing
on a real ambiguity) does. Split every candidate question into exactly one of two buckets, and use
one operational test to decide whether it's worth asking at all.

- **Technical/scope doubt** — the skill genuinely doesn't know something a correct plan depends on:
  which existing module this should extend vs. where it needs something new, whether a behavior
  should be opt-in (config-gated) or always-on, what "acceptable" means in a way that changes scope
  (a UX nicety vs. a hard constraint). These are answerable by asking, not by reading more of the
  repo — step 2 already exhausted what the codebase itself can tell you.
- **Direction unclear** — the provided description genuinely admits two or more mutually exclusive
  readings: which of several plausible scopes was meant (MVP vs. the fuller idea), whether a dedup
  hit means "extend that" or "this is separate," whether an ambiguous priority signal ("soon") maps
  to P1 or P2. Don't plan around an assumed direction and mention the assumption in passing — ask.

**The test: would the answer change the plan's shape** — which issues get filed, how many, their
priority, their dependency edges, or which area they target? If yes, it's a real question. If the
answer would only change wording, or if steps 2–3 already answer it unambiguously (an established
repo convention, a priority scheme already in `config.json`, whether the work structurally needs an
epic), decide it silently and let the drafted plan itself carry the decision for the user to see and
correct — don't interrupt for something the repo already told you. Likewise, don't ask about
implementation detail the plan doesn't need settled now (exact function signatures, line-level
changes) — that's the eventual goal's own Research/Plan phase, not scoping's.

Every dedup **duplicate**-strength hit (score ≥ 0.45) is always a question, phrased per-issue, never
folded into a single blanket mention. **related**-strength hits are mentioned too — individually
when there are few, summarized as a group with an offer to look closer when there are many — but
never silently dropped, per step 3.

## Why `scope.py`

`compile_plan.py`'s own CLI printed human text only when `scope.py` shipped — `assign.py` didn't
exist yet, so its CLI was never given a reason to emit a chainable report. (#1919 has since added a
`--json` arm to it for `sigma-goal-review`, so the two now overlap on the report itself; `--json`
prints where this one writes.) `assign.py execute` needs that report as a real JSON file
(`--report <path>`), and a genuine human-input pause (steps 4–7 of this very skill) sits between
compiling the plan and deciding assignment, so the report has to be persisted to disk in between —
a file is structurally required here, not a convenience. `scope.py`
closes exactly that gap: it calls the real, unmodified `compile_plan.compile_plan()` once and prints
its report as the same single JSON line `brainstorm.py`/`dedup.py`/`assign.py resolve` already use,
optionally also writing it to `--report <path>`. Neither `compile_plan.py` nor `assign.py` is
touched — this is a new, thin, independently-tested bridge, not a rewrite of either.

## Caveats

**Proposed, not actionable, until step 7 says so:** every issue `scope.py`/`compile_plan.py` creates
is armed at its plan priority, or parked when the deny-list matches (triage off: filed with the
legacy `sdlc:needs-confirmation`, never `sdlc:goal`). Only step 7's `assign.py execute` with a `start-now-*` path promotes it to
`sdlc:goal`. `file-and-stop` leaves it exactly as created, on purpose — nothing else happens until a
later `/sigma-triage` or manual promotion picks it up.

**Multi-blocker markers are never comma-joined:** `compile_plan.py` already guards the same
`backlog_check._BLOCK_RE` single-capture trap `/sigma-triage`'s own caveats document — one
`**Blocked by:** #N` line per blocker on a plan with several dependencies. Nothing extra to do here,
but don't hand-edit a created issue's blocker markers afterward without keeping that shape.

**Nothing rolls back on a partial failure:** if `scope.py`/`compile_plan.py` fails partway through a
multi-issue plan, whatever independent issues succeeded stay created; anything (transitively)
blocked on a failed or skipped issue is skipped, never created half-wired. Report `failed`/`skipped`
back to the user rather than silently treating the run as clean.

## Internal flow

```bash
# 1. Resolve the target
python3 "${CLAUDE_SKILL_DIR}/scripts/brainstorm.py" .sdlc "<raw invocation text>"

# 2. (No script — Read/Grep the repo areas the target touches)

# 3. Dedup against the board
python3 "${CLAUDE_SKILL_DIR}/scripts/dedup.py" .sdlc "<resolved text>"

# 4-5. Confirm the target, ask the clarifying-questions round (this skill's own reasoning; no script)

# 6. Compile the decided plan into real issues (armed or parked by triage)
python3 "${CLAUDE_SKILL_DIR}/scripts/scope.py" .sdlc \
  --plan .sdlc/plans/scope/<slug>.plan.json --report .sdlc/plans/scope/<slug>.report.json

# 7. Resolve assignment, then enact the chosen path
python3 "${CLAUDE_SKILL_DIR}/scripts/assign.py" resolve .sdlc "<area>"
python3 "${CLAUDE_SKILL_DIR}/scripts/assign.py" execute .sdlc \
  --plan .sdlc/plans/scope/<slug>.plan.json --report .sdlc/plans/scope/<slug>.report.json \
  --area "<area>" [--assignee <login>] --path file-and-stop|start-now-handoff|start-now-self
```

The plan artifacts (`.sdlc/plans/scope/<slug>.plan.json` + `.report.json`) are durable and
self-describing — they stay on disk after the run, so a later session can see exactly what was
decided and why, the same purpose `/sigma-triage`'s own plan files serve, though in a simpler
single-JSON shape (no date prefix, no `.md` twin, no `active.json` pointer).
