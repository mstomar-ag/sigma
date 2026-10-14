# Backlog: local files or GitHub issues

Where goals come from, how status maps onto GitHub, and how blockers and priorities work. Back to the [README](../README.md).

## Your backlog: local files or GitHub issues

Goals live in a backlog — you choose **where**, once, in `.sdlc/config.json` → `discovery.source`.
The loop runs the **same** way for both; only the source of goals and how status is recorded differ.

### Local goal files — default, zero-dep

Goals are markdown files under `.sdlc/goals/NNNN-slug.md`; the loop advances each file's frontmatter
`status: pending → in_progress → done | parked`, in priority order and then filename (`discovery.order: "created"` for strict filename order).

- **Add a goal:** copy `0001-example.md`, bump the number, fill `done_when` (a *checkable* condition).
- **Commit** `.sdlc/goals/`, `.sdlc/project.md`, `.sdlc/config.json`; **gitignore** `.sdlc/state/`
  (machine-written loop state — `/sigma-init` prints this tip).
- **Parked** goals collect in `.sdlc/state/review-queue.md` — your "needs a human" list.
  To resume one, edit its file under `.sdlc/goals/`: change `status: parked` to `status: pending` and the next
  pick offers it again (measured). There is no `/sigma-unpark` for local goals, and nothing removes the goal's
  entry from `review-queue.md`: delete it by hand once the question it records is answered.

Everything stays in your repo; nothing leaves your machine. This is the zero-dependency path.

### GitHub issues — opt-in, needs the `gh` CLI

Treat **GitHub Issues as the backlog** so planning and triage live where your team already works:

```json
"discovery": {
  "source": "github",
  "github": {
    "repo": "",
    "goal_label": "sdlc:goal",
    "project": { "enabled": true, "status_field": "Status" }
  }
}
```

File each goal as an **issue labelled `sdlc:goal`** (the issue body is the goal). The loop maps SDLC
status onto GitHub — both the **issue** and (when the board is enabled) its **Projects card** — so the
backlog mirrors reality:

| SDLC status | On the issue | On the Projects board |
|---|---|---|
| pending (in backlog) | open issue labelled `sdlc:goal` | card set to **Backlog** |
| picked up → Research / Plan / Implement | adds the `sdlc:in-progress` label | card set to **In Progress** |
| Review (the quality cycle) | issue stays open | card set to **QC** |
| done | **closes** the issue with a completion comment | card set to **Done** |
| blocked (waiting on another issue) | adds `sdlc:blocked` and drops `sdlc:in-progress` — **keeps `sdlc:goal`**, so sweeps still see it while nothing picks it | card set to **Blocked** |
| parked (needs you) | comments the reason, adds `sdlc:parked`, and removes `sdlc:goal` so it leaves the queue | card set to **Parked** (**Blocked** if the board has no Parked option) |

So your **review queue = open issues labelled `sdlc:parked`**, and **done = closed issues**;
**re-queue** a parked issue with `/sigma-unpark` (which answers *why* it was parked before flipping
the label — see below). The labels (and `priority:P0`–`P3`) are created before the first pick — by `/sigma-init --github`, `setup.py labels`, and again by `loop.py start` in github mode, which refuses to start, naming the label, if one cannot be created.

#### The label model, in one table

> **Full walkthrough:** [`docs/label-model.md`](label-model.md) — what each label means, why
> it works this way, what was broken before the label model was reworked, and **how to move an issue between states by hand
> without creating drift**. Written for someone who has never read Sigma's internals.

There are **three kinds** of `sdlc:*` label, and mixing them up is what makes the model look
confusing. Each kind answers a different question, and only the first decides what gets picked.

