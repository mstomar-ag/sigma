# Planning the backlog

Turning ideas into issues (`/sigma-scope`) and sequencing a backlog (`/sigma-triage`). Back to the [README](../README.md).

## Idea intake: turn a rough idea into a planned backlog

`/sigma-triage` sequences work that's already filed and understood. **`/sigma-scope`** solves the
earlier problem: turning a rough, not-yet-scoped idea into a fully-planned, ready-to-execute body of
work — real issues, a real epic when one is warranted, real priorities, a real assignment decision,
and (if you want) an immediate, sequenced start.

```bash
/sigma-scope add a UI for editing config.json instead of hand-editing raw JSON
```

**Four invocation forms**, auto-detected from what you type — no flag to remember:

| Form | Example | What happens |
|---|---|---|
| Inline free text | `/sigma-scope add a UI for editing config.json` | Scored against the live backlog; a clean idea with no real overlap resolves straight to a new plan. |
| A local markdown file | `/sigma-scope check the notes/ui-ideas.md file and help me plan it` | The invocation has to *be* the reference: the path alone, or the path in phrasing like this example's. A filename mentioned inside a longer idea stays free text. The file is located (a literal relative path is tried first, then a bare-filename search of the repo, skipping vendored/generated trees) and its content becomes the target. |
| A fuzzy reference to an existing issue | `/sigma-scope check the story issue where we talked about a config.json editor` | Scored against the backlog like free text, but a strong, singular match resolves the invocation to *that issue* instead of treating it as new. |
| A direct issue number | `/sigma-scope #901` | Fetched straight from the local backlog mirror, or a live `gh issue view` when it isn't mirrored yet. |

Free text and a fuzzy issue reference are two readings of the same kind of input, so a genuinely
in-between case — real overlap with something already filed, but not enough to commit to either
reading — resolves as **ambiguous** rather than guessing: you're asked directly whether it's new or
the same thing as the matched issue.

**Seven steps**, all before anything is created:

1. **Resolve the target** — one of the four forms above.
2. **Load the repo as real context** — the relevant module/skill, its README section, its own
   conventions — so the plan is grounded in what's actually there, not a generic response.
3. **Dedup against the backlog** — does anything already cover this, even partially?
4. **Confirm the target** — a short "scoping *this* — right?", or (for an ambiguous result) which
   reading is correct.
5. **Ask clarifying questions** — see below for exactly what earns a question.
6. **Plan** — draft each issue (title, a body grounded in step 2's real file/module names, a
   `P0`–`P4` priority, `blocked_by` edges between siblings), decide whether an epic wrapper is
   warranted, show you the draft, and — once you confirm — compile it into real issues (or real
   local goal files in local-goals mode). Triage then decides each issue at this step: it is
   armed (`sdlc:goal` at its plan priority) or parked with a declared question kind (a deny-list match, a cap, a question title such as "Question: ...", or `track --needs-person <kind>`). With
   `ai_filed.triage.enabled` false the legacy `sdlc:needs-confirmation` label is written instead.
7. **Resolve assignment and the execution path** — who owns this, and does it start now or wait?

**Dedup, and what counts as a genuine question.** Every backlog hit is banded by score: below the
surfacing floor, it's not mentioned at all (unrelated); at or above it but under the duplicate line,
it's **related** — surfaced, never silently dropped, individually when there are few and summarized
as a group when there are many; at or above the duplicate line, it's **duplicate** — always asked
about individually, per issue, phrased so you can answer at a glance: *"this looks related to #N:
'\<real title\>' — same thing, related-but-distinct, or unrelated?"* When the resolved target IS an
existing issue, that issue's own ref legitimately tops its own dedup pass — it's discarded before
the rest of the list is read, not treated as a signal.

