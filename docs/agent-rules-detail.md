# Agent rules — operational detail

This material is moved from `AGENTS.md` so every agent receives the compact
rules while the original examples and caveats remain available on demand.

## Before you run the loop: check the plugin version

**If the installed Sigma plugin is older than 1.0.0, do not start the loop.** That floor is the
one `AGENTS.md` states (the only statement `/sigma-doctor` parses), and it never sits above the
version `.claude-plugin/plugin.json` ships. Mixed versions writing `sdlc:*` labels on one board is
the configuration to avoid. Run `/sigma-doctor` first — it reports the installed version against
the marketplace's current one — and if it is below the floor, update with
`claude plugin update sigmaloop@sigmaloop` (then restart the session) before picking any goal. Use the full
`plugin@marketplace` id: older Claude Code releases failed the bare `claude plugin update sigma`
with "not found" (2.1.284 resolves it, measured), and the full id is unambiguous on every release. `claude plugin marketplace update sigmaloop` is a DIFFERENT
command that only refreshes the marketplace's listing cache — it does not upgrade an
already-installed plugin. `claude plugin install` on an already-installed plugin is also a no-op
and will NOT upgrade it.

## Labels

`docs/label-model.md` is the contract for every `sdlc:*` label — what each one means, which
combinations are legal, and the exact gestures for promoting, parking and unparking. **Read it
before writing any label by hand.** The short version:

- `sdlc:goal` is MEMBERSHIP. `sdlc:in-progress`, `sdlc:blocked` and `sdlc:blocking` are OVERLAYS
  that ride alongside it. `sdlc:parked` stands ALONE. The old `sdlc:needs-confirmation` label is
  legacy: Sigma files armed or parked now, and writes it only when `ai_filed.triage.enabled` is false.
- Anything waiting to be **picked** carries `sdlc:goal`; anything waiting for a **human** carries
  only its own label.
- Prefer `/sigma-promote` and `/sigma-unpark` over editing labels directly — they perform the whole
  transition atomically, which a two-step hand edit does not.

## The Dossier pipeline — where work comes from

**New to this? Start with `docs/how-the-dossier-pipeline-works.md`** — the same machinery in the
order it actually happens, walking one real idea ("Recurring tasks") from the sentence somebody
typed through to the four tech goals it became, and linking into the contract at every step. It
decides nothing, so never cite it as the rule.

`docs/dossier-pipeline.md` is the contract for the two ticket tiers **above** an ordinary goal —
what a Dossier is, what a design pass must produce, what confirms it, and what that confirmation is
allowed to write. **Read it before running `/sigma-dossier`, `/sigma-goal-design` or
`/sigma-goal-review`, before hand-writing a `story` or `epic` ticket body, and before turning
`goal_design` on.** The short version:

- **Three tiers, one direction.** A **Dossier** (`story`) holds business intent and is never
  `sdlc:goal`. A **design pass** writes `.sdlc/design/<n>.md` — an artifact, not a ticket, and not a
  label. A confirmed design produces the **Spec/Epic** (`epic`, also never `sdlc:goal`) and its
  **slices**, which are ordinary goals the loop runs unchanged. The Spec and the Epic are one
  ticket, not two — and a design concluding one coherent unit produces no Epic at all.
- **`sdlc:designed` has exactly one writer and exactly one reader.** `goal-review` writes it, only
  on CONFIRM, via `loop.py mark-designed`; `loop.py design-check` reads it and never writes it.
  Nothing else touches it, and the verdict is binary because the label is a presence check with no
  third value. `goal-design` alone never writes it, so an interruption between the two stages
  re-runs the design rather than silently skipping its review. **A REJECT writes nothing but a
  comment** — no label, no Epic, no children, so zero tickets is its correct output, not a failure;
  §7g is what it means and what discharges it.
- **Stage 0 does not read your code, and Stage 1 does nothing else.** The intake bank is eight
  fixed questions plus a capped `followup_`/`open_` tail; an `open_` entry is a doubt handed
  forward, is never capped, and never causes a refusal. Stage 1 inherits every one of them as a
  Doubt — never answer one away in passing.
- **The retrofit gate is OPT-IN and OFF by default** (`goal_design: {enabled, mode}`), it
  **park-and-defers** rather than designing anything itself, and turning it on retrofits the entire
  live backlog on the next pick. It fails open everywhere except the one label read it exists for,
  where it fails closed on purpose.
- **A hand-written `#N` in an issue body or comment can mint a real blocker edge.** Write
  `Design: .sdlc/design/<n>.md` and `Story #<n>.` on their own lines, express dependencies as
  `blocked_by` edges, and run the phantom-blocker control — after breaking it once — before
  compiling any plan.

## Branches and units of work