| Kind | Labels | Answers |
|---|---|---|
| **Membership** — at most one at a time, decides eligibility | `sdlc:goal` (in the world), `sdlc:parked` (a human's exit); the legacy `sdlc:needs-confirmation` (retired, inert) is never written unless triage is off | *is this Sigma's, and can it be picked?* |
| **Overlay** — orthogonal, additive, rides *alongside* `sdlc:goal` | `sdlc:in-progress`, `sdlc:blocked`, `sdlc:blocking` | *what is happening to it right now?* |
| **Annotation** — descriptive, never affects picking | `sdlc:followup`, `sdlc:blocking`, `sdlc:dependency` | *where did this come from, what does it hold up?* |

The rule in one line: **anything waiting to be *picked* carries `sdlc:goal`; anything waiting for a
*human* carries only its own label.**

```
   waiting to be PICKED                    waiting for a HUMAN
   ────────────────────                    ───────────────────
   sdlc:goal                               sdlc:parked              (alone)
   sdlc:goal + sdlc:in-progress
   sdlc:goal + sdlc:blocked
   sdlc:goal + sdlc:blocking
```

So `sdlc:goal` alongside `sdlc:in-progress`, `sdlc:blocked` **or `sdlc:blocking`** is correct and
normal. A blocked goal keeps its membership for the same reason a claimed one does: every sweep, census,
mirror and reclaim path queries `--label sdlc:goal`, so an issue that drops it is findable only by
whichever query happens to ask for the overlay — and if that one label is ever lost, the issue is an
orphan with no route back. Eligibility never depended on the removal: a blocked goal is **visible to
everything and picked by nothing**.

**A park is the only exit that gives up membership**, and deliberately: a park is a permanent,
human-owned block whose whole purpose is to sit in someone's `--label sdlc:parked` review queue, and
putting it back into the population every sweep re-examines is the opposite of what it is for. A
parked issue is **human domain** — nothing automatic adds `sdlc:goal` back to it, ever. Only
`/sigma-unpark` does, with a person answering the question that caused the park.

Three things worth knowing, because each of them surprises somebody:

- **`sdlc:goal` + `sdlc:in-progress` together is correct and normal** — it is what actively-worked
  work looks like. `sdlc:goal` is the *membership* predicate (every query the loop makes is
  literally `--label sdlc:goal`); `sdlc:in-progress` is an additive *occupancy* marker, not a state
  the goal moved into. They are two labels on purpose: recovery from a crashed run works by
  *removing* `sdlc:in-progress`, and it can only find the issue because `sdlc:goal` never left. Swap
  them instead and every killed process leaves an issue no query can return.
- **`sdlc:blocking` carries membership with it.** It marks an issue that other work is waiting on —
  and something other work waits on is, by definition, waiting to be *picked*. So wherever
  `sdlc:blocking` is applied, `sdlc:goal` is applied with it. (When
  `blocking_priority_override` is on, that issue also sorts ahead of the rest.) The three exceptions
  are the ones where membership is not Sigma's to grant — a **parked** blocker, a proposal a
  **human** filed, and a plain **third-party** issue: those get the blocking label as information
  and are surfaced for a person, never adopted.
- **`sdlc:followup` is provenance, not a status.** It is stamped on every issue Sigma files
  itself and never removed, so it answers "did a person ask for this, or did the loop find it?"
  after the legacy `sdlc:needs-confirmation` is long gone. Most goals on a mature board carry it — treating it
  as "needs review" would freeze the backlog. Its one machine use is the reconciler's census, which
  needs anchors like this to enumerate managed issues at all (a query can never return an issue
  whose defect *is* a missing label).

#### Approving and unparking

Two labels are the human's to move, and each has a skill so that moving them is one atomic
transition rather than two edits in the GitHub UI that can half-land:

| You want to… | Run | What it does |
|---|---|---|
| approve a stuck issue, or migrate leftovers of the retired queue | **`/sigma-promote`** | adds `sdlc:goal`, removes any legacy `sdlc:needs-confirmation`, moves the card to `Ready` — one swap |
| get a parked goal moving again | **`/sigma-unpark`** | asks what is actually blocking, records the answers **on the issue**, then unparks or keeps it parked with the reasoning attached |

There is no confirmation queue: triage arms or parks at filing, and a leftover legacy label is moved
off by `/sigma-promote migrate-confirmation`. Adding `sdlc:goal` by hand beside such a label leaves
an issue carrying both, which is not a state the model names —
`/sigma-promote list` has a `drift` bucket that finds any issue already in it, and a `deadlocked`
bucket for the unreachable blockers described above.

`/sigma-unpark` records every question and answer into the issue **body** as well as a comment, so
the next agent to pick the goal reads the context inline instead of rediscovering the obstacle and
parking it again. That block is fenced with a marker the blocker scanner strips, so an ordinary
answer like *"waiting on the design sign-off, see #1234"* cannot plant a phantom dependency in the
goal that was just unblocked.

#### A blocked goal is resolved, routed, or parked for a named reason — never silently skipped

Recording that a goal is blocked is only useful if something will eventually work the blocker. When
a block is found — whether Sigma filed the blocker itself or a human typed `Blocked by #N` —
each named blocker is classified and acted on:

| The blocker is… | What happens |
|---|---|
| already `sdlc:goal` | nothing — it is in the queue, and `blocking_priority_override` sorts it first |
| **Sigma's own** legacy-held follow-up (`sdlc:followup` + the legacy `sdlc:needs-confirmation`) | **promoted** — it gets `sdlc:goal` and a comment naming the goal it unblocks |
| assigned to someone else | **routed** — granted membership and recorded in the ledger for its owner |
| a proposal a **human** filed | left alone — that is a real decision; `/sigma-promote` is the route |
| parked | left alone — a park is human-owned; `/sigma-unpark` is the route |
| a plain third-party issue | surfaced, never adopted — it was never Sigma's to work |

Promoting Sigma's own follow-up does not bypass triage: the deny-list still applies, and an issue
that real, already-approved work is stalled behind is by definition not speculative. The label pair
is decisive: `sdlc:followup` means Sigma filed it, the legacy `sdlc:needs-confirmation` means **no
human has ever ruled on it**. When a
person filed the proposal themselves, `sdlc:followup` is absent and it is left alone.

The comment that follows names each blocker and what happened to it, so a block is never a bare
"blocked" a human has to investigate from scratch. When every blocker is now workable, the goal is
not parked at all: it gets the `sdlc:blocked` overlay, keeps `sdlc:goal`, and the auto-unpark sweep
resumes it when that work closes. When one is not (a parked blocker, a human's proposal, a
third-party issue), the goal is parked, and only a person resumes it.

#### The blocker chain, which is what the loop does by default

Filing a blocking follow-up does not stop a goal — it starts a chain, and the loop drains it:

```
   goal #42  ──files 3 blocking follow-ups──►  #7   #8   #9
        │                                       │
        │  #42 becomes sdlc:goal + sdlc:blocked │  each gets sdlc:goal + sdlc:blocking
        │  (still visible, not pickable)        │  and SORTS AHEAD of everything else
        │                                       ▼
        │                                  worked, closed
        │                                       │
        └───────────── all three closed ────────┘
                            │
                            ▼
              sdlc:blocked drops, card → Ready,
              #42 is picked up again and continues

   ...and it recurses: if #7 files its own blocker, #7 blocks the same way and the
   chain simply drains innermost-first.
```

Two rules make it terminate:

- **It ends when nothing is left blocking** — a follow-up that files no blocker of its own is just
  ordinary work.
- **It parks when a blocker genuinely needs a person** — a proposal a human filed, a parked blocker,
  or a third-party issue Sigma may not adopt. The park names which blocker and what it needs,
  so it is never a bare "blocked" you have to investigate from scratch.

**All three blockers must close** before the goal resumes, not one at a time.

Both halves are on by default and each can be turned off independently:

```json
"discovery": {
  "auto_unpark": { "mode": "off" },        // stop resuming blocked goals automatically
  "blocking_priority_override": false      // stop blockers sorting first
}
```

Turning them off is a real choice, not a tidy-up: with the sweep off, a blocked goal waits for a
human even after its blockers close. The cost of leaving them on is two extra `gh issue list` calls
per pick.

#### Caveat: how blockers are detected, and what it gets wrong

A dependency is inferred from prose — `Blocked by: #7`, `depends on #7` — because a human writing in
the GitHub UI should not have to learn a syntax. That inference has both error directions and
**neither is fully solvable with a pattern**:

- **The reliable form is `Blocked by: #N` with nothing between the phrase and the number.** That is
  what every Sigma writer emits, and what you should write by hand.
- Text where the reference sits across a clause boundary — *"waiting on the design sign-off, see
  #1234"* — is **not** read as a dependency (a comma, semicolon or full stop between the two ends
  the match).
