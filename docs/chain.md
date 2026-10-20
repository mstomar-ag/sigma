# Solving a whole dependency chain

The full reference for the dependency map, Sigma Chain and how dependencies are read from issue text is [the dependency map and Sigma Chain](dependency-map-and-chain.md); this page keeps the detail it links to.

`/sigma-chain` plans every open prerequisite behind one goal, asks the person every decision once,
arms the members, and drives them to done. The engine is `skills/sigma-loop/scripts/chain.py`; the skill
is a thin script over its verbs. Nothing here is model-driven: each verb is a plain command.

## Solving a whole dependency chain

Paste this block into the file your agents read (`AGENTS.md`, `CLAUDE.md`, or equivalent). It is the single source; a test pins it. `/sigma-init` writes it into the new `project.md` and Cursor rule (never into an existing file), and `/sigma-doctor` reports whether an agent file carries it.

```
## Solving a whole dependency chain

When a goal is blocked by other open goals, do not walk the blockers one by one. Run the chain:
use the sigma-chain skill, or `chain.py plan`, `sheet`, `answer`, `approve`, `arm`, `run` from
the sigma-loop scripts. It plans the whole chain, asks every decision together, and then solves it.
```

## Flow

| Step | Verb | Touches GitHub |
|---|---|---|
| Plan | `chain.py plan <sdlc_dir> <N> [--json] [--forecast [--as <login>]]` | reads only (about one request per member, for unconfirmed waits); writes the local record |
| See the map | `dag.py html --goal <N>` (or `--all`) | nothing, from the stored map; one offline page per chain under `.sdlc/state/dag-html/` |
| See the plan | `chain.py html <sdlc_dir> <chain> [--out DIR] [--titles FILE]` | nothing, from the stored plan record; `chain-plan-<N>.html` |
| Offer end goals | `chain.py plan <sdlc_dir> --longest [--by freeing]` | nothing, from the stored map |
| Sheet | `chain.py sheet <sdlc_dir> <chain> [--as <login>]` | nothing (its last line names the page command) |
| Answer | `chain.py answer <sdlc_dir> <chain> --answers <file>` or `--defaults` | nothing: an answer is a local record, enacted at `arm` |
| Approve | `chain.py approve <sdlc_dir> <chain>` | nothing; prints the forecast the answers give |
| Arm | `chain.py arm <sdlc_dir> <chain> [--dry-run]` | the only write path: what the answers ask for (release, take over, confirm or dismiss a wait), then label `chain:<root>`, one fixed comment, arming |
| Run | `chain.py run <sdlc_dir> <chain> [--session-pid P]` | one cycle: the loop's label-scoped pick |
| Stop / resume | `chain.py stop`, `chain.py resume [--cap N]` | nothing; local record only |

**The page of the whole plan.** `chain.py html` draws the stored plan record (what `plan` and `sheet` read), not the
map, so a wait written in prose or a comment (a dashed `unconfirmed` edge) and every deep member appear. Every
member, done prerequisite and external is on it, coloured by what it is: ready, waiting, needs decision,
someone else's, checkpoint, not a goal, awaiting its owner, parked, paid gate; closed prerequisites sit behind a
toggle. The questions are the sheet's, taken from the sheet's own text so the number and the wording cannot differ:
a panel reads "N questions before anything is armed", each question carries a numbered badge on the member it is
about (a grouped question on every member it covers, a question about the whole chain on the end goal), and a click
on a question or its badge highlights the other; an answered question shows its recorded answer. There is no plan
yet: it refuses (exit 2) and names `chain.py plan`. The sheet does not write the page (it stays a reading that
changes nothing); it prints one last line naming the command and the path. A closed prerequisite carries edges only
where a warning ties it to a member: the record does not hold what each one freed. Unmeasured: a chain deeper than
about 8 columns opens fitted but small; zoom, or click a question, which pans and enlarges.