**New to this? Start with `docs/how-branching-works.md`** — the same machinery in the order it
actually happens, walking one issue from declaring a unit through to its pull request landing and
linking into the contract at every step. It decides nothing, so never cite it as the rule.

`docs/branching-model.md` is the contract for `feature:<name>` — what a unit of work is, how an
issue declares one, how a goal's base is resolved from it, and what `.sdlc/features/` records.
**Read it before writing a `Feature:` marker, a `feature:` label or anything under
`.sdlc/features/` by hand.** The short version:

- an issue declares its unit **twice** — the `feature:<name>` LABEL and a two-line BODY marker —
  and on a disagreement the **body wins**. The marker must be BARE: its own line, at most three
  spaces of indent, never inside a fence, an indented block or an HTML comment.
- a goal declaring **no** unit bases on `work.base` exactly as it always did — that guarantee is
  about the BASE, and only about it. **Adoption is not uniform:** the registry sync and the
  cross-repo check opt out when there is no `.sdlc/features/`, but the label half does **not**.
  Declaring a unit whose `feature:` label does not exist sets the goal aside and writes
  `sdlc:needs-label` **on any repository, adopted or not**. So: create the label BEFORE any issue
  body declares the unit.
- Sigma **attaches** a `feature:` label that exists and **never creates one**. A goal whose
  declared label is missing keeps `sdlc:goal`, gains `sdlc:needs-label`, and resumes by itself the
  moment a human creates the label — one gesture, nothing to un-park.
- `.sdlc/features/units/<name>.json` is the write surface; `index.json` is **derived**. A write
  passes the **whole** entry, never a delta. Never hand-edit inside the managed block of
  `.sdlc/features/<name>.md` — it is regenerated, and the edit is reported to its owner. Write
  below the end marker.