- Text where it does not — *"after #40 lands"*, *"this actually needs #9 to land first"* — **is**
  still read as one, incorrectly. Nothing about the shape of those distinguishes them from a real
  dependency; only their meaning does.

Two escapes exist when that misfires. Each is a **comment on the goal's issue**, posted with
`loop.py note` — the helper scripts only print text, they never write to GitHub themselves:

```bash
# 1. dismiss ONE wrong match (kind + ref) found by the pick-time backlog cross-check
TEXT=$(python3 <installed-sigma>/skills/sigma-loop/scripts/backlog_check.py dismiss-text blocked-by 40 "sequencing note, not a dependency")
python3 <installed-sigma>/skills/sigma-loop/scripts/loop.py note .sdlc <goal> "$TEXT"

# 2. exempt ONE goal from the auto-unpark sweep entirely
python3 <installed-sigma>/skills/sigma-loop/scripts/loop.py note .sdlc <goal> "Deliberate checkpoint. <!-- sigma:keep-parked -->"
```

What each one does, and does not do:

- **`dismiss-text` only prints** a sentence ending in `<!-- sigma:dismissed-finding kind=blocked-by
  ref=40 -->`; nothing happens until `loop.py note` posts it. Once posted, the backlog cross-check
  downgrades that exact `(kind, ref)` finding from confident to advisory, so it no longer parks the
  goal at pick — the finding stays visible as an advisory note, it is not deleted. It does not change
  what the auto-unpark sweep or blocker promotion read. **GitHub mode only**: in local mode the note
  lands in `.sdlc/journey/<goal>.md` and the next park says the dismissal was *not applied*.
