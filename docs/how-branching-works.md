# How branching works

A guided path from *"I have an issue"* to *"it merged"*, for someone who has to work in a repository
using this model next week and has not read the contract.

**What the model is for:** so that several goals belonging to one body of work share one branch, and
so that an issue can say which body of work it belongs to. Without it every goal's worktree is cut
from the same configured base, and nothing connects an issue to the work it is part of
([§1](branching-model.md#1-the-one-paragraph-version)).

**This document decides nothing.** Every rule, threshold, default and guarantee lives in
[`branching-model.md`](branching-model.md) and is linked from here, never repeated. Where this file
and that one appear to disagree, the contract is right and this file has a bug. Read this to learn
*what happens, and in what order*; follow the link to learn *what the rule actually is*. Nothing on
this page is quotable as policy, and `tests/test_docs.py` holds it to that mechanically.

Two words recur. A **unit of work** is whatever several goals share a branch for — a feature, a
shared bug, a refactor, a contract change; the prefix is `feature/` regardless of which. A **goal**
is one issue Sigma picks up and works. [§2](branching-model.md#2-the-branch-shape) draws the
three branch names, says who cuts each one, and is worth twenty seconds before anything below.

---

## Does any of this reach you yet?

Two questions, in this order, before you read a single stage.

**Does your issue declare a unit?** If it does not, nothing below changes where its work goes: it
is based the way it was before any of this existed. That is the documented default — but read
[§6](branching-model.md#6-base-resolution)'s opening for how narrow the guarantee actually is,
because it is about the base and only about the base. A pick still pays for having asked
([§6e](branching-model.md#6e-what-a-pick-actually-costs-now-measured)).

**Has the repository adopted the registry?** `.sdlc/features/` is the directory the registry-backed
half opts out on: without it the pick-time sync
([§8f](branching-model.md#8f-what-a-pick-actually-does-to-the-registry)), the cross-repo access check
([§11](branching-model.md#11-cross-repo--the-tiers-and-the-both-green-check)) and their siblings
return before they spend anything.

**The label half is not gated the same way**, and this is the part that surprises adopters. An issue
body declaring a unit whose `feature:` label does not exist meets a pick-time check that runs on any
repository, adopted or not: the pick stops, a label is written onto the issue, and a comment is
posted — on a repository with no registry at all.
[§14](branching-model.md#14-adopting-it-and-the-gestures-that-are-correct) opens with that
measurement and explicitly rejects "inert" as a description of the state, so do not repeat it as
one. Take the ordering requirement §14 gives as a requirement, not as advice.

The registry's own adoption-readiness work was filed as **#1564**, now the epic **#9** on the public
backlog. The one item there that is *not* gated behind a missing `.sdlc/features/` is **#1571**, now
**#13** — a repository that never adopted the model yet already carries a `feature:*` label from
somewhere. The code half of #13 is fixed: `work.py` `_declaration_moves_the_base` lets a declaration
decide the base only where `.sdlc/features/` exists. Both issues were still open on the public backlog
when read on 2026-10-13 (REST), so the tracker has not caught up with the code.

---

## Stage 0 — the unit exists before any issue names it

Opening a unit is a human's gesture, performed once, and **the order of the four parts is
load-bearing rather than tidy**. [§14 › Opening a unit](branching-model.md#opening-a-unit) gives
them in the order that works, and the section above it says what going out of order costs. Doing
them in the order that feels natural is the most common way to end up with a goal that will not
start.

**There is a flow for it.** `/sigma-define` opens the branch, the label and the registry, and files
the issues that declare the unit — for whichever of three kinds the work is, on one branch prefix
for all three. [§14 › Defining a unit](branching-model.md#defining-a-unit--the-three-kinds-and-sigma-define)
is what it performs, which single step of it is the one no automated path in the kit will perform
for you, and what `--feature <name>` narrows a run down to.

**Once its issues exist, you're asked one more, optional question: does this unit get a
priority?** Answer with a `P0`–`P4` tier and it is recorded as data on the unit itself, on the
spot — never a write to any issue's own `priority:` label. `sigma-goal-review`'s own
feature-ification step (promoting an Epic into a unit after a design is confirmed) asks the
identical question at the identical point. Say nothing (or skip) and nothing changes — no code
path records a priority unless this question was actually answered. [§16 › Feature-level
priority](branching-model.md#16-feature-level-priority--optional-opt-in-and-recorded-on-the-unit)
is the full contract, including the tie-break this feeds into at pick time and the different,
still-available verb for genuinely levelling a unit's members' own labels by hand.

One more thing is per *repository* too, and it is the one most often skipped: the
agents working a participating repository have to have been told the standing rule
about committing to a feature branch. They do not know it by default.
[§14 › Step 0](branching-model.md#step-0-once-per-repository-put-the-rule-where-your-agents-read-it)
carries the block to paste, the grep that checks whether a repository has it, and where the record
of having done it belongs. In this repository, [`AGENTS.md`](../AGENTS.md) is where that rule is
stated; [§3](branching-model.md#3-the-no-direct-commits-rule-and-what-it-buys) is what it buys, what
detects a breach of it, and why detecting is a different guarantee from preventing.

## Stage 1 — the issue declares its unit

An issue declares its unit **twice**, and the duplication is deliberate: one half is what machines
query, the other is what a person reads.
[§4](branching-model.md#4-declaring-a-unit--the-two-halves) is the pair and why there are two.

Three things to know before you write one:

- the body half has a required shape, and several natural-looking ways of writing it declare nothing
  at all — silently. [§4a](branching-model.md#4a-the-marker-must-be-bare) enumerates them, and the
  list is longer than anyone guesses;
- there is a second body line whose job is to catch an issue that contradicts itself, and it does
  *not* name your goal's own branch, however much it reads that way —
  [§4b](branching-model.md#4b-the-branch-line-is-a-cross-check-never-a-second-source-of-truth);
- the name you pick is constrained by what git accepts as a branch segment, measured rather than
  assumed — [§5](branching-model.md#5-what-a-unit-may-be-called).

Reading an issue produces one of exactly five states, and which one you are in decides everything
downstream: [§4c](branching-model.md#4c-the-five-verdicts-and-the-body-wins-rule). One shape is not
among the five, because there is no honest answer for it —
[§4d](branching-model.md#4d-the-one-thing-that-is-not-a-verdict-an-issue-contradicting-itself)
— and it is the one that costs a goal.

## Stage 2 — Sigma considers the goal, and reconciles the two halves

Before a goal is started it is *considered*, and the two halves of the declaration are brought into
line at that point.
[§7](branching-model.md#7-at-pick-the-label-and-sdlcneeds-label) is the whole step: what Sigma
does with a label that exists, what it declines to do with one that does not, and why that
particular division. Every outcome the check can reach, including the two transient ones that
deliberately look identical, is tabulated in
[§7b](branching-model.md#7b-every-outcome-of-the-pick-time-check).

If the check sets your goal aside, what you see on the issue and what you do about it are in
[§7a](branching-model.md#7a-what-a-refusal-looks-like). It is worth reading before it happens to
you, because the recovery is smaller than it looks.

This stage is also where a pick starts costing something it did not cost before, on a budget worth
knowing about — including for goals that are considered and then refused, and including on
repositories that never adopted anything.
[§6e](branching-model.md#6e-what-a-pick-actually-costs-now-measured) is the measured table, row by
row, with the unit of cost named per row.

## Stage 3 — the goal starts: base, registry, upkeep, then the cut

`work.start()` does these in a fixed order before your worktree exists, and each has a section of
its own. The root checkout is checked first — tracked edits on the base refuse the start, except
registry files Sigma can show it wrote itself
([§15](branching-model.md#15-honest-limitations-and-the-gaps-that-are-deliberate)):

1. **the base is resolved** — three lines of precedence, from your issue's declaration down to the
   old behaviour ([§6](branching-model.md#6-base-resolution)). Two conditions decide whether the
   declaration is read at all ([§6a](branching-model.md#6a-when-the-declaration-is-read)); a read
   that fails behaves differently from a read that returned nothing, and the difference is recorded
   somewhere durable ([§6b](branching-model.md#6b-when-the-read-fails));
2. **the registry is synced**, before the fetch rather than after it, for a reason that only shows
   up on the very first goal of a new unit
   ([§8f](branching-model.md#8f-what-a-pick-actually-does-to-the-registry)). This step is also where
   what the registry claims is checked against the branches that really exist, and where two of the
   model's deliberate approximations live;
3. **rebase upkeep runs**, on a goal that declares a unit — the pass that brings the unit's branch
   forward and, on the way, measures the standing rule instead of trusting it. When it runs, when it
   does not, and what its silence does and does not prove are all in
   [§3](branching-model.md#3-the-no-direct-commits-rule-and-what-it-buys);
4. **the base is fetched, and the worktree is cut.** A declared unit whose branch does not exist
   stops here, and stops loudly — [§6d](branching-model.md#6d-a-declared-unit-whose-branch-does-not-exist)
   explains why that needs no rule of its own.

What lands in the registry as a result, and the shape of the entry it lands in, is
[§8](branching-model.md#8-the-registry--sdlcfeatures): what the registry is *for*
(it is not the live set of units — [§8](branching-model.md#8-the-registry--sdlcfeatures)'s opening
says what is), which file a pick writes and which is derived
([§8a](branching-model.md#8a-the-layout)), and the field-by-field schema
([§8b](branching-model.md#8b-the-schema)). If you ever write to it from your own code, the caller's
obligation in [§8d](branching-model.md#8d-pass-the-whole-entry-never-a-delta) is the one that will
cost you data if you skip it.

## Stage 4 — you work, and everything the goal files inherits the unit

Your work happens on the goal's own `sdlc/<goal-id>` branch, cut from the unit's branch
([§2](branching-model.md#2-the-branch-shape)).

A follow-up finding, a hand-off, a decomposition meta-issue — anything Sigma opens *from* your
goal is stamped with the same unit, so the chain does not end silently at the first machine-filed
issue. [§10](branching-model.md#10-stamping--every-issue-sigma-files-itself) is the mechanism,
including the four situations in which it deliberately declines to stamp and the one where it files
unstamped rather than file something a reader cannot see.

A unit also has a human-readable page, `.sdlc/features/<name>.md`, which has two owners: a block
Sigma regenerates and a region that is yours
([§9](branching-model.md#9-the-managed-block-in-namemd)). If you want to write a note about the
unit, [§9](branching-model.md#9-the-managed-block-in-namemd) says where it goes so that it survives.
If you get a message saying your edit was overwritten,
[§9d](branching-model.md#9d-every-outcome-of-a-sync) is the table of every outcome a sync can
produce and which of them report anything.

## Stage 5 — the goal's pull request merges into the unit's branch

The merge Sigma performs is your goal's own branch into the unit's branch, inside one
repository ([§2](branching-model.md#2-the-branch-shape)). That is the merge this walkthrough's title
ends at.

**It is not the merge that ships the work.** Getting `feature/<name>` to the integration branch is a
separate act, and [§13](branching-model.md#13-completion-and-branch-protection) says whose it is by
default. It also covers how a finished unit is recorded — which is not what most people reach for
first — and what the model's own branch-protection check actually asks, which is narrower than the
question people think they are getting an answer to.

**Your issue is closed by Sigma, not by the merge.** That surprises people who expect a
`Closes #N` to do it, and the reason it cannot — plus what actually performs the close, when, and
what it releases — is
[§13a](branching-model.md#13a-what-closes-a-goals-issue-since-the-merge-cannot).

**If your branch falls behind while you're on it**, `sigma-rebase` is what a human runs: it explains
what the base did while you were away — and why, from CHANGELOG.md, the landing PR, or a linked
design doc — before it touches anything, then rebases. On a conflict it shows that file's own
decision context and walks you through named resolution options one file at a time, rather than
the bare `git` error the automatic upkeep pass above leaves behind. Once the tree is clean, the
same skill runs this repo's own proving command and — only if it passes, and only if you say yes —
lands the branch with a plain `gh pr merge`. [§3a](branching-model.md#3a-the-manually-triggered-companion--sigma-rebase)
has the detail.

If your unit spans two repositories, the landing is not one merge and cannot be:
[§11](branching-model.md#11-cross-repo--the-tiers-and-the-both-green-check) has the two strategies,
which one applies to you, and the failure the whole shape is built around — which is not the one you
would guess. The readiness check you can query is
[§11a](branching-model.md#11a-the-both-green-check-is-a-queryable-check-not-a-merge-refusal), and
what it deliberately does not do is
[§11b](branching-model.md#11b-then-they-merge-back-to-back-deliberately-has-no-owner). Two ways a
goal can end up permanently unready through no fault of its own, both recoverable, are named in
[§11a](branching-model.md#11a-the-both-green-check-is-a-queryable-check-not-a-merge-refusal) rather
than left to be rediscovered.

---

## When it stops and wants a human

Five places where it stops and wants you. What you do about each is in the section beside it —
usually less than you would expect.

| What you see | Where it is written down |
|---|---|
| the goal keeps its membership but gains an extra label, and a comment appears | [§7a](branching-model.md#7a-what-a-refusal-looks-like) |
| one issue is set aside while the rest of the queue carries on | [§4d](branching-model.md#4d-the-one-thing-that-is-not-a-verdict-an-issue-contradicting-itself) |
| a goal declaring no unit at all is attached to one, or set aside, without your asking | [§18](branching-model.md#18-the-core-unit-and-ai-judgment-classification-of-a-dangling-goal) |
| an issue you filed is held rather than acted on, and addressed to somebody | [§12](branching-model.md#12-the-two-owners-and-the-per-unit-authorized-grant) |
| a note that a hand-edit inside a managed block was overwritten | [§9d](branching-model.md#9d-every-outcome-of-a-sync) |
| a merge landed and its line ends `but could not close #N` | [§13a](branching-model.md#13a-what-closes-a-goals-issue-since-the-merge-cannot) |

Who "somebody" is, and why there are two ownership levels rather than one, is
[§12](branching-model.md#12-the-two-owners-and-the-per-unit-authorized-grant) — worth reading
before you file cross-team work, because the gate is on the filing rather than on the working.

## Where each rule lives

The map, so that you know where to go rather than which page to search.

| The question | The section |
|---|---|
| what the whole thing is for, in a paragraph | [§1](branching-model.md#1-the-one-paragraph-version) |
| the three branch names and who cuts each | [§2](branching-model.md#2-the-branch-shape) |
| the standing rule about feature branches, what it buys, what detects a breach | [§3](branching-model.md#3-the-no-direct-commits-rule-and-what-it-buys) |
| how an issue declares its unit, and the five states that produces | [§4](branching-model.md#4-declaring-a-unit--the-two-halves) |
| what a unit may be named | [§5](branching-model.md#5-what-a-unit-may-be-called) |
| how a goal's base is chosen, and what a pick now costs | [§6](branching-model.md#6-base-resolution) |
| the label reconcile at pick, and every outcome of it | [§7](branching-model.md#7-at-pick-the-label-and-sdlcneeds-label) |
| what `.sdlc/features/` is, what is in it, and who writes what | [§8](branching-model.md#8-the-registry--sdlcfeatures) |
| the human-readable unit page and its two owners | [§9](branching-model.md#9-the-managed-block-in-namemd) |
| how the unit reaches issues Sigma files by itself | [§10](branching-model.md#10-stamping--every-issue-sigma-files-itself) |
| units spanning two repositories | [§11](branching-model.md#11-cross-repo--the-tiers-and-the-both-green-check) |
| ownership, and who may file work against a unit | [§12](branching-model.md#12-the-two-owners-and-the-per-unit-authorized-grant) |
| finishing a unit, and branch protection | [§13](branching-model.md#13-completion-and-branch-protection) |
| adopting the model, and the gestures that are correct | [§14](branching-model.md#14-adopting-it-and-the-gestures-that-are-correct) |
| how a unit is created, the three kinds, and running one unit only | [§14 › Defining a unit](branching-model.md#defining-a-unit--the-three-kinds-and-sigma-define) |
| the quick-reference of do-this / not-that | [§14 › The one-line summary](branching-model.md#the-one-line-summary) |
| **everything that does not work, stated by the model about itself** | [§15](branching-model.md#15-honest-limitations-and-the-gaps-that-are-deliberate) |
| giving a unit a priority, and how it is used as a tie-break | [§16](branching-model.md#16-feature-level-priority--optional-opt-in-and-recorded-on-the-unit) |
| the `core` unit, and classifying a goal that declared no unit at all | [§18](branching-model.md#18-the-core-unit-and-ai-judgment-classification-of-a-dangling-goal) |

[§15](branching-model.md#15-honest-limitations-and-the-gaps-that-are-deliberate) is the section to
read second, whatever you read first. It is the model's own inventory of its gaps — measured, not
hedged — and reading it is cheaper than discovering any one of them.

---

A temporary run label (`--label`) is not a unit and does not change a goal's base; see `docs/label-drain.md`.

*This page is a route through [`docs/branching-model.md`](branching-model.md). That file is the
contract, and it is the only one of the two that is authoritative. The `sdlc:*` lifecycle labels are
a different contract again — [`docs/label-model.md`](label-model.md).*
