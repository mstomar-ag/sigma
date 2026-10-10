# Sigma's Dossier pipeline — the Business → Product → Tech front end

**Audience:** anyone adopting Sigma who wants a place to put an idea that is not yet a goal. No
internals knowledge assumed.
**Status:** shipped. Every behaviour below was read out of the code that implements it, not
out of the proposal it came from — where the two disagreed, the code won, and §12 says where.

**This is a contract, not a tour.** What each stage takes in, what it writes, which combinations are
legal, and the exact gestures. `docs/label-model.md` is its sibling for the `sdlc:*` lifecycle
labels and `docs/branching-model.md` for units of work; this document changes neither. It adds one
overlay (`sdlc:designed`, §10) and one tier label (`story`, §10), and hands the tech loop the same
kind of goal it has always executed.

**Section numbers.** Every `§N` below is a section of **this** document. A section of a different
document is cited with that document named beside it (`docs/label-model.md` §2). The design
proposal this model came from was **deleted** when the work shipped, deliberately: it predicted five
open decisions that have all since been made, and two documents that can drift is the failure this
one exists to prevent. §12 records what it got wrong, so the reasoning is not lost with it.

> **Looking for the route rather than the rules?**
> [`how-the-dossier-pipeline-works.md`](how-the-dossier-pipeline-works.md) walks one real idea from
> the sentence somebody typed to the four tech goals it became, naming what happens at each step and
> linking back here for every decision. It is a guided path through this document and carries
> nothing of its own — where the two differ, this one is right.

---

## 1. The one-paragraph version

A goal used to start at whatever level of thought produced the issue: a two-line typo fix and an
undefined epic entered the same door. This pipeline adds two ticket tiers **above** today's tech
goal. A **Dossier** (a `story`-labelled ticket) captures pure business intent through a fixed
eight-question interview with no code exploration at all. A **design pass** maps that intent onto
the real codebase — blast radius, components, doubts, blockers, a slice count — and writes it to
`.sdlc/design/<n>.md`. An **independent confirmation gate** verifies that write-up, and only then
produces the **Spec/Epic** ticket, its children, and the `sdlc:designed` overlay. The existing
7-phase loop is completely unchanged: it just starts one or two tiers lower for anything that comes
through this front door, and gains an **opt-in retrofit gate** so anything that does *not* come
through it can be made to stop and get mapped first.

---

## 2. The gap this closes

`goal_decompose`'s classifier (`skills/sigma-loop/scripts/goal_size.py`) measures **depth** — word
count, section count, checkbox count, `Phase N` markers. It has no concept of **blast radius**. A
goal that reads as "update these two lines, P3" sails through decompose-check with `PROCEED` even if
those two lines fan out across the whole codebase.

The design pass (§5) is where that missing dimension lives, and the retrofit gate (§6) is the
coverage mechanism: **nothing skips the codebase-mapping step just by being born outside the Dossier
front door** — once the gate is on. It is off by default (§6a), and that is a real limitation, not a
detail (§12).

The second thing this closes is an entry-point ambiguity: there was no answer to *"I have a very
high-level idea, where do I even start."* Now there is one gesture, `/sigma-dossier`, and it produces
a ticket rather than a conversation.

---

