# Sigma's branching model — units of work, per-goal bases, and the registry

**Audience:** anyone adopting Sigma on a repository where more than one goal belongs to the
same body of work. No internals knowledge assumed.
**Status:** the levels described here are shipped. Every behaviour below was read out of the code
that implements it, not out of the design it came from — where the two disagreed, the code won, and
this document says so at each of the three places it happened.

**This is a contract, not a tour.** What each thing means, which combinations are legal, and the
exact gestures. `docs/label-model.md` is its sibling for the `sdlc:*` lifecycle labels; the two do
not overlap — nothing here changes what `sdlc:goal` means, and nothing there knows what a unit is.

**Section numbers.** Every `§N` below is a section of **this** document, and a reader may always
follow one. A section of a different document is cited with that document named beside it
(`docs/label-model.md` §2b-i). The design specification this model came from is **not shipped with
the kit**, so it is never cited by number at all — where the code amends it, the clause is described
rather than numbered, because a number an adopter cannot resolve is worse than a sentence.

---

## 1. The one-paragraph version

`work.base` is a single string in `.sdlc/config.json`, so without this model every goal's worktree
is cut from the same branch and nothing connects an issue to the body of work it belongs to. The
branching model lets an **issue declare its unit of work**; a goal that declares one is cut from
that unit's branch instead of from the configured base, is recorded in a committed registry under
`.sdlc/features/`, and passes that declaration down to every issue Sigma files from it. **A goal
that declares no unit runs exactly as it always did** — which is what makes adopting this break
nothing that already exists.

> **Looking for the route rather than the rules?** [`how-branching-works.md`](how-branching-works.md)
> walks one issue from the moment it declares a unit to the moment its pull request lands, naming
> what happens at each step and linking back here for every decision. It is a guided path through
> this document and carries nothing of its own — where the two differ, this one is right.

---

## 2. The branch shape

```
main                                  ← the integration branch (your `work.base`)
 └── feature/<name>                    ← the UNIT. Several people share it. NOBODY commits directly.
      ├── sdlc/<goal-id>               ← one per goal, cut from the FEATURE branch
      └── feature/<name>/<sub>         ← a dependency discovered mid-flight (max depth 3)
```

Three names, and each answers a different question:

| Branch | What it is | Who cuts it |
|---|---|---|
| `feature/<name>` | the **unit of work** — the thing several goals share | a human, before the first goal |
| `sdlc/<goal-id>` | **one goal**, cut fresh from its base every time | Sigma, at pick |
| `feature/<name>/<sub>` | a sub-child of a unit, for work found mid-flight | a human |

`sdlc/` is `work.branch_prefix` and is configurable; `feature/` is not — it is `features.BRANCH_PREFIX`,
and every consumer in the tree derives its answer from that one constant.

**The prefix stays `feature/` even for a shared bug.** The model is not features-only: a unit of work
is *anything several goals share a branch for* — a feature, a shared bug, a refactor, a contract
change. One prefix is what makes every branch belonging to a unit greppable, forever, including
after the unit finishes. Reopening finished work reuses the original name as a sub-child rather than
minting a new top-level name, for the same reason.

**Depth 3 is the model's rule, and nothing enforces it.** Stated plainly because it is the kind of
thing a reader assumes is checked: no code counts branch segments. What *is* enforced is one segment
narrower — a declared unit name may not contain `/` at all (§5), so `feature/<name>/<sub>` can never
itself be *declared* as a unit. A sub-child is reachable as a `Branch:` cross-check line and as the
`parent` field of a registry entry; it is not a second unit.

---

## 3. The no-direct-commits rule, and what it buys

