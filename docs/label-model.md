# Sigma's label system — how it works, and what changed

**Audience:** anyone using Sigma. No internals knowledge assumed.
**Status:** shipped in **1.0.0**, the first public release. Every number below was measured, not estimated, and re-verified
against a live board on the day this was published.

---

## 1. The one-paragraph version

Sigma reads and writes a small set of `sdlc:*` labels on GitHub issues to decide what to work on
next. The labels were never really the problem — the machinery *underneath* them was: a single state
change was written as three separate API calls that could each fail silently, and on a board-backed
project the labels were being ignored by the picker entirely. Both are now fixed, plus there is a new
reconciler that finds and repairs drift instead of letting it accumulate invisibly.

---

## 2. The labels

There are three kinds. Mixing them up is what made the old model confusing. (These are the `sdlc:*`
labels — the only ones Sigma creates. For `priority:*`, `model:*` and your own, see
§12 below.)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  MEMBERSHIP  — at most ONE of these. Decides whether it can be picked.   │
│                                                                          │
│    sdlc:goal                 in Sigma's world; eligible              │
│    sdlc:needs-confirmation   LEGACY: retired queue; written only when    │
│                              triage is off                               │
│    sdlc:parked               the EXIT — a human's permanent hold         │
└─────────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────────────┐
│  OVERLAYS  — orthogonal. Ride ALONGSIDE sdlc:goal. Not states.          │
│                                                                          │
│    sdlc:in-progress          someone is working it right now             │
│    sdlc:blocked              waiting on another ISSUE (auto-resumable)   │
│    sdlc:needs-label          waiting on a human to create a feature:     │
│                              LABEL (auto-resumable)                      │
│    sdlc:needs-unit           waiting on a human to declare ANY           │
│                              unit at all (auto-resumable)                │
│    sdlc:blocking             other work waits on THIS one                │
│    sdlc:designed             goal-design + goal-review both              │
│                              confirmed (gates retrofit pick)             │
│    sdlc:needs-triage         every tier tried and failed (no self-heal)  │
└─────────────────────────────────────────────────────────────────────────┘

**The whole model in one rule:**

```
     IN SIGMA'S WORLD (carries membership)      OUT OF IT (stands alone)
     ─────────────────────────────────────────      ────────────────────────
     sdlc:goal                                      sdlc:parked
     sdlc:goal + sdlc:in-progress                   (legacy: sdlc:needs-confirmation)
     sdlc:goal + sdlc:blocked
     sdlc:goal + sdlc:needs-label
     sdlc:goal + sdlc:needs-unit
     sdlc:goal + sdlc:blocking
     sdlc:goal + sdlc:needs-triage
```

The left column is about **membership**, not about who is owed the next action. Anything the loop
will eventually pick is in it; anything a human has taken out of Sigma's world stands alone —
and a parked issue is **human domain**: nothing automatic ever puts `sdlc:goal` back on it.

**One case is deliberately both, and it is the one to understand before hand-editing anything.** A
goal carrying `sdlc:goal + sdlc:needs-label` is waiting on a **person** (somebody has to create a
`feature:` label) *and* keeps its membership. That is not a contradiction, because the two columns
answer different questions: membership decides what can be *found* and eventually picked, and the
overlay decides that it is not pickable *yet*. It keeps membership because the loop resumes it
automatically the moment the label exists — nobody has to remember it, and no human gesture undoes
it. **Do not "tidy" it into standing alone**: dropping `sdlc:goal` would make it findable only by
whichever query happens to ask for the overlay, which is exactly the orphan class §4a is about.
`sdlc:parked` stands alone precisely because the opposite is true of it — nothing automatic ever
brings it back, so it belongs in a person's review queue instead. The retired `sdlc:needs-confirmation`
stood alone the same way; it is legacy now, and a leftover is moved off by `/sigma-promote`. A leftover that also carries `sdlc:goal` is routed by `blockers.classify` so the label is stripped and the issue enters the queue.
┌─────────────────────────────────────────────────────────────────────────┐
│  ANNOTATIONS — descriptive only. Never affect what gets picked.          │
│                                                                          │
│    sdlc:followup    came out of a retro / was filed by the loop          │
│    sdlc:blocking    something else is blocked on this one (self-healing) │
│    sdlc:dependency  originated as a cross-area hand-off                  │
└─────────────────────────────────────────────────────────────────────────┘
```

**The single most common misreading:** `sdlc:goal` + `sdlc:in-progress` together is **correct and
normal** — and so are `sdlc:goal` + `sdlc:blocked`, `sdlc:goal` + `sdlc:needs-label` and
`sdlc:goal` + `sdlc:blocking`. Those are what
an actively-worked, a blocked, and a blocking goal look like. Neither is a bug, and neither must be "cleaned up". What is *not* allowed is two
MEMBERSHIP labels at once, e.g. `sdlc:goal` + `sdlc:parked`.

**Why a blocked goal keeps its membership.** Same reason a claimed one does: every sweep, census,
mirror and reclaim path asks `--label sdlc:goal`. Drop it and the issue is findable only by whichever
query happens to ask for the overlay — and exactly one sweep asks for `sdlc:blocked`. Lose that one
label to a hand edit and the issue is an orphan with no route back. Keeping membership costs nothing:
the picker already refuses a blocked goal on both queue paths, so it is **visible to everything and
picked by nothing**.

A park is the one exit that gives up membership, and deliberately — its whole purpose is to sit in a
human's review queue, not to be re-examined by every sweep.

**The second most common misreading:** `sdlc:followup` is *provenance*, not a state. On our own
backlog **57 of the 89 pickable goals carry it**. Treating it as "needs review" would freeze most of
the board.

**`sdlc:designed`** rides alongside `sdlc:goal` exactly like `sdlc:in-progress`/`sdlc:blocked`/
`sdlc:blocking` do — it is the overlay `docs/dossier-pipeline.md`'s `goal-design` (Stage 1)
and `goal-review` (Stage 2) produce together: a goal carrying it has already been through a
codebase-mapping design pass AND had that design confirmed. `loop.py design-check` (the Tech-side
retrofit gate, opt-in via `goal_design.enabled`) is the only reader — it never writes the label.
`goal-review` (that contract's §7, `/sigma-goal-review`) is the only writer, and only ON
CONFIRMATION: an independent reviewer, fed only `review_context.py`'s `goal-review` brief (never the
`goal-design` author's own context), confirms the write-up before `loop.py mark-designed` lands the
label — never at `goal-design` alone, so an interruption between the two steps re-runs design rather
than silently skipping review. A goal that reaches `sigma-goal`/`sigma-loop` without it gets parked and
a "Design #N" meta-issue filed, the same park-and-defer shape `goal_decompose`'s `file` mode already
uses. `goal-review` never restores `sdlc:goal` itself either: a human's `/sigma-unpark` is the
separate gesture that does, if the retrofit check had parked the goal — never a raw label edit. Where
`goal-review` genuinely cannot run (a source with no story/epic tickets), the same by-hand fallback
this section used to describe as the ONLY path still works: apply `sdlc:designed` by hand once
satisfied with a filed design, then `/sigma-unpark`.

---

## 2a. "Why two labels while it's in progress?"

The most common question about this model, and the answer is that the two labels are read by
different machinery and answer different questions.

```
   sdlc:goal          = MEMBERSHIP.  "This issue is in Sigma's world."
                        Every single query the loop makes is literally:
                            gh issue list --label sdlc:goal --state open
                        Nothing outside that query exists, as far as the loop
                        is concerned.

   sdlc:in-progress   = OCCUPANCY.   "Someone is on it right now."
                        Applied ON TOP. Used as a filter over what the query
                        above already returned — never as a query itself.