## 3. The three tiers

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  DOSSIER / STORY   — one ticket. High-level intent. No code exploration.      │
│    Produced by:  sigma-dossier            (engine: sigma-dossier/scripts/       │
│                                           dossier.py)                        │
│    Label:        story                    (minted at attach, not bootstrapped)│
│    Assignee:     @me, always, no prompt                                       │
│    NEVER sdlc:goal — the loop must never try to execute a Dossier, for the    │
│    identical structural reason an `epic`-labelled ticket never is.            │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                         .sdlc/design/<n>.md   ← the design pass (§5).
                                    │            An ARTIFACT, not a ticket.
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  SPEC / EPIC   — one ticket. The classical spec. IS the technical epic —      │
│    not two tickets. Produced by goal-review once it CONFIRMS the design.      │
│    Label:        epic                     (compile_plan.py's EPIC_LABEL)      │
│    Overlay:      sdlc:designed            (written only on confirmation)      │
│    Body marker:  Originates from Story #<n>.                                  │
│    NEVER sdlc:goal — the same structural exemption epics already have.        │
│    MAY NOT EXIST AT ALL: a design concluding one coherent unit produces no    │
│    Epic (§7d-ii); the overlay alone is the whole output.                      │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  SLICES / TECH GOALS   — the real, workable children. Carry sdlc:goal,        │
│    priority:P1 (§7f), sdlc:designed, `Part of epic #N.` and `Story #<n>.`     │
│    Flow into sigma-goal / sigma-loop completely unchanged.                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

**The Spec is not a separate ticket from the Epic.** Stage 2 produces **one** ticket, and it is
simultaneously the executable spec and the parent epic its children hang off. There is no fourth
tier, and there is no path that creates a Spec without an Epic label or an Epic without the spec
content.

---

## 4. Stage 0 — the Dossier (`sigma-dossier`)

**In:** a very high-level idea, nothing more. **Out:** one `story`-labelled ticket.
**Engine:** `skills/sigma-dossier/scripts/dossier.py`. **Skill:** `skills/sigma-dossier/SKILL.md`.

**Hard constraint: no code exploration at this stage.** This is intent-gathering, not analysis. The
skill's `allowed-tools` omits `Read`/`Grep`/`Glob` against the repository — with one exception,
`Bash(ls skills/)`, which answers a question about Sigma's own layout and never about the idea's
codebase (§4e) — and that omission **declares intent and spends prompts; it is not a sandbox**
(#1957). `Bash(python3 *)` is in the same grant and is unrestricted code execution, so a source file
is one `python3 -c` away on Claude Code exactly as it is on Cursor: **the rule is prose on every
host**, and the SKILL.md paragraph is what carries it. That is a limitation, not an oversight
(§12).

### 4a. The bank — eight questions, fixed and ordered

`dossier.py bank` returns them, and they are **engine-owned data, not agent-improvised prose**:

| # | id | What it asks for |
|---|---|---|
| 1 | `title` | a few words to call it |
| 2 | `problem` | the problem or opportunity, in one or two sentences |
| 3 | `why_now` | why it matters now rather than later or never |
| 4 | `who` | which users, team or system feels it |
| 5 | `outcome` | what success looks like if it ships |
| 6 | `constraints` | a deadline, a budget, a system it must fit — `"none"` is a complete answer |
| 7 | `non_goals` | what is explicitly out of scope — `"none"` is a complete answer |
| 8 | `next_step` | **always last.** `"file and stop"` \| `"continue to Product"` |

The bank is **flat, not keyed.** `sigma-unpark`'s bank is keyed by park `reason_class` because
different parks need different questions; a Dossier has no equivalent axis — Stage 0 asks the
identical set of every idea, which is what "strict predefined structure" means here. `QUESTIONS` is
an ordered list with no per-slot `when` condition, and that absence is deliberate rather than a
missing feature.

Ask them one at a time, in order, never batched, `AskUserQuestion` where the host has it. Only
`next_step` carries `options`; the rest are open business content.

### 4b. The bounded tail — `followup_` and `open_`

A fixed bank cannot reach the semantics of one domain, and pretending otherwise is how a Dossier
comes out sufficient for intent and insufficient to design from. The answer is **not** a bigger
bank — every extra slot is a question asked of every idea forever — but a short tail on the
**record**, asked after the seven and **before** the terminal question.

Three triggers warrant one, and `dossier.py followups` returns them as engine-owned data for the
same reason the bank is:

1. **an unstated anchor** — the answer names behaviour whose reference point is never given (what a
   repeat counts from, what a share is a share of, when a window starts).
2. **no stated end** — something starts, recurs or is granted, and nothing says how it stops, is
   cancelled, or is undone.
3. **two answers that disagree** — an example under one slot contradicts a limit under another. This
   one is not a missing question at all: both answers are already on the page, and only reading them
   as a set finds it.

Each becomes **one** targeted question, recorded under one of two prefixes:

| Prefix | Meaning | Cap | On `file()` |
|---|---|---|---|
| `followup_<slug>` | asked and **answered** — ordinary intake content, arrived at late | **3** | refused above the cap |
| `open_<slug>` | Stage 0 **could not settle** it; the ANSWER is what is unknown and what would settle it | none | never refused; surfaced in `detail` |

**The cap applies to the answered half only, and that asymmetry is load-bearing.** Bounding both
halves with one number would make refusal the consequence of recording a doubt: an agent standing at
the cap deletes an `open_` entry to get the record filed, which is exactly the silent loss the
mechanism exists to stop. The interview is what must stay short. An idea needing a fourth answered
follow-up is more than one Dossier.

**A follow-up stays on the business side of the hard constraint.** It asks what the person
*intends* ("does the next one start from when the last was finished, or from the calendar?"); it
never asks where something lives in the code. Anything unphraseable as intent is an `open_` entry,
not a follow-up. An unattended run with nobody to ask records every gap as `open_` rather than
guessing.

### 4c. The record, and where it is written

The skill writes a `{answers, questions}` JSON file — **the same shape `sigma-unpark` already uses**,
so the two tools' answer files are interchangeable for a reader — and hands it to `file`:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/dossier.py" file .sdlc \
    --answers /tmp/dossier-answers.json --decision stop|continue [--dry-run]
```

`render_block` emits the persisted record into **both the issue body and a follow-up comment**,
identical bytes, so the two can never disagree. Its shape:

```
<!-- sigma:dossier-qa:start -->
## Dossier

_<the question, verbatim>_
- **<id>** — <the answer>
…                                  ← bank order, never dict-insertion order

### Follow-ups                     ← only if there are any
### Open questions                 ← only if there are any

<!-- sigma:dossier-qa:end -->
```

The tail is grouped **by kind**, not in the order it was asked, and that grouping is what makes the
second group usable downstream: §5 lifts it into Doubts, and a heading is how a human finds it.

**The fence is dossier-scoped on purpose.** `DOSSIER_QA_START`/`END` are deliberately NOT added to
`blocker_scan.py`'s shared unpark-QA registry: a Dossier is never `sdlc:goal`-labelled, so
`backlog_check`'s blocker scan never walks its body in the first place. The fence still avoids every
`_BLOCK_RE` trigger word in its own headings as hygiene, in case a Spec ever quotes the block
verbatim into a body that *is* scanned.

The ticket is created through `create_dependency(title, block, "@me", labels=["story"],
goal_label=False)` — `story`-labelled, **never** `sdlc:goal`, self-assigned with no prompt. The
comment is best-effort: a comment failure never erases a Dossier that was already created, because
the body already carries the identical record.

**Both discovery modes work.** `_resolve_source` reproduces `sources.get_source`'s own precedence
and threads a `run` for test injection; in local-goals mode the Dossier becomes a goal file with
`status: proposed` — the local counterpart of "queued, never auto-picked" — and the comment step
no-ops, because `LocalSource` has neither `_run` nor `_repo_args`.

### 4d. What `file()` refuses — before any write

Every one of these returns `{"outcome": "failed", "detail": …}` and reaches neither `gh` nor a local
write. Report the `detail` verbatim, fix the answers file, retry.

| Refusal | Why it is a refusal and not a warning |
|---|---|
| `--decision` is not `stop` or `continue` | the canonical token is the only thing the code branches on |
| any of the eight required ids is missing or blank | an incomplete record is not a Dossier |
| the recorded `next_step` answer disagrees with `--decision` | the body would say one thing while the code did another, with nothing to catch it |
| an extra id matching **neither** prefix | an unclassified extra would dodge the cap, so it becomes a fourth uncounted kind |
| a blank follow-up answer | the same rule the bank gets |
| a follow-up with **no matching entry in `questions`** | an answer whose question nobody wrote down is not a record; the bank's wording is recoverable, an improvised one is not |
| more than **3** `followup_*` answers | §4b |

`render_block` is deliberately more forgiving than `file()`: it renders an unrecognized id
unheaded after both groups rather than dropping it. **The renderer never silently loses something
somebody answered; the filer is the strict gate.**

### 4e. The terminal decision, and the handoff probe

`next_step` is **always asked, never inferred**, and its free-text answer is recorded verbatim; the
skill maps it to the canonical token before calling `file`:

| `next_step` answer | `--decision` |
|---|---|
| `file and stop` | `stop` |
| `continue to Product` | `continue` |

On `filed`, report the issue number, the title, **and every `open_` id the `detail` names** — saying
"filed" without them hands the next stage a record that looks complete. Then:

- **`stop`** — stop. Nothing else to do.
- **`continue`** — **probe whether this install actually has a Product-stage skill before claiming
  any handoff happened**: `ls skills/`, and look for a `goal-design`-shaped entry yourself. Found →
  hand off against the new issue number, **naming the `open_` entries in the handoff**, because
  §5 carries them into its own Doubts and that is the whole reason an unsettled question is written
  down rather than dropped. Not found → **say so plainly and stop.** The Dossier is filed,
  self-assigned and `story`-labelled, and sits ready. Never describe a handoff as happening when it
  did not.

The probe is dynamic, not a version check: nothing about the skill changes when goal-design appears.

**Stage 0 never edits an existing issue.** A second Dossier for a related idea is a second run of
this same flow producing a second ticket. There is also **no resume**: the issue does not exist
until `file()` succeeds, so an interrupted interview starts over rather than resuming from a body
the way `unpark.py` does.

---

### 4f. Stage 0: the PRD door (`sigma-prd-intake`)

A PRD file of any shape (no headings, no EARS, no ids) enters here instead of the interview. The
scope-to-goals compile is not the route for a PRD: it produces slices that skip the Business tier,
so a PRD takes this door and arrives as Dossiers. The model drafts, per business outcome, answers
over the same fixed bank, each with a verbatim PRD quote; `skills/sigma-prd-intake/scripts/intake.py`
is the deterministic half:

- **Cite or refuse.** Every non-empty answer needs a quote of at least 20 characters that is a
  substring of the PRD (whitespace-normalised). The check proves the quote EXISTS, not that it
  SUPPORTS the answer; a human reads the printed `PRD section -> Dossier title -> answer ids` table.
- **Gaps become `open_`.** A silent, vague or contradicted answer is not written as a bank answer:
  the slot holds a visible placeholder and a paired `open_<id>` entry (with its question and the
  quote) is recorded, which Stage 1 carries into Doubts. That carry-over is prose, not code (D-5).
- **No code reading.** A repository source file is refused as the PRD (`REFUSED [repo-source]`);
  this constrains the adapter, not the model. The guard roots at the repo of the cwd, of
  the state-directory flag, of the PRD path and of the plugin itself, and any `--prd` must be a markdown or
  plain-text file (`prd-not-text-doc`). Only bank ids and `followup_*` count as cited answers.
- **Known limit: duplicate on a crash.** `file` creates each ticket before `.filed.json` records it
  (the umbrella too); a crash in that gap refiles a duplicate on resume. The lever is
  `intake.py file --plan P --status` (lists recorded numbers); a human closes the duplicate.
- **Provenance.** Each Dossier carries a `### Source` block (path, sha256, section, per-answer
  cites) after the `dossier-qa` fence, then one `epic`-labelled umbrella (never `sdlc:goal`) lists
  every Dossier. Design runs on the output unchanged; `sdlc:designed` stays goal-review's alone.

Deferred, stated: stdin and issue-number PRDs, and the fast lane for clean PRDs.

## 5. Stage 1 — the design pass (`sigma-goal-design`)

**In:** an approved Dossier, or a goal the retrofit gate flagged. **Out:** `.sdlc/design/<n>.md`,
and a comment on the source issue. **Skill:** `skills/sigma-goal-design/SKILL.md`.

This stage produces a **design artifact only**. It never implements the target's own work, never
creates the Epic or its children, and **never writes `sdlc:designed`**.

### 5a. The two paths

1. **Product path** — a `story`-labelled Dossier, run directly (`/sigma-goal-design`). Its Q&A block's
   **Open questions** tail is inherited verbatim as Doubts (§5d): resolve each against the real code
   and say how, or hand it on still open. **Never let one disappear by being quietly answered in
   passing.**
2. **Tech-side retrofit path** — a goal `loop.py design-check` flagged (§6), reached either through
   the filed "Design #N" meta-issue or by running against the goal number directly. The meta-issue's
   own body names the real target in its `sigma:design-of=#{goal}` marker and the depth in its
   instructions — **design the TARGET, not the meta-issue.**

Both converge on the identical work. Either way, every claim in the source is a **hypothesis to
verify against the real repo**, not a fact to copy — **and the verdict on each material one is
recorded**, under `## Premise check` (§5d), as `verified` / `falsified` / `unverifiable` with its
evidence (#1976). A material claim is one the design would be built differently if it were false:
how the code behaves, where a boundary sits, what a component does. A **falsified** premise is
neither a Doubt (open) nor a Blocker (unresolved) — it is **settled and different**, which is why it
needs a heading of its own: `## Intent` is then written against the corrected premise, and every
slice below is derived from that rather than from the source's. The measured case: a Dossier claimed
`/sigma-doctor` stops at the repo boundary, the sweep found six places where it crosses, and with no
slot for the correction it went into `Intent` as prose — invisible to §7b, which adjudicates by id.

### 5b. Depth — `full` and `lane`

**The target is sized on both modes, and `mode` is a CEILING on the pass rather than a floor under
it** (#1954). Stage 1 classifies its target the way `discovery.py`'s own `LANES`
(`small`/`medium`/`large`, `DEFAULT_LANE = "medium"` on anything unsized) already sizes an ordinary
goal, records the answer in the artifact's `Lane` field (§5d), and runs the pass that size earns.
`discovery.py lane` does not resolve this — it reads a goal FILE's frontmatter and returns
`DEFAULT_LANE` for an issue number — so it is a judgement in that vocabulary, not a lookup.

- **`full`** (the default once the gate is on) — the ceiling is the comprehensive pass: sweep every
  file and component the intent plausibly touches, trace callers, do not stop at the first plausible
  answer. A target measuring `small` gets that lane's pass with the round budget held in reserve.
- **`lane`** — the same sizing with the ceiling brought down onto it, and the budget scaled with it
  (§5c). That is the only remaining difference between the two: whether an under-sized target is cut
  off and has to report `capped`.

`mode` picks **depth only**. It never decides whether the gate acts (§6a). And it is a bound, not an
instruction to spend: ceremony proportional to the work is `sigma-goal`'s own Lane routing, applied
one stage up, and without it a one-file change on a `full`-configured repo buys the treatment a
six-component change gets.

**Who resolves it differs by path, and only one of the two is resolved by code (#1958).** On the
**retrofit** path `design_check` resolves it and `design_goal.render_meta_body` renders the matching
depth instructions into the "Design #N" meta-issue, so the pass reads it there. On the **Product**
path no code reads `goal_design.mode` at all: `design-check` returns `OFF` before `mode` is reached
whenever `enabled` is not `true`, and even with the gate on it resolves `mode` only once it has
decided to file a retrofit meta-issue — which never happens on this path. So the Product-path design
pass reads the configured value itself (`state.load_config`), with `design_check`'s own precedence:
absent → `full`, unrecognised → `lane`. `enabled` does not gate that read; it gates the retrofit
gate and nothing else.

### 5c. The stopping rule, and the budget

"Every file the intent plausibly touches" is unbounded on any real codebase, so **`full` is
exhaustive over the SEEDS, not over the repo.** Extract seeds from the target's own text — the
modules, functions, fields, endpoints, commands and behaviours it names — sweep each, and add to the
seed set only the new symbols a sweep actually surfaced. Stop when **both** hold:

1. **Seed closure** — every seed swept, and every symbol a sweep surfaced either swept itself or
   written down under Doubts with the reason it was not.
2. **A quiet round** — the most recent round added no new in-scope site. **A site is new when it is
   a file or component not already on the blast-radius list** — one row each — and never merely a
   new grep line inside a file that list already carries. Read the other way round the sweep never
   terminates on a codebase whose files answer to more than one query; read this way it runs over a
   finite set that only grows. One quiet round is the signal; a second confirming round buys nothing.

**A round is quiet over the exclusions AS WRITTEN, never over a scope call held in the author's
head** (#1975). #1952 made a capped sweep loud in four places, and that fixed the reporting without
touching the judgement beneath it: *in-scope* was decided by the author after the fact with nothing
recording the decision and nothing able to check it — the re-run's own pass said it could have kept
round 3 quiet by ruling one file out. So every file a recorded **search** query returns that is not
already a blast-radius row is either added to the blast radius or written under `## Out of scope` as
`X-n`, carrying **the round it was excluded in**, the hit (or a group rule and its count) and the
reason; a round with unaccounted-for hits is not quiet but unrecorded, and the pass may not stop on
it. The test a hit is judged against is stated once: it is IN scope when the change this intent
implies would have to touch it, **or** when it would break if that change shipped as designed. This
makes the call **visible and disputable, not correct** — nothing executes it — and the one check that
does not rest on trusting the pass is re-running a query from `## Queries run` and comparing its file
list against blast radius ∪ `## Out of scope`. §7b adjudicates these the way it adjudicates Doubts.
A second self-assessed confidence number was considered and rejected: it imports the same problem
one level up.

**Seed closure is judged against a written record, never against memory** (#2028). The seed set the
stopping rule is defined over is written into the artifact as `## Seeds` (§5d) — one row per seed,
carrying the round it entered in, the round it was swept in, and `swept` or `unswept`. Before this
the set existed only in the pass's own head, which made `converged` an unfalsifiable claim about
something nobody else could see and left a `capped` pass naming its unswept seeds in prose alone.
The table is written on **every** artifact: on a converged one it is the evidence for the strongest
sentence in the document, and on a capped one it is the continuation list. **The banner's count IS
the table's count** — the `<k>` unswept seeds the banner names is the number of rows marked
`unswept`, one number written twice, and a banner disagreeing with its own table is a schema
violation §7a reports rather than adjudicates. What it does not buy is stated in the same breath:
nothing executes the table, it does not raise the budget, and a seed the pass never held leaves no
row.

**The budget is fetched from `goal_design.py sweep-budget`, never assumed** (#2032) — the identical
`dossier.py followups()` shape applied to a number instead of a policy object, so the ceiling a
pass obeys is READ BACK rather than remembered, and `goal-review` (§7a) re-runs the same verb to
check the artifact's `**Budget**` field (§5d) was not written from memory. Under `mode: full` the
unconfigured default is unchanged at **3**; an optional `goal_design.rounds` (a positive integer)
raises it so an operator can let a pass "scan the whole product" — bounded, never `"unbounded"`,
because each round is real Anthropic spend. Under `mode: lane` the key is inert and the budget
stays fixed per measured lane — one round on `small`, two on `medium`, three on `large` (only
`small` was ever numbered anywhere before this issue). Reaching the cap, raised or not, is a
legitimate outcome, not a failure — but it is a **different** outcome from converging, and the
artifact says which one happened: ending on the cap means **seed closure was not reached** and the
mapping is partial. Stop, and record it in three places rather than one — the `Sweep` header field
(§5d), the banner under it, and a Doubts entry per unswept seed, naming the seeds — then repeat it
as the first line of the publishing comment (§5e). The failure this replaces was measured: a real
pass spent all three rounds — the whole budget of that era, before #2032 — on 33 sites, was still
adding files in round three, and left the whole fact as one bullet at the bottom of a 31KB document
under a slice table that read as finished (#1952). If the FINAL round of a pass's own budget is
still turning up *new components*, that is itself a finding — record it under **Blockers** as "the
intent's radius exceeds one design pass", because a target that cannot be mapped inside the budget
needs splitting before it is designed, not a longer sweep. Either way the **unswept `## Seeds` rows
are the continuation list** a capped pass leaves behind — the thing whoever picks it up works from
instead of re-deriving the seed set from the intent. Nothing re-runs Stage 1 automatically (§7g),
and a second pass on the same `<n>` overwrites the file: the record makes a continuation possible,
it does not perform one.

**Every query actually run is recorded verbatim.** Coverage is guaranteed by re-running the query,
not by trusting that today's list was complete.

### 5d. The artifact — `.sdlc/design/<n>.md`, and its headings are a contract

`<n>` is the Dossier's or the target goal's own issue number. `mkdir -p .sdlc/design` first — the
directory does not exist until the first design pass on a repo. It mirrors `.sdlc/research/` and
`.sdlc/plans/`, and it is deliberately **not** in `setup.RUNTIME_IGNORES`: the write-up is the
durable, reviewable record (§11).

**§7a reads this file section by section, so an artifact that invents its own layout makes its
consumer guess.** Write exactly these headings, in this order, and put nothing the consumer needs
outside them:

```markdown
# Design: <target title>
**Target**: #<n> · **Path**: … · **Mode**: full | lane · **Lane**: small | medium | large · **Budget**: <n> · **Sweep**: converged | capped · **Date**: …

> banner — ONLY when Sweep is `capped`: seed closure not reached, how many seeds unswept, which

**Reading this as a person?** Start with `<n>-in-brief.md` — the same design in one page, plain
language, no citations.

## Intent
## Premise check         (`- **PC-1.** … — **verified** | **falsified** | **unverifiable**: …`)
## Queries run
## Seeds                 (table: ID | Seed | Found in round | Swept in round | Status | Carried as)
## Blast radius          (table: ID | Component | Site | What it is | Why affected)
## Components            (table: Component | What it is | What this intent does to it)
## Out of scope          (`- **X-1.** (round <r>) … — why it is out of scope`)
## Doubts                (`- **D-1.** …`)
## Blockers              (`- **B-1.** …`)
## Detailed design
## Slice-count estimate  (table: Slice | Covers | Depends on)
## Handoff               (must include a `<n>-in-brief.md` pointer — see below)
```

The rules about that skeleton, each of which a real run got wrong before it was pinned:

- **`Doubts` and `Blockers` stay two headings, and "none" is a real answer that must be written.**
  An absent heading and an empty one are not the same claim. `Premise check` and `Out of scope` are
  written on every artifact for the same reason, with the same single `- None.` bullet where there
  is genuinely nothing to say.
- **The `| --- |` separator rows are part of the skeleton, not decoration.** Copy a table without
  one and GitHub renders a run of literal pipes.
- **A `Site` is a `file:line` or a line range** (`models.py:8-13`). Most real ones are ranges.
- **`Handoff` is a heading in the schema precisely so §5f's mandated statement is not a deviation
  from "write exactly these".**
- **`Sweep` is written on every artifact, and `capped` gets the banner** (#1952). `converged` means
  both halves of §5c's stopping rule fired; `capped` means the budget ran out first and the mapping
  is partial. Omitting it is not an option, for the reason §7d's `(0 open questions carried)` count
  is written even at zero: **an absent value is indistinguishable from `converged`**. §7a reads this
  field first, and §7d carries `capped` onto the Epic body, because the tickets reach everyone with
  board access while the artifact reaches only whoever has the repo checked out — and not even them
  where a repo set a blanket `.sdlc/` ignore of its own (§5e).
- **`Lane` is written on every artifact too, and for the same reason** (#1954). It is the size the
  pass MEASURED (§5b), and it is what distinguishes a proportionate artifact from a lazy one: a
  three-page design over a one-file change and a one-page design over a six-component change are
  both failures, and nothing else in the document tells them apart. Write the lane the pass ran at,
  never the one the target's text claimed — the rule `Mode` already carries. `medium` is what an
  unsized target resolves to, not a way of declining to size one.
- **`Budget` is the FETCHED number, never a hand-typed one** (#2032). §5c requires calling
  `goal_design.py sweep-budget` before sweeping at all; this field is where that call's own answer
  is recorded — `3` on an unconfigured `full`-mode install, whatever `goal_design.rounds` resolved
  to where an operator raised it, or the fixed lane figure under `mode: lane`. §7a re-runs the same
  verb and compares it against this field, reporting a disagreement as a schema violation rather
  than adjudicating it — the same shape as the `Seeds` count check, applied to a single number
  instead of a table, and the fix for the hole the #2032 adversarial review found: a pass that
  skips the call and writes `3` from memory produces an artifact indistinguishable from a correct
  one.
- **`Premise check` is the slot for a source claim that did not survive contact with the code**
  (#1976). Each material claim as `PC-n`, one of `verified` / `falsified` / `unverifiable`, and its
  evidence. A `falsified` row obliges a rewrite rather than a note: §7a checks that `Intent` states
  the corrected premise and that the SLICES were rebuilt on it rather than annotated with it,
  because if the problem statement is wrong every slice derived from it inherits the error.
- **`Out of scope` is what makes the `converged` verdict auditable** (#1975). Every excluded hit as
  `X-n` with its round and its reason, per §5c; one entry may cover a group under a stated rule with
  a count, since a broad query otherwise makes the section unwritable. §7b reads the exclusions of
  the round the sweep ended on first — that is where the pressure to under-scope sits — and **an
  exclusion the reviewer disagrees with is a blast-radius row that is missing**, which is §7c's
  REJECT ground rather than a note to carry.
- **Doubts and Blockers carry ids — `D-1`, `B-1`, upward in document order, never renumbered**
  (#1955). §7b has to bucket every one of them in writing and needs a name for each; a `Blockers`
  entry names the slice it gates; and `compile_plan._DESIGN_ARTIFACT_ID_RE` already recognises the
  shape to refuse a `blocked_by` key that is really one of these. "none" is a single `- None.`
  bullet, with no id. The live run had to mint `D-1`…`D-8` for itself.
- **Blast-radius rows name their component, and past 20 rows the table may be split** under
  `### <Component>` sub-headings, each repeating the header and separator rows — the one exception
  to "write exactly these", and the only grouping affordance a 33-row table has (#1955).

**`.sdlc/design/<n>-in-brief.md` is a required sibling, same commit, same directory** (#2064). The
main artifact is written for §7a and `compile_plan.py`; the brief is written for the person deciding
whether to confirm it, and the two audiences take different documents. Rules the main artifact is
deliberately exempt from: every codename glossed in plain English on first use and used that way
thereafter; no `BR-n`/`D-n`/`B-n` ids, no `file:line`, no query text; one page; a fixed shape (what
was checked, what it found split into needs-no-ruling/needs-a-ruling, what a person must decide as
plain questions, the plan as a dependency table, any correction to the source stated once); a
`capped` sweep stated in its own first line, for the identical reason §5c gives the main comment.
Never read by any script — `compile_plan.py` and §7a look only at the main artifact.

`Slice-count estimate` is the row set §7d turns into `compile_plan.py` `issues[]` entries: **`Covers`
becomes the child body and `Depends on` becomes the `blocked_by` edges.** Whether those rows should
also carry a priority is open and deliberately undecided (§12).

**`Depends on` carries slice ids from this same table, and nothing else (#1956).** Comma-separated
`Slice` values — the cell is read by splitting on commas and stripping — or `—` when there are none.
Never a `Blockers` id (`B-1`), a `Doubts` id (`D-3`), a `Blast radius` id (`BR-7`), a `Premise
check` id (`PC-2`), an `Out of scope` id (`X-5`), an issue number, or prose. A `blocked_by` key may
only name another issue in the SAME plan, and a Blocker is a paragraph in this document rather than
a ticket, so there is nothing for such a key to resolve to.
The first real-codebase run wrote `3, and B-1` in one of these cells and the `B-1` was patched away
by hand between the two stages, silently. **A slice that also waits on a Blocker records that in the
Blockers entry instead** — the edge written the other way round, the blocker naming the slice it
gates and what that slice is waiting for. That is §7b's own operational test for its blocking bucket
(*name the slice that cannot start, and say what it is waiting for*), so a dependency written that
way is adjudicated at Stage 2 rather than dropped.

### 5e. Publishing it — the comment, and the three branchless facts

Link the artifact from the source issue's comments — `loop.py note` on the retrofit path, a plain
comment on the Dossier path. Three rules, in order:

- **The comment stands on its own; never post a bare path — and "stands on its own" means the
  in-brief content, never a compressed dump of the main artifact** (#2064). The comment IS (or
  opens with) `<n>-in-brief.md`'s own content — what was checked, what it found, what needs a
  ruling, the plan. Not "every doubt and blocker in full": Doubts and Blockers are written in the
  main artifact's own citation register, and reproducing that register in the one place most readers
  will ever look relocates the unreadable version rather than fixing it. On a shared backlog the
  comment may be the only half anyone reads before confirming. Both files are markdown links to
  their GitHub blob, never a bare filename in a code span, once the branch/local-only decision
  below is made.
- **Measure whether the file can reach the remote at all — do not assume it.** ("The file" means
  both siblings — `<n>.md` and `<n>-in-brief.md`, one directory, one commit.) `.sdlc/design/` is
  trackable by Sigma's defaults — `setup.RUNTIME_IGNORES` omits it deliberately — so every repo
  `/sigma-setup` touched tracks it, and **Sigma's own now does too** (#1953: the blanket `.sdlc/`
  rule became `.sdlc/*` plus an explicit `!.sdlc/design/`, since a negation under an excluded
  directory is inert). The case this measurement exists for is a repo that set a blanket rule of its
  own, and it is the one place an adopter's experience can differ from what these documents
  demonstrate. Run `git check-ignore -v .sdlc/design/<n>.md` and **read the exit code the right
  way round: 0 means the path IS ignored** and prints the matching rule; **1 means it is NOT
  ignored** and prints nothing at all. Silence plus non-zero is the good case; only 128 is a real
  failure. Anything reading non-zero as "the command failed" concludes the exact opposite of the
  truth. If it is ignored: say `artifact is local-only in this repo` in the comment and stop.
  **Never `git add -f` past a repo's own ignore rule.**
- **If it is tracked, it lands the way everything else lands** — on the goal's own `sdlc/*` branch,
  through `work.py commit` and a pull request. Never a direct commit to the base branch.

**Three paths are branchless, and each for its own reason:**

| Path | Why there is no branch | What to do |
|---|---|---|
| a "Design #N" meta-issue with no code changes | §6d says skip `work.py` entirely | the file stays local; the comment is the whole deliverable |
| the Dossier path | a Dossier is `story`-labelled and never `sdlc:goal`, so no `sdlc/*` worktree is ever cut for it and `work.py commit` answers `not started` | either cut one with `work.py start .sdlc <n>`, or take the local-only route — **and say in the comment which you did** |
| a repo that ignores `.sdlc/` | there is nothing to commit | say `local-only`, stop |

What is never an option on any of them is a direct commit to the base branch.

### 5f. What Stage 1 does not do

It does not create the Epic or its children, and it does not write `sdlc:designed` — both belong to
Stage 2, so an interruption between the two **re-runs design rather than silently skipping review**.
Because `sigma-goal-review` may not be installed, the artifact's `## Handoff` heading and the handoff
comment must both **state which you found** rather than assume. Where it is absent, a human reviews
`.sdlc/design/<n>.md`, applies `sdlc:designed` by hand once satisfied, and then restores `sdlc:goal`
with `/sigma-unpark` if the retrofit gate had parked the target — never a raw label edit
(`docs/label-model.md` §7). `design_goal.py`'s meta-issue template renders the identical hedge, and
the two must not drift apart again.

---

## 6. The retrofit gate — `loop.py design-check`

The Tech-side coverage mechanism (§2): the half that makes the mapping unskippable for work born
outside the Dossier front door. It is **park-and-defer**, never inline dispatch — it files a tracked
meta-issue and takes the next goal, and never runs a design pass itself.

### 6a. Config — `goal_design`, and it is OFF by default

```json
"goal_design": { "enabled": false, "mode": "full" }
```

- **`enabled`** — `true` and nothing else turns it on. Absent, `false`, or any non-`true` value
  returns `OFF` and changes nothing. **The check is `enabled is True`.**
- **`mode`** — `"full"` (absent resolves here) or `"lane"`. Anything else warns once to stderr and
  falls back to **`"lane"`**, the least-invasive rung.

Two things about `mode` that a reader coming from `goal_decompose` will get wrong. First, unlike
`goal_decompose.mode`'s three values (`log`/`park`/`file`, which pick the **action**), this axis
**never changes what `design_check` does** — once enabled it always park-and-files. There is no
log-only rung. Second, `mode` is resolved **late**, only after a meta-issue is confirmed to be
getting filed, so an operator's typo does not warn on every already-designed pick.

**Turning this on retrofits the entire live backlog** — every open `sdlc:goal` issue lacking
`sdlc:designed` gets parked on its next pick. That is a deliberate, standalone decision.

### 6b. Where it sits in the pick sequence

`precheck` → `decompose-check` → **`design-check`** → model prediction → lane → phases.

Placed after `decompose-check` for the identical ordering reason `decompose-check` is placed after
`precheck`: **a goal that is BOTH oversized AND undesigned must be split first**, so each eventual
child gets its own design check individually, rather than one design pass being filed against a goal
about to be decomposed out from under it.

### 6c. The three results, and the one read that fails closed

`design_check` prints one line the skill reads by its first word:

| Result | Meaning | What the loop does |
|---|---|---|
| `OFF` | `goal_design.enabled` is not `true` | carry on, nothing happened |
| `PROCEED` | already carries `sdlc:designed`, or exempt by construction | carry on |
| `PARKED <reason>` | not designed; a meta-issue was filed, or filing could not be confirmed safe | **do not research it — take the next goal.** The park counts as one iteration |

**Exempt by construction** means the **first body line** (CRLF-tolerant) carries
`goal_size.DECOMPOSE_OF_MARKER` or `design_goal.DESIGN_OF_MARKER` — a pure bookkeeping meta-issue is
never itself design-worthy. Deliberately **not** `DECOMPOSED_FROM_MARKER`: a decompose *child* is a
real, independently-implementable slice with no blast-radius mapping of its own, and exempting it
would defeat the whole coverage guarantee.

**Fail-open before the label read, fail-closed after it.** Everything up to and including
"can this source even answer the question" short-circuits to `PROCEED` — this gate must never block
or crash the loop on an ordinary transient failure. But **once `fetch_comments_strict` has been
called, a failure OR a malformed shape parks rather than proceeds**: an unreadable label set must
never be treated as "already designed", because collapsing that ambiguity into `PROCEED` silently
reopens the exact hole this feature exists to close. And once a park decision is made, `_record`'s
bookkeeping runs inside its own `try`, so a failure *there* still reports `PARKED`, never `PROCEED`.

A source with no `fetch_comments_strict`/`create_dependency` (a local backlog, say) can neither
confirm "not already designed" nor file a tracked meta-issue, so it degrades to an honest park:
`not yet designed — coverage check needs an issue tracker`.

### 6d. The "Design #N" meta-issue

Filed **once**, idempotency-guarded by a comment marker on the flagged goal:

- **Idempotency** — a `sigma:design-filed` substring in **any** comment on the flagged goal
  parks with `design already filed — see comments` and files nothing. Checked as a bare substring,
  not first-line-only: a comment has no "first line is a declaration" convention to anchor to.
- **Filing** — `handoff.create_tracked_issue(..., dedup=False)`. Dedup is off deliberately: every
  meta-body shares one template modulo the goal id, so a generic TF-IDF duplicate search would risk
  reusing a *different* goal's meta-issue. The comment-marker scan above is the correctly-scoped
  guard.
- **Shape** — title `Design #<goal>: <the goal's title>`, truncated to 256 characters; `area:` and
  `priority:` inherited from the flagged goal's own labels (`unknown` and `handoff.DEFAULT_PRIORITY`
  when absent); body from `design_goal.render_meta_body(goal, mode)`, whose first line is
  `<!-- sigma:design-of=#<goal> -->`.
- **The body is itself a normal SDLC goal** and is kept deliberately short — three `##` sections,
  nowhere near `goal_size.classify`'s thresholds — so the template is never itself mis-flagged as
  oversized when it is picked.
- **It instructs a reconcile first**: if the target is already closed, already carries
  `sdlc:designed`, or already has a linked write-up in its comments, say so and record done. If
  another open `Design #<goal>:` issue exists with a **lower** number, defer to it — lower-number-
  wins, so two concurrent duplicates do not each abort on seeing the other.
- **It writes no code**: no worktree, no branch, no PR. Skip `work.py` entirely and record the
  outcome directly.
- Then a marker comment lands on the flagged goal. If the meta-issue could not be assigned, the park
  detail says so explicitly — **an unassigned meta-issue is invisible to every loop until a human
  assigns it.**

---

## 7. Stage 2 — the confirmation gate (`sigma-goal-review`)

**In:** `.sdlc/design/<n>.md` plus issue `#<n>`. **Out:** the `sdlc:designed` overlay, and — when
the design calls for it — the Spec/Epic ticket and its children.
**Skill:** `skills/sigma-goal-review/SKILL.md`. It never writes the design write-up and never edits
the target's own source code.

### 7a. The independent brief — the maker is never the checker

```
python3 review_context.py brief .sdlc <n> --for goal-review --artifact <n>
```

`<n>` is passed as **both** the goal and the artifact deliberately: at this point there is no
separate Epic issue yet — that is this skill's own output — so the ticket under review and the goal
the change serves are the same one. `review_context.py` fetches and **inlines that issue's
title+body** under "## What you are reviewing" (mirroring its own `_parent()` block: an issue is
fetched and shown, never left as a bare pointer) and states plainly that **no branch or code exists
yet** — on the success path too, not only when something is missing.

Hand the fresh reviewer **only** the brief plus `.sdlc/design/<n>.md`'s path. The write-up is not
inlined: the reviewer has repo read access and opens it directly, the same way `plan-review`'s brief
points at `.sdlc/plans/` rather than inlining a whole plan.

**Read the premise check and the exclusions before anything else in the body.** A `falsified`
`PC-n` (§5d) says the framing this pipeline started from was wrong, so the check is whether `Intent`
AND the slices were rebuilt on the corrected premise or merely annotated with it — a slice derived
from a premise the same document disproves is an ordinary mapping error and goes to §7c's REJECT.
A missing `Premise check` heading is a schema violation, reported rather than read as "the source
was right", exactly like a missing `Sweep` — **and so is a missing `Out of scope`**, which is not an
artifact that excluded nothing but one that kept no record of what it excluded, the cheapest route
back to the honour system §5c just closed (§5d writes both headings on every artifact, `- None.`
where there is nothing to say). **And so is a missing `Seeds`** (#2028) — not an artifact that swept
everything, but one whose `converged` or `capped` verdict is a claim about a set it never wrote down.
Where the table IS written, its count of `unswept` rows and the capped banner's own number are the
same number twice: a disagreement between them is reported, never adjudicated, because this stage
cannot tell from here which of the two is right. And the exclusions of the round the sweep ended on
are read first (§5c), because `converged` is an assertion about precisely that round.

**`Budget` is a claim you check by RE-RUNNING `sweep-budget`, not by reading it** (#2032). The field
is self-reported — a pass that skips the verb and writes `3` from memory produces an artifact
indistinguishable from a correct one — so this stage runs `goal_design.py sweep-budget` itself, with
the artifact's own `Mode` and `Lane`, and compares its answer against the artifact's `Budget` field.
The two are the same number by construction, never two independent measurements: a disagreement is
a schema violation, reported and never adjudicated, exactly like the `Seeds` count above. A missing
`Budget` field is the identical failure a missing `Sweep` is — report it and ask for it, never read
the silence as "the default applied".

### 7b. Adjudicate every doubt and blocker — three buckets, in writing

Stage 1 is *required* to surface every doubt it has, and a `full`-mode pass that lists none has
almost certainly not looked hard enough. **So "the design still has open questions" is the normal,
healthy Stage 1 output, not a defect** — and it must not be the thing that leaves Stage 2 unable to
name a verdict. Before naming one, place **every** item in exactly one bucket, in writing, **naming
each by its `D-n`/`B-n` id** (§5d) rather than minting numbers of its own:

- **resolved** — the design answers it, and the answer holds against the real repo.
- **open, not blocking** — genuinely undecided, but every slice can still be scoped and started
  correctly whichever way it is later decided. This is the common case and is **not** a reason to
  reject.
- **blocking** — no slice can be correctly scoped or started until it is answered. **The test is
  operational, not a feeling: name the slice that cannot start, and say what it is waiting for.** If
  you cannot name one, the item is not blocking.

**Two further lists go through the same three buckets, by their own ids** (#1975, #1976): every
`X-n` under `## Out of scope`, and every `PC-n` marked `unverifiable`. Both are judgements Stage 1
made and offered up for disagreement, and a list nobody adjudicates is a list nobody wrote. A `PC-n`
marked `falsified` is **not** bucketed — it is settled; §7a says what to check about it instead.
A falsified premise is also named in the outcome comment by its id with the corrected claim, though
it is not an open item and not carried in the count: `#<n>`'s own body still states what the sweep
disproved, and the comment is the only place the correction lands beside it.

### 7c. Two verdicts, and why there is no third

- **CONFIRM** — the mapping holds against the real repo, blast radius and components are accurate,
  the slice count is plausible, and **nothing was adjudicated blocking**. Open-but-not-blocking
  items do not bar CONFIRM.
- **REJECT** — with concrete findings (`file:line` where applicable). Comment them on `#<n>`, write
  **no** label, and stop. Re-running Stage 1 against those findings is a human's next move or a
  later pick, not this skill's job. A target the retrofit gate parked **stays parked**. **§7g is
  that branch in full** — what it writes, what it deliberately does not, why zero tickets is the
  honest answer, and what discharges it.

**There is no third verdict because the label has no third value.** The verdict's only mechanical
consequence is `sdlc:designed`, and the gate reads it as a presence check — `"sdlc:designed" in
names`, nothing else. A "CONFIRM-ish" verdict would still have to either write the label (in which
case it is CONFIRM) or not (in which case it is REJECT), leaving the third state in prose while the
pipeline treated it as one of the two anyway — which is exactly how an open question gets silently
dropped. So the three-way judgement lives where it can be read (§7b) and the verdict stays binary.

**A CONFIRM that leaves items open is only honest if they survive it.** Every open item is named in
the outcome comment, one line each, with the slice it bears on; where a slice cannot be *finished*
until one is answered, that is expressed as a **`blocked_by` edge**, never as a sentence (§7e) —
and only where **another slice in this same plan** carries that item, because a `blocked_by` key
names a sibling and nothing else (`_validate_and_order`). An item no slice of this plan carries — a
Doubt, a Blocker — has **no edge to write**, and the comment line is its whole record; never reach
for a non-slice id to fill that gap (§5d, §7d-iii). And the comment **leads with the count and the
sweep marker** (§7d-v) —
`goal-review: CONFIRMED (<k> open questions carried) [sweep: converged|CAPPED] -- <outcome>` —
both written on every outcome, because an omitted count is indistinguishable from a review that
never adjudicated at all, and an absent marker reads as `converged` (#1952).

### 7d. On CONFIRM — the overlay, then the tickets

**i. Write the overlay** — `loop.py mark-designed .sdlc <n>`. **This is the only place
`sdlc:designed` is ever written**; the retrofit gate only ever reads it. It is a **pure add**: it
never removes `sdlc:goal`/`sdlc:parked`/`sdlc:in-progress` and never moves the board card, because
restoring `sdlc:goal` on a parked retrofit target is a separate human gesture and because
`sdlc:designed` has no column of its own. Prints `OK`/`FAILED` (best-effort) or **`UNSUPPORTED`** on
a source with no story/epic tickets — in which case stop; there is nothing further this skill can do
there.

**ii. If the design concludes ONE coherent unit** — §5b's light path on the Dossier side, the
common outcome on the retrofit side — **there is no Epic. That is not the same as no ticket, and
which one it means depends on the path** (#1954).

- **Retrofit path** — `sdlc:designed` alone is the whole output; skip iii entirely. `#<n>` already
  IS the tech goal the slice would have been, so there is nothing to create, and `<n>` is "the Epic"
  for §9's purposes. A human's `/sigma-unpark` is what restores `sdlc:goal`; this stage never
  performs that swap.
- **Dossier path** — run iii with a **one-row plan and no `epic` object**. `#<n>` is `story`-labelled
  and never `sdlc:goal` (§3), so skipping iii here would leave a confirmed design on a ticket
  nothing can ever pick: the idea dies at this gate wearing the overlay, which is the opposite of
  what a light path is for. `compile_plan.py` creates the Epic wrapper only for a plan carrying more
  than one issue, so `report["epic"]` comes back `null` — the case iii already handles — and
  `report["issues"]` carries the single `sdlc:goal` child. A small idea costs a story and a slice,
  **2 tickets**, where the six-component run cost 7.

**iii. Otherwise, build the plan and compile it.** One `issues[]` entry per slice — `key`, `title`,
`body` (the slice's `Covers` scope plus the two pointers, worded per §7e), `blocked_by` edges from
the design's stated sequencing **plus §7c's open-item edges** — and one `epic` object with `title`,
`body`, and **`originates_from: <n>`**. Write it to `.sdlc/state/goal-review/<n>.plan.json`, then:

```
compile_plan.py .sdlc --plan .sdlc/state/goal-review/<n>.plan.json --actionable --json \
    --forbid-priority > .sdlc/state/goal-review/<n>.report.json
```

`--actionable` because slices from a CONFIRMED design should be immediately pickable (`sdlc:goal`,
not the legacy `sdlc:needs-confirmation`). **`--json` because every step downstream is written against
`report["epic"]` and `report["issues"]`, and without it this CLI emits neither**: stdout carries
prose in dependency-topological rather than plan order. With it, stdout is exactly one JSON object,
so reading it **by key** makes the ordering irrelevant. The flag does not change stderr — **read the
diagnostics too, they are how you find out a child did not land.** `--forbid-priority` is §7f's
rule, enforced: it refuses the whole plan before any `gh` call if any entry carries a `priority`.

**A `Depends on` token that is not a slice id is a §5d schema violation: adjudicate it through §7b's
three buckets and write down which one — never drop it.** *Blocking* is a REJECT and never reaches
this step at all; *resolved* means the edge is discharged, left out of `blocked_by`, and named in
§7d-v's outcome comment; *open, not blocking* means `blocked_by` keeps slice keys only and §7c's
comment line carries the item. Never pass the token through — `_validate_and_order` refuses the
WHOLE plan before any `gh` call, so nothing at all is created — and never invent a plan key for it,
which files a real, immediately-pickable ticket for a design blocker with no scope of its own.

**A falsified premise is never restated on the epic or on a child (#1976).** Where `## Premise
check` carries a `falsified` row, the bodies written here state the **corrected** premise and the
epic body says in one clause that the source's framing moved (`PC-n`, and what is actually true).
They are written from the design's `Intent`, which §5a already requires to carry the correction, so
this is a check rather than fresh authorship — and it is the check that stops the error
propagating, because every child ends up pointing back at `Story #<n>` whose body still says the
wrong thing.

**iv. Stamp `sdlc:designed` on the new Epic and on every child.** Step i stamped `<n>`, which is a
*different* ticket from the Epic `compile_plan.py` just created. Stamping the children is **not
optional**: with the retrofit gate also on, the very next pick of an unstamped child re-triggers the
gate on a slice that was already produced by a confirmed design, reopening the exact hole Stage 2
exists to close. Anything in `report["failed"]` or `report["skipped"]` was never created — do not
stamp it; report it.

**v. Comment the outcome on `#<n>`** — the epic number and its children, or that no further slicing
was needed, plus every open-but-not-blocking item, one line each **by id**. It leads with both
counts that a reader must not have to open the design for: `(<k> open questions carried)` and
`[sweep: converged|CAPPED]`, each written on every outcome, because an absent marker reads as the
good news. This comment is the **only** place an open-but-not-blocking item is recorded against the
ticket, so one omitted here is one dropped.

### 7e. The phantom-blocker rule, and its control

**A hand-authored body containing `#N` can mint a real dependency edge.** `blocker_scan._compile`
matches `blocked by`, `depends on`, `depends upon`, `needs`, `after`, `requires` or `waiting on`
followed within **40 characters** by a `#N`, with no clause punctuation, newline or `#` in between.
So an ordinary sentence of scope prose — *"this needs the model field from #3"* — is read as a
dependency at confidence 1.0. That is **not advisory**: a confident finding is handed to
`blockers.resolve`, which **writes to the referenced issue** — a label swap, a board move and a
comment on a third, unrelated ticket — and the auto-unpark sweep then stamps `sdlc:blocking` on it.

`compile_plan.py` is meticulous about this for the text **it** writes (`Tracks #N`, `Part of epic
#N.`, `Originates from Story #N.` are all deliberately trigger-word-free). The bodies **you** write
get no such care by default. Therefore:

- **Write the pointers in the fixed shape** — `Design: .sdlc/design/<n>.md` and `Story #<n>.`, each
  on its own line. Never *"needs … #N"*, *"after #N"*, *"requires #N"*, *"waiting on #N"*.
- **Express a real dependency as a `blocked_by` edge, never as a sentence.** `compile_plan.py`
  writes the canonical `**Blocked by:** #N` marker itself, one per line, from the edge — that marker
  is *supposed* to match, and it is the one form the machinery reads correctly.
- **The same rule governs every COMMENT, not only the bodies.** The check-time scan's haystack is
  the body excerpt **plus the goal's own comment text**, with the full trigger set. An outcome
  comment saying *"…which is waiting on #1830 landing first"* mints the same confident edge.

**Run the control before compiling anything — a guard nobody has watched fail is decoration.** The
skill ships a `python3 -c` snippet that scans the plan file's own input bodies through `_BLOCK_RE`
and prints `PHANTOM …` per hit. **Prove it can fail before you trust a `clean`:** paste `this needs
the model field from #3` into one body, re-run, watch it print, then take it back out. The scan is
deliberately stricter than reality — the live check-time scan only sees a title plus the first
excerpt of a body — so reword whatever it flags rather than reasoning about whether the sentence
falls past the cap. **Comments have no file to scan: for them the wording is the only guard.**

### 7f. Priority is omitted, deliberately

**Omit `priority` on the epic and on every child.** `compile_plan.py` applies `DEFAULT_PRIORITY`
(which *is* `handoff.DEFAULT_PRIORITY`, `"P1"`) to any entry without one, so omitting it is a
defined single-constant behaviour, not a gap.

Do not derive one either. Stage 1's artifact format carries no priority anywhere, so any number
written here is your own judgement applied to a document that never expressed it — and it lands as a
real `priority:P<n>` label and, on a project-enabled repo, a real board field, where two passes over
the same design would disagree. **The ordering information the design does carry is its sequencing,
and sequencing belongs in `blocked_by` edges, which are checkable.** A genuinely different priority
is a human's one-gesture edit afterwards.

**And this one is enforced (#2027).** Everything above was already on this page during the
2026-09-01 validation run, which wrote `"priority": "P2"` into all six children of story #2017's
plan anyway — a rule nothing measures is a rule a pass walks past in silence, and the number lands
as a real `priority:P<n>` label and a real board field that nobody can distinguish from one a human
set. §7d-iii's invocation therefore carries **`--forbid-priority`**, and `compile_plan.py`
**refuses** a plan whose epic or any child carries the key. The refusal names every offender in one
message and fires before the backlog source is even resolved, so **exit 2 with zero tickets is the
correct output of a plan that broke this rule, not a tool failure** — the same shape as §7g, and
discharged the same way: delete the keys from the plan file and re-run the one command. Nothing
durable is lost, because `sdlc:designed` and `.sdlc/design/<n>.md` were written before this step.

Two properties of the guard are deliberate. It refuses rather than **strips**, for the reason
#1956 already settled about a design-artifact id: a strip is the bug moved into code where it is
harder to see, and it would discard a real `P0` as silently as an invented `P2`. And it checks
**presence, not value** — an explicit `"P1"` is refused too, because it is still a number a pass
chose, and it is precisely the case a strip could never tell apart from an omission. The guard is
**opt-in** at the library level, because `compile_plan.py` is shared with `/sigma-scope`, whose own
skill mandates a real `P0`–`P4` per issue; what makes it structural rather than one more
honour-system rule is that a plan file living in `.sdlc/state/goal-review/` — the directory §7d-iii
mandates — turns the refusal on **without** the flag.

### 7g. On REJECT — what it writes, and what discharges it

**Everything above is the CONFIRM branch. This is the other one, and it is not an error path.**
§7c makes the verdict binary; this section is what the second value actually does, because the
observable outcome — **zero tickets** — is the one an adopter is most likely to meet first and most
likely to read as the tool failing.

**i. What it writes.** One comment, through `sigma-goal-review` §3:

```
loop.py note .sdlc <n> "goal-review: REJECTED -- <findings>"
```

**And nothing else.** No `sdlc:designed` (§7d-i is never reached), no Epic, no children, no label
added and none removed, no board move, no edit to `#<n>`'s body. A `story` stays a `story`; a
target the retrofit gate parked stays parked (§6). **The comment is the entire output**, which also
makes it the entire record — see §12 for how that write is now made to survive, and what a
`FAILED` here means for the driving agent, rather than just what it used to cost.

§7d-v's mandated `(<k> open questions carried) [sweep: …]` prefix is written for the CONFIRM
comment. A REJECT line carries whatever its findings need, but the sweep marker is worth carrying
here too, for the reason §7c gives: an absent marker reads as the good news.

**ii. Why zero tickets is the honest answer, and filing anyway is worse.** By §7b's operational
test, a *blocking* item is one where a named slice cannot be correctly scoped or started. Its
scope — not its schedule — is what is undecided. Two consequences follow, and both are mechanical:

- **Anything §7d-iii compiles is immediately pickable.** `--actionable` files children as
  `sdlc:goal`, not the legacy `sdlc:needs-confirmation`, so a slice whose boundary is still a guess is picked
  and implemented before the guess is settled. Withdrawing that costs a closed ticket, a reverted
  branch and a board card; withdrawing a comment costs nothing.
- **There is no shape in which the plan could carry the item forward.** A `blocked_by` key names a
  sibling slice and nothing else (§7c), and **a design blocker is not a slice** — so the item
  cannot be expressed as an edge, and §7e forbids expressing it as a sentence. Filing the plan
  would therefore drop it silently, which is the exact failure §7c refuses a third verdict to avoid.

**iii. What a REJECT does not mean.** Not that the idea was rejected — the Dossier is untouched and
still holds the intent. Not that the mapping was wrong: a REJECT is compatible with a blast radius
that re-checks accurate at every cited line. Not that open questions caused it — they never do
(§7b), and a design carrying a long open-but-not-blocking list is the healthy Stage 1 output.

**iv. What discharges it.** The findings are the input to another Stage 1 pass (§5a), and there are
two shapes that discharge a sustained blocker rather than re-wording it:

- **a ruling**, made by a human and written into the design, so the next pass maps one posture
  instead of three; or
- **a re-slice**, splitting the held slice so that the part needing no ruling can start — which
  moves the item into §7b's *open, not blocking* bucket on the operational test rather than by
  assertion.

Then Stage 2 reviews the new artifact from the top. **Nothing re-runs Stage 1 automatically** (§12);
until somebody acts, the findings sit on the issue and that is the whole state.

---

## 8. The tickets, and the markers on them

Everything the pipeline files is filed through `compile_plan.py`, and every marker it writes is
**trigger-word-free by construction** (§7e).

| Marker | Written on | By | When |
|---|---|---|---|
| `Originates from Story #<n>.` | the **Epic** | `_create_epic`, from `epic.originates_from` | at creation, in the first body |
| `Tracks #<child>` | the **Epic** | `_patch_epic_with_subs` → `append_to_body` | after the children exist and have numbers |
| `Part of epic #<epic>.` | every **child** | `compile_plan.py` | at creation |
| `**Blocked by:** #<n>` | every **child** with an edge | `compile_plan.py`, from `blocked_by` | at creation |
| `Design: .sdlc/design/<n>.md` | Epic and children | **the skill's own body text** | at authoring |
| `Story #<n>.` | every **child** | **the skill's own body text** | at authoring |

The first four are code-written and code-shaped. **The last two are prose conventions, and nothing
in the tree reads them back** — including `Originates from Story #N`, which is a human-readable trace
and not a queryable edge (§12).

Children are created in dependency order (`_validate_and_order`, a topological sort on
`blocked_by`), and the epic is patched with its `Tracks` lines afterwards because the epic exists
before any child has a number.

---

## 9. Feature-ification — asked once, at the Epic level

Asked **exactly once**, right after Stage 2 confirms, and **only on CONFIRM** — a REJECT returns
before step 4 ever runs, so this can never fire on a rejected design. `<epic>` is
`report["epic"]` where a plan produced one, else `<n>` itself: per §3, both are "the Epic" in the
sense that matters. **The resolution keys off the report, never off which branch ran** — §7d-ii's
Dossier light path runs iii with a one-row plan, so slicing DID happen and `report["epic"]` still
comes back `null`, and a reading that asked only whether iii ran would point this question at
nothing.

The question is *"Promote `<epic>` into a `feature:<name>` unit of work?"* — and **there are exactly
three answers, all of which end in a recorded comment**:

| Answer | What runs | What the comment says |
|---|---|---|
| **Accept** | fetch naming material, ask type + name, `define.py open`, verify the label, stamp the body marker, `define.py declare` | `promoted to feature:<name> (branch feature/<name>)` |
| **Decline** (a human said no) | nothing | `declined -- asked and declined; <epic> stays on work.base` |
| **No human available** (an unattended pick) | nothing | `deferred -- no human available on this pick` |

**Decline and defer are not the same fact and a later reader acts on them differently.** "Asked and
declined" is a decision about `<epic>`; "nobody was there" is a question still open. And an
undecided ticket and a deliberately-not-promoted one look identical without the comment — which is
why the comment is written on all three paths, and is the entire output of this step on two of them.

**Nothing guesses a name unattended.** Creating a `feature:*` label is a gesture
`docs/branching-model.md` §14 documents as having **no deletion path**, so guessing is worse than
asking again later. On every path, `/sigma-define` remains available against `<epic>` by hand
afterwards, and the comment says so — that is the whole recovery path.

**On accept, the order is load-bearing:**

1. `brainstorm.py .sdlc "#<epic>"` — `resolve_target` recognizes the bare issue reference as its
   direct-issue-number form and returns the Epic's own title+body. **Naming material is fetched,
   never re-authored.**
2. Ask **type** (`feature`/`bug`/`refactor` — exactly three, ask, never infer) and **name** (one git
   branch segment; offer a slug as a suggestion, use the human's answer). `define.py open` validates
   the name and reports why on a refusal, so this step never re-implements that check.
3. `define.py open --name <name> --type <type>`. On anything other than `"ok": true`, read `why`
   back verbatim and **stop** — a refusal on `label` or `branch` needs a human's next move, never a
   silent retry.
4. **Confirm the label exists, THEN stamp the body markers.** `gh label list --search
   "feature:<name>"`. This is the half that does not self-heal: an issue body declaring a unit whose
   `feature:` label does not exist sets the goal aside and writes `sdlc:needs-label` **on any
   repository, adopted or not** (`docs/branching-model.md` §14). **Stamp first and find out
   afterwards, and you have set aside every ticket in this pass at once.** If the label is not
   listed, stop and report it — and do not create it by hand here; `define.py open` owns that.
5. Stamp the two-line marker (`Feature: <name>` / `Branch: feature/<name>`, bare, no fence, no
   indent) onto `<epic>` and every child via `append_to_body`. `define.py stamp` does **not** apply:
   it rewrites a plan JSON's bodies *before* they are filed, and by this point every ticket exists.
6. `define.py declare --unit <name> --issues <epic>,<child1>,…` — on **every** ticket in one call.
   The pick-time label attach would eventually cover a stamped *child*, but `<epic>` never carries
   `sdlc:goal` and is therefore never picked, so it would never self-heal. Declaring all of them
   together costs nothing extra and avoids leaving the epic and its children in two different
   eventual-vs-immediate states. `not-supported` per row is expected on a `LocalSource` repo (no
   label surface; the body marker still landed, which is the half that matters locally).

---

## 10. Labels

Two labels beyond the `sdlc:*` lifecycle set participate, and they are **not** the same kind of
thing as each other.

| Label | Colour | Bootstrapped by `_ensure_labels`? | Written by | Meaning |
|---|---|---|---|---|
| `story` | `#d4c5f9` | **no** | `dossier.py`'s `create_dependency` call | the Dossier tier. Never `sdlc:goal`. |
| `epic` | — | no | `compile_plan.py`'s `EPIC_LABEL` | the Spec/Epic tier. Never `sdlc:goal`. |
| `sdlc:designed` | `#0e8a16` | **yes** | `sources.mark_designed`, only | goal-design **and** goal-review both confirmed |

**`story` and `epic` are minted at attach time, not bootstrapped.** `create_dependency` issues an
idempotent `gh label create` (no `--force`, so an existing label is never restyled) before attaching,
which means a fresh adopter with no pre-created label still gets a working first run. `feature:*` is
the one prefix Sigma attaches and never creates; `story` is not in that class.

**`sdlc:designed` is an OVERLAY, in the `docs/label-model.md` §2 sense** — it rides alongside
whatever state the issue is already in, exactly like `sdlc:in-progress`/`sdlc:blocked`/
`sdlc:blocking`. It joined `_ensure_labels`'s bootstrap table in #1826, making eight seeded labels.
`loop.py design-check` is its **only reader** and `goal-review` its **only writer**; there is no
sweep that clears it and no gesture that removes it.

---

## 11. What each stage writes, and where

| Path | Written by | Tracked? | Why |
|---|---|---|---|
| the `story` issue body + one comment | Stage 0 | n/a (remote) | identical bytes, so the two can never disagree |
| `.sdlc/design/<n>.md` | Stage 1 | **yes** — on every `/sigma-setup` repo and on Sigma's own (#1953); **no** on a repo that set its own blanket `.sdlc/` rule, which §5e measures rather than assumes | the durable, reviewable record. Deliberately **not** in `setup.RUNTIME_IGNORES` |
| a comment on `#<n>` carrying the design's substance | Stage 1 | n/a (remote) | on a shared backlog it may be the only half anyone can read |
| the "Design #N" meta-issue | the retrofit gate | n/a (remote) | a tracked home for the deferred pass |
| `.sdlc/state/goal-review/<n>.plan.json` | Stage 2 | **no** — `.sdlc/state/` is in `RUNTIME_IGNORES` | a one-shot compile input |
| `.sdlc/state/goal-review/<n>.report.json` | Stage 2 | **no**, same | its receipt |
| the Epic + children | Stage 2, via `compile_plan.py` | n/a (remote) | the tech-workable output |

**The traceable link back from every created ticket is the body marker, never the plan file** —
`Originates from Story #<n>.` on the Epic and `Story #<n>.` in each child.

---

## 12. Honest limitations, and the gaps that are deliberate

**Coverage**

- **The retrofit gate is OFF by default**, so out of the box nothing stops an undesigned goal
  reaching Implement (§6a). The coverage claim in §2 is conditional on `goal_design.enabled`, and
  turning it on retrofits the whole live backlog on the next pick — which is why it is not a default.
- **The gate parks; it never designs.** The filed "Design #N" meta-issue must itself be picked, and
  if `create_tracked_issue` could not assign it, no loop can see it until a human does. The park
  detail says so, which is the only reason it is visible at all.
- **Nothing counts `open_` entries, and nothing checks that a follow-up was warranted** by one of
  §4b's three triggers. The first because refusing a doubt is worse than recording a long list of
  them; the second because no local check can read a question's intent.
- **The "no code exploration" constraint is prose on every host, Claude Code included.** The Stage 0
  grant omits `Read`/`Grep`/`Glob`, which declares the constraint, but `Bash(python3 *)` beside it is
  unrestricted code execution, so nothing structurally prevents a code read anywhere (#1957). The cap
  on answered follow-ups is the opposite case: it lives in `dossier.py` and therefore holds
  everywhere. Across all three stages `allowed-tools` **declares intent and spends prompts; it is not
  a sandbox** — the one property it does keep, that every command the skills' steps name is granted,
  is measured by `tests/test_pipeline_allowed_tools.py` rather than asserted, because prose asserted
  it three times and was wrong three times (#1911, #1927, #1957).

- **The design artifact reaches only the repo, and tracking it has a distribution cost.** It is
  visible to exactly the repo's collaborators and never leaves that access-control boundary, which
  is the point. But Sigma ships its whole tracked tree as the plugin, so every artifact this
  repo commits rides into every adopter's plugin cache — measured at 31KB against a 26M payload and
  accepted, stated here rather than discovered. An adopter's own artifacts stay in their own repo.
- **Nothing checks the in-brief against the main artifact** (#2064). It is the author's own
  paraphrase, written in the same pass and trusted the way the rest of this document trusts a
  single unaudited author. It can drop a Blocker, soften a finding, or drift from what the main
  artifact actually says, and no script would notice — `compile_plan.py` and §7a never read it, by
  design, which is exactly what makes it unpoliced. The mitigation is the same one this whole stage
  already relies on for everything else it writes: a human reads it before confirming, which is the
  one property the file exists to make possible in the first place.

**Recovery and state**

- **Stage 0 has no resume.** The issue does not exist until `file()` succeeds, so an interrupted
  interview starts over. That is a genuinely smaller surface than `unpark.py`'s, not a missing
  feature — there is nothing yet to resume from.
- **`mark_designed` is best-effort.** It returns True only if the write landed, and the CLI prints
  `FAILED` when it did not — but nothing retries, and nothing sweeps for a confirmed design whose
  label did not land. On a source with no story/epic tickets it prints `UNSUPPORTED` and Stage 2
  stops there entirely: no Epic, no children, no overlay.
- **Stage 2 never restores `sdlc:goal`.** A retrofit target the gate parked stays parked until a
  human runs `/sigma-unpark`. That separation is deliberate (`docs/label-model.md` §7), and it means a
  CONFIRMED design does not by itself put the goal back in the queue.
- **A REJECT is terminal for this pass, and its whole record is one comment — now retried, and now
  loud on final failure (#1986).** Nothing re-runs Stage 1 automatically; the findings sit on the
  issue until a human or a later pick acts on them (§7g). Because §7g-i writes no label and no
  ticket, that one comment is all there is, so `sources.GitHubSource.note` retries a transient `gh`
  error itself (the identical shape and reason as the label-swap retry in §7d-i) before giving up —
  a duplicate REJECT comment from a retry that followed an already-landed write is accepted as a
  categorically safer failure than a silently missing one. What survives that RAISES, and `loop.py
  note`'s CLI verb — previously a bare `try/except` that printed one non-fatal stderr line and
  always exited 0 — now prints `OK`/`FAILED` on stdout (mirroring `mark_designed`'s own convention)
  and returns a non-zero exit on `FAILED`, so a REJECT whose comment still did not land after a
  retry is at least mechanically distinguishable from one that succeeded, not merely logged where
  nothing is instructed to look. `sigma-goal-review` §3 tells the driving agent to check for
  `FAILED` and not stop as though it had succeeded. This does not make the write durable — nothing
  sweeps for a REJECT whose comment never lands even after that — it makes the failure loud instead
  of silent, which is the fix #1986 chose over inventing a second recording channel nothing reads.

**Traceability**

- **`Originates from Story #N.` and `Story #<n>.` are prose, not edges.** Nothing in the tree reads
  either back. The chain `slice → epic → dossier` is walkable by a human and by no query.
- **Two of the six markers in §8 are written by the skill's own body text, not by code**, so they
  hold only as far as the SKILL.md prose is followed. `tests/test_sdlc_goal_review_skill.py` pins
  the instruction; nothing pins the result on a real ticket.

**Judgement the pipeline does not make**

- **The lane is a judgement, and nothing measures or checks it** (§5b). `discovery.py lane` cannot
  answer for an issue number, so Stage 1 sizes its own target and writes the answer down; no code
  reads the `Lane` field back, and nothing refuses a thirty-row blast radius over a one-file change.
  Recording it is what makes a short artifact reviewable as proportionate rather than lazy — it is
  not what makes it proportionate. Same class as the sweep budget below: a stated discipline, not an
  enforced one.
- **The light path is reachable, not automatic** (§7d-ii). A one-slice design costs 2 tickets
  instead of 7, but only where the pass genuinely concluded one slice; nothing detects a small idea
  at intake, because Stage 0 may not read code and sizing without the code is guessing. The front
  door's honest answer for work that needs no mapping at all is an ordinary `sdlc:goal` issue, which
  `sigma-dossier` now says out loud and no mechanism enforces.

- **Slices carry `priority:P1` and nothing derives otherwise** (§7f). **#1922** settled only the
  half that was a defect — goal-review demanding a priority the artifact never carried — by
  omitting it; it is closed. Whether the slice table should carry a priority *at all* is the
  residual question, and nothing open tracks it, so a repo that wants per-slice priorities is
  editing labels by hand until someone files it.
- **A comment's phantom-blocker safety is wording alone** (§7e). The control reads the plan *file*;
  a comment has no file to scan, so re-reading each one before posting is the entire guard.
- **The sweep budget is a stated cap, not a measured one.** The unconfigured default — three rounds
  under `mode: full`, one/two/three under `mode: lane` for small/medium/large — was, until #2032, a
  number chosen only to make the pass terminate, not a figure derived from how many rounds real
  designs need. #2032 makes the `full`-mode ceiling CONFIGURABLE (`goal_design.rounds`, a positive
  integer an operator raises to let a pass "scan the whole product" — bounded, never
  `"unbounded"`, because every round is real Anthropic spend) and CHECKABLE (`goal_design.py
  sweep-budget` is now the one source both the design pass and `goal-review` read the ceiling from,
  §5c/§7a, so an artifact's `**Budget**` field can no longer be typed from memory without
  `goal-review` catching the mismatch). Reaching the cap, raised or not, is still a legitimate
  outcome and is recorded under Doubts rather than treated as a failure. `## Seeds` (#2028) makes
  the two questions a derivation would rest on — which seeds are still open, and whether the last
  round was still finding new ones — answerable from the artifact, but **it does not derive it**:
  an operator raising the number is still a judgement call, not a figure `## Seeds` computes, and
  one more recorded pass is not a curve. Deriving the RIGHT cap from several real designs read
  together, and whether the split rule §5c states is enforced or dropped — it has never once been
  written despite its trigger condition appearing verbatim in a capped banner — are both still
  undone; #2032 shipped the operator's escape hatch, not either of those two open questions.

**What the proposal predicted, and what actually shipped**

The design proposal listed five decisions it deliberately left open. All five have since been made,
and three of them landed somewhere other than where the proposal pointed:

| The proposal left open | What shipped |
|---|---|
| the dossier → slices conversion mechanics | §5d's `Slice-count estimate` table is the conversion: `Covers` → child body, `Depends on` → `blocked_by`, compiled by §7d-iii through `compile_plan.py --actionable --json` |
| the Q&A shape — bank vs. free-form | bank-based, and **flat rather than keyed** (§4a), plus a bounded `followup_`/`open_` tail the proposal did not anticipate at all (§4b) |
| how the retrofit check triggers a design pass | **park-and-defer** (§6), never inline phase insertion |
| the `goal_design` config shape and the lane rubric | exactly `{enabled, mode}`, off by default, `mode` resolved late, unrecognized → `lane`; the rubric is `discovery.py`'s own `LANES`, plus a sweep budget (§5c) the proposal never mentioned |
| `review_context.py`'s `goal-review` phase case | shipped: `--for goal-review --artifact <n>`, with `<n>` as both goal and artifact, the issue **inlined** rather than pointed at |

Three further places where the code amends the proposal:

- **The proposal's §7 said `sdlc:designed` "needs adding to `_ensure_labels`"** and that both labels
  were pre-created on the repo. `sdlc:designed` was added; **`story` was not**, and does not need to
  be — `create_dependency` mints it idempotently at attach (§10), so no adopter depends on a label
  someone created by hand ahead of time.
- **The proposal implied an Epic always exists.** §7d-ii is the case it did not have: a design
  concluding one coherent unit produces no Epic at all, and the overlay alone is the output.
- **The proposal said feature-ification's owner is "whoever drove the Product stage".** The shipped
  step asks a human, has **three** outcomes rather than two (§9), and does not set an owner at all —
  ownership is the registry's, resolved by `docs/branching-model.md` §12's own inference.

---

*This page is the contract. [`how-the-dossier-pipeline-works.md`](how-the-dossier-pipeline-works.md)
is a route through it and is not authoritative. The `sdlc:*` lifecycle labels are a different
contract again — [`docs/label-model.md`](label-model.md) — as are units of work
([`docs/branching-model.md`](branching-model.md)).*