Beyond dedup, a candidate question is asked only when **the answer would change the plan's shape** —
which issues get filed, how many, their priority, their dependency edges, or which area they target.
Two real categories clear that bar: a **technical/scope doubt** (which existing module this should
extend vs. where it needs something new, whether a behavior should be opt-in) and **direction left
genuinely unclear** (which of several plausible scopes was meant, whether an ambiguous priority
signal maps to P1 or P2). Anything the repo's own conventions already answer, or that would only
change wording, is decided silently and left for you to see (and correct, if wrong) in the drafted
plan itself — never asked about.

**Assignment: three options, presented, never auto-picked.** `assign.py resolve` offers up to three
choices — **self** (you, the one running this, always offered), a **CODEOWNERS**-resolved owner for
the plan's primary area (reused live from your existing `CODEOWNERS` file), or up to 3 currently-
**active** repo members (ranked by recent issue-assignment volume, one bounded `gh issue list` call
— offered only when CODEOWNERS doesn't resolve). You pick exactly one login, or explicitly leave the
plan unassigned.

**Then one of three paths** — the same run-now-vs-file-and-stop choice `/sigma-triage`'s own "Start
now" step already uses:

- **File-and-stop** — apply the assignment decision (if any), record it, stop. The issues stay
  exactly as step 6 left them (armed or parked; the legacy `sdlc:needs-confirmation` only when triage is off) — nothing else happens.
- **Start now, handed off** — promotes every issue to `sdlc:goal`, computes the dependency-ordered
  wave schedule, and posts a comment on the plan's anchor issue (the epic, or the first issue in
  dependency order) marking the hand-off starting point.
- **Start now, self-assigned** — the same promotion + wave schedule, persisted as a
  `.sdlc/plans/<issue>-<slug>.md` file, and the drain actually starts (`loop.py next` picks the
  first ready issue) — for when the invoking user is also the assignee.

The plan itself is written to `.sdlc/plans/scope/<slug>.plan.json` before anything is created, and
its compile report to `.sdlc/plans/scope/<slug>.report.json` — durable, inspectable artifacts, the
same general idea as `/sigma-triage`'s own plan files, so a later session can see exactly what this
run decided and why.

**Config.** Board dedup (step 3) has its own tunables, calibrated differently from
`backlog_check`'s cross-check (a short, unstructured brainstorm sentence scores measurably lower
against a full issue body than two full issues compared to each other, even for a genuine
duplicate): `dedup.threshold` (default `0.18`, the surfacing floor), `dedup.duplicate_threshold`
(default `0.45`, "related" vs. "duplicate"), `dedup.top_k` (default `8`). Target resolution's own
fuzzy-match step (step 1's free-text-vs-existing-issue judgment) instead reuses `backlog_check`'s
existing `dup_threshold`/`top_k` — one shared calibration for "is this basically the same issue,"
not a second, independent one.

## Campaign planner

Pick what to work next, detect blockers, and compile a dependency-sequenced drain plan — then run
it immediately or save the plan to schedule later.

```bash
/sigma-triage
```

Walks through a five-step flow:
1. **Survey** the backlog, inbox, parked, and in-flight goals (grouped by triage bucket).
2. **Question round:** pick/defer/drop per bucket, refine per-issue, confirm dependencies and wave
   capacity.
3. **Compile** the plan: topo sort over detected + user edges, sized into waves.
4. **Dry-run** the enactment (assign + label + mark blockers), confirm.
5. **Start now** (flow into `/sigma-loop`) or **Save for later** (plan waits; a later loop start
   picks wave-1's first goal).

The drain plan is idempotent: enacting the same plan twice produces zero actions on the second run.
Chain runs use plain `next`, not `next-batch`, to drain waves in order (blocked goals alone would
claim and park, disrupting the wave sequence).

**Known caveat:** `enact` treats **any** `_BLOCK_RE` match naming that blocker as an already-present
marker — including incidental prose like "after #12", "needs #12", or "requires #12" — and
**skips writing the real `Blocked by:` marker**, potentially causing the goal to run out of order.
Verify edges in the question round. See the SKILL documentation for full details on marker
suppression.