The rule is stated for the humans and agents working the repo in
[`AGENTS.md`](../AGENTS.md#nobody-commits-directly-to-a-feature-branch), and is quoted here once —
verbatim, rather than restated — so the two cannot drift into two different rules:

> **Nobody commits directly to a feature branch. All work reaches it through `sdlc/*` goal
> branches.**

Concretely, for a unit: every change to `feature/<name>` arrives as a pull request from a
`sdlc/<goal-id>` branch cut from it. `AGENTS.md` is what to do; this section is what it buys and
what actually holds it up.

```
   WITHOUT the rule                        WITH the rule
   ─────────────────────                   ─────────────────────
   two people commit straight to           every change arrives as a PR from a branch
   feature/x; a third cuts a goal          cut from feature/x's own tip
   branch from whatever tip they               ↓
   happened to fetch                       every goal's base is a reviewed state
        ↓                                  every change to the unit has a diff, a
   the unit's history is whatever          review and an issue number attached to it
   arrived, in whatever order                  ↓
        ↓                                  "what is on feature/x?" is answerable by
   "what is on feature/x?" is a            listing the merged PRs into it
   question only `git log` can answer
```

What it buys, concretely:

- **the base of every goal is a state somebody approved.** `start()` cuts fresh from
  `<remote>/<base>`, so a goal branch inherits whatever is on the unit branch at that instant. If
  that can be an unreviewed direct push, so can every goal cut after it.
- **the unit's contents are enumerable.** Every goal recorded against a unit in `.sdlc/features/`
  corresponds to a PR into the unit's branch. A direct commit is a change with no issue, no PR and
  no registry row.
- **it is what makes the registry a backup rather than a guess.** The registry records which goals
  belonged to a unit precisely so that a deleted branch does not take that knowledge with it. A
  direct commit is a change the registry never hears about.

**Nothing prevents a direct commit, and one thing detects it.** Those are not the same guarantee,
and the difference decides what you may rely on:

- **prevention is branch protection on the host, or nothing.** Sigma neither configures nor
  requires it (§13), and no code path in the kit refuses a push to `feature/*`. **Protecting
  `feature/*` in the ordinary way turns the detection off** — see §13 before you do it.
- **detection is the rebase upkeep pass** (`feature_rebase.py`, run from `work.start()` on every
  pick of a goal that declares a unit *whose caller names its session* — see §15 — switchable with
  `work.rebase_upkeep`), which MEASURES the rule rather than trusting it. Before it force-pushes `feature/<name>` it walks that
  branch's own `--first-parent` line (`landed_commits`) and asks of every commit whether GitHub left
  a trace of the pull request that landed it — `(#N)` from a squash landing, `Merge pull request #N`
  from a merge landing (`arrived_through_a_pull_request`). One commit with no trace and the pass
  stops: the branch is left exactly as it was, and the finding is filed as a tracked issue naming
  each unaccounted-for commit. `--first-parent` is what keeps a merge landing's second-parent
  commits from reading as direct ones — and it holds only because the replay itself runs
  `git rebase --rebase-merges` (as does `/sigma-rebase`), which recreates a merge landing instead of
  flattening its commits onto the first-parent line (#2756; a plain rebase did exactly that on a
  real host repo and locked the unit on every later pick).
- **the sanctioned exit is an ack** (#2756). Commits a human has confirmed arrived through a pull
  request but lost its trace are acked with `feature_rebase.py ack .sdlc <unit> <sha>...` (or
  `--all`), which writes `.sdlc/features/rebase-acks/<unit>.json`. The ack is keyed by
  **patch-id**, so it survives the rebase it unblocks; it is read from the working tree AND from
  the remote integration branch, so landing that file on the integration branch through a pull
  request makes it every teammate's answer. Only a commit the check currently reports can be
  acked; everything else stays strict.
- **an ack is not a licence to lose content** (#161). It answers only "did these commits come
  through a pull request?", so it lets the pass reach the replay and nothing more: the replay's
  result is still measured by the §3b tree guard before any push, so an acked branch whose base
  holds a revert of its work is still refused (`would-drop`) with the remote unchanged. The same
  holds for `--rebase-merges`: it changes the replayed history's shape (merge commits in it), and
  the guard reads trees plus the branch's history with `git log -m`, so a base revert of a merge
  landing's content is refused too.

The check is narrow on purpose and its blind spots are inventoried with the model's other gaps in
§15. The short version: it runs only on a pick, and only when the branch is behind — and when it
does not run, it says NOTHING, because `current` is not in `IN_CLAUSE` and `clause()` returns the
empty string. It cannot run at all under `merge_method: rebase`, and it takes a commit subject at
its word.

### 3a. The manually-triggered companion — `sigma-rebase`

Detection is the rebase upkeep pass above; **explanation and resolution are `sigma-rebase`**
(`skills/sigma-rebase/SKILL.md`), a separate, human-attended trigger layered alongside it, never a
replacement for it. Where upkeep runs unattended, once per pick, and on a conflict does nothing
more than abandon its throwaway worktree and file a tracked issue with a raw `git` error, this
skill runs whenever a person asks, assembles a decision-context brief BEFORE touching anything —
each commit the base picked up while the branch was away, sourced from its own CHANGELOG.md entry,
its landing pull request's description, or a linked design write-up, with an honest "no written
explanation exists" floor rather than a fabricated reason — and flags the files most likely to
conflict before the rebase runs. On the clean path it rebases (autostash if dirty) and pushes with
`--force-with-lease`, exactly as this section's upkeep pass does. On a genuine conflict it shows
that file's own decision context and hands off to an interactive conflict-options walker
(`conflict_walk.py`): Recreate here, Follow the move (only when a symbol search finds a plausible
destination), Take the base's version of the whole file, or Resolve by hand — already strictly
better than a bare `git` error even for the file it cannot classify.

It resolves its base the identical way this pass does — `work.base`, unconditionally — so it works
whether or not a repo has adopted the registry below, and on any branch, not only
`feature/<name>`.

Once the tree is clean, the same skill's `verify_merge.py` runs this repo's own `verify.command`
against the current working tree and reports pass/fail plainly; only if it passes does it ask
whether to land `feature/<name>` onto the integration branch, always asked and never automatic.
Answering yes finds or opens the landing pull request and merges it with a plain `gh pr merge` —
the same human-owned act §13 describes, just carried out through this skill on explicit request,
never routed through `work.py merge()` or `unit_completion.py`'s own (deliberately merge-less)
completion signal.

### 3b. Upkeep refuses a replay that would delete what the branch has (#144)

A replay can succeed and still be destructive. The reported case: the integration branch's tip was
a deliberate **revert of the feature branch's own commits** (the work had been moved off `main` onto
the branch). Git treats those commits as already present — they are in the base's history, merely
reverted — so bringing the branch forward re-applies the revert's deletions and nothing restores
them. The pass used to report `brought forward onto main (0 replayed, 0 conflicted, 0 skipped)`,
the "nothing to do" line, over a force-push that removed 72 files, and `git log -1` looked healthy
because the new tip kept the old subject.

So before it pushes anything, upkeep compares the feature tip it started from with the replayed
head (`feature_rebase.dropped_paths`, one `git diff -M --raw` between the two trees). A path the
branch has **loses its content** in either of two ways, and both count:

- it is **missing** from the replayed head, net of a rename by the base;
- the replay **restores an older version** of it: the replayed blob, at the same path or at the
  destination of a rename, is one that path already held somewhere in the branch tip's own history
  (one `git log -m --raw` over those paths, per 200 of them). That is what a revert of the branch's
  work looks like when no path disappears: a 500-line edit rolled back to one line, or the branch's
  rename of `legacy.py` to `engine.py` undone — which rename detection alone reads as a harmless
  rename. "Anywhere in the history" includes the versions a **merge commit** created (a conflict
  resolution, or a clean two-sided merge — `git log --raw` prints nothing for merges unless asked,
  hence `-m`) and the **root commit's** versions. A **mode-only** change (a chmod: the blob is
  unchanged) is not a rollback and does not count; a mode change that also reverts content does.

Every read the guard makes pins its own git configuration (`log.showRoot`, `log.diffMerges`,
`log.follow`, `log.showSignature`, `diff.relative`, `diff.renames`, `diff.external`, colour,
`core.quotePath`), so a person's own git config cannot switch the check off or change the shape it
parses; decodes git's bytes as UTF-8 with `surrogateescape` rather than with the locale, so a
non-ASCII path is not garbled on a non-UTF-8 Windows code page; and is bounded by
`SIGMA_REBASE_GUARD_TIMEOUT` (seconds per read, default `120`). A read that times out is a refusal
that says it timed out — never "nothing dropped". These reads go through the guard's own reader, not
the injected runner every other git call here uses.

A merge-base comparison cannot do this job: the reverted commits are in the base's history, so they
sit *below* the merge-base, and "what the branch added since the fork" is empty for exactly the
content being lost. If any path loses content, the outcome is `would-drop`
(`feature_rebase.WOULD_DROP`) and the pass stops:

- **nothing is pushed** — the remote ref and the branch tip stay byte-identical, and the throwaway
  worktree is removed as on every other path;
- the pick line says so: `upkeep: feature/<name> was NOT rebased: bringing it forward onto <base>
  would remove or roll back N tracked path(s) it has (a, b, c and N-3 more) …` — a blocked pass, never shaped
  like `rebased`;
- the finding is filed as a tracked issue (up to 20 paths listed, the count always exact);
- `/sigma-doctor` shows it too: `.sdlc/state/features/<name>.rebase-blocked.json` records the
  refusal, `doctor.py features` reports the unit as BLOCKED and `doctor.py check` adds a failing
  row. The next clean pass (`current` or `rebased`) removes the record;
- `feature_rebase.py upkeep <sdlc_dir> <unit> [goal]` exits 1 on it, as on `failed`.

The check runs where the replayed head is computed, before the push and before anything else that
acts on the replay, so it is the outer guard on that result. It fails closed: a comparison that
cannot be made (either read, or a timeout) reports `failed` and pushes nothing.

The human-attended `sigma-rebase` skill is behind the same check at its **single push chokepoint**,
`rebase_brief.push_branch`, so every caller of it is covered — `attempt_rebase` (`rebase_brief.py
rebase`, and Slack's `--rebase`), `conflict_walk.walk_conflicts`'s push once every conflict is
resolved, and its manual-recovery push after a rebase a human finished with raw git. Before pushing
it compares HEAD with `<remote>/<branch>`, exactly the commit the lease would overwrite, and with the
**pre-rebase head** — the head the branch had before this rebase (#278). Everything the rebase
itself loses (pre-rebase head → HEAD) is refused. A loss already present between the remote tip and
the pre-rebase head is let through **only when it is the branch's own deliberate local deletion**
(`feature_rebase.own_losses`; narrowed by review block #2 on #278). A path `P` is exempt ONLY IF
all three hold: **(a)** the loss is a pure deletion — `P` is in the remote tip and absent from the
pre-rebase head; **(b)** a non-merge commit unique to the branch — reachable from the pre-rebase
head, from neither the remote tip nor the base nor **any remote-tracking ref**
(`git log --no-merges <pre> --not <remote>/<branch> <base> --remotes -- <P>`, full history) — has a `D` status for exactly `P` in its own diff; **(c)** the base left `P`
alone since the remote tip — no commit in `git log -m <base> --not <remote>/<branch> -- <P>`
touches it, so the deletion cannot be a base revert replayed. A local, unpushed `git rm` / "drop
obsolete" commit passes, and still does when amended or squashed (the `D` survives in a unique
commit); deleting then re-adding `P` leaves it in the pre-rebase head, so it is not a loss at all.
**A rollback is never exempt**: a path both hold but at an older version in the pre-rebase head,
and any other modification-type loss in that range, is refused even when the branch's own commit
made it — after a rebase every surviving branch commit is a rewritten copy and a commit git skipped
as already upstream leaves no trace, so a sibling commit that edited the same file would otherwise
pass for the author of a loss an earlier lossy rebase made (review block #2's repro: a pushed
commit copied to main and reverted there, a hand `git rebase origin/main` that dropped it, and a
sibling edit of the same file — `x.txt` 359 → 60 lines, which upkeep then force-pushed). A local
rollback that IS deliberate is confirmed by the human with the documented manual gesture the
refusal prints — `git push --force-with-lease <remote> HEAD:<branch>` — never silently. The same
loss left by an **earlier local rebase that was never pushed** is refused whatever its shape (the
deletion or rollback came in with the base's revert, so (b) or (c) fails — review block #1), and
so is a base revert cherry-picked onto the branch (the base touched `P`, (c)) and a deletion a
merge of the base brought in (merges never supply (b)). In a **shallow clone** nothing is exempt
and the refusal says so (the history (b) and (c) read is cut off; `git fetch --unshallow` first).
All the guard's reads for one push share one wall-clock budget, `SIGMA_WATCH_CALL_TIMEOUT` seconds
(default `120`, the fleet's own per-call bound), on top of each read's own
`SIGMA_REBASE_GUARD_TIMEOUT`; running out is a refusal that names the budget. Anything refused is
named, and the remote is
left alone. `attempt_rebase` passes the head it started from; the walker reads the stopped rebase's
own `orig-head`; the manual-recovery push reads it from the branch reflog when the newest entry is a
rebase's own landing on that branch — `rebase (finish): <its full ref> onto <sha>` (a plain,
`--apply`, `-i`, or `--continue`/`--skip`-concluded rebase, and a `pull --rebase` that stopped and
was continued), `pull <argv> (finish): …` (a `pull --rebase` that landed in one go), or `rebase
(continue) (finish): …`, all measured against real git 2.55; older gits' `rebase finished: …` is
accepted but not measured — and takes `<branch>@{1}`. The base is the walker's own brief (`walk`
resolves `work.base` for the recovery push too). When the pre-rebase head or the base is not known —
reflogs off, something newer on the branch, a caller that passes no base — no remote → pre-rebase-head
loss is exempt: that can refuse a healthy local deletion, and does not pass a loss the attribution
rule would refuse. With no remote-tracking ref and no pre-rebase head there is nothing to compare, so
nothing is refused.

The refusal separates **undoing this rebase** from **recovering content already lost before it**.
`git reset --keep <pre-rebase head>` does only the first; a prior lossy rebase can leave that head
missing the same paths. The message names the exact retained remote-tip commit as a recovery
source for paths it still contains (`git show <remote-tip-sha>:<path>`). Inspect and restore the
chosen content while preserving unpushed local commits; resetting the whole branch to the remote
tip would discard those commits.

When a refusal cannot put the branch back — `git reset --keep` itself fails, typically because
uncommitted edits are in the way — the local branch still holds the lossy replay, and a later run
would find it current and push it. The refusal is then recorded in the repository's common git dir
(`sigma-push-refused/<branch>.json`), and every push of that branch — `push_branch`, `work.rebase()`'s
two force-pushes and `work.pr()`'s push — is refused, printing `git reset --keep <pre-rebase head>`,
until HEAD is back at that head (the record then clears itself) or a person deletes the file.

A human's decision in the walk exempts **only a deletion**: a path the walker resolved with action
`removed` (ABANDON on a file the base deleted). A **content** resolution — whichever option made it —
stays guarded, because resolving one conflicted hunk says nothing about the rest of the file, where
git has already merged everything outside the markers, including a base revert's removal of
hundreds of the branch's lines (#278: one resolved line used to exempt the whole path). The walker's
option `[3]` says what it does — "Take the base's version of the whole file" — not "Abandon this
hunk". The manual-recovery push has no record of decisions, so everything it would lose refuses it.
Every refusal gives the `git push --force-with-lease` to run by hand if the loss is intended, and
names the pre-rebase head for `git reset --keep` — never the remote tip, which would throw away
local commits not yet pushed. `attempt_rebase` additionally puts the local branch back with `git
reset --keep` on a refusal; the walker leaves the local branch as the rebase left it and says how to
undo it.

A **goal** branch's own replay (`work.rebase()`: the BEHIND reconcile, `ensure_fresh` before verify,
upkeep's goal replay, and its CHANGELOG union rescue) runs the same comparison before its
force-push, scoped to the paths the goal changed since it forked: a goal whose commit reached the
base as a copy (a rebase-merge) that was then reverted is skipped by `git rebase` as already
upstream, and the replay would silently drop the goal's work. It returns `rebase refused, it would
lose content: …` — its own wording, which the loop classifies `needs_decision`, not
`merge_conflict`, because nothing conflicts (`ensure_fresh` says "the automatic rebase was
refused", not "could not apply cleanly") — pushes nothing and puts the worktree back at its
pre-rebase head. A comparison that cannot be made still returns `rebase deferred: …`. The scope keeps a plain base
deletion of a file the goal never touched from deferring every goal's rebase.

What it does **not** flag: paths the branch deleted itself (they are not in the tip), paths the
base **renamed** to a name the branch never used (rename detection pairs them), and a base edit that
produces a version the branch never had (the ordinary upstream edit). What it flags that is not a
revert of the branch — each identical to one as trees, so each is refused the same way, costing an
upkeep pass and a human decision, never data: a base that simply deleted a file the branch still
carries; a base move that rewrites the file past git's rename similarity (it reads as a deletion);
and a base that reverts its *own* older change to a file the branch carries unchanged. What it
does not see is any replay result that is a version which **never existed** — a *partial* revert
merged with other changes, or a full base revert of a file the branch has **kept editing** since;
§15 names both.

**Resolving it** is a human decision about which content the branch keeps, and either way it
reaches the feature branch through a goal branch (§3's no-direct-commits rule),
never as a commit made on the feature branch itself. To keep the branch's work, cut one goal branch
(`sdlc/<n>`) from the feature branch, merge `origin/<base>` into it and re-apply the reverted
commits in the same branch (`git revert <the revert's sha>`), then land that goal through its pull
request **as a merge commit, not a squash**: the feature branch then contains its base, and upkeep
reports `current`. A squash landing flattens the merge away, the branch is still behind, and the
next replay re-applies the revert and is refused again. To accept the
loss, land a goal that removes (or rolls back) those paths on the branch itself. Meanwhile
`work.rebase_upkeep: "off"` stops the retries (and `/sigma-doctor` stops reporting the block). If an older
Sigma already pushed such a replay, restore the old tip with
`git push --force-with-lease=refs/heads/feature/<name>:<bad-sha> origin <good-sha>:refs/heads/feature/<name>`
and check a few of the removed paths with `git cat-file -e <sha>:<path>`.

---

## 4. Declaring a unit — the two halves

An issue declares its unit **twice, on purpose**, and the duplication is the point.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  THE LABEL   feature:<name>          MACHINE-readable                    │
│              Every query, sweep and census Sigma runs filters         │
│              server-side by label. Without it the pick path would need    │
│              a body fetch per issue just to know where a goal belongs.    │
├──────────────────────────────────────────────────────────────────────────┤
│  THE BODY MARKER                     HUMAN-readable                      │
│              Two lines, in the issue body. This is what a person reading  │
│              the issue sees, and the half a person actually writes.       │
└──────────────────────────────────────────────────────────────────────────┘
```

The body marker is exactly two lines:

```
Feature: voice-interview
Branch: feature/voice-interview
```

### 4a. The marker must be BARE

**Write it as its own line, at the left margin.** Every one of these declares *nothing*:

| What you write | Why it declares nothing |
|---|---|
| `    Feature: voice-interview` (4+ spaces, or a tab) | four spaces is an indented code block |
| a marker inside a fenced code block | fenced content is not read at all |
| a marker inside `<!-- ... -->` | a declaration no human can see is not a declaration |
| `> Feature: voice-interview` | a blockquote is someone quoting the format |
| `- Feature: voice-interview` | a list marker is the same |
| `see the parent — Feature: voice-interview` | the marker owns its line, or it is a sentence |
| `Feature: we should add voice interviews` | one value per key; this is a paragraph opening |

At most **three spaces** of indent are tolerated — CommonMark's own boundary, and the reason the
fence rule is complete: an indented code block starts at four, so a marker indented that far is
already unmatchable.

The key is **case-insensitive** (`Feature:`, `feature:`, `FEATURE:` are one key); `Featue:`,
`Features:` and `feature=` are not that key at all and read as prose. The **value is not**
case-folded when it is checked as a name, but two spellings that differ only in case are treated as
one declaration, because GitHub label names are case-insensitively unique and a repo cannot hold
both.

> **If you fence a marker you meant,** Sigma prints one line on stderr saying so. The return
> value is still "no declaration" — the note is what makes the silence discoverable.

Every document that teaches this format, including this one, shows the marker inside a fence. That
is not an accident: it is exactly why fenced content is never parsed.

### 4b. The `Branch:` line is a cross-check, never a second source of truth

The branch is **derived** from the unit — `feature/<name>` — so the `Branch:` line exists only to
catch a body that contradicts itself.

| `Feature:` | `Branch:` | Verdict |
|---|---|---|
| `voice-interview` | `feature/voice-interview` | agrees |
| `voice-interview` | `feature/voice-interview/timing` | agrees — a sub-branch of the unit |
| `voice-interview` | `feature/voice-interview` **and** `feature/voice-interview/timing` | agrees — several agreeing lines are not rivals |
| `voice-interview` | `feature/billing` | **raises** |
| `voice-interview` | `sdlc/1465` | **raises** |
| *(absent)* | `feature/voice-interview` | declares nothing at all |

**`Branch:` names the branch of the UNIT, never the goal's own.** This is worth stating out loud
because the key is genuinely ambiguous: `sdlc/<goal-id>` is what Sigma cuts for every goal, so a
writer could reasonably read `Branch:` as "the branch this goal is on". It is not, and
`Branch: sdlc/1465` raises deliberately.

### 4c. The five verdicts, and the body-wins rule

Reading an issue produces exactly one of five states. Nothing collapses them.

| Verdict | Body | Label | The unit is… |
|---|---|---|---|
| `agree` | declares `x` | `feature:x` | `x` |
| `body_only` | declares `x` | absent | `x` — and the label is attached at pick (§7) |
| `label_only` | no marker | `feature:x` | `x` |
| `conflict` | declares `x` | `feature:y` | **`x` — the body wins** |
| `none` | no marker | absent | none — the goal bases on the configured base, exactly as today |

**On a conflict the body wins.** It is what a human wrote, where the label is what a machine
attached. The verdict still carries both sides, so anything acting on a conflict can name what it
chose between — and at pick, a conflict is announced rather than silently resolved, because base
resolution reads the body while most other machinery reads the label.

### 4d. The one thing that is not a verdict: an issue contradicting itself

Two rival `Feature:` lines, a `Branch:` line that disagrees, or **two distinct `feature:` labels**
raise instead of resolving. There is no honest verdict for "this one side contradicts itself", and
first-wins would base a goal on a branch nobody chose.

The cost is bounded on purpose: it refuses **one goal, never the queue**. Every sweep over many
issues catches it per issue, treats that one issue as unresolvable, and carries on.

---

## 5. What a unit may be called

A unit name becomes four things: a `feature:<name>` label, a `feature/<name>` branch segment, a
`units/<name>.json` shard, and a `<name>.md` file. One rule governs all four.

**A unit name is one thing git accepts as a branch segment.** Measured against `git check-ref-format`,
not assumed:

```
   LEGAL              voice-interview   int-contract   billing2
                      v1.2              voice.LOCK     a.lockfile

   NOT LEGAL          a/b          a '/' at all — ambiguous with the model's own sub-branch
                                   shape, so it is refused rather than guessed at
                      .hidden      git: a segment may not begin with '.'
                      TBD.         git: a segment may not end with '.'
                      v1..2        git: no segment may contain '..'
                      voice.lock   git: no `.lock` suffix — CASE-SENSITIVE, so `voice.LOCK`
                                   above is legal and this is not
```

**The `.lock` rule is case-sensitive**, and this is not a detail: git rejects `voice.lock` and
accepts `voice.LOCK`, so a case-insensitive version of the rule would reject a name git allows. The
name must also start with an alphanumeric.

`Feature: TBD.` at the start of a line is ordinary hand-written issue prose. Before this rule
existed it declared a unit git would then have refused.

---

## 6. Base resolution

This is the whole point of the model, and it is three lines of precedence under one condition:

```
   0.  ONLY where `.sdlc/features/` exists   →  otherwise line 1 does not apply at all
   1.  the unit THIS GOAL'S ISSUE declares   →  feature/<name>
   2.  else `work.base` from config          →  whatever you configured
   3.  else the current HEAD                 →  exactly as before any of this existed
```

**Line 0 is an adoption condition, not a formality.** A declaration moves the base only in a
repository that adopted the model: `work._declaration_moves_the_base` requires the same
`.sdlc/features/` directory §14 lists. Without that condition, a repository that never heard of
this model would have its first pick retargeted by a label it has used as an ordinary issue
CATEGORY for years — the label parse strips after the prefix, so the colon-space form parses too,
measured:

```
   'feature: auth'    →  'auth'          'feature'        →  None
   'feature: request' →  'request'       'feature:voice'  →  'voice'
   'FEATURE:Billing'  →  'Billing'
```

That repository's goal would be cut from `feature/auth`, a branch nobody ever cut, and the `fetch`
in §6d would fail with the goal already claimed. With line 0 it is cut from `work.base` instead.
Measured, one goal declaring `voice-interview`, the same issue and the same runner both times:

```
   .sdlc/features/ exists?  False  →  git worktree add -b sdlc/1467 … origin/main
   .sdlc/features/ exists?  True   →  git worktree add -b sdlc/1467 … origin/feature/voice-interview
```

**The condition gates the BASE, never the READ.** The declaration is still fetched on an unadopted
repository and still reaches the registry pass — which is the only thing that distinguishes a
project that meant to adopt and forgot `mkdir -p .sdlc/features` from one that never wanted the
model, because that pass is what names the missing directory and the gesture that creates it. So no
row of §6e moved: the call list is byte-identical on both sides of line 0, and only the branch the
worktree is cut from differs.

**Declaring no unit is unchanged behaviour, and that is a guarantee about the BASE.** A goal with
no declaration runs the identical fallback expression it always ran, and outside the gate in §6a
base resolution makes no call at all.

**It is not a guarantee about what a pick COSTS.** Scoping that correctly matters, because the
rest of this model is not gated the same way — §6e is the measured list, and §14 is the adoption
consequence.

**It is not a guarantee about whether a pick happens at all, either — that is a different axis
from BASE and always has been.** This section fixes which branch a goal declaring no unit is cut
from; it says nothing about *pickability*, the question of whether the goal is offered to the
picker in the first place. A separate, opt-in structural rule (sigma#2253) may hold a goal
declaring no unit aside from picking entirely, on repos that opt in — the same shape
`sdlc:needs-label` already uses for a goal declaring a unit whose label does not exist: membership
kept, `sdlc:goal` intact, picked by nothing until a human resolves it. That gate, where a repo
enables it, sits upstream of this section's own base-resolution call, not inside it — the base a
held goal *would* resolve to if picked is unaffected, exactly as this section already promises;
only whether the pick happens at all is a different rule's to answer. §18 is that rule in full,
including the `core` unit it may attach instead of setting the goal aside.

### 6a. When the declaration is read

Two conditions, both load-bearing: `discovery.source == "github"` **and** the goal's stem is a
number. `isdigit()` alone is not enough — a local goal file can be `.sdlc/goals/0002.md`, and
reading "issue #2" of whatever repo the checkout happens to point at could base a local goal on a
stranger's feature branch.

The read is one REST call (`gh api repos/<slug>/issues/<n>`), deliberately not `gh issue view`:
`issue view` is GraphQL under the hood and GitHub meters GraphQL on a separate hourly budget, which
has been exhausted on this loop before while REST still had headroom. The slug comes from
`discovery.github.repo`, falling back to gh's own `{owner}/{repo}` placeholders — the goal *number*
came from that source, so the read has to go back to the same repo.

> **That argument does not extend to the pick as a whole, and the honest version has to say so.**
> The label step in §7 reads the same issue again through `gh issue view --json body,labels`, which
> **is** the GraphQL surface the paragraph above avoids, and it does so on **every** pick a GitHub
> source makes. So base resolution keeps GraphQL off its own path and the model spends it one line
> later anyway. §6e prices it.

### 6b. When the read fails

**Fail-open, with a note.** A transport hiccup must not stop a goal that would have run fine
yesterday. But "the read failed" is **not** "the issue declares nothing", and collapsing the two is
exactly how unit work lands on the integration branch with no trace. So:

- the goal proceeds on the fallback base;
- the one-line result says the question went unanswered;
- `base_resolved: false` is written into the goal's worktree record **and** into the durable action
  log, so the fallback leaves a trace something can act on later rather than a line of stdout nobody
  reads at 3am.

An empty reply, a `null` or a `[]` counts as a **failed read**, not as an absent declaration: a real
REST reply for an issue is always a JSON object.

### 6c. The one thing that is not fail-open

An issue that contradicts **itself** (§4d) refuses the goal, loudly, rather than defaulting to the
configured base. Defaulting there would silently base unit work on the integration branch.

### 6d. A declared unit whose branch does not exist

Fails **closed**, and it needs no rule of its own. After the base is resolved, `start()` runs the
registry sync (§8f) and then `git fetch <remote> <base>` — and git refuses a ref it cannot find.
The error is re-raised with the one fact git cannot know — *where the base came from* — so an
operator whose config says `base: main` is not left staring at a ref name with no route back to the
issue body. (The sync deliberately runs **before** that fetch, which is why the ordering is worth
stating in both places: the fetch is what fails, and it is not the first thing to happen.)

### 6e. What a pick actually costs now, measured

Every row is a call that did **not** exist before this model. **The first three rows run whether or
not the repository ever adopted it**; every row below them opts out on a missing `.sdlc/features/`.
Those three are the adoption hazard §14 opens with, and the whole reason this is a table rather than
a sentence saying "nothing changed".

**The unit of cost is not the same in every row**, and getting that wrong understates the total.
Some are paid per goal the loop **considers**, one per goal it **claims**, most per goal it actually
**starts**, and one per sweep. A goal that is considered and refused still paid for itself.

| Call | Transport | Paid per | Gated on `.sdlc/features/`? |
|---|---|---|---|
| `gh issue view <n> --json body,labels` | **GraphQL** | goal **considered** | **no** |
| the `feature:<name>` label lookup | **GraphQL** | goal **considered**, and only while a declared label is still absent | **no** |
| `gh issue list --label sdlc:needs-label` | `gh` | **sweep** — one per `_next()` that is not budget-halted | **no** |
| a second read of the same declaration, over REST, when the first one failed | REST | goal **considered**, and only on a failed first read | **yes** |
| the scope gate's own repo resolution — `git remote get-url <remote>`, **local** | git | goal **considered**, and only while `discovery.github.repo` is unset | **yes** |
| the ownership gate's repo resolution — the same local read again | git | goal **considered**, same condition | **yes** |
| `gh issue view <n> --json author` | **GraphQL** | goal **considered**, and only where the unit has an owner and no grant (§12) | **yes** |
| the cross-repo access check — one issue read, then one access probe per repo the unit names | `gh` | goal **claimed** — after the claim is durable, before anything has been built | **yes** |
| `gh api repos/<slug>/issues/<n>` | REST | goal **started**, in github mode with a numeric stem | no — gated on §6a instead |
| `git ls-remote --heads <remote> 'feature/*'` — **twice, not once** | git | goal **started**: once for the registry reconcile (§8f), once for rebase upkeep (§3) | **yes** |
| the rest of rebase upkeep (§3) — one `fetch`, one `push --force-with-lease`, and nine local git calls around them (plus one local `git log -m` per 200 paths the replay modified or renamed, §3b) | git | goal **started**; the `push` only where the feature branch is actually behind its base | **yes** |
| the sibling registry copy (§15) | `gh`, Contents API | goal **started** × sibling repo, and only on a `granted` verdict | **yes** |

**How this was measured, and how to redo it — because it went stale once and will again.** Every
git and `gh` call on the start path goes through one injected runner, so the *started* rows are
counted by driving `work.start()` against a throwaway project with a recording runner and diffing
an adopted project against one with no `.sdlc/features/`: for one goal declaring a unit whose branch
is behind its base, **4 calls become 17** — one `ls-remote` for the registry reconcile, and twelve
for rebase upkeep (its own `ls-remote`, a `fetch`, a `push --force-with-lease`, and nine local git
calls — the ninth is #144's pre/post tree comparison, §3b, which runs through the guard's own
config-pinned reader rather than the injected runner, so the measurement records it explicitly). **The baseline itself is 4, not 3, because of one call this table's own rule excludes**:
`work.start()`'s dirty-root-checkout guard (#2014) runs a `git status --porcelain` unconditionally,
on every start whether or not `.sdlc/features/` exists — so it fires before either project in this
diff has a chance to differ, and correctly never appears as a row above (it did not exist before
this branching model either, but it is not a cost the model *added*, so a row here would misstate
what a reader is being asked to budget FOR). The *considered* rows are counted the same way at each gate's own entry point
(`feature_labels.attach_at_pick`, `feature_propagate.gate_at_pick`, `feature_owner.gate_at_pick`,
`cross_repo.check_at_pick`), each run twice — with the directory and without it — which is how the
last column was decided rather than assumed. A conditional row carries its condition instead of a
number, because the condition is the honest answer: one would overstate the common case and zero
would understate the case the row exists for.

**"Considered" is the row to budget against, and a run of refusals is where it bites.** The label
step runs inside the loop's candidate loop: a goal it refuses is skipped and the *next* candidate is
read, so a single `_next()` call can spend one `gh issue view` per refused goal before it starts
anything. Ten goals at the head of the queue all declaring units whose labels do not exist cost ten
GraphQL reads and produce one started goal — and, because §14's hazard is not gated on adoption,
they cost that on a repository with no registry too.

Two rows are worth reading twice. The label lookup **caches only names it FOUND**, never names it
did not — which is what lets a label a human created a moment ago be seen on the next pick, at the
price of re-asking while it is absent. And the sweep is one `gh issue list` that carries every held
issue's body and labels, so releasing N goals costs no per-issue reads.

---

## 7. At pick: the label, and `sdlc:needs-label`

The body is what a human writes; the label is what everything queries. So at pick, on the
`body_only` verdict — the body declares a unit and the label is missing — Sigma reconciles the
two halves:

```
   the label EXISTS, this issue lacks it   →   ATTACH it            (automatic)
   the label DOES NOT EXIST                →   REFUSE the pick      (a human acts)
```

**Sigma never creates a `feature:` label.** Attaching is additive, reversible and adjudicates
nothing — a human wrote the unit in the body and the label is the mechanical projection of that.
*Creating* one mutates the repository's label namespace permanently, and a single typo in a body
marker would mint junk that outlives the unit. Feature labels are never deleted (`open: false` is
how a finished unit is marked), so there is no cleanup path either.

The guarantee is structural, not a convention: the refusal sits at the single chokepoint every `gh`
call in the GitHub source passes through, so a `label create` carrying a `feature:*` argument is
refused **on every path**, including the ones in other modules that reach for the runner directly.

> **The bound on that claim, stated rather than implied.** What is refused is the `gh label create`
> verb. `gh label clone` (which copies every label of another repo, `feature:*` included),
> `gh label edit --name`, a delete-plus-recreate, and the REST form `gh api -X POST .../labels`
> would each still mint one. None of them appears anywhere in this tree — so this is a bound on the
> claim, not a hole in it.

### 7a. What a refusal looks like

A refusal is **not a park.** The goal keeps `sdlc:goal` — the membership every sweep, census, mirror
and reclaim path queries by — and gains an overlay of its own:

```
   sdlc:goal  +  sdlc:needs-label      visible to everything, picked by nothing
```

`sdlc:needs-label` is an OVERLAY in exactly the sense `docs/label-model.md` §2 defines, and that
document's §2b-i is its full lifecycle. It is a label Sigma *does* create — it is one of the
lifecycle labels the GitHub source ensures on a fresh repo — unlike `feature:*`, which it never
does.

Why not one of the labels that already exist:

| Alternative | Why not |
|---|---|
| `sdlc:parked` | gives up membership, and only a human undoes a park — two human gestures where one suffices, for a condition that is often momentary |
| silence (skip it, say nothing) | the goal stays in the pickable queue, so `/sigma-triage` reports a permanently-refused goal as READY TO PICK, and every later slot re-reads it |
| `sdlc:blocked` | the auto-unpark sweep believes it owns every instance of that label and strips it whenever the body names an already-closed dependency — so the two sweeps flap against each other forever, at two label swaps, two board moves and one false "blocker closed" comment per cycle |

**It self-heals, and the human's gesture is exactly one.** A sweep runs before every pick and removes
the overlay the moment the missing label exists. Create the label (or fix the typo) and the goal is
pickable on that very call — there is nothing to un-park and nothing else to do. The **label is the
attribution**: nothing else writes it, so every issue the sweep finds is one Sigma holds. There
is no side record to consult, and therefore no way for the state's own recovery to depend on an
optional feature.

### 7b. Every outcome of the pick-time check

| Outcome | Pick proceeds? | Overlay | Flag comment |
|---|---|---|---|
| label attached | yes | — | — |
| nothing to do (`none`, `agree`, `label_only`) | yes | — | — |
| **conflict declared** | yes, **and it says so** | — | — |
| the issue could not be read | yes — a read we could not make is evidence of nothing | — | — |
| **the label does not exist** | **no** | yes | yes |
| **the issue contradicts itself** | **no** | yes | yes |
| the label lookup could not answer | **no** | **no** | **no** |
| the attach write did not land | **no** | **no** | **no** |

The last two rows are the transient directions and they are deliberately identical: a network blip
is not evidence a label is absent, so it must not summon a human or write a state — but it must not
be read as "exists" and let the goal start against the wrong base either. The comment that *would*
have been posted says the label is missing, which on the write-failure path is simply false.

**A conflict proceeds and no label is written.** Swapping the label would remove something a human
put there, and this step was not asked to adjudicate. Saying nothing was the bug: base resolution
reads the **body** while everything else reads the **label**, so the worktree gets cut from one unit
while the work is recorded under the other, silently.

---

## 8. The registry — `.sdlc/features/`

**Remote is the truth.** The `feature/*` branches that actually exist *are* the live set of units.
The registry is not that; it is the **committed backup**, because a remote branch can be deleted —
by cleanup, by accident, by a merge — taking with it every trace of what that unit was and which
goals belonged to it.

### 8a. The layout

```
.sdlc/features/
├── index.json              the CHART SHEET — the whole registry as one document
├── units/
│   ├── int-contract.json   one file per unit — THE WRITE SURFACE
│   └── voice-interview.json
├── int-contract.md         the human-readable per-unit file (§9)
└── voice-interview.md
```

> **This is the first of three places the code amends the design.** The design draws only
> `index.json` plus the `.md` files. Its own requirements then say that a write for unit A must
> never open unit B's file, and that two concurrent picks on different units must never touch the
> same path — and neither is satisfiable by one shared JSON document, where every pick rewrites the
> same file and every pair of concurrent picks becomes a merge conflict on the one file that exists
> to be a backup. **The requirement won over the drawing.**

The three rules that follow from it:

- **`units/<name>.json` is what a pick writes.** A write opens that path and nothing else — not
  `index.json`, not another unit's file, not any `.md`. Two concurrent picks on *different* units
  are therefore structurally incapable of colliding.
- **`index.json` is derived.** It is the snapshot a fresh clone carries and the form the registry is
  propagated in. It is written only by an explicit materialisation, **never on the pick path**.
- **A read is their union, and a unit's own shard WINS for its own unit.** A shard is a write that
  has not been folded into the sheet yet, so it is the later statement. It **replaces** rather than
  merges: two half-descriptions of one unit merged together invent a third that neither source ever
  said.

**Why `units/` is a subdirectory** rather than `<name>.json` sitting beside `<name>.md`: `index` is
itself a legal unit name, so a per-unit file in the same directory would one day *be* the chart
sheet. A subdirectory removes the collision instead of reserving a name against it.

**`.sdlc/features/` is deliberately not gitignored.** A gitignored backup is not a backup. (The
per-unit locks live under `.sdlc/state/` for the opposite reason — that path already is.)

### 8b. The schema

Both files carry the same document shape, versioned, because `index.json` is copied wholesale
between repos and is therefore genuinely untrusted input:

```json
{
  "schema": "sigma/features@1",
  "features": {
    "int-contract": {
      "title": "Manifest \u2194 video duration contract",
      "owner": "@unit-owner",
      "open": true,
      "parent": null,
      "tracking_issue": "acme/app#412",
      "priority": "P1",
      "repos": {
        "acme/app": {
          "branch": "feature/int-contract",
          "owner": "@app-owner",
          "authorized": true,
          "goals": [2871, 2879]
        },
        "acme/api": {
          "branch": null,
          "owner": "@api-owner",
          "authorized": false,
          "goals": []
        }
      }
    }
  }
}
```

| Field | Meaning | The rule |
|---|---|---|
| `schema` | the version key | a document that does not carry **exactly** this string contributes **nothing** — reading a `@2` document as `@1` is the failure the key exists to prevent |
| `title` | display | a non-string degrades to the **empty string**, never to absent — so a unit always has a title field, and §9 renders it as empty backticks rather than as `—` |
| `owner` | the **unit's** owner | see §12 |
| `open` | is the unit live? | **only a real boolean `false` closes it.** `"false"` the string does not |
| `parent` | the unit this one is a sub-child of | kept **verbatim**, even a value that is not a legal unit name — it is never turned into a path, and the registry is a record, not a cache |
| `tracking_issue` | the issue that owns a cross-repo pair | free text |
| `priority` | the **unit's own** tier — intended as a tie-break among issues that are already eligible, never a rank written onto any member issue. **Nothing reads it for ordering yet**: the field is recorded, and the queue comparator that will consult it is separate, later work | free text, kept **verbatim** and never validated here — ranking is `discovery.priority_rank`'s single job wherever a reader eventually asks, and that function accepts `P0`–`P4` in any case, tolerates the `priority:` prefix, honours an adopter's configured aliases, and ranks anything else as *unprioritised* rather than guessing. **Absent is not P4** — it means the unit is unranked. One value per unit, not per repo. Set it through the locked whole-entry write (`feature_sync.amend`), never by writing a one-field entry — that replaces rather than merges |
| `repos.<owner/name>.branch` | the branch recorded for this unit **in that repo** | |
| `repos.<owner/name>.owner` | the **repo's** owner | see §12 |
| `repos.<owner/name>.authorized` | the per-unit grant | **absent means false. Only a real boolean `true` grants** |
| `repos.<owner/name>.goals` | issue numbers, de-duplicated, in **pick order** | not sorted — the sequence is itself part of what the backup remembers |

The two defaults point in opposite directions, and both point at *the state you get by doing
nothing*: under a truthiness test the string `"false"` would **grant**, which is the exact opposite
of what it says; and closing a unit is what makes its goals stop being recorded, so the safe guess is
that nobody closed anything.

**A field added inside `@1` is lost on every install that predates it, and that was the choice.**
The read side is a whitelist: a key it does not know is dropped on the way through, and the reduced
form is what gets written back — so a peer on an older plugin does not merely fail to read a newer
field, it **erases** that field from a sibling's file on the next cross-repo propagation. Bumping
the version is the loud alternative, and it is not the narrower one: a document declaring a version
the reader cannot read contributes *nothing at all*, so a bump trades one optional field for every
unit, every recorded goal and every grant on every install that has not upgraded. `priority` was
added inside `@1` on that reasoning, deliberately — an install that does not know it orders its
queue exactly as it did before the field existed, which is the state every repo is in today. **This
is a judgement per field, not a standing yes:** a field whose absence would make an older peer do
the *wrong* thing, rather than the *previous* thing, still needs the bump.

**Both files are written ASCII-only** — which is why the title above reads `\u2194` and not the
character itself, while `<name>.md` (§9) renders the real one. That is not cosmetic: the read side
accepts anything, including a lone surrogate out of a propagated `index.json`, and encoding one as
UTF-8 raises — so folding the registry back into the sheet could die on data the registry had just
handed out. Nothing is lost; only the on-disk spelling changes, and `<name>.md` is the file humans
read.

### 8c. Reading never fails. Writing fails on exactly one thing.

**The read side is total.** A missing directory, a missing file, truncated JSON, undecodable bytes, a
document nested deep enough to blow the recursion limit, a schema version this code cannot read, a
directory where a file should be — every one degrades to "no units known", or for one bad unit file,
to "that one unit is not known". A registry that throws takes the pick path down with it, which is
strictly worse than a registry that is merely empty.

**One unreadable shard costs exactly that one unit, and costs it outright.** It does *not* fall back
to the chart sheet's older entry. The shard is the write surface, so it is authoritative for its own
unit, and the sheet's entry is by construction the state *before* the write that produced the shard.
Serving it would answer a question about a unit with information known to be superseded. An absent
unit makes the caller look; a stale one does not. The cost is named plainly: this is strictly less
available than falling back would be.

**The write side raises on exactly one thing** — a name that is not a unit name. A caller writing
`../../etc/passwd` has a bug, not a corrupt file, and there is no path such a name could safely
become. It is a named exception (a `ValueError` subclass), so catching the contract you were told
about cannot accidentally catch an encoding failure from somewhere else.

**It REFUSES on one other thing, and refusing is not raising.** A read-modify-write over a shard
that cannot be read is abandoned rather than restarted from `{}` — the file is left byte-identical
and the pass reports `refused` with the shard's path and the gesture that clears it. That is the
one case where the read side's "that unit is not known" must NOT be taken at face value by a
writer: to a reader an unreadable shard and an absent one are the same answer, and a writer that
believed it would replace a hand-written entry — title, owner, tracking issue, sibling repos, every
`authorized: true` — with a one-goal stub, and report success. §12 requires humans to hand-edit that
file, so a misplaced comma is the ORDINARY way an unreadable shard comes to exist. Measured against
a shard with a doubled comma:

```
   amend  →  changed: False, written: False, refused: "…cannot be read, so it was left exactly as
             it is rather than replaced by a one-goal stub. Repair or delete that file…"
   file   →  byte-identical
```

The refusal is a divergence (`shard-unreadable`) like any other, so it reaches stderr and the pass
that found it carries on: one unreadable unit costs that unit and no other.

### 8d. Pass the WHOLE entry, never a delta

**This is the caller's obligation and it is not optional.** A read *replaces* a unit's entry with its
shard rather than merging the two, so whatever a write omits is **gone** from the effective registry:
a write that knows only its own repo and names only that repo erases the unit's other repos, their
goals, its `tracking_issue`, its `parent`, and any field outside the schema. The loss becomes
permanent the moment anything folds the registry back into the chart sheet.

The safe shape is read-modify-write: read the effective entry, amend it, write the whole thing back.
The registry module deliberately ships **no helper** for that, because doing it safely under
concurrency is the pick path's problem (§8e), not something a helper at that level could honestly
promise.

### 8e. Concurrency: the per-unit lock, and what `landed` means

Two picks on the **same** unit are not exotic — it is what a unit of work *is*, and what a parallel
drain produces by design. Measured at real process level: **10 processes, one unit, one goal number
each, five runs — 5 to 6 of 10 goal numbers lost per run.** Never corrupt; every file parsed. Just
silently absent, which is worse than a crash because nothing looks wrong.

The answer is a **per-unit `flock`**, held across the whole read-modify-write. It is kernel-mediated,
so it has no stale-lock problem: a holder that crashes releases it. It is **bounded, not blocking** —
a wedged holder costs this pick its serialisation and nothing else, because a pick that hangs forever
on a lock is a worse outcome than the loss the lock exists to prevent.

**It fails open**, on a platform with no `fcntl`, a filesystem that cannot flock, or a holder that
did not release in time. And the honesty about that path is structural rather than promised:

```
   written      the shard on disk said what we wrote, AT THE MOMENT WE LOOKED.
                Provable. This is what "the goal was recorded" rests on.

   serialised   the read-modify-write happened under an exclusive lock.

   landed       written AND serialised, and NOTHING ELSE.
                The only field that means "recorded, and nothing could have taken
                it away" — so it is the only one that must never overstate.
                On the fail-open path it is False even when the write plainly went
                in, because False there is the true statement.
```

Measured on the fail-open path: **40 of 40 writes reported success and 11 to 16 of them were absent
afterwards.** A retry narrows the loss and is exact for a write that never landed at all, but it
cannot see a clobber that arrives after it looked. So the pass **re-reads at the end** and reports
`lost` for a goal number that is no longer there — a *measurement*, not an inference.

> **The residual, named rather than left to be discovered:** a clobber that arrives after that final
> re-read is invisible to this process, and no lock-free scheme on POSIX can see it. That is a reason
> to have the lock, not a reason to pretend the fallback is one.

### 8f. What a pick actually does to the registry

On every goal pick, before the fetch that cuts the worktree:

1. **record the goal** under the unit its issue declared, for this repo;
2. **cross-check** what the registry claims against the branches that actually exist;
3. **regenerate** the managed block in `<name>.md` (§9).

It runs **before** the `git fetch`, deliberately: a brand-new unit has no branch yet, and that fetch
correctly fails closed on exactly that — so a sync placed after it would miss the first goal of every
new unit, which is the one goal that creates the entry.

It is **advisory and never blocking**. A registry is a record; losing one is bad, and losing the
*goal* because the record could not be written is worse.

It is **not reached on a resume.** A supervisor relaunch returns "already started" before this
point, so it neither records nor reconciles — the goal was recorded by the start that cut the
worktree, and a resume adding it again would be the duplicate that idempotence exists to prevent.

**The reconcile rules, and the two approximations in them:**

| Finding | What it means | What is done |
|---|---|---|
| `branch-missing` | the entry named a branch and it is not on the remote any more | the entry stops claiming it; **every goal recorded against it is kept** |
| `branch-absent` | the picked unit's branch does not exist yet | the entry claims no branch; this is the *ordinary* first pick of a new unit |
| `closed` | the unit has no branch **and** no recorded goals | `open: false`; the entry and its file are kept |
| `picked-while-closed` | a goal was picked onto a unit somebody closed | recorded, and the unit is **left closed** — two humans disagree and the registry says so rather than settling it |
| `remote-unreadable` | the branch list could not be read | **nothing is judged at all** |
| `no-repo` | no `owner/name` could be resolved | the goal is recorded nowhere, and the gap is reported |

- **`None` and "no branches" are never the same answer.** Reading "the remote could not be read" as
  "the remote has no feature branches" is the single most destructive bug available here: it would
  delete every recorded branch and close every unit, in one pass, silently, on a laptop that happened
  to be offline.
- **Only this repo's half is judged.** A sibling repo's branch was never measured, and unmeasured is
  not "gone".
- **Only inside the `feature/` namespace.** The live set is fetched with a `feature/*` pattern, so it
  cannot contain `main` — measuring a recorded `main` against it would report a deleted branch on the
  strength of a question that was never asked.
- ***"No open goals" is read as "no goals recorded".*** **A deliberate approximation, named here
  because it is exactly the kind of thing a reader assumes is exact.** Knowing whether issue 2871 is
  still open costs one network read per goal per pick, which this path will not spend. It errs
  towards leaving a unit **open**, which is the safe direction.
- **`open` is never set back to `true` here.** Only a human (or the field's own default) opens a
  unit.

**Folding the shards into `index.json` is a separate, explicit act** — run
`python3 <plugin>/skills/sigma-loop/scripts/feature_sync.py fold .sdlc`, where `<plugin>` is the
installed Sigma directory. It never happens on a pick: `index.json` is the one file every unit
would share, and folding it back in on every pick would restore exactly the merge conflicts on
exactly the file that exists to be a backup.
**A folded shard is never removed**, either: a shard written by a concurrent process between the read
and the delete would be deleted having never been folded, and the union is already correct with both
present.

---

## 9. The managed block in `<name>.md`

`units/<name>.json` is the machine's half of the registry. `<name>.md` is the half a person opens.
**One file, two owners:**

```
<!-- sigma:begin managed sha256:<digest> -->
... regenerated from the registry — do not edit ...
<!-- sigma:end managed -->

## Notes
Anything a human writes below is theirs and is never touched.
```

A real one, rendered from the entry in §8b:

```
<!-- sigma:begin managed sha256:<digest> -->
<!-- Generated from the feature registry. Do not edit inside this block: it is
     regenerated on every sync, and an edit here is overwritten and reported to the
     ledger. Write below the end marker instead. -->

# int-contract

- **title:** `Manifest ↔ video duration contract`
- **owner:** `@unit-owner`
- **open:** yes
- **parent:** —
- **tracking issue:** `acme/app#412`
- **priority:** `P1`

| repo | branch | owner | authorized | goals |
| --- | --- | --- | --- | --- |
| `acme/api` | — | `@api-owner` | no | — |
| `acme/app` | `feature/int-contract` | `@app-owner` | yes | 2871, 2879 |

<!-- sigma:end managed -->

## Notes

Anything written below the end marker above is yours and is never touched.
```

A value is delimited by backticks so that **absent** (no delimiters) and **empty** (empty
delimiters) stay distinguishable — two different registry states that a shared blank spelling would
collapse into one, silently editing the backup on the way through.

### 9a. The four rules, in precedence order

1. **The registry is authoritative for structured state.** The block is rendered *from* an entry.
   Prose is never parsed to recover state on the normal path.
2. **The region outside the block is human-owned and never rewritten.** The file is handled as
   **bytes** from end to end and the human region is never decoded at all: reading it as text would
   translate CRLF to LF and replace undecodable bytes, and writing it back would then silently
   rewrite prose that was promised untouched — while every result-shaped test still passed.
3. **A hand-edit inside the block is overwritten, and the divergence is reported.** Silently
   discarding somebody's edit is the one outcome to avoid; overwriting it while saying so is fine,
   because the block is marked as not theirs.
4. **A `<name>.md` with no entry in the registry** is recovered from if its block still parses, and
   otherwise **left untouched and flagged** — never deleted, never truncated.

### 9b. Why the begin marker carries a digest

> **This is the second place the code amends the design.** The design's marker carries no attribute.

Rule 3 has to separate *"a human typed in the block"* from *"the registry changed"*, and the two
obvious tests cannot:

```
   compare the block on disk with the block about to be written
     → reports a divergence on EVERY ordinary registry change.
       A signal that fires every time is not a signal.

   re-render what the block parses to, and compare
     → looks stricter; is blind exactly where it matters. A hand-edit that
       changes a TITLE leaves a perfectly canonical block, so the re-render
       matches, the check passes, and the edit is discarded in silence —
       the precise failure rule 3 exists to prevent.
```

So the begin marker carries `sha256:` plus 16 hex characters, over the block's body. A body its
digest vouches for was last written by Sigma; one it does not vouch for was not, and is reported.
It is **tamper-evident, not tamper-proof** — someone who recomputes a digest to hide an edit has
decided to hide it, and no marker in a text file can stop that. The truncation to 64 bits is
deliberate: the property needed is "an edit changes it", and this line is the first thing a human
sees in the file.

### 9c. No digest is "cannot prove", not an accusation

A block with **no** digest at all — one somebody wrote by hand, or one whose attribute a person
deleted — cannot vouch for itself. *"Cannot prove nothing was discarded"* has to fail towards saying
so, so it is reported. But it lands in an outcome of its own, with wording of its own and **no
addressee**: a format upgrade is not somebody's business to answer for. It is **one report per file,
once**; after that the block carries a digest and it never fires for that file again.

That landing place is **permanent, not transitional**, and the reason matters: a person *cannot*
hand-write a valid digest, because it covers a body they have not written yet. No amount of
documenting the attributed form changes that.

> **⚠ A document that prints this marker MUST use a placeholder that cannot be read as a digest.**
>
> ```
>    sha256:0123456789abcdef       → parses as a digest → MISMATCH → accuses a NAMED OWNER
>    sha256:<digest>               → parses as absent   → cannot-prove → benign, one report
> ```
>
> A plausible-looking concrete digest in a document is **strictly worse than printing no attribute at
> all**: every file copied from it lands as a divergence naming somebody, for exactly the population
> the benign outcome exists to protect. Sixteen lower-case hex characters is the shape to avoid —
> fifteen is safe, upper case is safe, and so is a word, an ellipsis or an angle-bracketed
> placeholder. **This document uses `<digest>`, which does not parse.**

### 9d. Every outcome of a sync

| Outcome | The file was… | Anything reported? |
|---|---|---|
| `created` | absent; a whole file was written | — |
| `unchanged` | already exactly right; **nothing written** | — |
| `updated` | vouched for, and the registry moved | — |
| `overwritten` | **not** vouched for; regenerated | yes — divergence, addressed to the unit's owner |
| `stamped` | carried **no** digest; regenerated and stamped | yes — **no addressee**, no accusation |
| `prepended` | had no block; the block went **above** it, every byte kept | — |
| `flagged` | markers damaged, or unreadable — **nothing written** | yes |
| `recovered` | no registry entry, but the block still parses — **untouched** | — |
| `orphaned` | no registry entry and nothing recoverable — **untouched** | yes |
| `nothing` | no entry and no file | — |

**Ambiguity is `flagged`, and that is a refusal to guess rather than a failure to parse.** With the
end marker missing there is no boundary, so any splice would eat somebody's prose. With the markers
**doubled** — which is exactly what a file that *quotes* the format produces, and the adopter doc for
this format does — picking a pair means picking which prose to destroy.

**`prepended` has one named cost:** YAML front matter has to begin at byte 0, so a `<name>.md` that
opened with some no longer parses as having any. Every byte survives — it is a position change, not
a loss — but a reader should know it can happen.

**A CRLF checkout is not a divergence.** `.sdlc/**` is pinned to nothing, so a consumer on
`core.autocrlf=true` receives every `\n` as `\r\n`. Digesting those raw bytes made an ordinary
checkout a permanent false divergence — reported as edited, the whole body handed back as discarded,
a named owner accused, and the block rewritten as LF, which git converts straight back on the next
checkout. Line endings are folded before any comparison, on both sides, so such a checkout writes
nothing at all.

### 9e. The one door through which prose becomes state

Recovery — rule 4 — is the only path on which the `.md` is read for state, and it is bounded by the
file's own name: a `<name>.md` speaks for `<name>` and nothing else, so a block someone copied
between two files cannot make one unit's doc recover another unit's entry. A block that parses to
*nothing substantive* (no title, no owner, no parent, no tracking issue, no repos) is **not** a
recovery: rebuilding a lost registry as a registry of hollow units is strictly worse than knowing it
was lost.

---

## 10. Stamping — every issue Sigma files itself

A unit is only real if it survives the work **nobody wrote by hand**. A follow-up finding, a
cross-area hand-off, a decomposition meta-issue — each is opened by Sigma *from* a goal that
belongs to a unit, and each filed without that unit ends the chain silently.

So the unit is resolved and stamped at the one place the kit opens an issue on its own behalf.
Callers pass nothing; the question *"which unit is the goal I am filing FROM in?"* is answered there.
It costs **one `gh issue view` per filing**, paid on a path that is already opening an issue over the
network — never on the pick path.

**The two halves are written by different means, and the asymmetry is deliberate:**

```
   THE BODY MARKER   composed INTO the new issue's body BEFORE it is created,
                     so it is atomic with the issue existing at all. There is no
                     window in which a machine-filed issue is live declaring nothing.

   THE LABEL         attached AFTER creation, never handed to `gh issue create --label`.
                     That looks like the weaker choice and is the safer one:
                     `--label feature:x` FAILS THE WHOLE CREATE when the label does
                     not exist, so the atomic-looking route loses the entire follow-up
                     in exactly the case this has to survive. Attaching afterwards
                     degrades to `body_only` instead — the issue exists, it declares
                     its unit where a human reads it, and the label is attached the
                     first time that follow-up is itself picked. Self-healing rather
                     than lossy.
```

**The writer verifies its own output through the reader.** A marker that lands inside a fence, an
indented block or an HTML comment is invisible (§4a), and there is no error anywhere. So the stamp
composes a candidate, feeds it back through the real parser, and only uses it if the unit reads back.
If the natural placement (appended, after the prose) does not, it tries the top of the body — where
no construct can yet be open — and if that does not read back either, it files the issue **unstamped,
with a warning**. Shipping an issue whose marker nothing can see, while reporting success, is the one
outcome worth more than a lost marker.

**Four things it deliberately does not do:**

1. **It never stamps an issue Sigma did not just open.** The duplicate-reuse path resolves to a
   pre-existing issue, which may already belong to another unit — and a second declaration would make
   it raise for every reader afterwards.
2. **It never overrules a declaration the caller already made — in either of the two places one can
   be made.** A rival unit in the **body** leaves the body exactly as written *and* withholds the
   label. A rival `feature:*` in the **caller's own labels** withholds both inherited halves the same
   way. Inheritance is a default; an explicitly passed label is an instruction, and an instruction
   beats a default. (A caller-supplied label that merely *restates* the inherited unit is dropped
   from the create call and attached afterwards instead — for the create-abort reason above.)
3. **It never fails a filing.** Every path degrades to "filed without a unit", which is exactly the
   behaviour that predates the feature, and says so through the existing warnings channel.
4. **It does not cover `/sigma-scope`'s plan compilation.** That opens issues directly and
   deliberately: there is no "calling goal" to inherit from — it is a human turning an idea into a
   plan, not Sigma filing from work in flight.

---

## 11. Cross-repo — the tiers, and the both-green check

GitHub cannot merge two PRs atomically, so a unit spanning two repos lands by one of two strategies,
and which one applies depends on what the actor **actually has**:

| Tier | Condition | What happens |
|---|---|---|
| **tier 1** | access to **both** repos | a tracking issue owns the pair, and neither side is reported ready while the other is not |
| **tier 2** | **no** access to one side | contract-first, or a human coordinates; the unavailable half is raised through the ledger, addressed to whoever owns it |
| **flagged** | it could not be measured | **no tier is selected at all**, and a human is told |

**The check runs at pick time, not at merge time.** A permissions failure discovered at merge is
discovered after the worktree, the branch, the code and the PR already exist, and the answer is that
it can never land. Found at pick, it is found before any of them do. This is a property of the
*shape*, not a promise about call order: the merge-time consumer reads a recorded file and has
nowhere to put a runner.

**The failure this is shaped around is not "no access" — it is "no answer, read as no access".**
Tier 2 is the safe-*looking* strategy, which is exactly what makes it dangerous as a default: a 502,
a rate limit, a DNS blip or an expired token would each silently select it, record "no access"
against a repo that is in fact fully reachable, and raise a request to a human who had nothing to
answer. So the verdicts are three, not two — `granted`, `denied`, `unknown` — and **`unknown` is
never a softer `denied`**. The whole rule is one line: *a check that cannot get an answer must not
answer.*

**The wrong-account 404.** GitHub answers a read of a private repo the token cannot see with a 404 —
byte-identical to the answer for a repo that does not exist. That is ordinarily fine: "cannot see it"
and "it is not there" are the same operational fact. It stops being fine the moment the identity
behind the token is not the intended one, and `gh`'s active account is a single device-global slot
that any other tool on the machine can switch. So the authenticated login is resolved once and
compared against the login the project is configured to act as, and **no verdict survives an
unpinned identity** — positive or negative. A drifted account that *has* push access would select
tier 1, and the merge would then fail at exactly the moment this check exists to move earlier.

**A `granted` verdict also authorises a write into that repo, and this is where to learn that before
it happens.** On every pick of a cross-repo unit, the unit's registry entry is copied into each
sibling the entry names — as a direct commit on that repository's **default** branch, and it lands
outside any pull request. §15 states the whole of it: what is copied, what can never be, and what
stops it.

### 11a. The both-green check is a queryable check, **not a merge refusal**

> **This is the third place the code amends the design, and it is the largest.**

The design says two contradictory things. Its **cross-repo clause** says Sigma enforces the pair
by *refusing to merge* while the sibling PR is not ready. Its **completion clause** — the half this
document kept, in §13 — says completion is **human-only by default**: a person raises the feature →
integration-branch PR. Both cannot hold, because enforcing the pair at merge assumes Sigma
performs a merge that human-only completion says it never does. **Completion won**, because it is
the deliberate safety property and the cross-repo clause is an implementation guess.

Wiring the check into the merge step was tried, and it fails in both directions:

```
   Every merge Sigma can perform is   sdlc/<goal-id>  →  feature/<unit>,
   inside ONE repo. It touches `main` in neither repo and cannot land half of anything.

   The branch the check looks up is the UNIT's branch, so the PR it looks for is the
   sibling's LANDING PR:  feature/<unit>  →  main.

   ⇒ while a unit is being BUILT no landing PR exists in either repo, so the first
     cross-repo goal in EACH repo refuses waiting for the other and the unit can never
     be built at all — a symmetric deadlock, on the happy path;
   ⇒ once the sibling's landing PR is green, every unrelated goal merge passes vacuously.
```

So what ships is a **pure, queryable check**. It answers a question; it enforces nothing:

| Answer | Meaning |
|---|---|
| `(True, "")` | **not applicable** — no registry, not cross-repo, no declared unit, or tier 2. **Zero API calls.** This is every ordinary goal. |
| `(True, "<reason>")` | **checked and ready** — every sibling repo has exactly one open PR on its recorded branch, and it is green (and approved, if this project requires approval) |
| `(False, "<reason>")` | **not ready**, and the reason names which of eight causes it is |

It **fails closed** — the deliberate opposite of the review gate's fail-open. That gate can shrug at
an unreadable state because three others stand behind it; here an unreadable answer is the only
evidence there is.

*"Has not answered yet" is not "failed."* A sibling still building **waits**; only a sibling that
genuinely answered no refuses. That is why the per-sibling vocabulary has three members where two
would do.

**Two ways a goal can be permanently unready through no fault of its own**, both stated rather than
left to be rediscovered: a goal claimed **before** its repo adopted the registry has no recorded
decision and is refused for that goal forever after; and a transient issue-read failure at pick
records `flagged`, which refuses on every later call. **Both are recoverable by a re-pick**, and both
name themselves in the returned reason — which is why the reason is a sentence and not a code.

### 11b. **"Then they merge back to back" deliberately has no owner**

Said plainly, because it is a gap and not an oversight: **nothing in this tree merges a feature
branch**, so nothing performs the back-to-back landing the design's third acceptance criterion
describes. Giving it one is a decision for a human to take explicitly. Do not read a `True` from the
readiness check as licence to reintroduce an automatic merge.

---

## 12. The two owners, and the per-unit `authorized` grant

> **Not to be confused with AREA ownership** — who owns `area:doctor`, which resolves
> through `.github/CODEOWNERS` and `ledger.owners`. That is `docs/ownership.md`. This section
> is about who owns a **unit**, which lives in the registry and is per-unit.

There are **two** ownership levels in an entry, and they answer different questions:

```
   entry.owner                  WHO OWNS THE UNIT.
                                The person a divergence about this unit is
                                addressed to — a regenerated block, a branch
                                reconciled away, a unit closed.

   entry.repos[<repo>].owner    WHO OWNS THAT BOARD.
                                One per participating repo. A unit spanning
                                three repos has three of them.
```

And one grant:

```
   entry.repos[<repo>].authorized     Has THAT repo's board owner agreed that this
                                      unit may have work filed against their board?

                                      PER REPO, PER UNIT. Not a blanket grant.
                                      ABSENT MEANS FALSE. Only a real boolean `true`
                                      grants — `"true"` the string does not, `1` does
                                      not, and neither does anything else truthy.
```

The grant is answered by a check that **tolerates every missing layer** — a malformed entry, a
`repos` that is not a mapping, a repo key that is not even a string — and answers `false` for all of
them. That tolerance is deliberate: an access check that raises is an access check somebody wraps in
a bare `except` at the call site, and a swallowed exception defaults to whatever *that* call site
felt like.

**It is enforced as of #1479** (`feature_owner.py`), and the enforcement is exactly the policy change
recording the field first made possible — no data migration, no schema bump. An issue carrying
`feature:<name>` may be created **directly** only by the unit owner or by the owner of the repo it is
created in; **where those two disagree the board owner wins**, and the more restrictive gate applies.
Anyone else is never blocked from raising it — it lands as `sdlc:needs-confirmation`, inert until
promoted, plus a ledger entry addressed to the owner. Two gates, reached by different callers:
`handoff.create_tracked_issue` for every issue Sigma opens (follow-ups included), and
`feature_owner.gate_at_pick` for an issue anyone else opened, which reads the issue's **author**
rather than its picker. The filing gate is asked about the unit the issue **will declare** — a
caller-supplied `feature:<name>` label counts, not just an inherited one — so `handoff track --label
feature:<x>` cannot file directly-actionable work under somebody else's unit.
`docs/label-model.md` §2b-iii is the label contract for it.

The ask reaches its owner through the ledger's own address namespace: `to` is written as a bare
login, because every consumer of that field (`ledger.mine`, `autowatch`, `watch_classify`, `triage`)
compares against `ledger.actor`, while the registry's `owner` carries a leading `@`.
`ledger.addressed_to` normalises both sides, so entries already on disk in either spelling are
retrieved.

The grant is asked through `feature_owner.authorized`, never through `feature_registry.is_authorized`
directly: that function does an exact `repos.get(repo)`, while the comparison every other reader of
an entry uses (`feature_sync.same_repo`, and `repo_key` on the write side) is case-insensitive — so
asking it under a different spelling of the same repo returns `false` in silence.

**Ownership is set once.** Declared; failing that, the unit is named from the login that opened the
first goal picked onto it — the only inference in the model — and it is changed thereafter only by
editing the registry. No reassignment on inactivity: an owner reassignable by whoever picks up work
next is a lease, not an owner. It is inferred at the **pick**, from the issue's author, because that
is the one point where a GitHub login is already in hand: every name in this level is compared
against `author.login`, so a name from anywhere else (a config string, `$USER`) is not a weaker
answer but a different person. **Nothing writes `authorized`**, deliberately, and there is no CLI
verb that could: a grant a machine can mint is not a grant.

`authorized` is a **different question** from whether the API lets us in at all (§11). A repo can be
fully reachable and ungranted, or granted and unreachable.

---

## 13. Completion, and branch protection

**Completion is human-only by default.** A person raises the `feature/<name>` → integration-branch
PR. The loop's own merges are `sdlc/<goal-id>` → `feature/<name>`, inside one repo. Landing
through `verify_merge.py` (attended confirmation) and the Slack `--unsafe-merge` path may merge `feature/<name>` onto
`work.base`; the loop does not initiate those feature-branch landings. The chat path has no confirmation of its
own: with unit upkeep off, a non-bot message in the authorized channel is the whole request; with it on, chat landing
runs through the landing engine and needs the same single-use unit approval made locally, and the requester is recorded.

Marking a unit finished is a registry edit, not a branch deletion: **`open: false` is how a finished
unit is marked**, because feature labels are never deleted and the entry, the shard and the `.md` are
all kept. A unit with no branch and no recorded goals is closed automatically by the reconcile
(§8f); one whose branch merged away but whose goals are recorded is **left open**, because those
goals are precisely what the backup exists to preserve.

### 13a. What closes a GOAL's issue, since the merge cannot

The section above is about finishing a UNIT. This is the half a reader meets far more often, and it
is a direct consequence of the base this model chooses.

**GitHub honours a closing keyword only on a merge into the repository's DEFAULT branch.** Every
goal that declares a unit targets `feature/<name>`, so a `Closes #N` in its pull-request body would
be inert — the body would assert a close nothing performs, and the issue would sit open while its
PR said otherwise. **So the keyword is not written.** Measured, the same goal on two bases:

```
   base main            →  Closes #1467
   base feature/voice   →  Refs #1467
                           …no closing keyword is written here — it would assert something this
                           merge cannot do. Sigma closes the issue itself instead…
```

**Sigma performs the close, at the moment it confirms the merge landed** — a REST write,
right after `gh pr merge` succeeds. It decides whether there is anything to do by reading the
ISSUE's own state at that moment, never a base (#2615). An earlier version (#1649) compared the
goal's recorded base against the repository's default branch, and that comparison could be wrong: a
recorded base that matches the default branch is not proof the keyword fired (the issue can still
be open when this reads it). #2615's own plan review considered comparing a live base instead, and
rejected that too, in the other direction: a PR first opened against a unit branch carries
`Refs #N` in its body, never `Closes #N`, so retargeting it onto the default branch before merging
does not rewrite that text either. Reading
the issue directly sidesteps both failure modes at once: already CLOSED means GitHub's own keyword
(or a human) got there first, and the merge line says nothing extra. Still OPEN means Sigma
sends a close request rather than claim it — GitHub's own keyword processing, or a person, could
still close #N in that same narrow window, so the line never says Sigma did the closing, only
that #N `was still open after the merge; sent a close request, which succeeded`, reporting only
what it observed and did. If the close request itself fails, the merge still LANDED and the line
says Sigma could not close #N (…); `record done` remains the right next step — its own
`source.complete()` retries this exact close, and parks the goal instead of silently losing the
record if that retry fails too; §15 records what that costs.

**This is what releases a dependent goal.** A goal whose body carries `Blocked by: #N` is held at
pick while `#N` is open, and the hold re-reads `#N`'s live state before it reports, so clearing a
blocker releases its dependents on the very next pick rather than after the board mirror's TTL.
Without the close above, every goal landed through a feature branch would stay open and hold
everything behind it.

**Nothing in the branching model configures branch protection**, and the model's own check is
read-only. (One unrelated part of the kit *can* touch protection: `merge_queue_enable.py apply`
creates a `merge_queue` ruleset — a separate feature, on the integration branch, refused outright
unless an operator passes `--yes-enable-merge-queue`. It knows nothing about units.)

The model's own check asks a narrower question than people expect: *does protection actually
REQUIRE anything on this goal's base?* A repo can run CI on every PR while requiring nothing, and
then a green mergeability status means only that GitHub was never asked to object. A 404 from the
protection API is reported honestly as **"not protected — nothing is enforced on merge"**.

Two consequences worth knowing:

- for a goal that declared a unit, that check is about **`feature/<name>`**, not about `main` — the
  base is the unit's branch;
- **the no-direct-commits rule (§3) is PREVENTED by branch protection or by nothing.** If you want a
  direct commit to be impossible, protect the `feature/*` namespace on the host yourself. Sigma
  will report what you set; no part of the branching model sets it for you. A violation is
  *detected* — rebase upkeep refuses to rewrite a branch carrying one (§3) — but that runs after the
  commit exists, only on a pick, and it is not a substitute for protection;
- **and protecting `feature/*` in the ordinary way TURNS THE DETECTION OFF.** This is the one place
  the two halves of §3 collide, so it is stated with its measurement rather than as a caution. Every
  upkeep pass ends in a `--force-with-lease` push of `feature/<name>`, so a rule that blocks
  non-fast-forward pushes there — `receive.denyNonFastForwards`, or a ruleset with no bypass actor —
  makes every pass end `failed`, its clause carrying git's own non-fast-forward refusal. Measured on
  a **clean** pass, with nothing to detect. Not once: on every pick, forever, which is exactly the
  branch rot upkeep exists to remove. **Feature branches are meant to
  stay force-pushable**, which is what `feature_rebase.switch()` means when it defaults an
  unrecognised `rebase_upkeep` to ON. So: protect the **integration** branch as much as you like; if
  you protect `feature/*`, grant upkeep's actor a bypass, or accept that upkeep stops.
- **and `work.auto_merge: "protected"` cannot land a goal's PR into a unit branch, ever** (#1689).
  That policy merges only where the base genuinely enforces something, and a `feature/<name>` branch
  never does — by the point above, on purpose. `merge()` correctly declines to arm ("… merging it is
  yours to make"), and `record done` refuses too — since #232 it refuses on every unmerged PR, so
  the goal is recorded `review` and its issue stays open until the PR is observed merged. A repo running goals inside units needs
  `auto_merge: "always"` for those goals to merge autonomously; `"protected"` still gates a goal
  based directly on the integration branch exactly as before.

### 13b. Sigma never deletes a `feature/<name>` branch — a hard invariant, not an absence

§13's opening sentence — "marking a unit finished is a registry edit, not a branch deletion" — is
about what closes a UNIT. This section is the broader claim underneath it: **no automatic code
path in Sigma deletes a `feature/<name>` branch, ever, under any config or heuristic.** Not at
completion, not as cleanup, not as a heuristic response to a stale or merged-away unit. The only
thing that may delete one is **an explicit, in-the-moment human instruction — typed by a person,
right then** — never a standing config toggle, never a default, never something a future flag turns
on by itself.

**What was actually checked, not assumed.** The kit deletes exactly one kind of branch
autonomously: the GOAL's own short-lived `sdlc/<goal>` branch (`work.branch_prefix` +
the goal's stem, built once in `work.start()`), and only once its pull request is independently
confirmed `MERGED` — never before, never speculatively. That happens in exactly two places, both in
`skills/sigma-loop/scripts/work.py`: `merge()`'s eager remote delete right after a direct landing,
and `finish()`'s backstop (local `git branch -D` + a redundant remote delete) for a PR that armed
and landed asynchronously instead. Both operate on the goal's own branch (`rec["branch"]`)
exclusively — neither reads, nor could structurally reach, `rec["base"]`, which is the field that
holds `feature/<name>` for a unit-based goal. `feature_rebase.py`'s rebase upkeep (the other place
a `feature/<name>` branch is touched at all) only ever **force-pushes** it — rewriting history to
replay goal branches forward — which is never a delete.

Three further sites *build* a two-sided git push refspec (`<src>:<dst-ref>`) — the syntax git itself reads as a delete
of that ref whenever `<src>` is empty, independent of any `-d`/`-D` flag or `--delete` verb: `skills/sigma-define/scripts/define.py`'s `_step_branch` (which is how a
`feature/<name>` branch is *created* — `resolved_base()` refuses loudly rather than ever handing
it an empty source) and `feature_rebase.py`'s own `_pushed` (whose source side is the fixed literal
`HEAD`, never a runtime variable). The third is `release_manifest.py`'s
`publish_to_ledger_branch`, which publishes the receipt rollout manifest to the `sdlc-ledger`
ops branch: its source side is the same fixed literal `HEAD`, and its destination is the
`sdlc-ledger` ops-branch ref rather than any `feature/<name>` ref, so it cannot name a unit
branch at all. All three are audited, not merely absent-mindedly safe.

**The one exception: backup refs.** An optional part of upkeep (section 13c, off by default) keeps the old tip of
a rewritten unit branch under `refs/sigma/backup/<unit>/<UTC stamp>`. Those are not `feature/<name>` branches,
so the invariant above is unchanged for them, and the guard states its rule for that namespace in one sentence,
which this section carries verbatim:

> The never-delete guard pins every delete-shaped call it can see under skills/ and hooks/; under refs/sigma/backup/ the only sanctioned automatic deletion is a prune by literal prefix.

Two places in `skills/sigma-loop/scripts/feature_backup.py` are pinned beside the older ones:
`_atomic_leased_push`, which builds the two refspecs of the atomic push (the source of each is the literal `HEAD`
or a full object name that was checked, never empty), and `_delete_chunk`, the prune, which uses the `--delete`
flag with one lease per ref and names only refs that parse as exactly `<prefix><unit>/<stamp>`. When upkeep is
enabled the engine's `_pushed` hands the unit push to `_atomic_leased_push`, whose source is the same literal
`HEAD`; `_pushed`'s own refspec line, used when upkeep is off, is unchanged. Nothing else in Sigma deletes under that
namespace, and nothing deletes a `feature/<name>` branch. Until a pass calls the prune unattended, the sentence is a
rule for that future caller: today a person runs it.

**The guard.** `tests/test_no_autonomous_feature_branch_deletion.py` makes this checked rather than
observed: a structural scan pins the exact, reviewed set of branch/ref-delete-shaped (and
delete-capable) call sites across every file the kit tracks under `skills/` and `hooks/` — a new
one appearing anywhere fails the pin immediately, by file, function and kind — plus a behavioral
proof that `finish()` and `merge()` never name a `feature/<name>` branch in a delete-shaped call
even when the very goal being finished declared that unit as its base.

### 13c. Backups of a rewritten unit tip (off by default)

When upkeep is enabled (`upkeep.enabled: true`), each time the pick-time rebase pass force-pushes a unit branch it also
keeps the old tip under `refs/sigma/backup/<unit>/<UTC stamp>`, for example
`refs/sigma/backup/billing/20261010T031500Z`, in the same `git push --atomic` as the leased update of the branch, so
both land or neither does: a branch that moved under the pass refuses both, and a second backup of the same unit in
the same second is refused rather than overwritten (the pick line says the backup ref is taken; retry a second
later). A unit name over 220 bytes is refused before the replay, because the ref would pass 255 bytes: the pass
reports `name-too-long` and pushes nothing, never a push without a backup. With upkeep off the pass pushes exactly as
it always did. The attended rebase and the goal-branch pushes rewrite branches with no backup at all. The rest of
the pass (landed-unit skip, restarts, hooks policy, the ack file, goal replays, skip reasons) is a later change.

Two commands work on backups, both behind the upkeep gate (while it is closed they do nothing and exit 3):

```
python3 skills/sigma-loop/scripts/feature_rebase.py restore .sdlc billing --list
python3 skills/sigma-loop/scripts/feature_rebase.py restore .sdlc billing <stamp> --expect <tip>
python3 skills/sigma-loop/scripts/feature_rebase.py prune .sdlc --dry-run
```

`restore` puts the branch back at the backup you name. You state `--expect`, the tip the branch has now (`--list`
prints it), and the push is leased on that tip: if the branch moved, nothing is changed. The tip being replaced is
kept as a new backup in the same atomic push. `prune` removes backups older than `backup.keep_days` (14) and
keeps the newest `backup.keep_last` (5) of each unit whatever their age; age is read from the stamp in the name,
never from the commit, and so from the clock of the machine that made it (one running ahead ages backups early, one
running behind stamps a fresh backup as old; the newest five of each unit are kept whatever their age); a stamp in the
future is kept; at most 200 are removed per run, so a large backlog takes several runs. It selects by the exact prefix itself, because a listing by pattern also returns branches, tags
and other namespaces whose names merely end with it. `backup.former_prefixes` ships empty; a namespace an
earlier product wrote can be listed there, in the form `refs/<name>/backup/`, to have it pruned the same way.

Git resolves a short ref name by tail-matching, so `git push --delete` of a backup name could take a branch, tag or
other ref carrying the same name when the backup itself is gone. The prune therefore keeps (and reports as
`skipped_ambiguous`) any candidate that has such a lookalike in the listing, and re-reads the exact refs just before
each delete, skipping any that vanished or moved (`skipped_vanished`). The re-read asks for each backup name and for its `<name>/HEAD` form, because a tail pattern of the name cannot
return a remote-tracking head (its last component is `HEAD`); one that appears after the first listing is caught there.
A lookalike that appears in the instant between that re-read and the push is a named residual window, not zero: the
window is one `ls-remote` round trip plus the process start of the push, **unmeasured** (no timing was taken on a
hosting service or on a local remote), so no bound is claimed beyond "one network round trip per chunk". A lookalike also keeps its backup out of the prune, so
a stalled backup is cleared by removing the lookalike by hand.

Nothing prunes by itself, and the scheduler of section 13d does not either, so while upkeep is enabled run the prune now and
then; each rewrite adds one ref. The prune is the one sanctioned deletion in Sigma (section 13b): it removes only refs
that parse as exactly the backup prefix, one unit and one stamp, and nothing else.

A restore is not sticky. While upkeep is enabled the next pick of a goal in that unit brings the branch forward again,
keeping the restored tip as a backup; set `work.rebase_upkeep` to `off` first to keep the restored branch where it is.

Limits, stated rather than discovered: a host that rejects, hides or caps the namespace, or that cannot do an
atomic push, fails the whole push on every pass (turn upkeep off, which restores the plain push); a client pre-push
hook that allows only branches refuses the backup; the commands have no network timeout of their own.

With the gate closed, or on a machine that has only a plain clone, restore by hand with plain git. A plain clone does
not carry `refs/sigma/*`, and the old tip is on no branch, so fetch the backup before pushing it:

```
git ls-remote <remote> refs/heads/feature/<unit>
git ls-remote <remote> 'refs/sigma/backup/<unit>/*'
git fetch <remote> refs/sigma/backup/<unit>/<stamp>
git push --force-with-lease=refs/heads/feature/<unit>:<tip> <remote> <backup sha>:refs/heads/feature/<unit>
```

The first command prints the tip the branch has now (`<tip>`); the second lists every backup of the unit with its sha
(`<backup sha>`) and its stamp. The lease makes git refuse if the branch moved since you read it. The same note about
a restore not being sticky applies. Unlike the `restore` command, this route keeps no backup of the tip it replaces:
record that tip first if you may want it back.

### 13d. The upkeep scheduler (off by default, three opt-ins)

Without the scheduler, a unit branch is brought forward only when a goal of that unit is picked. With it, the ledger
watcher also decides, once per tick, whether one unit is due for a maintenance pass and starts that pass as a detached,
bounded job. Nothing of it exists until all three opt-ins are on: `upkeep.enabled` is the boolean true in the project
config, `ledger.enabled` is exactly true, and the environment variable `SIGMA_UPKEEP_JOB` is exactly the text `1` in the
environment of the watcher. Any other spelling (the text `true`, the number 1) leaves it closed, and with it closed the
watcher's tick, files and output are byte for byte what they were.

What one open tick does, in order: it notes the outcome of a job that finished (as one unaddressed ledger note, written by
the watcher itself); it stops there if a job is still alive; otherwise it lists the units in the registry that
`upkeep.units` admits (an exclude always wins), skips those inside their cooldown without touching git, measures the rest
within a time budget, and picks at most one due unit, the most behind first. It records the attempt before the work, then
starts the job. A unit is due when it is behind its base by `triggers.drift_merges` arrivals (`auto` derives that
number from recent history, between `auto.floor` and `auto.ceiling`), or when the last success is older than
`triggers.every_hours` (`triggers.dormant_every_hours` for a unit with no arrival of its own for `triggers.dormant_days`).
A unit that cannot be measured counts as behind for the backstop and never fires the drift trigger. A pass that failed
or was parked restarts only the cooldown, `triggers.min_interval_minutes`.

The job runs the same engine as the pick-time pass, so a rewrite keeps a backup ref (section 13c). It runs in its own
session with an allowlisted environment, under a wall-clock cap derived from `verify.timeout_minutes` plus a stated
margin, with a heartbeat; one job at a time. Windows is refused: the status file says so once and no job starts.

Levers and signs of life, all under `.sdlc/state/`: the file `upkeep.stop` (create it to stop a running job and prevent
the next; Sigma never deletes it), the watcher's own `watch.stop` (which also stops a running job), and the status file
`upkeep/scheduler.json`, rewritten on every open tick, so its age is the tick's liveness. `/sigma-doctor` shows the
scheduler rows only while the block is on: tick age, the job's heartbeat and the last outcome.

**Ceiling.** One unit per tick is `86400 / interval` units a day: 96 at the 900 second default. A first enable on N units
with no recorded state drains in N ticks, so N / 96 days.

**One opted-in machine per set of units.** The scheduler has no cross-machine lock beyond the per-unit rebase lock the
pass takes and the lease on the push. Two machines opted in for the same units would both start passes; the lease makes the
second push refuse, but each wastes a fetch and a replay and each adds a ledger note. Opt in exactly one machine for any
given set of units (use `upkeep.units.include` and `exclude` to split units between machines on purpose).

**What is and is not proven.** The pass, the backup, the restore and the prune were run end to end on a bare remote in a
temporary directory, offline, with the clock set: see `docs/launch/evidence/upkeep-local-run.md`. That is a local bare
remote only. It has not been run against a hosting service, so nothing here is a claim about branch protection rulesets,
bypass actors, ref-name or ref-count limits, authentication, rate limits or network cost there. The hosted run described next is a single
sanitized record, and a gap list.

**Hosted run (2026-10-10, one private throwaway repository).** The real scripts passed init with a board, a unit with two
goals through pull requests, the pass with an atomic backup ref the host accepted, restore (and a refused restore on a
wrong expected tip), a restore that is not sticky, a prune of exactly the old backups, a guarded head-pinned landing
(rehearsal, merge, then `already-landed` on replay), the mechanical conflict level (a non-union conflict refused and parked,
a union conflict resolved and pushed with a backup) and the scheduler's detached job. The record, with its limits, is
`docs/launch/evidence/upkeep-hosted-run.md`. **Not covered:** a real-model resolver or reviewer, the chat door against a
chat service, and branch rulesets or bypass actors (a private repository without a paid plan has none), so the bypass advice
in section 13 is untested. One host limit was seen: branch names starting with `refs/` are rejected. The scale figures above
are reasoned, not measured: nothing was run at tens of units.

### 13e. Config reference: the `upkeep` block

Scaffolded as `"enabled": false` by `/sigma-init`. One invalid key, an unknown key, a section that is not an object or a pair
of settings that contradicts itself closes the whole block, and `/sigma-doctor` names the key path.

```
| Key | Default | Range | What it does |
| --- | --- | --- | --- |
| enabled | false | boolean | project opt-in; only the boolean true opens it |
| units.include | ["*"] | list of unit names, at least one | units the scheduler may pick (case-folded globs) |
| units.exclude | [] | list of unit names | units it never picks; wins over include |
| triggers.drift_merges | "auto" | "auto", a whole number 1 to 1000, or null | arrivals on the base that make a unit due; null turns the trigger off |
| triggers.every_hours | 24 | 1 to 8760 | backstop: a pass at least this long after the last success |
| triggers.min_interval_minutes | 120 | 1 to 10080 | cooldown after any attempt, failed or not |
| triggers.dormant_days | 14 | 1 to 3650 | a unit with no arrival of its own for this long is dormant |
| triggers.dormant_every_hours | 72 | 1 to 8760 | the longer backstop for a dormant unit |
| auto.window_days | 21 | 1 to 365 | history the auto threshold reads |
| auto.burst_window_hours | 24 | 1 to 720 | window that counts a burst of arrivals |
| auto.target_hours | 12 | 1 to 720 | how stale the auto threshold aims to let a unit get |
| auto.floor | 3 | 1 to 1000 | lowest auto threshold; must not exceed the ceiling |
| auto.ceiling | 40 | 1 to 1000 | highest auto threshold |
| verify.clean_rebase | false | boolean | validated now; not acted on by this part |
| verify.timeout_minutes | 60 | 1 to 1440 | with a stated margin, the job's wall-clock cap |
| backup.keep_days | 14 | 1 to 3650 | the prune removes only backups older than this |
| backup.keep_last | 5 | 1 to 1000 | the newest backups of each unit are kept whatever their age |
| backup.former_prefixes | [] | list of names | namespaces an earlier product wrote, pruned the same way |
| conflicts.resolve | "off" | off, mechanical, agent | how far a unit conflict may be resolved |
| conflicts.mechanical_without_verify | false | boolean | lets Level 1 resolve a changelog conflict when no verify command is set; see `docs/upkeep-part-b.md` |
```

The relation to the older switch: `work.rebase_upkeep` (on by default; an unrecognised value reads on) still decides the
pick-time pass and still turns the project door off when it is `off`. The two have opposite typo rules: the older one
fails open, the `upkeep` block fails closed.

### 13f. How an existing adopter picks up rule edits and the new block

Rule edits reach new adopters only. The scaffold never overwrites a file that exists, so a repository adopted earlier keeps
the rule text and the config it was given. What reaches an existing adopter, and how:

- Skills, docs, hooks and the scripts arrive with the plugin update (restart the session afterwards). That includes the
  scheduler and the backup commands, which stay closed until the block is on.
- The `upkeep` block in `.sdlc/config.json` and any new text in `.sdlc/project.md` are never refreshed: copy the block by
  hand from the config template shipped in the installed plugin, and leave `enabled` false until you mean it.
- The routing sentence ("rebase, catch up or land a unit: use the `sigma-rebase` skill, not a raw git command") is rule text,
  so it reaches new adopters only, on every host. An existing adopter re-runs `/sigma-init --codex` (the managed block is
  rewritten), deletes both Cursor rule files before `/sigma-init --cursor`, or pastes the sentence into `.sdlc/project.md`
  by hand. Sigma never rewrites an adopter's own files silently.
- The Codex managed block in `AGENTS.md` is rewritten in place when `/sigma-init --codex` is run again; other bytes are kept
  and nothing printed tells a refresh from a first write.
- The two Cursor rule files are kept when present: delete both, then run `/sigma-init --cursor` again.
- `migrate.py` does not refresh a block already spelled for Sigma, and the doctor has no row for a stale copy, because the
  copies carry no version. Staleness is invisible, so this is a limitation, not a promise of a refresh.

---

## 14. Adopting it, and the gestures that are correct

Adoption is opt-in and incremental — but **only two thirds of it are, and the third that is not
can refuse a pick.** This is the one place in this document where the honest answer is worse than
the tidy one, so it is stated first.

**What the `.sdlc/features/` directory gates.** **Base resolution first** (§6, line 0) — without
the directory a declared unit does not move a goal's base at all, and the goal is cut from
`work.base` exactly as it was before any of this existed. Then the registry sync, the scope gate,
the ownership gate, rebase upkeep, unit completion and the cross-repo access check: each opens with
the same `feature_registry.registry_dir(sdlc_dir).is_dir()` test and returns `not-adopted` before
spending anything — no `ls-remote`, no reconcile, no access check. Measured end to end in §6e: one
goal declaring a unit costs **3 calls** without the directory and **15 with it**.

**What it does not gate: the label half.** `attach_at_pick` runs for every goal a GitHub source
**considers** (§6e), and the needs-label sweep once per `_next()`, both gated only on that source
exposing the six methods they duck-type on.
They never look at `.sdlc/features/` at all. Measured, against a temporary `.sdlc` with **no**
`features/` directory and an issue whose body declares a unit whose label does not exist:

```
   features/ exists?  False
   Decision(proceed=False, outcome='refused-no-label', unit='voice-interview')
   calls: fetch_body_labels → label_exists → mark_needs_label → fetch_comments_strict → note
                                             └─ a LABEL WRITE, and a COMMENT, on a repo that
                                                never adopted the branching model
```

**So the pick is refused, `sdlc:needs-label` is written, and a comment is posted, on a repository
with no registry.** It is not exotic. It is what the *natural* adoption order produces — write the
marker on some issues, create the label afterwards — which is exactly the reader this section is
for.

**This is the decision (#1543), not an unexamined gap.** Two other shapes were weighed and
rejected: gating the label half on `.sdlc/features/` too (matching the two consumers above), and
gating it on *some* `feature:*` label already existing on the repository (the human act as the
opt-in signal, rather than the directory). Both were rejected for what they buy against what they
cost — the marker's own contract already requires a bare, unindented, unfenced `Feature: <name>`
line (§3), so nobody reaches this state by accident, and the write it produces is the same
reversible overlay an adopted repository gets for the identical mistake. `feature_labels.py`'s own
module docstring carries the full weighing; `tests/test_feature_labels.py` pins the label half's
non-gating structurally, pins the outcome as identical on both sides of the adoption line by
executing both, and pins the census of every `.sdlc/features/`-gated call site in the tree so a
future consumer's choice is never a side effect nobody reviewed.

> **Therefore the order in *Opening a unit* below is a requirement, not a suggestion.** Create the
> label **before** any issue body declares the unit. A body marker whose label does not yet exist
> is a goal Sigma will set aside, on any repository, adopted or not. That is recoverable in one
> gesture (§7a) — it is simply not "inert".

### Step 0, once per repository: put the rule where your agents read it

Everything in this section assumes the agents working a participating repository already know §3's
rule. They do not know it by default, and an agent that has not been told it is the actor most
likely to break it: asked to "commit this" on a checkout that happens to be sitting on
`feature/<name>`, it will do exactly that. The next upkeep pass then either refuses to bring the
unit forward at all, or — if the subject it wrote happens to read as a landing (§15) — rebases that
commit out from under whoever was holding it.

`/sigma-init` writes the rule into `.sdlc/project.md` on every scaffold, and into
`.cursor/rules/sdlc.mdc` with `--cursor`. A repository that already had its own agent-instruction
file before it adopted any of this gets nothing automatically. Paste this into that file, verbatim
— the wording is identical everywhere the kit states the rule, on purpose, so that two readers of
two files are never reading two different rules:

```markdown
## Nobody commits directly to a feature branch

**Nobody commits directly to a feature branch. All work reaches it through `sdlc/*` goal
branches.** For a unit of work that means the `sdlc/<goal-id>` branch cut for a goal, then a pull
request into `feature/<name>`.

This is load-bearing, not style. Rebasing a shared branch rewrites published history, which
normally forces every holder of that branch to hard-reset and puts their uncommitted work at risk.
Here that risk disappears entirely, and it disappears for exactly one reason: nobody is holding
commits on `feature/*`.

Commit straight to a feature branch and the next upkeep rebase is where that work goes.

Sigma **detects** a violation; it cannot prevent one. Before it force-pushes `feature/<name>`
it asks of every commit on that branch whether a pull request accounts for it, and refuses to
rebase when one does not — the branch is left exactly as it was and the finding is filed as
tracked work. It looks only on a pick of a goal that declares the unit, and only when the branch is
behind its base; when it skips, it reports nothing at all, so silence is not evidence that the rule
held.

Only branch protection on the host can prevent the commit — and protecting `feature/*` in the
ordinary way blocks the force-push that upkeep itself ends in, which stops upkeep on every pick,
forever. Protect the integration branch; on `feature/*`, grant upkeep's actor a bypass, or accept
that upkeep stops.
```

Put it in whichever file that repository's agents actually read — `AGENTS.md`, `CLAUDE.md`,
`.claude/CLAUDE.md`, a rule file under `.cursor/rules/`. Where several are read, it goes in each:
an agent that loads one of them is told by one of them.

**Recording that a repository has it.** The edit is a change to that repository, so it is tracked
on **its own board**, under its own review discipline. A change that arrives from outside is
untracked where it lands, and an uncommitted edit left sitting in someone else's checkout is how
work gets silently reverted by the next sync. What the repository that owns the unit holds is the
checklist — one row per participating repository (§11), and nothing else:

```markdown
- [ ] `<repo>` — rule present in `<path>`
```

Tick a row against the check, never against a memory of having done it:

```bash
grep -Fq 'Nobody commits directly to a feature branch.' <path> && echo "states the rule"
```

The needle is the rule's FIRST SENTENCE, and both ends of it are deliberate. It stops there because
the second sentence wraps across lines in most files that carry the rule (four of the five in the
kit today), so a longer needle reports a correctly-patched file as unpatched. It keeps the full
stop because `## Nobody commits directly to a feature branch` is a heading — in the block above and
in Sigma's own `AGENTS.md` — and a check that a heading satisfies proves only that someone
pasted a title.

### Defining a unit — the three kinds, and `/sigma-define`

A unit does not begin with a command. It begins with a **human deciding that one exists** — that
some body of work is large enough that several goals will share a branch for it. `/sigma-define` is
where that decision is carried out. It asks which of three kinds the work is, what the unit is
called, what the work actually is, and who it belongs to; from the first two it performs the three
creation steps of *Opening a unit* below, in the order given there; and it hands the rest to
`/sigma-scope`, so that the issues filed against the unit carry the bare two-line marker (§4) from
the moment they exist. **That is the normal path.** The commands below are what it runs, written
out because a gesture nobody can read is a gesture nobody can check — and because a repository
opening a unit without the skill still has to get them right.

**Three kinds, and one branch prefix for all three.**

| Kind | The unit is | What the name names |
|---|---|---|
| `feature` | something being built that several goals each build part of | what is being built |
| `bug` | one defect large enough that several goals go into fixing it | the defect |
| `refactor` | one change of shape that several goals carry out | the thing being reshaped |

Three is the whole set, and it is a **classification of the work, not a second list of what may be
a unit.** §2's list — a feature, a shared bug, a refactor, a contract change — is the wider
question and is unchanged; a contract change is opened as whichever of the three it actually is.
What the kind steers is what the skill files and how the unit's page reads.

**It does not steer the branch name.** `feature/` is `features.BRANCH_PREFIX`, it does not vary
with the kind (§2), and so **there is no `bug/` branch and no `refactor/` branch**. One prefix is
what keeps every branch belonging to a unit greppable, forever, including after the unit finishes.
§5's rule on what a unit may be called applies to all three kinds unchanged.

**Creating the label is a human's act, so `/sigma-define` is the one place in the kit that may
create one.** §7 is where that rule and its reason live, and nothing here weakens it: no automated
path mints a `feature:*` label from a marker it merely read, and the loop still attaches and still
refuses exactly as §7 and §7a describe. `/sigma-define` is not such a path — it runs because a
person invoked it, once, with a name that person has just chosen — so it is less an exception to §7
than the gesture §7 leaves to a human, finally given somewhere to live. **The distinction is who
chose the name**, never which process typed the command. Being the single exception is also why the
skill says so out loud when it creates one rather than doing it quietly.

**The order is this section's, not the skill's.** The three creation steps run in the order
*Opening a unit* gives them, for the reason measured at the top of this section, and neither the
order nor the reason is restated here — a requirement written down twice is a requirement with two
chances to drift. What the skill adds is that the order stops being something a person can get
wrong by hand: the issues carrying the body marker are filed last, by the same flow that created
the label those issues will need.

**Running one unit and nothing else: `--feature <name>`.** `/sigma-loop` and `/sigma-goal` both take
it, and both make the same guarantee, in these words:

> **While a `--feature` run is active, no goal outside that unit may be picked.**

**Exclusive, not preferential** — not as a fallback, not when the unit is drained, not as a
"nothing else to do" convenience. A flag that merely *preferred* the unit would be worth nothing,
because the reason to reach for it is precisely that a run must not wander. When the unit has
nothing pickable left, the run says so and stops, and says it in terms a reader can tell apart from
a drained board.

Membership is §4's pair of declarations adjudicated by §4c's rule, never the label alone: the label
is attached at pick (§7), so a member nobody has picked yet may carry none, and scoping by label
alone would silently skip exactly the goals a scoped run exists to reach. Beyond that the flag is a filter on which goals a run may
pick and nothing more — a goal that *is* in the unit is then based, reconciled, recorded, stamped
and rebased exactly as the sections above describe, and a goal excluded by it is left untouched,
unlabelled and uncommented-on.

### Opening a unit

The four steps, in the order that works. `/sigma-define` above performs the first three and
files the issues that carry the fourth; by hand, they are these.

```bash
# 1. the branch — remote is the truth, so pushing it is what makes the unit exist
git push <remote> <base>:refs/heads/feature/<name>

# 2. the LABEL — a human, once per unit. Sigma never creates one (§7).
gh label create "feature:<name>" --description "unit of work: <name>" --color 1d76db

# 3. the REGISTRY — once per REPOSITORY, not per unit. Nothing in the kit creates it (#1576).
mkdir -p .sdlc/features

# 4. only NOW declare it, as the bare two-line marker in each issue body:
#      Feature: <name>
#      Branch: feature/<name>
```

Steps 1 and 2 are a human's, once per unit — whether performed by `/sigma-define` or typed out;
step 3 is a human's too, once per repository; and
**step 4 comes last for the reason above**. After that, every goal that declares the unit is cut
from its branch, recorded against it, and passes it down to whatever it files.

**Step 3 is the one this section used to omit, and omitting it was silent.** Measured before the fix: with
no `.sdlc/features/`, steps 1, 2 and 4 all work — the label is attached, the base resolves, the
worktree is cut from `feature/<name>` — and the registry sync returns `not-adopted` and records
nothing, so the sentence above was false for the whole repository with nothing anywhere saying so.
A goal that declares a unit against a missing directory now says so on stderr at the pick, naming
the directory and this gesture. **Goals picked before the directory exists are never backfilled**
(§15) — which is why step 3 belongs before step 4, the same way step 2 does.

Two things about it that are easy to get wrong:

- **It is empty, so git will not carry it.** `mkdir` alone adopts the registry in *your* checkout
  and in no one else's; the directory becomes real for everybody the moment the first pick writes
  `units/<name>.json` and `<name>.md` into it **and you commit them** (§8a — the registry is a
  committed backup and is deliberately not gitignored). A clone taken before that commit is
  unadopted again, and its picks will say so.
- **Creating it is what makes the registry half live**, and the registry half is not yet ready for
  first adoption — the composition defects tracked in **#1564** are inert only for as long as this
  directory is absent, which is why nothing in the kit creates it and why **#1576** is deliberately
  the last item in that epic. Until then, run step 3 on a repository you are prepared to be the first
  adopter of, and not otherwise.

### The one-line summary

| You want | Do this | Do **not** do this |
|---|---|---|
| a goal to belong to a unit | write the two-line marker in its body, bare | indent it, fence it, or bury it in a sentence |
| a goal to use the old behaviour | declare nothing — that is the default | invent a "none" unit name |
| a new unit | `/sigma-define` — it creates the branch **and** the label **before** any issue declares it, and files the issues that do | declare it in a body first — that refuses the pick until the label exists (§14) |
| a new unit, by hand | the four steps of *Opening a unit*, in that order | reorder them because the order looks arbitrary |
| a unit for a shared bug or a refactor | the same `feature/` branch, with the kind recorded rather than spelled into the name | a `bug/` or `refactor/` branch — neither exists |
| a run confined to one unit | `--feature <name>` on `/sigma-loop` or `/sigma-goal` | assume a scoped run still sweeps the rest of the backlog |
| to fix a typo'd unit | correct the `Feature:` line in the body | add a second `Feature:` line |
| to move a goal between units | change the body marker **and** the label | change one of them — that is the conflict state |
| to record a finished unit | set `open: false` in its entry | delete the branch and expect the record to follow |
| to know whether this repository adopted the registry at all | `ls .sdlc/features` — the directory's existence *is* the adoption, and `mkdir -p .sdlc/features` is all of it | read an empty `show` as "this repository has no units" |
| to see the whole registry | `python3 <plugin>/skills/sigma-loop/scripts/feature_sync.py show .sdlc` — `{}` means **not adopted** just as often as it means **no units yet**, and the row above tells them apart | read `index.json` alone — it is derived and may be stale |
| to refresh `index.json` | the same script's `fold` verb | expect a pick to do it |
| to add a note to a unit's page | write it **below** the end marker | edit inside the managed block |

### The gesture that is intuitive and wrong

```
   ADDING THE LABEL AND STOPPING                CORRECTING THE BODY
   ─────────────────────────────                ───────────────────
   + feature:x  on the issue                    the two-line marker in the body
        ↓                                            ↓
   the label declares x; the body               both halves agree
   declares nothing → `label_only`                   ↓
        ↓                                       base resolution and every downstream
   works, but the half a human reads            reader see the same unit
   says nothing at all
```

`label_only` is legal and is picked up correctly. It is just the half a person cannot read.

```
   TWO feature: LABELS ON ONE ISSUE  →  RAISES, and the goal is set aside until a
                                        human removes one. Nothing may pick between
                                        two declarations a human wrote.
```

---

## 15. Honest limitations, and the gaps that are deliberate

- **Upkeep's loss guard sees whole reverts, not every loss** (§3b, #144). It stops a replay that
  removes a path the branch has or restores a version of it the branch's history already held —
  merge-born and root-commit versions included — including an undone rename. It is a
  version-identity test, so it cannot see a replay result that is a version which **never
  existed**, and there are two such shapes, both NOT CAUGHT:
  - a **partial** revert, merged with other changes into a new version;
  - a **full** base revert of a file the branch has **kept editing since**: the base rolls
    `x.txt` back from 330 lines to 30, the branch had one later commit tweaking one line of it,
    and the replay applies that tweak on top of the 30-line text. The result — 30 lines plus the
    tweak — is a version no commit ever held, so it is pushed, and the 300 lines are gone from the
    branch. `test_144_r2_known_limit_a_revert_masked_by_the_branchs_later_edit` pins this as a
    strict expected failure, so fixing it turns a test red and this entry gets rewritten.
  What IS caught around it: the same revert when the branch did not edit that file again, a revert
  to a version created by a merge or in the root commit, and every push the `sigma-rebase` skill
  makes (all behind `push_branch`, §3b). It errs the other way on purpose: a plain upstream
  deletion, a move-and-rewrite past rename similarity, and a base reverting its own older change to
  a file the branch carries are all refused like a revert, because as trees they are the same. Its
  cost grows with history: one `git log -m` over the branch tip's whole history per 200
  modified-or-renamed paths, each bounded by `SIGMA_REBASE_GUARD_TIMEOUT` and failing closed.
  **The conservative refusal is paid fleet-wide, per branch.** Upkeep runs per feature branch, so
  one base change the guard reads as a revert blocks upkeep on EVERY feature branch that carries
  the affected paths: a reverted dependency bump (a lock file and a manifest rolled back to a
  version each branch's history held) refuses upkeep on every feature branch cut in the window
  between the bump and its revert, and each stays blocked — a failing `/sigma-doctor` row and an
  issue per branch — until a person resolves that branch (§3b), or sets `work.rebase_upkeep:
  "off"` while it stands. Nothing is lost and nothing is pushed while it waits; the cost is N
  human decisions and N branches drifting further behind their base until each is taken. The
  `sigma-rebase` skill's pushes (§3b) are the same guard and cost the same decision.
  A **goal** branch's replay (`work.rebase()`) is guarded only over the paths the goal changed
  since it forked, so a goal whose commits reached the base as the **same** commits (a true
  merge) and were then reverted is not seen there — its diff since the fork is empty. Replaying a
  goal whose PR has merged is not a flow the loop takes.
  The `sigma-rebase` pushes exempt a loss between the remote tip and the pre-rebase head only when
  it is a pure deletion the branch's own non-merge commit made, of a path the base has not touched
  since the remote tip (§3b). **Fork/upstream shape:** a hand rebase onto `upstream/main` can import
  a deletion, followed by upkeep onto `origin/main`, which never touched the file. That deletion
  is now refused because (b) excludes every locally available remote-tracking ref (`--remotes`
  after `--not`), including upstream's history. No implicit fetch of other remotes is added; this
  evidence is only as complete as the refs held locally. A deliberate deletion already published
  to another remote is conservatively refused too: it is no longer unique local work.
  Each attribution history read also enumerates fetched remote refs: 10x/100x refs means
  10x/100x references to enumerate, with overlapping histories shared by Git's traversal;
  the same per-read and total deadlines still apply.
  That costs something on purpose: a deliberate local **rollback**
  pushed after a rebase is always refused and needs the human's `git push --force-with-lease
  <remote> HEAD:<branch>`. It is only as good as the base ref it is told: a base **rewritten**
  (force-pushed) after an earlier lossy local rebase no longer reaches the revert that caused it,
  so a DELETION that revert made can still read as a unique branch commit's `D` and be exempt
  once **no other remote-tracking ref reaches that deletion**, if the rewritten base does not
  touch that path. `test_292_known_limit_force_rewritten_upstream_erases_deletion_provenance`
  pins that remaining gap as a strict expected failure. No loss detector based on these current
  refs can establish that disappeared remote provenance. A caller with no base, or no
  pre-rebase head, exempts nothing instead.
  **Shallow clones.** Both the rollback detection (`dropped_paths`' history read) and the
  deletion attribution are history-dependent, and a shallow clone cuts that history off: a rollback
  to a version held only below the shallow boundary is NOT SEEN by `dropped_paths` there. The
  exemption fails closed — in a shallow repository (`git rev-parse --is-shallow-repository`) nothing
  is exempt and the refusal says the clone is shallow — but the loss detection itself is weaker;
  run upkeep and `sigma-rebase` from a full clone (`git fetch --unshallow`).
  **Budget.** The history reads come in 200-path batches, each bounded by
  `SIGMA_REBASE_GUARD_TIMEOUT`; all of one push's reads together are bounded by
  `SIGMA_WATCH_CALL_TIMEOUT` (default `120`s), and exceeding it refuses the push rather than
  passing it. At 10x/100x the losing paths that is 10x/100x batches inside the same budget, so a
  very large loss on a slow disk refuses (safe) rather than stalls. **Measured history cost:**
  the local real-Git benchmark in `.sdlc/research/292-benchmark.py` measures 100/1,000/10,000-commit
  synthetic linear histories (1x/10x/100x), one affected path, three sequential samples per case,
  without flushing caches. See `.sdlc/research/292.md` and `292-benchmark.json` for timings,
  environment and raw samples. These measurements do not establish production or fleet latency:
  merge-heavy histories, many refs, cold disks and additional path chunks remain unmeasured.

- **The back-to-back cross-repo landing has no owner** (§11b). Nothing merges a feature branch.
- **`authorized` is enforced on the FILING of work, never on the working of it** (§12). A goal that
  is already on the board and legitimately filed is picked by whoever picks it; ownership is a
  question about who may create the work, not about who may do it.
- **A DECLARED `owner` spelled as anything other than a GitHub login holds every issue** (§12).
  Inference cannot produce one — it copies an `author.login` — but a hand-written entry can, and
  nothing may second-guess a declared value. It fails **closed**: the issue is held, never filed
  wrongly. The held issue's own flag comment prints the recorded owners beside the observed author
  and says that correcting the registry is the fix, so the mismatch is visible on the issue rather
  than only in behaviour.
- **A unit first picked from a bot account's issue is owned by that bot** (§12), by the same
  inference. Where Sigma files and picks under a service account, declare `owner` — or grant the
  unit once with `repos.<repo>.authorized = true`, which is what the per-unit grant is for.
- **A unit's owner is the author of the SECOND goal picked onto it, not the first** (§12) — the
  cost is *which person*, not a delay. The very first pick runs before the registry has an entry,
  and the gate deliberately does not create one (that is the sync's job), so the naming falls to the
  next goal. Those are frequently different people, and the naming is once-only and hand-edit-only
  thereafter, so a passer-by's issue can durably own a unit somebody else created. During that
  window, asks addressed to `owner` by propagation, sync, completion and the doc writer all land
  unaddressed — worst at exactly the moment a new unit raises the most questions. **The follow-up
  that removes it:** the author is already in hand at the pick, so threading that one string forward
  to `work.start` and claiming after the entry exists costs no new call — it needs the `loop`→`work`
  seam widened past the goal id, which is a change to the loop's own contract and not an ownership
  change.
- **A propagated unit's board owner is named by the first goal picked in the RECEIVING repo**
  (§12), not by whoever owns the unit. The two owners are two facts, and a propagated entry arrives
  carrying only the first: the whole entry is copied into the sibling, unit owner included, and
  `repos.<sibling>.owner` is empty. So each repo infers its own board owner, once, from the author
  of the first goal picked there — with the same *which person* cost as the bullet above, and the
  same hand-edit-only correction.

  The alternative was measured and is worse: holding the sibling's work for the unit's owner made
  every issue in that repo that the foreign owner did not open inert, indefinitely, for somebody in
  a **different repository** who may not know the unit reached this one — a refusal with no local
  remedy at all, which is the loop-wedging shape a gate must never have. Before it was fixed the
  sibling's own inference could not rescue it either, because the arm that infers was only reached
  when *neither* owner was recorded (#1575). The unit owner is never overwritten by this: what the
  sibling fills is the board half.
- **A hand-run `work.py start` names nobody** (§12). Inference is a record, not a defence; the field
  is defended by the one-directional registry write (§8f) whatever reaches it.
- **`/sigma-promote` does not clear an ownership or scope hold on its own** (§12, §8f). Both
  pick-time gates recompute from the registry — plus, for ownership, the issue's author — and read
  no label, so a promotion returns board membership and the next pick reaches the same answer. The
  hold ends with a **registry edit**: `repos.<repo>.authorized = true`, a corrected `owner`, or the
  repo added to `repos`; promoting is the gesture that comes after one of those. `/sigma-promote`
  refuses the promotion it can tell would be undone and names the edit, and a goal that is promoted
  and set aside again says so on the issue and in the ledger rather than going quiet (#1569).
- **Depth 3 is a convention, not a check** (§2). What *is* checked is that a unit name has no `/`.
- **"No open goals" means "no goals recorded"** (§8f), because the exact question costs one network
  read per goal per pick. It errs towards leaving a unit open.
- **On the fail-open write path, `landed` is False even when the write plainly went in** (§8e) —
  because there is no lock, that is the true statement, and a clobber arriving after the final
  re-read is invisible to any lock-free scheme on POSIX.
- **A project with `work.enabled` false never syncs the registry**, because the sync lives on the
  path that cuts worktrees. Such a project also cuts no branches and opens no PRs, so there is
  nothing to record — but the two facts are connected by coincidence, not by construction.
- **A goal picked before `.sdlc/features/` existed is never recorded, and no later pass recovers
  it** (§14). Recording happens on the pick, which is the only moment the declared unit is in hand.
  Afterwards the sole local trace is `state/work/<goal>.json`'s `base` — and `finish()` unlinks that
  record, so a backfill would recover whichever goals happened to be **in flight** and silently miss
  every goal that had already finished. That is worse than the gap it closes: a registry that looks
  complete and is not, in the one file whose whole value is that you can trust what it says. `base`
  is not evidence of a declaration either — `work.base` set to `feature/<name>` produces a byte-
  identical record for a goal that declared nothing, and nothing persists the difference. So nothing
  infers past membership. The entry is hand-editable by design (§8), and a human who knows which
  goals belong to the unit can add them; what removes the need is upstream, in the stderr line the
  first such pick now prints (§14), while the correction still costs nothing.
- **Nothing commits the registry after the very first write, and that first commit is a human's,
  not the kit's** (#1565). §14 step 3's "and you commit them" is the ONLY commit this kit ever
  describes; every later pick's `sync_at_pick` amends the same shards again, and no code path here
  stages or commits `.sdlc/features/` for it — `work.commit()`'s `git add -A` runs in the GOAL'S
  WORKTREE, the registry is written in the ROOT checkout, and the root checkout's own `git status
  --porcelain` for that path stays dirty until a human runs one. **The committer is the adopting
  repository's own maintainer, never Sigma:** `git add .sdlc/features && git commit` is the
  whole gesture, run by hand after a pick (or a drain) changes the registry. Automating it is not a
  small addition — several goals picked concurrently share ONE root checkout, `feature_sync`'s
  `flock` (§8e) serialises only the JSON write, and nothing here serialises the `git commit` that
  would have to wrap it, so building that safely is undesigned rather than merely undone.
- **The never-create guarantee is verb-shaped** (§7). Four other `gh` routes could mint a label;
  none appears in this tree.
- **Adoption is not uniform: the label half is not gated on `.sdlc/features/`, deliberately**
  (§14, #1543). The registry sync and the cross-repo check opt out on a missing directory;
  `attach_at_pick` and the needs-label sweep do not, so a repository that never adopted the model
  can still have a pick refused and a label written by declaring a unit in an issue body. Measured,
  not inferred — and chosen, not merely observed: the marker that triggers it is not something a
  repository writes by accident, so the directory would screen for whether an unrelated, optional
  artifact exists yet, not for an accidental declaration.
- **A pick COMMITS THE UNIT'S ENTRY INTO EVERY SIBLING REPO THE ENTRY NAMES, on that repo's DEFAULT
  branch and outside any pull request** (§11). It is the only *commit* Sigma makes into a
  repository other than the one it is running in, and the item on this list to read before adopting
  a cross-repo unit rather than after. `feature_propagate.propagate_at_pick` PUTs
  `units/<name>.json` through the Contents API with **no `branch` argument**, and that omission is
  what selects the default branch — the registry exists because a feature branch can be deleted and
  take every trace of the unit with it, so a copy living on that same branch would not be one.
  Nothing opens a pull request for it, nobody is asked first, and the first the sibling's owner sees
  of it is a commit reading `chore(features): propagate unit <name> from <repo> (goal <id>)`.
  **It is not an authorisation hole, and saying so is not a defence of the surprise.** The write
  happens only on a `granted` verdict §11 already measured at pick against a pinned identity —
  `unknown` selects no write at all — and a goal started outside the loop, which records no verdict,
  holds every sibling and ledgers it instead. Nor can it escalate anything: `authorized` is the
  destination's own **always** and is never copied, so no repo's registry can grant itself the
  per-unit permission (§12) in another. The copy is a read-modify-write that fills empty fields and
  unions `goals`, never removing or overwriting what the destination already states, and only
  `units/<name>.json` travels — never `index.json`, never `<name>.md`. **What actually stops it is
  the sibling's own branch protection**: a protected default branch refuses the write, and that
  refusal is ledgered to that repo's owner with GitHub's own reason rather than retried or hidden.
- **A close that fails after a landed merge is retried by `record done`, and parks the goal if
  that retry fails too** (§13a). The merge-time close (right after `gh pr merge` succeeds) is
  best-effort and reports once: if it fails, the merge line says `— but could not close #N (…)`,
  because a merge that already landed must not be reported as a failure. That is not the end of
  it — `record done` is still the right next step, and its own `source.complete()` retries this
  exact close whenever the issue is still open, which is the ordinary case here since the
  merge-time probe never marked it CLOSED. Only if that retry also raises does `_record` park the
  goal instead of silently dropping the local record — a human-visible state, not a silent stall,
  and the point at which closing the issue by hand becomes the right lever. This is the same shape
  as *detecting a direct commit is not preventing one*: the system knows, retries once more on its
  own, and only then hands off.
- **Discovery stays one-directional** (§8f). The reconcile only ever narrows **its own end**: a
  branch on a remote this checkout never measured is *unmeasured*, which is not "gone" — so no pass
  can delete another repo's half of a unit, and what a sibling propagated in is left alone.
- **Two people on two machines can still start the same goal.** Inherited unchanged from
  `docs/label-model.md` §11 — the flag comment and the resume sweep are read-then-write with nothing
  serialising them, so overlapping windows cost a duplicate comment, never a wrong state.
- **A conflicted issue is picked up and worked.** Body and label disagreeing is announced, not
  refused — only an issue contradicting *itself* is set aside.
- **Detecting a direct commit is not preventing one** (§3). Rebase upkeep refuses to rewrite a
  feature branch carrying a commit no pull request accounts for — which protects whoever is holding
  that commit — but the commit is already there, and only host branch protection could have stopped
  it being made.
- **The direct-commit check runs only when the branch is BEHIND, and when it does not run it is
  SILENT** (§3). The pass returns `current` as soon as `rev-list --count feature..base` is zero, and
  that return is before the check — so `direct` stays empty, no issue is filed, and, because
  `current` is not in `IN_CLAUSE`, `clause()` returns the empty string and the pick line says
  nothing at all. Calling `direct_commits()` against the same two refs finds the commit; the pass
  simply never asks. Three conditions have to coincide before a violation surfaces: a goal declaring
  that unit is **picked** (a resume of an already-started goal returns before upkeep is reached),
  something has landed on the integration branch since, and the commit's subject carries no pull
  request reference. Cheapest-first by design: the cost is silence for as long as the integration
  branch is quiet, never a wrong rebase.
- **An un-named caller plus a live agent marker skips the whole pass, and says so** (#1687). Upkeep
  sits behind the same "is a different live process in this goal's worktree?" guard the resume paths
  use, and that guard compares the registered agent pid against the pids of the `work.py` process
  itself. `work.py start` therefore has to be TOLD who is calling it — `--session-pid "$PPID"`, the
  value `loop.py agent-start --pid` was given — because the agent that registered the marker is the
  parent of the shell that ran the command, never the command's own process. A caller that names
  nobody reads its own registration as a stranger and the pass does not run. This was silent until
  #1687 and cost a real 44-commit drift across two picks; the skip now costs one clause on the pick
  line, but only where a pass genuinely existed (a goal with a unit, in an adopted repo, with
  `rebase_upkeep` on — the other three cases have nothing to skip). `feature_owner.py` documents the
  hand-run `work.py start` as naming nobody deliberately, so this is a supported case, not a
  misconfiguration: a hand-run pick maintains no branch while an agent holds the goal.
- **Under `merge_method: rebase` the check has no local evidence and the whole pass declines** (§3),
  reporting `unverifiable`. That method preserves the goal branch's original commit messages, so a
  landed goal and a hand-typed commit are indistinguishable to any local check. The remote still
  knows (`gh pr list --base feature/<name> --state merged`); matching rewritten shas back to those
  pull requests is a hard follow-up nobody has done, not an impossibility.
- **A commit subject is taken at its word, and there are TWO imitable forms** (§3). The classifier
  reads subject lines against two patterns, so a hand-written commit ending `(#123)` **or** one
  beginning `Merge pull request #9 from ...` is accepted as a landing. Those are the cases — both of
  them — where a direct commit is rewritten under whoever is holding it.
- **The mirror-image misread is a standing outage, not one lost pass** (§3). A repo that squashes
  with a custom title template dropping the `(#N)` reference has no landed commit that the classifier
  can account for, so the pass returns `direct-commits` on **every** pick and the branch is never
  brought forward again. It costs no data — nothing is rewritten — but calling it "one upkeep pass"
  understates it: upkeep is off for that project until the template or `merge_method` changes.

## 16. Feature-level priority — optional, opt-in, and recorded on the unit

A unit's own individual issues each carry their own `priority:P0`–`P4` label as always (§4 is
silent on priority entirely, by design — a unit is about *branching*, not urgency). A unit may
**additionally** carry its own priority — one value, recorded on the unit's `.sdlc/features/`
registry entry (§8b's `priority` field, #2261), never a write to any member issue. **Optionally**,
at the moment a unit is opened (`/sigma-define`'s own `declare` step) or promoted
(`sigma-goal-review`'s §5 feature-ification, same `declare` step), whoever is driving that pass is
asked one more question: *"Give `feature:<name>` a priority? (P0–P4, or skip)"*.

- **Answered** — `define.py set-priority --unit <name> --priority <P>` records `P` on `<name>`'s
  own registry entry, through `feature_sync.amend`'s locked read-modify-write (§8e/§8d — the whole
  entry goes back, so a concurrent pick recording a goal on the same unit cannot clobber it, and
  nothing else about the unit's title, owner, repos or goals is disturbed). Idempotent: re-stating
  the value already recorded writes nothing (§8e). The value can be changed at any later time by
  running the same command again — there is no "one-time" window to miss, because nothing here
  stamps anything that would need re-triggering.
- **Skipped** (or asked on a host with no interactive way to ask) — nothing happens, and nothing
  about today's per-issue priority behavior changes. There is no code path that calls `set-priority`
  unprompted.

**This is THE answer to "prioritise this feature."** A unit's recorded priority is read by the pick
comparator as a lexicographic **tie-break**, never a rule that runs one branch to completion: on the
label queue, `GitHubSource._pick_key` inserts a `feature_rank` term immediately after the issue's
own priority rank — `(priority_rank, feature_rank, not_a_bug, issue_number)` — so a P0 issue in any
unit still outranks every P1 regardless of either unit's own priority, and only a genuine tie at the
issue's own tier is ever broken by it; the board queue applies the identical term at the identical
depth. Consulted at every tier uniformly — there is no configurable cutoff tier below which it stops
mattering. Gated behind `discovery.feature_priority.enabled`, **default ON since #2284** (was off
through #2266) — set it to the literal `false` to turn it off; anything else, including leaving the
key unset entirely, leaves it on. Safe as a default specifically because the term is inert on its
own: a unit that has never had a priority *recorded* (`set-priority` above) resolves to the same
`UNPRIORITISED` sentinel a feature-less issue already gets, so an adopter who never calls
`set-priority` sees a pick order untouched regardless of this switch. Full mechanism: `sources.py`'s
own `_pick_key`/`_feature_rank`/`_unit_priority_rank`.

**A DIFFERENT, still-legitimate verb exists for genuinely levelling a unit's members by hand:**
`define.py bump-priority --unit <name> --priority <P>` sets **every** open `feature:<name>` member
issue's own `priority:` label to exactly `P`, unconditionally — even downgrading one a human had
set individually higher. It is idempotent and human-invoked, and neither setup flow above calls it
by default any more: once a unit's priority is data the comparator reads, unconditionally
overwriting every member's own tier is actively harmful — it erases the very distinctions the
tie-break exists to read. It remains available for the different job of deliberately levelling a
unit's members, invoked directly, by name, never as a side effect of answering the question above.
There is no tooling to detect or restore a repository's priorities from a *previous* run of it —
this is forward-looking only, and there is no evidence it has ever been invoked against a real
repository this project runs.

**Blocker propagation is `bump-priority`'s concern only, never `set-priority`'s.** A bumped member
issue's own `blocked_by` dependencies are picked up by the existing, separately opt-in
`discovery.blocker_promotion` mechanism (`GitHubSource._promote_blockers`) on its own next board
sync — transitively, cycle-safely, with its own audit comment — through the same priority-label
chokepoint that bump uses. `set-priority` writes no label at all, so there is nothing for that
mechanism to pick up from it; building a second blocker-priority mechanism here would duplicate an
existing one regardless of which verb triggered the write.

Full design record: Epic #2161 (the shipped first cut) and Epic #2260 (its "Superseding
`bump-priority`" section — why the first cut was superseded rather than built on).

## 17. The feature-level DAG — `loop.py feature-frontier`

A unit's members already declare dependencies on each other the same way any issue does —
`**Blocked by:** #N`, `depends on #N` — and those edges are already extracted at fetch time and
persisted locally (§8's registry does not carry them; the board mirror does). `loop.py
feature-frontier <sdlc_dir> <unit>` is a **read-only view** over that data: it resolves `<unit>`'s
open members via the same live, full-body declaration read §7's label step already performs (never
the mirror's own truncated excerpt), then walks their persisted blockers with the identical
ready-set, cycle-detection and transitive-fan-out algorithms a goal's own slice plan already uses
one level down. Nothing is compiled — every call re-reads the mirror and re-fetches the open
members, so a dependency that lands between two calls is reflected on the next one with no replan
step.

It never claims a goal, writes a label, or touches the pick path in any way — it is a diagnostic a
human or a skill consults, not a gate. That is also why its one deliberate departure matters: an
edge this view cannot resolve (not a fellow open member, not found closed in the local mirror, not
found open in it either) keeps its dependent **out** of the ready set, the opposite of the pick-time
dependency gate's own fail-OPEN posture on the same shape of gap. Full reasoning, including why that
divergence is safe here specifically because this is advisory output rather than a claim gate:
`skills/sigma-loop/scripts/feature_frontier.py`'s own module docstring, and design #2253
(BR-4, D-10).

## 18. The `core` unit, and AI-judgment classification of a dangling goal

§6's base-resolution guarantee is silent about **whether a pick happens at all** for a goal that
declares no unit — that is a different axis, gated separately (§6's own closing paragraph), and
this section is that gate in full: the no-dangling-goal pair `docs/label-model.md` §2b-iv
(`sdlc:needs-unit`) and §2b-v (`sdlc:needs-triage`) document from the label side, and Epic #2260's
completion work (issue #2260) shipped.

**`core` is a real unit, not a sentinel — a reversal, stated so the earlier shape is not
rediscovered as a regression.** Design #2253's own D-6 originally proposed the opposite:
a bare string `_handle_no_unit_at_pick` wrote as a comment and the registry never checked, chosen
specifically because "a real unit gets a branch, which nobody wants for cross-cutting work." A
2026-09-10 correction reversed that call: `core` is bootstrapped **once, ahead of time**, through
the same `define.py open` path any other unit goes through — a real `feature:core` label, a real
`feature/core` branch, a real `.sdlc/features/units/core.json` entry — and is never a "miscellaneous"
bucket. Conceptually it is the **root of the repository**: every other feature branch is its child
in a code sense, and it exists for root/base-level work that is not any single feature's — the
motivating example is a rename that touches every feature at once (`loop.py` → `core.py`), not a
place to put an issue nobody bothered to classify.

**Gated exactly like `sdlc:needs-unit` (D-7), because it is the same opt-in surface with a second
outcome.** Both conditions are required: `discovery.no_dangling_goal.enabled` is `true`, **and**
`.sdlc/features/` exists. Off, or on an unadopted repo, a goal declaring no unit is picked exactly
as it always was — nothing in this section changes that promise.

**The classifier: a strict, ordered, 4-tier chain, first match wins, and it never creates
anything.** `skills/sigma-loop/scripts/feature_classify.py` is the implementation and its own module
docstring is the source of record for the mechanism — this is a summary, not a substitute for it:

1. a **single existing unit, open or closed**, resolves the goal to it. A match on a **closed**
   unit **reopens it** — a read-modify-write of the whole registry entry, only `open` flips — the
   one deliberate, narrow exception to "reopening a unit is always a human's hand-edit"
   (`docs/label-model.md`'s own Reopening-an-issue section names it, and
   `feature_registry.resolve_open_unit`'s docstring defers to this module for it).
2. **multiple plausible units, or root/base-level work**, attaches to the **configured** catch-all
   — resolved through `discovery.no_dangling_goal.core`, never a hard-coded `"core"` string, and
   never reopened if it happens to be closed.
3. an **identifiable but unregistered** component, backed by **concrete evidence** (a real path
   this repository actually has), is set aside under the existing `sdlc:needs-unit` overlay
   (`docs/label-model.md` §2b-iv) with the suggested name threaded into the flag **comment only** —
   never the issue body, for the identical reason the old `core` sentinel's own docstring gave.
4. **genuinely unknown**, expected rare: the new `sdlc:needs-triage` overlay (`docs/label-model.md`
   §2b-v).

At every tier, the module creates no `feature:*` label, no `feature/*` branch, and no registry
entry — only ATTACHES a label that already exists, routed through the same `attach_label` every
other unit-declaration path uses, which is refused structurally (not merely by convention) by two
independent checks: `sources._swap_labels` resolves every name to a real label first and raises
rather than minting one, and `GitHubSource._run` separately refuses any `gh label create` carrying
a `feature:*` name.

**Honest limitation: tiers 1 and 3 — the actual AI-judgment matching this feature is for — are not
reachable via any real invocation today.** Both integration points (`classify_at_pick`, called from
`_handle_no_unit_at_pick` at pick time, and `classify_for_filing`, called from
`handoff.create_tracked_issue` when the loop opens an issue on its own behalf) accept an optional
real judge and default to `_default_judge` when none is supplied. That default never guesses a unit
or invents evidence — it unconditionally **abstains**, which routes every goal through tier 2, the
configured catch-all. No caller anywhere in the kit supplies a real judge yet, so every
auto-classified issue today lands on `core` (or the configured catch-all) exactly as tier 2
describes — a genuine improvement over the old sentinel (a real, reversible label attach that other
tooling can see, rather than a comment nothing checks), but not yet the intra/inter-feature
judgment call the classifier exists to make. Wiring a live judge in is separate, undecided future
work — a prompt-level CLI channel at filing time (cheapest, but does not reach the pick-time path),
an in-process model call from inside `feature_classify.py` itself (reaches both integration points,
at the cost of real latency/reliability surface on what is today a deterministic pick path), or
shipping the honest-abstain default as this feature's behaviour for the foreseeable term — and this
document will say so once one is chosen, rather than implying tiers 1/3 already work end to end.

Full design record: design #2253 D-6 (the sentinel this reverses) and issue #2260's own
comment history (the 2026-09-10 call that reversed it, and the plan-review that verified the
never-creates-a-unit guarantee before implementation began). Full mechanism:
`skills/sigma-loop/scripts/feature_classify.py`'s own module docstring.

---

*Sibling contract: [docs/label-model.md](label-model.md) for the `sdlc:*` lifecycle labels.
Per-change rationale for everything here is in `CHANGELOG.md` and in the commit messages on the
`feature/branching-model` branch, which is kept for that reason.*