- **Unit attachment is not a manual convention to remember — with `discovery.no_dangling_goal.
  enabled: true`, Sigma itself attaches one, every time, whether or not the person filing the
  issue ever ran `/sigma-define`.** This is deliberate: `/sigma-define`/`declare()` is the human path
  for *opening a brand-new* unit and self-declaring it up front; the no-dangling-goal machinery is
  the *safety net* underneath it, catching anything that reaches pick time or filing time with no
  declaration at all. Two DIFFERENT mechanisms cover two
  DIFFERENT moments, and both are needed — relying on everyone remembering to declare a unit by
  hand is exactly the gap this exists to close:
  - **Classification (pick-time and filing-time):** `feature_classify.classify_at_pick`/
    `classify_for_filing` run the 4-tier chain (an existing unit match, the configured catch-all,
    an evidence-backed unregistered component, or genuinely-unknown triage) and **attach the
    label** — never create one, per this section's own rule above. With
    `discovery.no_dangling_goal.live_judge.enabled: true` this is backed by a real, metered
    assigner→validator model call (`feature_judge.py`) instead of the judge-less default (which
    only ever reaches tiers 2/4). Run live against this repo's own board 2026-09-11: the validator
    is genuinely the load-bearing check, not decoration — it correctly refused an assigner's own
    tier-3 guess for carrying too-vague supporting evidence, falling back to the safe catch-all
    rather than a wrong specific attribution. Tier 3 is designed to be hard to trigger by accident;
    that is a feature of the design, not a bug in it.
  - **Registry membership (work-start-time):** the LABEL alone is not the whole record. The
    registry's own per-unit `goals` list is reconciled by `feature_sync.py`, which runs at
    `work.start()` — the moment a goal is actually picked up and worked, not merely labeled. A
    labeled-but-not-yet-picked issue is correctly invisible to the registry until then; this is
    the intended division of labor (classification decides WHICH unit, the sync keeps the
    unit's OWN membership record current once real work begins on it), not a gap where the label
    and the registry can silently disagree forever.
  - **The side-job ensure (every later trigger):** `feature_sync.sync_at_pick` is
    deliberately not reached on a resume ("a resume is the same pass continuing... the next PICK
    reconciles it") — so a label attached or corrected AFTER a goal's own pick (a late
    classification, or a human editing the issue) would otherwise sit unrecorded until some OTHER
    goal on the same unit is next picked. `loop._ensure_unit_tracking` closes exactly that gap:
    on every later goal-scoped trigger (`note`/`verify`/`agent-start`), it re-checks whether this
    ONE goal's registry membership still agrees with its CURRENT label and repairs the narrow
    membership gap if not — never a second full sync, never a write that widens a unit's `repos{}`
    past what it already authorizes. This is a **side job**: config-gated, cooldown-bounded,
    fail-open exactly like `_ensure_watcher`, and a repair it cannot make produces a warning (one
    issue comment, one local log line) rather than ever blocking the goal.

## Nobody commits directly to a feature branch

> **Nobody commits directly to a feature branch. All work reaches it through `sdlc/*` goal
> branches.**

This is load-bearing, not style. Rebasing rewrites published history, which normally forces every
holder of a branch to hard-reset and puts their uncommitted work at risk. That risk disappears
**entirely** only because no human ever holds commits on a `feature/*` branch — Sigma owns the
`sdlc/*` worktrees and re-bases them itself. The failure mode is quiet: someone works once without
Sigma, commits straight onto a feature branch, and from then on the unit is not brought forward
at all. `docs/branching-model.md` §3 is what the rule buys and §15 is where its gaps are inventoried;
this section is the rule.

**Part of it is enforced, and knowing which part is the point.** Rebase upkeep
(`skills/sigma-loop/scripts/feature_rebase.py`) does not trust the rule — before it force-pushes
`feature/<name>` it walks that branch's own `--first-parent` line (`landed_commits`) and asks of
every commit whether GitHub left a trace of the pull request that landed it: `(#N)` from a squash,
`Merge pull request #N` from a merge (`arrived_through_a_pull_request`). One commit with no trace and
the pass stops — the branch is left exactly as it was, and the finding is filed as a tracked issue
naming each unaccounted-for commit. Filing is best-effort: if it fails the finding still reaches
stderr, and is attempted again on the next pick.

Do not read that check as a guarantee. What it does **not** cover:

- **it stops a rebase; it never stops a commit.** No code path in the kit refuses a push to
  `feature/*`, and nothing in the branching model sets protection on it. (The one ruleset writer in
  the tree, `merge_queue_enable.py apply`, targets the integration branch and knows nothing about
  units.) Only branch protection on the host can prevent the commit — **and protecting `feature/*`
  in the ordinary way turns this detection off**, because every pass ends in a force-push of that
  branch: block non-fast-forwards there and every pass ends `failed`, on every pick, forever. If you
  protect `feature/*`, grant upkeep's actor a bypass. See `docs/branching-model.md` §13.
- **a branch that is not behind its base is never checked — and the skip is SILENT.** The pass
  returns `current` as soon as `rev-list --count feature..base` is zero, which is before the check;
  `current` is not in `IN_CLAUSE`, so `clause()` returns the empty string and the pick line says
  nothing at all. Calling `direct_commits()` on the same two refs finds the commit; the pass never
  asks. So a violation surfaces only when a goal declaring that unit is **picked** (a resume of an
  already-started goal returns before upkeep runs) *and* something has landed on the integration
  branch since. On a quiet integration branch that can be a long time, and you will not be told.
- **`merge_method: rebase` cannot be checked at all.** That method preserves the goal branch's
  original commit messages, so a landed goal and a hand-typed commit are indistinguishable to any
  local check; the pass declines as `unverifiable` and rebases nothing.
- **`rebase_upkeep: off`, a repo with no `.sdlc/features/`, and a goal declaring no unit** each
  return before any measurement is taken.
- **a subject line is taken at its word, in two forms.** A hand-written commit ending `(#123)`, or
  one beginning `Merge pull request #9 from ...`, is accepted as a landing — those two cases are
  where a direct commit IS rewritten under whoever is holding it. The mirror image is a standing
  outage rather than a lost pass: a squash template that drops the `(#N)` reference makes every
  landed commit unaccountable, so the pass returns `direct-commits` on every pick and the branch is
  never brought forward again.

So: put the commit on an `sdlc/*` branch and land it through a pull request. If you ever work a
`feature/*` branch without Sigma, keeping the unit consistent is yours — the tool tells you it
stopped, and it tells you nothing before you commit.

## Golden tasks

Golden-task directories under `evals/golden/` are checked by `python3 evals/golden/verify.py` (CI runs it; `--only ID` checks one task). It has nothing to do with `contract/golden/`. See [the evals README](../evals/README.md#golden-tasks).

## Output

All status reporting in this repo follows `docs/output-contract.md`.

Read that file before your first status emission. A status block is constructed, never
hand-written: pass facts to `skills/sigma-loop/scripts/render.py status|event|decision` and relay
its stdout verbatim — `python3 skills/sigma-log/scripts/log.py slots .sdlc` is the live Block A,
and `phase_report.py end` prints Block B at every phase boundary. A refusal (stderr, exit 2, empty
stdout) means the facts were malformed: fix the facts and run it again; never patch the prose by
hand. What stays yours is the facts — both axes on every slot line (colour marker + `P<n> NAME`
phase token), the linked goal ref — and, most importantly, the emission triggers. Tool calls are
not a trigger. If no trigger fires, emit nothing.

Dispatch labels follow it too: every subagent label, task title, and branch name
carries the goal ref and the phase.

This governs status output only. Content the user explicitly asked for (a report,
a walkthrough, an explanation) is given in full.