No GitHub write exists in any verb before `approve`. `run` is one cycle and is called again until it
reports done; exit 3 means capped or stuck with one reason (decision, held-gate, claimed, external,
checkpoint, rate-limit, degraded-read, capped). The stop names what HOLDS the chain: of the members still open, only
those that wait on no other open member are considered (a parked goal that itself waits on an open goal is held, not
holding), and the same rule gives the forecast's roots. When more than one member holds the chain, the run prints the
others on a line of its own (`also holding the chain: #a, #b`, at most 6 and a count), and `chain.py status` repeats it;
the stop line itself is unchanged. A closed prerequisite's open follow-up never counts as a dependent waiting on itself:
a follow-up names its parent and so waits on it by construction, and asking it to "keep waiting" for itself is no
question. A plan read from the stored map, with an assignee filter configured and a chain small enough for the live
read, is planned live instead: the map holds no assignees, and a forecast that cannot apply the filter promises work
the runner may not take. Before a closed-prerequisite question is answered, the sheet's forecast reads the recommended
answer (keep waiting) and says so in a note.

One `run` cycle reads each issue and each comment page at most once (`chain_reads.py`): a repeat of a GET of one issue
is served from memory for the length of that call, any call that can write drops everything kept, and the store is
bounded (400 entries, 16 MB). It covers the in-process sweep that opens the cycle (its own repeats and its writes included);
the pick that ends it is a separate process with its own reads and is outside it.
Not measured on a real repository; the private re-test harness counts the calls.

## The sheet

The sheet asks every question the chain needs, once, before anything is armed, **deep members as well as near ones**: a
run that stops on something the sheet never asked is a defect. A question is asked only when its answer changes what
`arm` or `run` does, and every answer is enacted:

| Question | Asked for | `keep` / `wait` (recommended) | The other answer, enacted at `arm` |
|---|---|---|---|
| `park:<n>` | a parked member: why it is parked (its recorded park question, else the first line of its park note or body, cut to 140 characters; withheld when the decision records are keys-only) and who waits behind it | `keep it parked`: the chain stops before it and the members that wait on it, named | `release it`: the existing atomic unpark gesture (`unpark.resolve`, decision `unpark`: one label swap, the Q&A block, one comment); an undeclared kind is declared with the recommended guess |
| `wait:<a>-<b>` | a wait read only from prose or a comment, whose blocker is **open** and in the chain and whose edge the explicit edges do not already imply (a wait on a closed issue is only shown) | `confirm it`: the marker `deps.py mark` writes (`**Blocked by:** #b` in the body, one note) | `dismiss it`: the dismissal marker (`backlog_check.dismiss_comment`, posted the way `loop.py note` posts it); the plan, the forecast and the run drop the wait |
| `elsewhere:<n>` | a member assigned to someone else, or in progress in another session | `wait for them`: the run pauses there and says whose it is | `take it over`: assign the runner, then remove the old assignee (one assignee). The runner is the `--as` login the plan was made for, not the login that ran `plan`; the ask says "Planned by X for Y" when they differ. Never offered for a member in progress: only `wait for them` is, and the sheet says why |
| `irreversible:<n>` | a paid step: a declared paid line in the body, or a label listed in `chain.paid_labels` | none: never defaulted | `go ahead`; anything else is skipped by every run cycle |
| `cap:c<n>` | every chain | none: never defaulted | the answer is the goal cap the run enforces (`chain_spend.admit`); the tighter of it and `chain.max_goals` wins, and the forecast models it |
| `cross_unit:<a>-<b>` | an edge between two units | `keep going` | `stop here`: the dependent and what waits behind it are held (labelled, never armed, skipped by every run cycle) |

The old per-kind question for a parked member (a 13-option "which kind of question is this?" and the per-kind route
questions) is not asked of a chain; the park question replaces them. A question with no listed option says where it
is answered (`/sigma-unpark`); only the cap offers a number. Park, wait and elsewhere questions are never folded into
a group (each carries its own reason), however long the sheet is. `arm --dry-run` prints the same lines the sheet's
"Writes that follow approval" lists, and a second `arm` finds nothing to do: each step reads the member's current
state first. A step that fails is reported, the chain is not stamped armed, and running `arm` again finishes it.
The sheet's forecast is computed from the answers (a released member is armed, a kept-parked one stops the run, a
taken-over one is the runner's, a dismissed wait is no edge), `approve` prints it, and it equals what the run does.
Not measured: a live repository; the harness replay is offline.