- **The `sigma:keep-parked` marker** makes the auto-unpark sweep skip that issue before any blocker
  matching, for as long as the comment stays. The sweep only ever resumes `sdlc:blocked` goals (a
  goal labelled `sdlc:parked` is never auto-resumed anyway), so this is for a `sdlc:blocked` goal
  you want held. `/sigma-unpark`'s `keep-parked` decision posts the same marker for you. There is no
  `auto_unpark.py` command for it: that script's only verb is `sweep <sdlc_dir> [--apply]`.

`/sigma-triage`'s `enact` has the mirror-image caveat: it treats any such match as an
already-present marker and skips writing the real one, so confirm edges in its question round. **Setup:** run `gh auth login` once; leave `repo` empty to auto-detect from the git remote,
or set it to `owner/name`.

**What lands on the issue instead of a file.** A local goal carries its state in frontmatter; an issue
has none, so anything a goal file would hold goes on the **issue timeline** as a phase comment — the
**lane** Research sized it into, the blocking questions it raised, and the plan-review findings that
were *rejected* and why. Research and plan-review artifacts themselves stay local working files in
both modes (like the radar digest); the comment is what makes them visible to everyone else on the
board. No extra columns: Research sits inside the same **In Progress** state as Plan and Implement.

**Sharing one board across a team.** Set `discovery.github.assignee` to `"@me"` (or a username) so
each person's loop only picks issues **assigned to them** — several people can run the loop on the same
board without two loops grabbing the same issue. Assign work with GitHub's normal assignee field; the
Projects card sync still shows the whole team's backlog. Empty (the default) = one shared queue, every
open `sdlc:goal` issue in scope.

#### Projects v2 board

With `discovery.github.project.enabled` (off after `/sigma-init` until `--board yes` pins a board), the loop also drives a **GitHub
Projects v2 board**: on first run it finds-or-creates a board titled `<repo> — SDLC`, adds every
`sdlc:goal` issue as a card, and keeps GitHub's **built-in Status field** in sync
(**Backlog → In Progress → QC → Done**, **Parked** for parked, falling back to **Blocked** when the board has no Parked option) as goals move — the table above. The
**QC** card move happens at the Review phase. It needs the `gh` token's **`project`** scope and is
**fail-open**: no scope, or any API error, and the loop simply continues on issues + labels (nothing
breaks). Tune it under `discovery.github.project` — `owner`/`title` (default `<repo> — SDLC`), `number`
(reuse an existing board), `status_field` (the field's name), and `columns` (override the column names
to match an existing board).