```

So why not just swap one for the other on pickup? Because **recovery is a `sdlc:goal` query.**

```
   WITH two labels (today)                WITH one label (the alternative)
   ────────────────────────────           ─────────────────────────────────
   pick   → {goal, in-progress}           pick   → {in-progress}
   crash  → {goal, in-progress}           crash  → {in-progress}
             ↓                                      ↓
   the reclaim sweep searches              the sweep searches sdlc:goal…
   sdlc:goal, finds it, removes            …and this issue is not in the
   in-progress → back in the queue         results. It cannot be found by
                                           anything. Ever.
```

Every laptop that sleeps mid-run, every killed process, every expired lease would land in that
right-hand column. One extra label on screen buys the guarantee that a crash costs a little repeated
work instead of an issue nobody can find.

**The short version:** `sdlc:goal` is a *noun* — this **is** a goal. `sdlc:in-progress` is a *verb* —
it is **being worked**. A thing does not stop being a noun while the verb is happening.

---

## 2b. "How does an `sdlc:blocking` issue get picked up?"

**It doesn't — and that is worth knowing before you rely on the label.**

`sdlc:blocking` is applied automatically to an issue that other work is waiting on. It is a
**signal**, not a queue: it re-ranks issues that are **already** eligible.

```
      What people assume                    What actually happens
      ──────────────────                    ─────────────────────
      sdlc:blocking                          the query is:
        → "this jumps the queue"               --label sdlc:goal --label sdlc:blocking
                                                       ↑
                                             both required — GitHub ANDs them

      so an issue with sdlc:blocking
      and NO sdlc:goal is:                   ✗ not in any queue
                                             ✗ not ranked
                                             ✗ picked by nothing
```

And it gets worse, because the two halves interlock:

```
   ┌──────────────────┐   "Blocked by #7"   ┌───────────────────────┐
   │  #42  sdlc:parked│ ──────────────────► │ #7   sdlc:blocking    │
   └──────────────────┘                     │      (no sdlc:goal)   │
            ▲                               └───────────┬───────────┘
            │                                           │
            │   the sweep resumes #42 only              │  nothing can pick #7
            │   once #7 CLOSES                          │
            └───────────────────────────────────────────┘
                        deadlock — neither side moves
```

The commonest way in is old state: a follow-up filed under the retired confirmation queue still carries
the legacy `sdlc:needs-confirmation` and no `sdlc:goal` — and later, something declares itself blocked
by it. Triage now arms or parks at filing, so new ones do not arrive this way.

**What we do about it: resolve it, not just report it.** When a block is recorded — whether
Sigma filed the blocker or a human typed `Blocked by #N` — every named blocker is classified and
acted on:

```
   the blocker is…                             what happens
   ───────────────────────────────────────     ─────────────────────────────────────────
   already sdlc:goal                       →   nothing; it is already in the queue
   Sigma's OWN legacy-held follow-up    →   PROMOTED — gets sdlc:goal, comment says why
     (sdlc:followup + legacy needs-confirm)
   assigned to someone else                 →   ROUTED — membership + a ledger entry for them
   a proposal a HUMAN filed                 →   sdlc:blocking only — that is a real decision
   parked                                   →   sdlc:blocking only — a park is human domain
   a plain third-party issue                →   sdlc:blocking only — never ours to adopt
```

The same policy runs on **both** paths that can mark something as blocking: the moment a block is
recorded, and the sweep that derives `sdlc:blocking` from live "Blocked by #N" references. Wherever
that label goes on, membership goes with it — except in the three rows above, where the issue is
waiting on a person and Sigma must not put it in the queue behind their back.

**Is promoting Sigma's own follow-up skipping a ruling?** No — and the reasoning is
worth having ready, because it is the obvious objection. The old queue existed to stop *speculative*
AI-filed work from consuming the backlog. An issue that real, already-approved work is now stalled
behind is by definition not speculative. And the labels answer it exactly: `sdlc:followup` means
Sigma filed it, the legacy `sdlc:needs-confirmation` means **no human has ever ruled on it**. There is no
decision to override. When a person filed the proposal themselves, `sdlc:followup` is absent, and it
is left alone.

The park that follows names each blocker and what happened to it — so a park is never a bare
"blocked" someone has to investigate from scratch. When every blocker was resolved, the goal is
parked *behind work that is now moving*, and the sweep resumes it the moment that work closes.

Only the residue reaches `/sigma-doctor` and `/sigma-promote`'s `deadlocked` bucket: the cases that
genuinely need a person.

---

## 2b-i. `sdlc:needs-label` — the same shape as `sdlc:blocked`, a different reason