Every decision the chain needs, shown together: the label drain's questions over the member set, plus a
cross-unit edge, an end goal that cannot be armed, and the cap. Every answer is a bank option (the cap is a
positive whole number), never free text. `--defaults` takes each recommended option except where there is
none: the cap, a paid step (`irreversible:<n>`, which is never accepted for the owner) and a follow-up waiting
for its owner (`owner_gate:<n>`, which is never decided for the owner). Those stay open, and the chain is not
approved until a person answers them.

### What the sheet shows first (unblock order)

The sheet opens with the header, then the forecast line, then one block per kind of thing a person can act on,
always in this order, each printed only when it has members, and a member is in exactly one:

1. `Forecast:` how far a run from this login gets, and where it stops (see [the forecast](#the-forecast)).
2. **Ready and waiting:** members a run can claim, now or once what they wait on is done (and, marked as such,
   the ones it cannot: someone else's, not workable).
3. **Owner decisions:** parked, not-armed, upper-tier, not-a-goal and awaiting-owner members, closed
   prerequisites with something still open, and cross-unit edges.
4. **Unconfirmed waits:** waits written in prose or a comment, with the commands that settle them.
5. **Checkpoints:** never worked; met when their unit lands, or when a person closes the issue.
6. **Paid gates:** members that ask for paid work. Never defaulted; a run does not start one until the answer
   to `irreversible:<n>` is `go ahead`. A paid member is listed here and nowhere else among the member blocks.

Then the numbered decisions and the writes that follow approval, as before.

### Unconfirmed waits

A wait written in prose or in a comment, in wording no explicit form recognises, is read by Chain for this chain
and listed, not hidden and not trusted blindly. `chain.py plan` reads each member's body and newest comments (GET
only). A directional phrasing in a body ("only once #A, #B and #C are closed", "#N must land on main before ...",
"these plug into #N's port"), and the same or an explicit form in a comment by the issue's assignee or an owner,
member or collaborator ("Blocked by: #N added", "hold on #N before any paid run", "#N is now a blocker"), is an
**unconfirmed wait**. The plan treats it as an edge tagged `unconfirmed` (waiting is the safe direction: nothing
starts early), follows it to the issue it names so that issue and what it waits on join the chain, and the
sheet lists every one in a block of its own. Follow-up wording ("filed as a follow-up to #N") stays a **weak
mention**: advisory, never an edge.

The block names the two ways to settle a wait: `deps.py mark <sdlc_dir> <goal> <ref> <reason...>` for a real one
(it becomes a body edge every reader trusts), or the dismissal marker from `backlog_check.py dismiss-text
blocked-by <ref>` for a wrong one (the plan stops reading it); run `chain.py plan` again after either. Only
Chain's plan reads these as edges. The pick gate, the stored map and the label drain still read explicit forms
only, so a goal outside the chain is never held by prose. Inside the chain, `run` holds back every member whose
recorded blocker is not confirmed closed even when the pick would offer it, so an unconfirmed wait is honoured at
the time a goal is claimed, not only in the plan. The stored map holds no prose, so a map plan that finds one
falls back to the live read and says why. A member whose comments could not be read, a wait chain deeper than
the rounds the plan follows, or a cap on findings makes the plan INCOMPLETE. The full rules, the closed list of
phrasings and the bounds are in [the reference](dependency-map-and-chain.md#waits-written-in-prose-or-a-comment).
Not measured: network time, and how often the loose phrasings match a remark that is not a wait (a wrong match
costs a line in the sheet and a member the person dismisses, and a cycle it makes is refused by name).

## The forecast

`chain.py plan <sdlc_dir> <N> --forecast` (and the sheet, always) marks every member and says how far a run will
really get, before anything is armed:

| Marker | Meaning |
|---|---|
| `ready now` | a run may claim it and it waits on nothing open |
| `after #N` | a run may claim it once the named members are done |
| `needs decision` | parked, not armed, upper-tier, not a goal, waiting for its owner, or kept waiting on a closed prerequisite's open follow-up: a person must act |
| `someone else's` | assigned to another login, or already in progress |
| `checkpoint` | never worked: met when its unit lands or a person closes it |
| `not workable` | held by a gate, a paid step the owner answered not to run, carries the blocked label, waits on a prerequisite outside the chain, or could not be read |

A member that carries the blocked label because of waits on other members of the chain is classified by those
waits (`after #N`) and is counted as finishing once they do: Sigma's own sweep sets that label and clears it when the
waits close, so reading it as permanent promised 1 goal where a run did 3.

One line follows: `a run from <login> can finish K of N before it stops on <reason>: <each root>`. The roots are
the members that wait on nothing else that is left, each with its marker; the reason is the one word `run` itself
would print (decision, held-gate, claimed, external, checkpoint, degraded-read). It reads the answers already in the
record: a paid step answered "do not", and a dependent kept waiting on a closed prerequisite, are not counted as
finishing. `--as <login>` asks for another runner (a
runner who owns none of the work finishes none of it); the default is whoever planned.

The forecast plays `run` cycle by cycle at the configured width and uses the rules `run` uses: the claim rule and
the stuck rule live in `chain_forecast.py` and both call them, and the assignee clause is the label drain's own. A
test plays a real run on a fixture and compares. It assumes every goal a run claims merges, and it does not model
the cap. A plan read from the map holds no assignees, so there the assignee filter is not applied (the output
says so). The record keeps each member's assignee logins for this; it is local state and holds no titles or text.

## Member states

Live verdicts: armed, unarmed, parked, upper-tier, assigned-elsewhere, held-by-gate, not-a-goal. From the
stored map only the life is known: armed, unarmed, parked, unknown; a member whose life is `blocking` or
unarmed is therefore read once (a GET, at most the plan's member bound) and the live verdict decides, and a
`blocking` member that cannot be read is `unknown` (the plan is INCOMPLETE), never `armed`. An upper-tier,
parked or gate-held member is labelled, never armed.

### Not a goal (`not-a-goal`)

An open issue that carries `sdlc:blocking` but not `sdlc:goal` is something other work waits on that nobody can
start: the loop cannot pick it and the doctor already reports it as a deadlock. It is never `armed` and never the
first pick. The sheet asks one question per such member (`not_goal:<n>`): leave it out (the recommended
answer), arm it as a goal, or treat it as a checkpoint. Only "arm it as a goal" arms it. `chain.py arm` and
`label_drain.py arm` touch such a member, with or without `--apply` or `--dry-run`, only after that answer: with
any other answer, or none, it gets no `chain:<root>` label, no comment and no goal label, and `arm` lists it as
refused. "Treat it as a checkpoint" is recorded only; the label that makes it one is a person's step (see the
dependency docs).

### Closed, but not done (`closed_gate`)

Chain treats a closed prerequisite as resolved. Some were closed without meeting their goal and the real gate moved to
an issue nobody linked. `plan` therefore inspects the closed prerequisites it lists under `done`, and warns, next to
the dependent, for each one that has either of two signals: an open issue carrying `sdlc:followup` whose body says
`Follow-up to|of|on|from #N` (the tight forms that count as an edge elsewhere; a follow-up that was never labelled is
not seen), or an owner question as its **newest** comment (a line starting `Owner question:`, `Question for the owner:`
or `Needs the owner's answer|decision|call:`, a bullet or bold allowed, with nothing posted after it). The warning is
advisory: it adds no edge and no member. It is in the record (`closed_gates`, numbers only), in the `plan` output
as `warning: closed #N with ...`, and on the sheet as the question `closed_gate:<N>`: keep waiting (the recommended
answer) or proceed. "Keep waiting" holds the dependent: `arm` labels it but does not arm it, and every `run` cycle skips
it; a later `plan` that no longer raises the warning (the follow-up closed) releases it. "Proceed" changes nothing.

Bounded: at most `chain.max_closed_checks` (default 10; 0 turns the check off) closed prerequisites are inspected per
plan, lowest number first, at most three reads each plus one list of open follow-ups; the plan prints
`closed prerequisites inspected: N, skipped: M (#...)`. A read that fails costs only that signal and is listed as not
fully read; it never refuses a plan. Not measured: the added reads against a live repository (measured on the offline fake: a plan with
three closed prerequisites made 13 calls with the check on and 6 with `chain.max_closed_checks` at 0, which is one list, three
issue reads and three comment-page reads more).

### Waiting for the owner (`awaiting-owner`)

A member that carries the legacy confirmation label (`sdlc:needs-confirmation`, or the name under
`ledger.handoff.proposed_label`), with or without `sdlc:goal`, is a follow-up still waiting for its owner. Approving
a chain is not that confirmation, so it is never armed by `chain.py arm` or `label_drain.py arm`, with or without
`--apply` or `--dry-run`, unless the sheet asked about that member and the person answered "promote it". The
sheet asks one question per such member (`owner_gate:<n>`): leave it out, promote it, or close it. It has no
recommended answer, so `answer --defaults` leaves it open (the fate of a follow-up is the owner's to decide) and
the chain is not approved until it is answered. Members that share the options are shown as one group (`group:owner_gate`)
whatever the number of questions, and an answer to the group reaches each of them. "Promote it" arms the member
through the same machinery as any other, and also strips the leftover label, so the queue stops refusing it; the
other two answers write nothing (Sigma does not close an issue for you). The same holds for an open blocker that
`label_drain.py arm` would otherwise pull into the label. Known gap: the stored map cannot see the leftover label on
a member that also carries `sdlc:goal`, so the sheet may show it as armed; `arm` reads every member's current labels
and still leaves it untouched, listing it as refused.

## What a run does with the answers

`run` calls the loop's own pick, scoped to the chain label, and adds what only the record knows. It hands the pick
a skip list of: members the person said to keep waiting on a closed prerequisite; paid steps answered anything but
`go ahead`; and members whose recorded blocker (an unconfirmed wait included) is not yet confirmed closed. A member
that was never labelled (a checkpoint, or a not-a-goal or awaiting-owner member nobody promoted) is not a member of
the run: it is neither claimed nor waited on to close, and work that waits on it stops the run, naming it. The
reasons are the existing ones: a paid step the owner declined is `held-gate` ("answer go ahead, or change the
plan"), a left-out member that work waits on is `decision`, a dependent kept waiting is `decision`, a checkpoint is
`checkpoint`. The forecast uses the same rules, so it agrees.

## Checkpoints

A checkpoint (`sdlc:checkpoint`, see [the label model](label-model.md#2b-vi-sdlccheckpoint--an-issue-that-means-that-units-code-is-on-main))
stands for "that unit's code is on the integration branch". It is a row in the plan with the verdict `checkpoint`,
its unit and a status (`met`, `waiting` or `unknown`). It is never picked, never armed and gets no `chain:<root>`
label; `Blocked by` on it is an ordinary edge; the sheet shows checkpoints in their own block, with what each one
holds up. When no other reason is open, `run` stops with the reason `checkpoint` and names it; a `met` one says
that its issue is still open and the step that releases it (close the issue). It is met when the unit's tip is
contained in `<remote>/<work.base>` (local git ancestry, no fetch, no network) or a person closes the issue.
A unit landed by squash, or whose branch is gone, reads `waiting` or `unknown` until a person closes the
checkpoint. The loop's own pick gate still reads an open blocker as open, so a `met` checkpoint does not release
its dependents until it is closed: the status is a report, the close is the release.

## Growth

`run` reports members added to and closed in the label since the last cycle. It does not act on them. A
change in membership is bounded by `chain.max_added` (below).

## Cap and honest limits

The cap is checked before each claim, so it can be overshot by the cost of the goals in flight. With no cap
configured nothing reads the spend ledger; with one set, an unreadable ledger fails closed.

`chain.py status` reads the local records and prints each chain's state and the age of its last progress; it
makes no GitHub call. `run --no-grow` is a flag. Run width is not a flag: it comes from the `parallel` key
under `chain` in the project config, off by default (see [parallelism](parallelism.md)). Not available yet: a
`--cap-usd` flag (set `chain.max_usd`). `resume --cap N` caps the number of goals, not dollars. Not measured:
a run against a live repository, and two drivers on one chain (the record has no lock; run one).

## Configuration

Keys under `chain` in the project config.

| Key | Default | Meaning |
|---|---|---|
| `chain.max_members` | 100 | Largest closure `plan` accepts; over it the plan is refused with its count, never truncated. |
| `chain.max_goals` | members plus `chain.max_added` | Most goals the chain may hold. |
| `chain.paid_labels` | `[]` | Labels that mark a paid step (a list of label names, for example one a repository puts on its paid issues). A member carrying one gets the spend question up front even though its body declares no paid line. Empty by default: nothing is asked by label unless the operator opts in. A value that is not a list of label names is refused at `plan`. A plan read from the map goes live for this (the map holds no labels); a chain too big for the live read is INCOMPLETE rather than silently missing them. |
| `chain.max_closed_checks` | 10 | Closed prerequisites `plan` inspects for an open follow-up or an unanswered owner question; the rest are skipped and counted. 0 turns the check off. |
| `chain.max_added` | 5 | Goals the chain may grow by when `chain.max_goals` is unset. |
| `chain.freeze` | false | When true (or `run --no-grow`), nothing joins by itself: each new prerequisite is asked on a delta sheet. A fourth such pause (max_reasks, default 3) ends the run stuck. |
| `chain.parallel` | unset (width 1) | Whole number of 2 or more: members one run cycle may claim at once. Capped by `parallel.goals.max_concurrent` (default 3) and honoured only with `parallel.goals.enabled` true and `work.enabled` on; otherwise the width is 1. A value below 2 is read as unset. When `chain.max_usd` is set and the remaining budget does not cover the goals in flight at the p90 cost per goal, the run falls back to width 1. |
| `chain.max_active` | 1 | Most chains that may run at the same time. Setting it, or `chain.parallel`, turns the active-chain lock on. |
| `chain.max_usd` | unset | Dollar cap on spend attributed to members since approval. |
| `chain.max_minutes` | unset | Minutes cap measured from the approval stamp. |
| `chain.auto_longest` | reserved | Documented only: nothing reads it yet. |

## The scratch scenario gesture

The offline scenario test drives the engine only through these argv, in this order (`run` is called again
until it reports done, and `status` closes it). The test compares what it ran with this block.

```
chain.py plan <sdlc_dir> 1
chain.py sheet <sdlc_dir> 1
chain.py answer <sdlc_dir> 1 --defaults
chain.py answer <sdlc_dir> 1 --answers <file>
chain.py approve <sdlc_dir> 1
chain.py arm <sdlc_dir> 1
chain.py run <sdlc_dir> 1
chain.py status <sdlc_dir> 1
```

## Measured on the scratch scenario

The scenario is a chain of four behind end goal 1 (waits on 2, 2 on 3, 3 on 4): goal 4 parked, goal 2
unarmed, goal 3 in another unit, a fifth goal filed mid-run, and an armed goal outside the chain. It runs on a
stateful fake of the host, with no network.

- GitHub calls for `plan`: 9 (counted from the fake's call log; it was 7 before the reader for waits written in prose
  or a comment, which adds the issue read and the comment page of the one member that has a comment; the
  closed-prerequisite check adds none here because the scenario has no closed prerequisite).
- Time to the sheet: measured as wall time on the fake, which is not meaningful against a live host.
  It is not measured live.
- Estimate versus actual spend: not measured (the scratch ledger holds no priced goals).
- Not measured: a run on a live repository or a private clone.

Each of four guards was broken alone and seen to fail the scenario: the pick without the label scope (an outside
armed goal is claimed), the done clause without its open-member checks (an empty pick reads as done), the cap with
an unreadable spend ledger read as empty (the run goes on), and the approval gate removed (a write before
approval).

## The regression scenario

`tests/e2e/test_chain_regression.py` is the check to run before a release that touches how Chain reads, plans, asks
or drives. It reproduces, on a synthetic backlog of 20 generic issues (`tests/fixtures/chain_regression/issues.json`),
the shapes a real tangled backlog showed: a wait written only in a comment by the assignee; six loose phrasings
("must land on main before", "plug into #N's port", "depends on rulings in #N", "only once #A, #B and #C are closed",
"blocked on #N and #M", "starts only once #N has landed"); a checkpoint issue labelled `sdlc:blocking` with no
`sdlc:goal`; follow-ups awaiting the owner under the legacy confirmation label; a closed prerequisite whose real
gate moved to an open follow-up; a paid step; goals assigned to two logins; a parked goal; a mention that is not a
wait; and armed goals outside every chain. `tests/fixtures/chain_regression/reference.json` says what the right
answer is: the 15 hard waits, the ones that are not waits, each chain's members, and each stop with its root, its
accepted reasons and the goals that finish before it.

It runs the documented gesture (plan, sheet, `answer --defaults`, `answer --answers`, approve, arm, run until it
stops, status) for two end goals through `chain.main(argv)` on the same fake host as the scratch scenario. A scripted
owner answers the sheet from the reference only; a question the reference cannot answer is named and fails the run,
never guessed. Scripted goal sessions close each claimed goal, or park it with the reference's kind. The pick is a fake
of the loop's scoped pick that judges a goal ready by the reference's waits, not by what Chain read, so a wait Chain
missed is a missing edge and a wrong stop, never a lucky pass. After a stop whose root the reference resolves (a person
unparks it), the scripted world applies the resolution and the run goes on.

```
python3 -m pytest -q -p no:cacheprovider tests/e2e/test_chain_regression.py
```

Each criterion is judged alone, so a red run is a checklist: every reference wait is an edge in the plan or a question
on the sheet; no phantom edge; the checkpoint is never claimed or armed; `answer --defaults` leaves a question open for
every follow-up awaiting confirmation, and none is armed or claimed against the owner's answer; the paid goal is never
claimed after "do not"; each stop names its reference root with an accepted reason, and the goals closed before it are
the reference's; goals outside the chain are untouched and the pick is scoped to the chain label; every write stays in
the scratch repository, no process starts, no ledger file appears and nothing is written outside the scratch `.sdlc`.

The assertions were written for the intended behaviour, not for the engine as it stood, and the engine now meets them:
the scenario is green for both end goals and nothing is marked xfail. It was measured RED on 1.0.17 at the commit it
was written against, which is the record that it can fail:

| End goal | Red on 1.0.17 | Why |
|---|---|---|
| 1 | members, dependencies, awaiting, stops, closed | the closure ends at the end goal (all 11 waits behind it are unread), `--defaults` decides for the owner, and the run stops with no root |
| 11 | members, dependencies, stops, closed | `blocked on #5 and #12` yields #5 only; #12, the checkpoint and its blocker are unread |

With every wait rewritten as the canonical `Blocked by` line, the dependency, member and phantom criteria went green
and what stayed red was the rest: follow-ups awaiting confirmation armed and claimed, the paid goal claimed, and the
checkpoint armed and claimed.

What the scenario then pins, beyond the criteria above: the six loose phrasings and the comment wait are edges (tag
`unconfirmed`) and the chain follows them to the issues they name; the closed prerequisite's open follow-up is a
question on the sheet that the scripted owner answers "keep waiting", so its dependent is held and not claimed (the
follow-up carries `sdlc:followup`, the only follow-up Chain looks for); the left-out checkpoint stops the run naming it;
and `test_forecast_equals_actual_run` asserts the printed "can finish K of N before it stops on <reason>" equals the
run's first segment (goals finished, members, and the root).

The checker is controlled too: a synthetic perfect outcome passes, and each clause broken alone turns its own
criterion red (`test_checker_controls`, `test_checker_counts_a_question_as_finding_a_wait`); the fake pick, the
scripted owner, the hidden-wait builder and the world are each broken clause by clause by hand and seen to fail their
tests. A new kind of question on the sheet needs its answer added to `reference.json` before the scenario can pass.

Limits: the fake serves REST reads and writes only (no search endpoint, no timeline); the pick is a fake of the loop's
scoped pick, not the loop; goal sessions do no work; the stop reasons for a checkpoint and a held paid goal accept a
list because the reason vocabulary is not fixed for them; a GraphQL write carries no repository name, so the
scratch-repository check covers REST writes only. A run on a live repository is not measured.

## The active-chain lock

When `chain.parallel` or `chain.max_active` is set, each run cycle records its chain in
`.sdlc/state/chain-active.json` (pid, heartbeat, claimed pool) under a `flock` on a sidecar file. A chain whose
pool overlaps another active chain's, or that would exceed `chain.max_active`, is refused with the reason.

An entry is reclaimed only when its pid is dead AND its heartbeat is older than 900 seconds (a default the owner
is to ratify). Either clause alone is not enough: a dead pid with a fresh heartbeat may be a driver between
cycles, and a live pid with an old heartbeat is a hung driver a person should look at. So a driver whose cycles
are further apart than 900 seconds can be reclaimed by another chain while it is still meant to be running.

Manual recovery: `chain.py stop` on the chain releases its entry; a chain that ended in any non-running result, stuck included, has already released it, so only a crashed or killed driver leaves an entry (reclaimed as above, or released by `chain.py stop`). A lock file that cannot
be read is named in the refusal; once no chain is running, delete that one file and run again. Without
`fcntl` the lock refuses loudly rather than run unlocked.