**Board back-fill (#1305).** A verb about one issue cards only that issue. Goals missing from the board are carded by the watcher tick, at most `discovery.github.project.backfill_cap` per tick (default 20, `0` = off), and only while GraphQL remaining is at least `backfill_min_graphql` (default 1000; an unreadable limit skips the tick).

**`queue_source`: which one actually decides what gets picked.** Two modes, under
`discovery.github.project.queue_source`:
- **`"status"`** (the default, once the board has a `Ready` option) — **labels decide ELIGIBILITY,
  the board decides ORDER.** A card is picked only if it is in `Ready` *and* its issue carries
  `goal_label` *and* carries no not-eligible label (`sdlc:parked`, `sdlc:blocked`,
  the legacy `sdlc:needs-confirmation`). Among the cards that qualify, board position still sets the order, so
  drag-to-prioritise works exactly as it always has. A card anywhere other than `Ready` — `In
  Progress`, `QC`, `Done`, `Blocked` — is *structurally* un-pickable, not merely unlikely.
  A `Ready` card that is skipped for ineligibility says so on stderr, naming the issue and the
  reason, so a silent no-pick never has to be diagnosed by guesswork.
  > Before the label model was reworked (under the plugin's previous name), this lane decided eligibility entirely on its own, which meant the label model was
  > not merely bypassed on a board — it was *inverted*: a `Ready` card carrying `sdlc:parked`, or
  > the old `sdlc:needs-confirmation`, or **no labels at all**, was picked, while a correctly-labelled
  > `sdlc:goal` card sitting in `Blocked` was not. That old label — then the AI-filed-awaits-a-human
  > gate — was decoration on any board-backed repo.
  > **Upgrade note:** dragging a card into `Ready` is no longer sufficient on its own; the issue
  > must also carry `goal_label`.
- **`"label"`** (also what a repo silently runs on if its board has no `Ready` option, or
  `project.enabled` is off) — the picker never reads the board's columns **at all**. Dragging a card
  to `Blocked` looks exactly like a guard; it is not one. Only the label queue's own exclusions —
  `parked_label` (default `sdlc:parked`), `in_progress_label` (default `sdlc:in-progress`),
  `sdlc:blocked` (the always-on state `mark_blocked` writes — no config needed),
  the legacy `sdlc:needs-confirmation` (still excluded here so both queue paths
  agree), and the optional human-set `blocked_label` (unset by default — see
  `discovery.github.blocked_label`) — keep an issue out of this queue; a board column's name plays
  no part in it. The `sdlc:blocked` entry is load-bearing: a blocked goal KEEPS
  `sdlc:goal`, so that exclusion is the only thing making it unpickable here.

`/sigma-doctor`'s features dashboard states which of the two is actually live for your config, under
**"pick-path board gating (queue_source)"** — so this is never left to be discovered the hard way,
by an issue quietly getting picked back up out of `Blocked`.

> **No manual "group by" step.** The loop drives GitHub's **built-in `Status` field** — it sets that
> field's options to Backlog → In Progress → QC → Done → Blocked via the API (`updateProjectV2Field`),
> so the Board view groups by it **natively**. The one thing GitHub exposes to *no* tool is the view
> *layout* itself: if a new project opens as a Table, switch it to **Board** once (a standard GitHub
> step, not kit-specific). For **zero per-repo setup**, point several repos at **one shared board** via
> `discovery.github.project.number` — configure it once, reuse everywhere.

**A reopened issue's card can be stranded at Done.** GitHub's built-in project workflows move a card
*to* Done in exactly one direction — closing an issue fires `Item closed`, which moves its card —
and there is no `Item reopened` workflow that fires the other way. Reopen an issue and its card just
sits at Done for the rest of its life, until a human resets it on the board. The loop's own Priority
mirror knows about this: it never writes the board's Priority field on a card in that state (loudly,
on stderr, every sync pass it happens), so it isn't quietly kept "current" while sitting in the wrong
column. `/sigma-doctor` is the detection half — it names any open issue whose card is stuck at Done,
the sibling of its "board marks closed items Done" check for the opposite direction. Neither can fix
the card itself (there's no API for it); reset its Status on the board by hand.

**Sprint / PM scaffolding.** Run **`/sigma-init --github`** to also install GitHub project-management
hygiene into `.github/`: **epic** and **task** issue templates (epics decompose into task sub-issues),
a **bug** template, an **auto-add-to-project workflow** that drops every new issue into the board's
Backlog, a **critical-insight** comment template (record findings/decisions on the issue), and a
**label guide** (one `type` + ≥1 `component`/`area`). The board itself needs none of this to work —
the loop already adds every `sdlc:goal`-labelled issue for free on its own, PAT-less. The optional
auto-add workflow is only for same-tick immediacy on ANY issue (not just goal-labelled ones); set
the repo variable `SDLC_PROJECT_URL` and the secret `ADD_TO_PROJECT_PAT` only if you want that.

**Recording the audit trail.** As the loop runs each phase, it records a journey-log note (and 🔒
critical insights for key decisions) — as a **comment on the task issue** in github mode, so the issue
timeline and the board card hold the full history, or appended to `.sdlc/journey/<goal>.md` in local
mode. Recording is **fail-open** (never breaks a run).

**Priority: the field decides, the label follows.** The board's built-in **Priority** column (P0–P4,
auto-provisioned as a single-select alongside Status) is what the picker actually ranks by — set it
**there**, not just as a label, for it to take effect. The queue orders by priority tier, then the unit (feature) rank, then a
`bug` label, then oldest first: priority leads, a `bug`-labelled issue beats a same-tier non-bug, and
issue age breaks any remaining tie.

The `priority:P0`…`priority:P4` label is a fallback and a mirror, not a second source of truth: on
every sync the **field wins** any disagreement — the label is corrected to match, loudly, on stderr,
so a stale field left over from before the board was in active use doesn't silently overwrite a
label you *just* updated (if you see that warning and the label was the fresher one, fix it on the
board, not the label). A bare label with no field value still gets read and fills the field, so the
column catches up instead of showing blank. An issue with neither is left alone rather than blanked.

**Already using your own vocabulary — Critical/High/Medium/Low, Urgent/Normal, anything?**
`discovery.priority_aliases` maps it onto P0-P4 instead of asking you to migrate:

```json
"discovery": {
  "priority_aliases": {
    "critical": "P0",
    "high": "P1",
    "medium": "P2",
    "low": "P3"
  }
}
```

Unset (the default), a value outside P0-P4 ranks UNPRIORITISED exactly as it always has — this is
opt-in, never a behavior change for a repo that hasn't configured it. Configured, it works on BOTH
sides and in both directions: a label spelled `priority:critical` or a board field showing `Critical`
each resolve to P0 for ordering purposes, matched case-insensitively; and if the field's own options
are your vocabulary only (no `P0` option at all), mirroring a label's tier onto a blank field still
finds the matching alias option rather than giving up. A field value that matches NEITHER a literal
P0-P4 tier NOR a configured alias is never treated as authoritative — it's left alone, the same
"nothing to justify a guess" rule that already covered a genuinely blank field, just applied
correctly to unrecognised-but-non-blank values too (previously the one gap: any non-blank field text
was treated as authoritative, so an unrelated or stale value could silently overwrite a correct
label). Any correction always writes the canonical `priority:P<n>` label, never an alias spelling —
labels stay in one vocabulary even when the field speaks another. Aliases resolve everywhere
priority is read: the live picker (label queue and board-field queue alike), `/sigma-triage plan`'s
wave order, `survey`'s enqueued bucket, and the 200-issue-cap widening search.

Config: `discovery.github.project.priority_field` (default `"Priority"`) names the board column;
`discovery.github.priority_label_prefix` (default `"priority:"`) names the label prefix;
`discovery.priority_aliases` (above) maps alternate vocabulary onto P0-P4, shared by local frontmatter
and GitHub labels/fields alike. `discovery.order` (`"priority"` default | `"created"`) picks the
queue order — under `"created"`, priority is ignored entirely, by design, so a repo that just wants
strict filing order still gets it.

**A blocker is never ranked below what it blocks.** Whenever one issue blocks another, the blocker's
priority is at least the priority of what it blocks: a lower-priority blocker is raised to match when
the blocked and blocking marks are assigned, and again whenever the reconcile tick finds the graph
changed. Nothing is ever lowered. It is on by default, with no config key to set.

Worked cases:

- A P0 goal becomes blocked by an open P3 issue: the P3 issue becomes P0 in the same step that marks
  the pair.
- A P3 goal blocked by a P0, P1 or P2 issue: no change on either side.
- A P2 blocker of a P2 goal stays P2; the same at P0, P1, P3 and P4.
- A follow-up filed because a P0 goal cannot proceed, with no priority or an unknown one, is filed at P0.
- A chain raises through the middle: if C blocks B and B blocks A, C is raised to A's priority. A cycle
  is reported once and never loops.
- An issue waiting on the legacy confirmation label is never a blocker: the label is stripped from a
  blocker, not honoured.
- A blocker in another repository that Sigma cannot write to is reported on stderr, never skipped silently.

Every raise writes one comment on the blocker naming the old and new priority and the issue it blocks.
An unknown priority counts as the lowest. Writes per reconcile tick are bounded (10 writes, 200 edges),
and the board field write is skipped when the GraphQL budget is low; the label write still lands.

**One-way ratchet, deliberately.** A raise is never automatically undone. Once a blocker's priority is
raised it stays there, even after the issue it was blocking closes or is unblocked another way. There is
no de-priority path.

**Explicit `off`.** `discovery.blocker_promotion.mode` is `"always"` when absent. Setting it to `"off"`
is byte-identical to a release without the feature: no read, no write, no state file, and `/sigma-doctor`
shows no row. The older `"smart"` value is still accepted for the board pass only and is not the rule above.

**Rollout.** On the first reconcile tick after an upgrade, the reconciler prints one stderr line saying how
many blockers it would raise, then raises them; later ticks print nothing. The count is kept in a small
state file under the state directory, and `/sigma-doctor` shows one row, flagged when violations persist
for two reconcile intervals.

**Where this reaches.** The live GitHub-mode board sync: every open `sdlc:goal` issue is
re-evaluated against this rule on every board touch, and a promotion writes through, live.
`/sigma-triage plan`'s wave ordering applies the identical rule too, but read-only there — it only
changes sort order within the compiled plan (a promoted blocker's wave can move earlier), never
writes anything back to GitHub.

It does **not** reach local-goals mode. A local goal's own backlog survey never sees a blocking edge
to promote on in the first place: a local goal's reference is a file path, not a bare issue number,
so the same "Blocked by #N" marker scan that finds edges in GitHub mode can never match one in a
local goal's body, by construction — nothing to promote, so the config key is simply never read
there.

Config: `discovery.blocker_promotion.mode` (`always` default | `off`) is the only key this feature reads.

**A goal BLOCKED on another issue resumes by itself once that blocker is resolved (a closed issue or a merged pull request). A goal a human
PARKED never does.** That split is why this sweep ships on. When work on a goal finds a genuine
dependency, the loop gives it the `sdlc:blocked` overlay: it keeps `sdlc:goal`, and nothing picks it
while the overlay is on. `discovery.auto_unpark` is the sweep that lifts it:

```json
"discovery": {
  "auto_unpark": {
    "mode": "on"
  }
}
```

`discovery.auto_unpark.mode` is **`"on"` by default**: the scaffolded config ships it, and a missing
or unrecognised value also reads as `"on"`. Set it to `"off"` to turn the sweep off. On every pick
it reads each open `sdlc:blocked` issue. If every `blocked by #N` reference the issue recorded (in
its body or its comments; Sigma's own fixed "Parked by Sigma — needs human review: " prefix is never
read as one) now points at a closed issue or a merged pull request (a pull request closed without merging does not resolve it), it drops `sdlc:blocked` (and a stale
`sdlc:in-progress`), re-adds `sdlc:goal` if it is missing, moves the card back to Ready on a board
repository, and posts a comment naming the closed blocker(s). A label never changes silently. A goal
naming several blockers waits for all of them, and one with no machine-detectable reference is left
alone. The same sweep keeps `sdlc:blocking` in sync on the blocker issues themselves.

**It never resumes an `sdlc:parked` issue** (#1394). The sweep decides from the text's shape alone,
so it cannot tell a stale dependency from a person's deliberate checkpoint that happens to say
"blocked by #N". A park stays a human decision: resume it with **`/sigma-unpark`**, which answers the
question that caused the park before it changes a label, or with `/sigma-triage`'s `enact`, the
third stage of a manually driven `survey` → `plan --pick` → `enact --apply` campaign. The sweep still
reads a parked issue for the blockers it names, so those blockers get `sdlc:blocking`.

**A cooldown.** The pick that lifts a block never also picks that goal up; a later pick does, so the
audit comment lands first. Goal-level parallelism (`parallel.goals.enabled: true` plus
`max_concurrent >= 2`, both opt-in) can shrink that window to milliseconds: a goal lifted in one
`next_batch()` slot can be dispatched from another slot of the same batch.

**A per-goal opt-out.** A comment carrying the HTML marker `<!-- sigma:keep-parked -->` makes every
sweep skip that one issue, for as long as the comment stays on it: a blocked goal carrying it stays
`sdlc:blocked` until a person acts. The check runs before any blocker phrase is read, so the wording
of the park text cannot defeat it. Post it like any comment the loop writes,
`python3 <installed-sigma>/skills/sigma-loop/scripts/loop.py note .sdlc <goal> "<text>"`, or generate
the text with `auto_unpark.keep_parked_comment(reason=...)`; `/sigma-unpark`'s keep-parked decision
writes the same marker.

Same reach as `blocker_promotion`, for the same reason: GitHub mode only. A local goal's own "blocked
by #N" reference is a file path, never a bare issue number the same regex can match, so there is
nothing for local-goals mode to ever sweep.

Config: `discovery.auto_unpark.mode` (`on` default | `off`) is the only key this feature reads.

### Label reconciliation (optional, off by default)

Even with every write path correct, a backlog's label state drifts: people edit labels by hand, an
older install of the plugin still writes the old way, and an issue auto-closed by a merged PR's
"Fixes #N" never runs the loop's own completion step at all. Drift is invisible by nature — the
worst case is an issue carrying *no* lifecycle label, which no label search can return, so it simply
stops existing as far as the loop is concerned.

`reconcile.py` addresses that, and it is **read-only unless you opt in**:

```bash
# Read-only. Safe any time. Lists every issue Sigma is responsible for and classifies it.
python3 <installed-sigma>/skills/sigma-loop/scripts/reconcile.py census .sdlc

# Read-only against GitHub; writes a local proposal file with the EVIDENCE for each correction.
python3 <installed-sigma>/skills/sigma-loop/scripts/reconcile.py propose .sdlc

# Apply an approved proposal. Dry-run without --apply, like every apply-shaped verb here.
python3 <installed-sigma>/skills/sigma-loop/scripts/reconcile.py apply .sdlc --plan .sdlc/plans/reconcile/<file>.json --apply
```

It **enumerates the population, not the corruption** — asking "which issues am I responsible for?"
and classifying each, rather than searching for broken ones, because a search by label can never
return an issue whose defect *is* a missing label.

Corrections are tiered, and the tiering is the safety property:

| Tier | What it covers | Who approves |
|---|---|---|
| **Automatic** | a CLOSED issue still carrying a stale lifecycle label | nobody — it is safe by construction |
| **Proposed** | anything touching an OPEN issue, with its evidence attached | you, in one command |
| **Flagged** | anything needing judgement | nobody touches it |

Only the automatic tier ever runs unattended, and only on **closed** issues — which no picker can
serve, which hold no claim, and which have no worker, so correcting one can wake nothing up. It
additionally waits 24h after closure, re-reads each issue immediately before writing, caps
corrections per sweep, writes its audit comment *before* the change, never removes the goal label,
and refuses outright if any census query failed (a partial read must never drive writes).

Where two labels genuinely conflict, it decides from GitHub's own label-event history: the later
*intent* wins. Two labels applied within 60 seconds of each other are one interrupted operation
rather than two decisions, so their order carries no meaning — those are handed to a human instead of
guessed at.

```json
"discovery": {
  "reconcile": { "mode": "on", "ttl_minutes": 60 }
}
```

`mode` is `"off"` by default; `ttl_minutes` throttles the survey (it costs ~13 `gh` calls, and a
backlog does not drift between two picks minutes apart). When on, the sweep runs at most once per `ttl_minutes`, at each pick or batch (a not-due call costs one file read). `/sigma-doctor` reports the census as a check row either way, so drift is visible
without turning any writing on.

**Which to pick?** **Local** for a self-contained, zero-dependency repo where the backlog ships with
the code. **GitHub** to keep goals visible to your team, triaged in Issues/Projects, and tied to the
PRs the work produces.