Added in the branching model (#1468). A goal whose **body** declares a unit of work —

```
    Feature: voice-interview
```

— but whose `feature:voice-interview` **label does not exist on the repository** cannot be started:
every later step (which branch to cut from, which unit to record the work under) reads the *label*,
so starting would resolve against the wrong base. Sigma attaches an existing label
automatically; it never **creates** one, because creating mutates the repository's namespace
permanently and a typo in a body marker would mint a label that outlives the unit. That is the
FLAGGED tier of §5, and this label is what makes it queryable.

```
   sdlc:goal + sdlc:needs-label
        │
        │   visible to every sweep, census and mirror   (it keeps membership)
        │   picked by nothing                            (not_eligible_labels refuses it)
        │   reported as "ready to pick" by nothing       (/sigma-triage, /sigma-status)
        │
        └── a human creates the label  ──►  Sigma removes sdlc:needs-label itself,
                                            on the very next pick. Nothing to un-park.
```

**Why not just use `sdlc:blocked`?** They have the same *shape* and different *reasons*, and the
reason is wired into machinery. `sdlc:blocked` means "waiting on another issue", and the auto-unpark
sweep clears it from any goal whose body names a `blocked by #N` whose refs have all closed —
whoever set it, and whyever. A goal held for a missing label that *also* names an already-closed
dependency would be cleared by that sweep and re-held by this one, forever: two label swaps and one
false "blocker closed" comment per cycle. Two reasons need two labels.

**Who clears it:** only the loop, and only by observing that the label now exists. A human never has
to remove it, and `/sigma-unpark` is not its remedy — it is not a park, and the goal is already
approved. Create the label (and the branch), and the goal moves on its own.

---

## 2b-ii. Scope holds — a park with the kind `scope_hold`

Added in the branching model (#1477), and rewritten when the confirmation queue was retired. A goal
that already carried `sdlc:goal` can be set aside by a pick gate; since the queue is gone, the gate
parks it: `sdlc:parked`, `sdlc:goal` given up, and the machine line `sigma-qkind: scope_hold` in the
park comment. (With `ai_filed.triage.enabled` false the gate still writes the legacy
`sdlc:needs-confirmation` instead, and nothing else about this section changes.)

The branching model's registry (`.sdlc/features/`) records, per unit of work, which repos that unit
spans. Discovery flows **one way** — registry → repos, never repo → registry. A goal declaring a
unit from a repo the unit's own `repos` block does **not** list is asking to widen that unit into a
codebase its owner never agreed to touch. So the goal is held:

```
   sdlc:parked + kind scope_hold     (stands alone — sdlc:goal is given up)
        │
        │   the registry is NOT edited: `repos` is unchanged
        │   a ledger entry goes to the UNIT's owner, naming the goal and the repo
        │   one comment lands on the issue itself, saying the same thing
        │
        └── the owner adds the repo to `repos`, then runs /sigma-unpark
```

**Why a park and not `sdlc:needs-label`'s overlay shape.** A missing label self-heals, so membership
is worth keeping. A scope expansion needs a **decision**, and nothing a machine can observe resolves
it. `/sigma-unpark` refuses while the registry still holds the goal, and the auto-unpark sweep skips
this kind, so a release can never send the goal back for exactly one more pick.

**Who clears it:** a human, through `/sigma-unpark`, after adding the repo to the unit.

---

## 2b-iii. Ownership holds — a park with the kind `owner_hold`

Added in the branching model (#1479). §2b-ii asks whether a unit may touch this **repo**; this one
asks whether the person who opened the issue was entitled to put work on this **board** at all. They
are short-circuited against each other: a goal already held for scope is never also held for
ownership, so one pick produces one comment and one ledger entry.

An entry records **two** owners, and they are accountable for different things:

```
   entry.owner                  WHO OWNS THE UNIT — its scope, its tags, whether it is still open
   entry.repos[<repo>].owner    WHO OWNS THAT BOARD — whether the unit may have work filed there
```

**The rule.** An issue carrying `feature:<name>` may be created **directly** only by the unit owner
or by the owner of the repo it is being created in. **Where the two disagree, the board owner wins,
and the more restrictive gate applies.**

**Nobody is ever blocked from raising it.** Only the path changes:

```
   sdlc:parked + kind owner_hold     (stands alone — sdlc:goal is withheld, or given up)
        │
        │   the registry is NOT edited: no grant is ever written by a machine
        │   a ledger entry goes to the BOARD owner (the unit owner where there is no board owner)
        │   one comment lands on the issue itself, saying the same thing
        │
        ├── the owner authorizes the author and runs /sigma-unpark, OR
        └── the owner sets `repos.<repo>.authorized = true` once, and every issue under that
            unit is filed directly from then on — follow-ups included
```

**Two gates, because they are reached by different callers.** `handoff.create_tracked_issue` files
the issue held instead of armed — that covers every issue *Sigma* opens, and it is asked about the
unit the issue **will declare** (a `--label feature:<name>` on the command line counts).
`feature_owner.gate_at_pick` holds an issue *somebody else* opened, at the pick, reading the issue's
**author** and never its assignee or its picker.

**The grant is per unit, not per issue.** Per-issue approval would mean promoting essentially every
goal on a boundary unit forever. **Absent means false**, and only a real boolean `true` grants.
Revoking stops *future* issues; it never retroactively un-approves what is already filed.

**Ownership is set once.** Declared in the registry; failing that, the unit is named from the login
that opened the first goal picked onto it, and it is changed thereafter only by editing the registry.

**Who clears it:** a human, through `/sigma-unpark`, or once and for all through the registry grant.
Leftover holds from before the queue was retired are classified by `migrate-confirmation` from their
flag comment and parked with these same kinds.

---

## 2b-iv. `sdlc:needs-unit` — §2b-i's shape, for a goal declaring no unit at all

Added with the branching model's no-dangling-goal rule (#2263, design #2253). §2b-i's
overlay fires when a goal's **body declares** a unit whose **label doesn't exist yet**. This one
fires on the opposite input: the goal declares **no unit anywhere** — no bare `Feature:` line in the
body, no `feature:*` label — on a repository that has opted into requiring one.

**OPT-IN, unlike `sdlc:needs-label`.** §6 of `docs/branching-model.md` has always guaranteed that a
goal declaring no unit bases on `work.base` exactly as before — that guarantee is about the BASE
only, and it still holds unconditionally. Whether the goal is *picked* at all is a separate axis,
gated on TWO conditions together: `discovery.no_dangling_goal.enabled` is `true`, **and**
`.sdlc/features/` exists. Applying this to a repository that never adopted the branching model would
set aside its entire backlog — every issue declares no unit on a repo that has never heard of one —
so both conditions are required, not either alone. Off (the default), or on an unadopted repo, a
goal declaring no unit is picked exactly as it always was, down to the byte.

```
   sdlc:goal + sdlc:needs-unit
        │
        │   visible to every sweep, census and mirror   (it keeps membership)
        │   picked by nothing                            (not_eligible_labels refuses it)
        │   reported as "ready to pick" by nothing       (/sigma-triage, /sigma-status)
        │
        └── a human declares a unit  ──►  Sigma removes sdlc:needs-unit itself,
                                           on the very next pick. Nothing to un-park.
```

**Why not `sdlc:needs-label`?** Same reason §2b-i gives for not reusing `sdlc:blocked`: same shape,
different reason, and the reason is what `resume_needs_label`/`resume_needs_unit` each key their own
sweep on. `needs-label` means "a unit is declared and its label is missing"; `needs-unit` means "no
unit is declared at all". An issue whose body is edited from one state into the other needs the
overlay to *change*, not to persist under a label that no longer describes it — sharing one label
would make that transition unobservable.

**The alternative: a configured catch-all, and Epic #2260's completion work REVERSED what that
means (2026-09-10, design #2253 D-6).** A repository may configure
`discovery.no_dangling_goal.core` to a unit name (checked clear of collisions on that repo first)
instead of leaving it empty. Design #2253's original D-6 made that name a **sentinel** —
never a real `feature:*` label or branch, only a comment naming it, specifically so writing it would
not cause the very next read to see a genuine declaration and try to cut a branch nobody wanted.
That call was reversed: `core` is now bootstrapped once as a **real** registered unit (a real
`feature:core` label, a real `feature/core` branch, a real registry entry) — full contract in
`docs/branching-model.md` §18 — and a goal declaring no unit is no longer simply attributed to it by
comment. It is run through a strict, 4-tier classification chain instead
(`skills/sigma-loop/scripts/feature_classify.py`):

1. a single existing unit (open or closed — a closed match **reopens** it, see the Reopening
   section below) → attach to it, not the catch-all at all;
2. more than one plausible unit, or root/base-level work → the **configured** catch-all — attached
   as a real label now, not merely commented;
3. an identifiable but unregistered component with concrete evidence → falls back to THIS section's
   own `sdlc:needs-unit` set-aside, the suggested name threaded into the flag comment;
4. genuinely unknown → the new `sdlc:needs-triage` overlay, §2b-v below.

**Honest limitation, stated where an adopter configuring `core` will read it: tiers 1 and 3 are not
reachable via any real invocation today.** Both places this chain runs default to a judge that never
guesses — it abstains, every time, because nothing in the kit supplies a real one yet. Abstaining
routes every undeclared goal through tier 2, so *in practice*, configuring `core` today still means
every undeclared goal lands on the configured catch-all — the same outcome the original sentinel
produced, now via a real, reversible label attach that other tooling (the board, the registry) can
actually see, rather than a comment nothing checks. `docs/branching-model.md` §18 is the full
statement of that gap and what would close it.

**Who clears `sdlc:needs-unit`:** only the loop, and only by observing that the issue now declares a
unit — the same "only the loop, only by observing the condition resolved" rule §2b-i states for its
own sweep. `/sigma-unpark` is not its remedy, for the identical reason: it is not a park, and the
goal is already approved.

---

## 2b-v. `sdlc:needs-triage` — tier 4 of AI-judgment classification, and the one overlay with no self-heal

Added with Epic #2260's completion work (#2364). Where `sdlc:needs-unit` means "no unit is declared
anywhere", `sdlc:needs-triage` means something narrower and rarer: a goal that went through every
tier of the classification chain `docs/branching-model.md` §18 describes — a single confident match,
the configured catch-all, an identifiable-but-unregistered component — and **matched none of them**.
Expected to be rare by design; §18 is where the mechanism that produces it lives.

```
   sdlc:goal + sdlc:needs-triage
        │
        │   visible to every sweep, census and mirror   (it keeps membership)
        │   picked by nothing                            (not_eligible_labels refuses it)
        │   reported as "ready to pick" by nothing       (/sigma-triage, /sigma-status)
        │
        └── NO automatic sweep clears this one. A human declares a unit or attaches an
            existing `feature:<name>` label by hand, and ALSO removes `sdlc:needs-triage`
            itself — both gestures, not one.
```

**This is the one overlay in this table with no self-healing sweep of its own**, and that is
deliberate, not an oversight shared by accident with `sdlc:needs-label`/`sdlc:needs-unit`. Those two
mean "one specific, checkable condition is missing" (a label; any declaration at all), so a sweep
that re-checks the one condition is a small, bounded thing to build. `sdlc:needs-triage` means
"every automatic classification tier failed", which is not a single condition a sweep can re-poll —
resolving it is a genuinely human judgment call, the same one the classifier itself could not make.
So unlike its two siblings, removing this label is entirely a human's own gesture: it does not come
back on its own once a unit is declared, because nothing watches for that on its behalf. The
reconciler's census (`reconcile.py`) still reports it as a bucket, separately from `clean` — not
because it is corruption, but because it names something a human still owes the issue.

---

## 2c. "Do we still need `sdlc:followup`?"

Yes, though it is the least load-bearing of the three annotations. The three answer different
questions and have different lifetimes:

```
   needs-confirmation (legacy) a STATE    retired; a leftover is migrated away
   sdlc:blocking             DERIVED      comes and goes with live references
   sdlc:followup             PROVENANCE   permanent, never removed
```

Once an issue is armed, any legacy `sdlc:needs-confirmation` is gone — and nothing else on the issue records
that a machine found it rather than a person asking for it. That is `sdlc:followup`'s whole job.

It has exactly **one** machine use, and it is the one that matters: the census uses it to enumerate
the **population** of issues Sigma manages.

> A search can never return an issue whose defect **is** a missing label.

To find issues that lost their lifecycle label, you have to be able to list managed issues by
something *other* than a lifecycle label. `sdlc:followup` is one of those anchors. Remove it and the
census goes blind to precisely the orphans it exists to find.

---

## 3. The normal life of a goal

```
                    ┌──────────────────────┐
   AI files it ───► │ sdlc:needs-confirm.  │
                    └──────────┬───────────┘
                     human removes the label
                               ▼
   human files it ───►   ┌───────────┐
                         │ sdlc:goal │ ◄──────────────────────┐
                         └─────┬─────┘                        │
                       loop picks it up                       │
                               ▼                              │
                    ┌────────────────────────┐                │
                    │ sdlc:goal              │                │
                    │ + sdlc:in-progress     │                │
                    └───┬──────────┬─────────┘                │
                        │          │                          │
             finishes   │          │  hits a dependency       │
                        ▼          ▼                          │
                  ┌──────────┐  ┌──────────────────────┐      │
                  │  CLOSED  │  │ sdlc:goal            │      │
                  └──────────┘  │ + sdlc:blocked       │      │
                                └──────┬───────────────┘      │
                                       │  blocker closes      │
                                       └──────────────────────┘
                                          (sweep resumes it)

   Declares a feature: unit whose label does not exist yet:
                                      ──►  sdlc:goal + sdlc:needs-label   (§2b-i)
                                           cleared by the loop once the label exists

   At any point a human can park it:  ──►  sdlc:parked  (ALONE — membership is given up)
   (nothing automatic ever un-parks a human's park)
```

Three rules worth remembering:

- **There is no confirmation queue.** Triage arms or parks at filing; a leftover legacy
  `sdlc:needs-confirmation` is moved off by `/sigma-promote migrate-confirmation`.
- **`sdlc:blocked` resolves itself.** It rides *alongside* `sdlc:goal` — the goal stays in
  Sigma's world, just unpickable — and when the blocking issue closes, a sweep drops the overlay
  and the goal returns to the queue.
- **`sdlc:parked` does not.** A park is a human decision and only a human undoes it.

---

## 4. What was actually broken

### 4a. A state change was three API calls, not one

Parking a goal meant: remove `sdlc:goal`, remove `sdlc:in-progress`, add `sdlc:parked`. Three
separate calls, each independently able to fail, each failure silently swallowed.

```
   BEFORE                                    AFTER
   ────────────────────────────────          ─────────────────────────────
   remove sdlc:goal          ✓               ┌───────────────────────────┐
   remove sdlc:in-progress   ✗ (502)         │ ONE request:              │
   add    sdlc:parked        ✗ (502)         │   +sdlc:parked            │
                                             │   −sdlc:goal              │
   result: {sdlc:in-progress}                │   −sdlc:in-progress       │
           ↳ an illegal state                └───────────────────────────┘
                                             either all of it lands, or
   Worst case: NO labels at all.             none of it does.
   Nothing can find that issue again —
   every search is "find label X".
```

Measured on our own backlog: **126 of 605 issues (about 1 in 5)** were sitting in a state the system
cannot name. That is where they came from.

The new version also **retries** (label writes previously got zero retries, while drawing on exactly
the API quota we exhausted six times in one night), and **fails in the safe direction**: if the write
does not land at all, the goal stays pickable and simply gets redone — rather than vanishing.

> **The governing principle, in one line:**
> *every failure should converge on redoing a bit of work — never on an issue nobody can find.*

### 4a-i. The second route into that same illegal state — a claim on a ticket that is no goal

§4a is about a state change that half-landed. This one lands perfectly and is still wrong: it wrote
`sdlc:in-progress` onto an issue that never carried `sdlc:goal` at all, producing the identical
orphan — `{sdlc:in-progress}` alone, findable by nothing.

**`sdlc:in-progress` has exactly one writer.** `GitHubSource.mark_in_progress`, reached only from
`loop.py:_claim`. Every other mention of the label anywhere in the kit *removes* it. So the label is
not a state anything sets directly — it is the visible half of a **claim**, and a claim is only
legitimate on a member of the backlog.

`_claim` has three callers, and until #2029 only two of them had established membership first:

| caller | how membership was known |
|---|---|
| `_next()` — the pick | the query that found the goal was `--label sdlc:goal` |
| the `claim` verb | the same queue |
| `_ensure_claimed` — arming (#1962) | **it was never asked** |

Arming exists so a goal named directly (`/sigma-goal <issue>`, direct dispatch) still emits a
`claimed` event instead of reaching `record` unclaimed. It fires on the first `note`, `verify` or
`agent-start` for whatever issue number it is handed, and its only guard was the ledger opt-in —
which answers *"may Sigma record anything on this repo?"* and never *"is this ticket a goal?"*.

**Measured on this repo, 2026-09-01.** `sigma-goal-review` closes by commenting, through `loop.py
note`, on the two tickets that are never goals by construction: the Dossier (`story`) and the
Spec/Epic (`epic`). Story #2017 and epic #2020 were labelled `sdlc:in-progress` two seconds before
each comment landed (`11:34:41Z` label / `11:34:43Z` comment; `11:35:02Z` / `11:35:04Z`), and
`doctor.py`'s orphan scan then reported both — two orphans out of four open in-progress issues.

Two consequences neither of which is visible on a board:

- **On a real but unpicked goal it is a silent backlog deletion**, bounded by the lease. The pending
  query excludes anything carrying `sdlc:in-progress` unconditionally, so a `note` from a
  plan-review or a retro on a goal nobody had picked removed it from the queue until the stale-claim
  reclaim returned it 12 hours later — and that reclaim posts a public "claimed but not started"
  comment onto a ticket nothing ever claimed.
- **A REJECT verdict was writing a label.** `docs/dossier-pipeline.md` §7g and `AGENTS.md` both
  promise a rejected design writes nothing but a comment. It wrote a comment *and* a label.

**The fix, and the posture that matters more than the fix.** Arming now asks the membership question
before the one step of a claim that leaves this machine (`loop.py:_arming_may_mark`), and it fails in
**opposite directions on purpose**: the local record — ledger, action log, session registry — is
written whatever the answer, because that is this machine's honest observation that work happened;
the external label is written only on a definite yes, and a read that failed is not a yes. Cost is
one `gh issue view` per goal per machine, behind the same marker the ledger read already sits behind.

Local mode is untouched, and that is the correct answer rather than a concession: a local goal
resolves to a file under `.sdlc/goals/`, so membership is inherent in the argument and there is
nothing outside this machine to protect.

### 4b. On a project board, the labels were being ignored

This is the one that will matter most to anyone using the board UI.

```
   BEFORE — the board decided everything
   ┌────────────┐
   │   Ready    │  ← anything dragged here got picked...
   │ ───────────│
   │ #1 goal    │  ✓ picked
   │ #2 parked  │  ✓ picked   ← should NOT have been
   │ #3 needs-  │  ✓ picked   ← the old hold label did nothing
   │    confirm │
   │ #4 (no     │  ✓ picked   ← not even a goal!
   │    labels) │
   └────────────┘

   AFTER — labels decide ELIGIBILITY, the board decides ORDER
   ┌────────────┐
   │   Ready    │
   │ ───────────│
   │ #1 goal    │  ✓ picked
   │ #2 parked  │  ✗ skipped (and says so, loudly)
   │ #3 needs-  │  ✗ skipped
   │    confirm │
   │ #4 (no     │  ✗ skipped
   │    labels) │
   └────────────┘
```

We proved this on a real throwaway board (a throwaway test repository), five issues, all five
cards dragged to `Ready`:

| | picked |
|---|---|
| old code | `1, 2, 3, 4, 5` — **all of them** |
| new code | `1, 5` — only the genuinely eligible |

**Practical change for the team:** dragging a card into `Ready` is no longer sufficient on its own.
The issue must also carry `sdlc:goal`. Card position still sets the *order* — drag-to-prioritise
works exactly as before.

---

## 5. The reconciler — how drift gets repaired

Even with the write path fixed, labels still drift: people edit them by hand, older installs of the
plugin still write the old way, and an issue closed by a merged PR never runs Sigma's own
completion step at all.

So there is now a reconciler with **three tiers**, and the tiering is the important part.

```
  ┌────────────────────────────────────────────────────────────────────┐
  │ 1. CENSUS  (read-only, always safe)                                │
  │    Lists every issue Sigma is responsible for and classifies   │
  │    it. Reported by /sigma-doctor.                                   │
  └────────────────────────────────────────────────────────────────────┘
                                 │
  ┌──────────────────────────────┼─────────────────────────────────────┐
  │ 2. AUTOMATIC  — fixed with no human. Two cases, TWO SEPARATE       │
  │    off-by-default gates:                                           │
  │      a) a CLOSED issue still carrying a stale label — an ACTIVITY  │
  │         overlay, or `sdlc:goal`.        NEVER `sdlc:parked` or    │
  │         the legacy needs-confirmation: a human's decision.         │
  │         Safe because a closed issue can never be picked, has no    │
  │         worker, and waking nothing up. `reconcile.mode`.           │
  │      b) an OPEN issue whose MULTI_LABEL/ZERO_LABEL correction the  │
  │         timeline oracle resolves decisively TO `sdlc:goal` — full  │
  │         stop, never to `sdlc:parked` or the legacy hold label,     │
  │         and never touching an overlay (structurally impossible —   │
  │         see below). Re-checked fresh, a second time, immediately   │
  │         before the write, because a human can act on the same      │
  │         issue in the gap between census and apply.                 │
  │         `reconcile.open_issue_mode` (#2295) — its OWN gate,        │
  │         independent of (a)'s `reconcile.mode`.                     │
  └────────────────────────────────────────────────────────────────────┘
                                 │
  ┌──────────────────────────────┼─────────────────────────────────────┐
  │ 3. PROPOSED  — written to a file WITH its evidence; a human runs   │
  │    one command to approve. Everything else touching an OPEN issue: │
  │    an AMBIGUOUS resolution, or a decisive one that does NOT land   │
  │    on `sdlc:goal` (e.g. resolves to `sdlc:parked` instead).        │
  └────────────────────────────────────────────────────────────────────┘
                                 │
  ┌──────────────────────────────┼─────────────────────────────────────┐
  │ 4. FLAGGED  — surfaced, never touched. Anything needing judgement. │
  └────────────────────────────────────────────────────────────────────┘
```

**Case (b)'s safety condition, stated precisely, because an earlier draft of it was weaker and
wrong:** the promotion rule is `winner == sdlc:goal`, full stop — not "doesn't remove
`sdlc:in-progress`". The two are not the same thing: an un-claimed, already-re-parked issue carries
no `sdlc:in-progress` to remove, so the weaker rule would have let its `sdlc:goal` membership be
silently stripped unattended. `winner == sdlc:goal` is narrower on purpose, and it has a structural
payoff worth naming: the reconciler only ever adds an overlay label to a correction's removal list
when the winner is something *other* than `sdlc:goal` — so once condition (b) holds, no promoted
correction can *ever* touch `sdlc:in-progress` or `sdlc:blocked`, by construction, not by a
case-by-case check. `reconcile.open_issue_mode` ships **off by default** and is deliberately a
separate switch from `reconcile.mode` (case (a)'s existing gate) — a repo that already has
`reconcile.mode: "on"` does not silently start auto-applying to open issues the moment it adopts a
newer Sigma; it opts in to case (b) explicitly.

### When it runs

The AUTOMATIC tier is throttled by a TTL watermark (`discovery.reconcile.ttl_minutes`, default 60
minutes) — but a TTL is only ever *checked*, not scheduled; something still has to call it. Two
independent callers do, both gated by the same `discovery.reconcile.mode`/`ttl_minutes`, so there is
one config an operator has to understand, not two:

- every `/sigma-loop` pick (`next`/`next-batch`), which is where this always ran; and
- a wall-clock heartbeat that fires even when nobody is actively picking — `watch_daemon.py`'s own standing
  tick (`reconcile_tick.py`, threaded in alongside `agent_watch.py`/`comment_watch.py`), or, for an
  operator who does not run `watch_daemon.py` at all, the `skills/sigma-loop/AUTOWATCH.md` Desktop/CLI adapter pattern.

Before this second caller existed, an idle repo — nobody driving `/sigma-loop` — got zero automatic
correction no matter how short the TTL was set, because nothing was calling the function that checks
it. Off (the default) is unchanged either way: no `gh` calls, from either caller, until this is
turned on. Case (b)'s own gate (`reconcile.open_issue_mode`) is throttled and triggered the
identical way, riding the same TTL watermark and the same two callers.

### How it knows which label is right

GitHub records every label add and remove, with a timestamp. Replaying that history tells us what
actually happened.

```
  Issue #226 — carried BOTH sdlc:goal and sdlc:parked

    2026-08-11 12:55   + sdlc:goal
    2026-08-13 10:45   − sdlc:in-progress
    2026-08-13 10:45   + sdlc:parked        ← 45.8 hours later

    Verdict: PARKED. The park is the later, separate intent;
             sdlc:goal is leftover from a removal that never landed.
```

This matters because a previous audit looked at #226 and **could not tell**, so it correctly refused
to guess. Now it can — and #226 has since been corrected on exactly this evidence.

**And it still refuses when it genuinely should.** Two labels added *within 60 seconds* of each other
are one interrupted operation, not two decisions — their order means nothing, so the answer is "a
human needs to look at this". Real evidence backs the threshold: genuine competing intents on our
boards were separated by **1.1 hours at minimum**, while a single operation takes about **4 seconds**.

```
     ← one interrupted operation →│← two real decisions →
    0s ······ 4s ·············· 60s ············· 1.1h ·············►
         (refuse — ambiguous)     │   (decide — later one wins)
```

---

## 6. The blocker chain — what the loop does on its own

This is the flow the labels exist to make possible, and it is **on by default**.

A goal that hits a dependency does not stop. It files the work it needs, steps aside, and comes back
when that work lands:

```
   goal #42  ──files 3 blocking follow-ups──►   #7      #8      #9
        │                                        │       │       │
        │   #42:  sdlc:goal + sdlc:blocked        │  each: sdlc:goal + sdlc:blocking
        │   still visible to every sweep,         │  and each SORTS AHEAD of
        │   just not pickable                     │  everything else in the queue
        │                                         ▼       ▼       ▼
        │                                      worked · worked · worked
        │                                        │       │       │
        └────────────── all three closed ────────┴───────┴───────┘
                              │
                              ▼
             sdlc:blocked drops · card returns to Ready
             #42 is picked up again and carries on
```

**It recurses.** If #7 turns out to need something itself, #7 blocks in exactly the same way and the
chain drains innermost-first. Nothing special happens at depth — it is the same transition each time.

**All three must close** before #42 resumes. Not one at a time.

### Where it stops

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │  ENDS NATURALLY   a follow-up that files no blocker of its own is    │
   │                   just ordinary work. The chain has a bottom.        │
   ├──────────────────────────────────────────────────────────────────────┤
   │  PARKS FOR YOU    a blocker Sigma may not resolve on its own:    │
   │                     · a proposal a HUMAN filed                       │
   │                     · a blocker somebody already parked              │
   │                     · a third-party issue that was never ours        │
   │                   The park names WHICH blocker and WHAT it needs.    │
   └──────────────────────────────────────────────────────────────────────┘
```

That second box is the important one for the team: a park from this flow is never a bare "blocked".
It tells you the issue number and the one thing that would unstick it.

### The one thing the machine will never do

```
   sdlc:blocked      →  resumed automatically when the blockers close      ✓
   sdlc:needs-label  →  resumed automatically once the label exists        ✓
   sdlc:parked       →  NEVER resumed automatically                        ✗
```

A park is a person's decision, so only a person reverses it (`/sigma-unpark`). That is precisely what
lets the automatic sweep be on by default without ever overriding somebody — it can only undo the
loop's own state, never yours.

### Turning it off

Both halves are independent, and both are on unless you say otherwise:

```json
"discovery": {
  "auto_unpark": { "mode": "off" },        // stop resuming blocked goals automatically
  "blocking_priority_override": false      // stop blockers sorting first
}
```

Off is a real choice, not a tidy-up: with the sweep off, a blocked goal waits for a human even after
its blockers close.

---

## 7. The two moves that are yours, not the machine's

Two labels are a human's to move, and each now has a command — because doing it by hand means two
edits against an API with no transactions, and getting one of them wrong is exactly how the states
in §4a happen.

### Approving a stuck issue — `/sigma-promote`

Sigma no longer queues what it files for confirmation: triage arms it or parks it. What is left for
this command is an issue still carrying the legacy `sdlc:needs-confirmation` (a leftover, or a repo
running with triage off) and anything blocking other work.

```
   THE GESTURE PEOPLE EXPECT          THE GESTURE THAT IS CORRECT
   ─────────────────────────          ───────────────────────────
   + add sdlc:goal                    swap: add goal, remove the legacy label
        ↓                                     ↓
   {goal, needs-confirmation}         {goal}
        ↓                                     ↓
   not a state we have a name for     approved
```

`/sigma-promote` does both halves as **one** swap, moves the board card to `Ready` with it, and
leaves an audit comment. `/sigma-promote list` shows three buckets:

```
   ┌──────────────────────────────────────────────────────────────────┐
   │ deadlocked  blocking other work, pickable by nothing  ← do first │
   │ awaiting    a leftover legacy needs-confirmation label           │
   │ drift       already carrying BOTH labels  ← one removal fixes it │
   └──────────────────────────────────────────────────────────────────┘
```

There is also `demote`, the exact inverse — promoting something by mistake now has an undo.

### Getting a parked goal moving again — `/sigma-unpark`

**A park is a question nobody answered.** Un-parking without answering it is the worst option
available:

```
   just flip the label                 answer first, then flip
   ───────────────────                 ───────────────────────
   goal picked up                      a person is asked, in plain English,
        ↓                              what is actually blocking it
   agent rediscovers the                    ↓
   same obstacle                       answers recorded ON the issue
        ↓                                    ↓
   parked again, with a new            goal picked up WITH the context
   comment nobody reads                     ↓
        ↓                              it can actually finish
   (repeat forever)
```

So the answers are the artifact; the label change is a consequence of having them. The flow:

```
   list ──► brief ──► ask, one question at a time ──► decide ──► record
            │                                        │
            │  why it was parked, what kind of       │  "start it again"  → unpark, card to Ready
            │  park, which blockers are STILL open,  │  "leave it parked" → keep, with the
            │  what a previous round already         │                      reasoning attached
            │  answered (so nothing is re-asked)     │
```

The questions come from a fixed bank, not improvised per run, and every one of them obeys rules a
test enforces: one sentence, name the concrete thing, no Sigma jargon.

> *"#7 is still open. Do you want to wait for it, work around it, or drop this?"*
>
> …not *"The dependency reason_class park indicates an unresolved external blocker — how would you
> like to proceed with respect to the blocking relationship?"*

Answers are written into the issue **body**, not just a comment, because the body is what the next
agent actually reads. That created one trap, and it is worth knowing it was closed deliberately:

```
   A perfectly ordinary answer:
       "waiting on the design sign-off, see #1234"
                 └──────────────┬──────────────┘
                    reads as a BLOCKER to the scanner
                    ("waiting on" … "#1234")
                          ↓
       phantom dependency planted in the body of the goal
       that was just unblocked → re-parked on the next pick

   Fix: the recorded block is fenced, and the scanner skips that span.
        A real "Blocked by #99" anywhere else is still caught.
```

---

## 8. Two safety properties worth knowing

**Nothing automatic ever overrides a human.** A `sdlc:parked` set by a person is never un-parked by
the machine. And where a project uses the *same* label name for both a human "do not touch" hold and
Sigma's own machine-managed blocked state — which one of our adopters does today — `/sigma-doctor` now
reports it as a configuration error, because a reconciler cannot tell those two apart.

**Two agents can never share a worktree.** Previously, reclaiming a goal from an agent that was
actually still running could hand its working directory to a second agent, whose next commit would
sweep up the first one's half-finished work. Three fixes close it: a liveness heartbeat (so a
long-running agent stops looking dead), reclaims no longer erase that liveness record, and starting
work now refuses a directory another live process is registered on.

---

## 8a. The board's Status column — what each option means

Labels decide **eligibility**; the board decides **order** (§4b). But the board is also the surface
people actually read, so each Status option has to mean one thing, and it has to agree with the
labels. This is the mapping the loop writes:

| Status | Written when | Carries | Who moves it next |
|---|---|---|---|
| **Backlog** | filed, not yet in the sprint | `sdlc:goal` | a human, by dragging it to Ready |
| **Ready** | queued for the loop to pick | `sdlc:goal` | the loop, when it picks it |
| **In progress** | actively being worked | `sdlc:goal` + `sdlc:in-progress` | the loop |
| **In review** | PR open, review/verification running | `sdlc:goal` + `sdlc:in-progress` | the loop, on merge |
| **Done** | merged and recorded | *no* `sdlc:*` label | nobody — terminal |
| **Blocked** | a real dependency was found | `sdlc:goal` + `sdlc:blocked` | **the loop, by itself**, as soon as the blocker closes |
| **Blocked** | declares a `feature:` unit whose label does not exist | `sdlc:goal` + `sdlc:needs-label` | **the loop, by itself**, as soon as a human creates that label |
| **Parked** | a human checkpoint | `sdlc:parked` **alone** | **only a human**, via `/sigma-unpark` |

The last two rows are the distinction the whole release exists to draw, and **they are not the same
kind of stop.** A `Blocked` card is still the loop's work — it keeps `sdlc:goal`, every sweep
re-examines it, and it returns to `Ready` on its own the moment the last issue it names has closed.
A `Parked` card has left the machine's world entirely; it is in a human's review queue and stays
there until a person decides otherwise.

Before `Parked` existed, both wrote the single `Blocked` column, so on a board with both options the card
contradicted the labels, and a person reading the board to find what needed them saw self-clearing
blocks mixed in with real decisions. A park now writes `Parked`, falling back to `Blocked` on a board
that has no such option — so nothing changes for a board that predates it.

---

## 9. What this means for you day to day

| If you… | Then… |
|---|---|
| want the loop to pick something up | give it `sdlc:goal` (a board card alone is not enough) |
| want to approve a stuck issue or clear a leftover legacy label | **`/sigma-promote`** — or swap the legacy `sdlc:needs-confirmation` for `sdlc:goal` by hand |
| want a parked goal moving again | **`/sigma-unpark`** — it asks what is blocking before it flips anything |
| want to stop the loop touching something | give it `sdlc:parked` — nothing automatic will undo it |
| use the board to prioritise | keep doing it; card order still decides order among eligible issues |
| see `sdlc:goal` + `sdlc:in-progress` | that is normal, active work — leave it alone |
| see two lifecycle labels at once | that is drift; `/sigma-doctor` will report it |
| want to see the current state | `reconcile.py census .sdlc` — read-only, safe any time |

---

## 10. Moving an issue by hand, without making a mess

Most days you will not touch a label directly — `/sigma-promote` and `/sigma-unpark` exist so you do
not have to. But when you do, these are the gestures that are *correct*, and the ones that quietly
create the drift this release spent its life removing.

### Approving a stuck issue (legacy `sdlc:needs-confirmation` → workable)

```bash
/sigma-promote                 # lists what is stuck; approve from there
```

Doing it by hand: **remove the legacy `sdlc:needs-confirmation`, then add `sdlc:goal`.** For many
leftovers, run `/sigma-promote migrate-confirmation` once.

> ⚠️ **Adding `sdlc:goal` on its own does nothing.** The issue then carries *both* labels, and both
> queue paths refuse it — so it looks approved and is picked by nothing. That is the single most
> common way to create drift, and it is the intuitive gesture, which is why the command exists.
> `/sigma-promote list` has a `drift` bucket that finds anything already in this state.

Not ready to approve it? Leave it alone: the legacy label is inert, and the loop never picks it.

### Getting a parked goal moving again (`sdlc:parked` → workable)

```bash
/sigma-unpark                  # asks what was blocking, records the answers, then unparks
```

Doing it by hand: **remove `sdlc:parked`, then add `sdlc:goal`.**

> ⚠️ **Adding `sdlc:goal` without removing `sdlc:parked` leaves it unpickable** *and* carrying two
> membership labels — worse than where it started.
>
> And the reason to prefer the command is not tidiness: a park is a *question nobody answered*.
> Flip the label without answering it and the next agent rediscovers the same obstacle and parks it
> again. `/sigma-unpark` writes the answers onto the issue so that does not happen.

### Parking something yourself

Add `sdlc:parked` and remove `sdlc:goal`. Nothing automatic will ever undo it — that is the
guarantee. If it also carries `sdlc:in-progress` or `sdlc:blocked`, remove those too: an overlay is
only ever legitimate alongside `sdlc:goal`.

### Closing an issue

Just close it. There is no done-label.

**The loop strips `sdlc:goal` itself** when it completes a goal, and the reconciler
treats a leftover `sdlc:goal` on a closed issue as stale and removes it. So a Done issue reaches
"no lifecycle label" on its own, and you no longer have to remember.

Closing an issue **by hand or via a PR's `Fixes #N`** is the one path the loop never sees; those are
cleaned by the reconciler on its next sweep (it deliberately waits 24h after a close before touching
anything, so it can never catch an issue mid-transition). Strip the label yourself if you want it
tidy immediately.

**`sdlc:parked` and the legacy `sdlc:needs-confirmation` are NOT stripped, closed or not** — they
record a human's decision (a park, a withheld approval), and nothing automatic undoes those. Provenance
labels (`sdlc:followup`, `sdlc:dependency`) stay too: they are history, and the reconciler's census
uses them to find issues that have lost their lifecycle label.

### Reopening an issue

**Add `sdlc:goal` back yourself.** A closed issue has no membership label, so reopening one leaves it
with nothing — the loop will never pick it up, and **nothing will tell you that**: an issue with no
`sdlc:*` label at all classifies as `clean`, so it is not flagged as drift either. It simply sits
there.

That is deliberate rather than an oversight. Re-entering the queue is a decision, and an issue
silently returning to it because someone reopened a ticket is exactly the kind of unrequested work
the membership model exists to prevent. But it does mean the gesture is yours: **reopen, then add
`sdlc:goal`** (or `/sigma-promote` it, which does both halves atomically).

**A different "closed", with one narrow, deliberate exception (#2362).** Everything above is about
an *issue's* `sdlc:goal` membership, which this section says is always a human's gesture, with no
exception. A `feature:<name>` **unit** carries its own, separate closed state — `open: false` in
its `.sdlc/features/` registry entry (`docs/branching-model.md` §13) — and that is a different axis
entirely: an issue can be reopened while its unit stays closed, and vice versa. For a unit, the rule
this section states for issues has held without exception until now: `feature_registry.
resolve_open_unit` answers `None` for a closed unit precisely so nothing built on it is ever
tempted to retarget one, and reopening one has always meant a human hand-edit. Epic #2260's
tier-1 auto-classifier (slice B, **shipped** — `skills/sigma-loop/scripts/feature_classify.py`,
`docs/branching-model.md` §18) introduces the one narrow carve-out to that: it may reopen a closed
unit **without** a human editing anything, but only when its own AI-judgment match is confident. It
does so via the sibling resolver `resolve_open_unit` cannot serve for this — `feature_registry.
resolve_any_unit`, which matches a unit regardless of `open` state — never by loosening
`resolve_open_unit` itself. Nothing about issue-level `sdlc:goal` reopening changes.

**Shipped and tested, but not yet reachable by a real pick.** Both places tier 1 could fire default
to a judge that never guesses a confident match — it abstains, unconditionally, because nothing in
the kit supplies a real one yet (§2b-iv above has the full statement of that gap). So today this
carve-out is exercised by this repository's own tests, not by production traffic: no issue is
actually reopened this way until a live judge is wired in. The exception stated above is the
contract once one is; it is not yet a claim about what happens on a real board.

### The one-line summary

| you want | do this | do NOT do this |
|---|---|---|
| clear a leftover legacy hold | swap legacy `needs-confirmation` for `goal`, or `/sigma-promote migrate-confirmation` | add `goal` and leave the legacy label |
| resume a park | `/sigma-unpark`, or remove `parked` + add `goal` | add `goal` and leave `parked` |
| park something | add `parked`, remove `goal` + any overlay | add `parked` and leave `goal` |
| hand work to the loop | add `goal` | drag a board card and stop there |
| stop the loop touching it | add `parked` | remove `goal` and leave it bare — that is an orphan |

**Never leave an issue with no `sdlc:*` lifecycle label at all.** It is not "out of the system", it
is *invisible* to it — the census exists specifically to hunt those down.

---

## 11. Honest limitations

- **Two people on two machines can still start the same goal.** GitHub labels take a second or two to
  propagate and there is no shared lock available. The consequence is duplicated effort, never lost
  work. Not fixed, and not fixable without infrastructure Sigma does not have.
- **Older installs of the plugin still write the old way.** A clean board will drift again if
  something out-of-date is still pointed at it. Worth confirming everyone is current.
- **Semantic mistakes are still possible.** If an issue is labelled correctly but the label is simply
  the *wrong* one for reality, no amount of atomicity catches that — that is what the census and
  `/sigma-doctor` are for.

---

## 12. Labels outside the `sdlc:` prefix — `priority:*`, `model:*`, and yours

Everything above is about the `sdlc:*` lifecycle labels. Those are the only labels Sigma
**defines**: `_ensure_labels` seeds exactly ten — the three membership labels and the seven
overlays in §2 (`sdlc:designed` joined the table in #1826, `sdlc:needs-unit` in #2263,
`sdlc:needs-triage` in #2364) — and
nothing else. Any other label on your board is yours.

Two non-`sdlc:` prefixes appear in Sigma's own output, and they are **not** the same kind of
thing as each other:

| Prefix | Does Sigma define it? | Does Sigma read the value? | What the value means |
|---|---|---|---|
| `priority:*` | no | **yes** | dispatch order. `/sigma-triage`'s own scripts (`triage.py`'s `survey`/`plan`/`enact`) sort waves by it and never write it themselves — `_bucket_hygiene`'s detection stays read-only by contract, same as every other survey bucket. `define.py bump-priority` (branching-model.md §16, opt-in, human-invoked directly by name) is one place Sigma **writes** this label — sets a `feature:<name>` unit's own open members to exactly the unit's chosen tier, never on its own initiative. Since #2266, neither `/sigma-define`'s nor `sigma-goal-review`'s own priority question calls it any more: giving a unit its own priority now records a value on the unit's `.sdlc/features/` registry entry instead (`define.py set-priority`), which never writes this label at all — that value feeds the pick comparator's tie-break, not this table's row. Since #2296, `/sigma-triage`'s own question round is a second, narrower write path: for a picked issue `_bucket_hygiene` flags as missing `priority:*`, its computed `priority_hint` (the P0-P4 rubric applied mechanically to that issue's own title+body) is, when non-ambiguous, applied as a real label by the agent running the skill (`gh issue edit <n> --add-label priority:P<tier>`, one explicit command, before the plan is compiled) — surfaced in the survey, never silent. Still never on the script's own initiative: a genuinely ambiguous hint (two adjacent tiers) is asked about, not guessed |
| `model:*` | no | **no** | nothing. Sigma defines no values and reads none |
| anything else (`area:*`, `feature:*`, your own) | no | no | yours |

### What "yours" guarantees — and the one thing it does not

**A label that already exists on your board is never modified.** Not its colour, not its
description, not by any code path in the kit. This is worth stating because it was untrue until
#1917: every `gh label create` Sigma issued carried `--force`, which on an existing label
overwrites its colour — so filing a single `/sigma-scope` plan that named `priority:P1` and
`priority:P2` repainted both to the kit's own `#d4c5f9`, and `_ensure_labels` repainted the
`sdlc:*` labels themselves on every loop start. Nothing reported it. The flag is gone from all
four sites; `gh` refuses to re-create a label that exists, and that refusal is now the guarantee.

**The one thing "yours" does not mean is that Sigma will never MINT one.** When it is asked to
attach a label you have not created yet — a `priority:*` value from a triage plan, a `--label` you
passed to `handoff track` — it creates it rather than dropping the label or failing the issue.
`feature:*` is the deliberate exception: Sigma attaches one and never creates one, because a
typo there mints a label that outlives the unit — §2b-i, and the refusal is enforced in
`sources.GitHubSource._run`. So the honest summary is: Sigma defines no vocabulary outside
`sdlc:*`, mints only what it was told to attach, and changes nothing that is already there.

### `model:*` does not choose the model

This is the one worth stating plainly, because the name invites the opposite conclusion. **Putting
`model:opus` on an issue does not run that goal on Opus.** No code path reads the value.

The tier a goal actually runs at is decided by `/sigma-model`'s predictor (`predict.py`) reading the
goal's **text**, gated on `"model_selection": "auto"` in `.sdlc/config.json`. Its tiers are
`haiku | sonnet | opus | fable` — the Task tool's tiers, not a label vocabulary. With
`model_selection` off (the default) portable prediction is disabled. Inline work runs at the
session model; a dispatched Codex phase passes `off` to the host resolver, which selects its
versioned ordinary-work mapping. A label cannot influence either outcome, because the predictor takes the goal text and
nothing else. (The one later change to a goal's tier is a review send-back raising it one rung through
`loop.py escalate`; that reads no label either.)

So what *is* a `model:*` label? An annotation, in whatever vocabulary you choose, that `/sigma-triage`
will display in its survey and carry through a plan you write. That is the whole contract. Because
Sigma defines no value set, no value is "wrong" — and because it reads none, no value has an
effect.

### Why there is no recommended value set

There was, briefly and by accident, and it did damage. `/sigma-triage` used to report a missing
`model:*` as a **hygiene gap** — a claim that an issue was not ready to be picked — while the kit
documented no legal values anywhere and its own two hardcoded ones (`model:daily` on decompose
meta-issues, `model:bulk` in a README example) matched neither each other nor the predictor's tiers.
Every adopter met the same instruction with no way to satisfy it correctly, and invented something:
one audited board had grown `daily`, `sonnet` and `haiku`; Sigma's own had five values.

Publishing a recommended set would not have fixed that, it would have entrenched it — a documented
`model:opus` reads as a *setting*, and this document is exactly where a reader is entitled to believe
what it says. The demand was removed instead (#1602). A goal with no `model:*` label is exactly as
enqueue-ready as one with it, `/sigma-triage` no longer flags its absence, and Sigma no longer
attaches a value of its own to anything.

**If you want a tier vocabulary, use one** — pick your values, create the labels yourself in the
colours you want (Sigma will mint one it was asked to attach and has never seen, but it will
never restyle one you made), and triage will carry them. Just do not expect them to steer the
model; that is `model_selection`'s job, and it reads the goal, not the board.

---

*Per-change rationale for everything here is in [CHANGELOG.md](../CHANGELOG.md).*
