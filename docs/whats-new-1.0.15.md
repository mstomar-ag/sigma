# What is new in 1.0.15

This page lists every change in this release in grouped, readable form, each with its issue number. The same
changes are in the `1.0.15` section of the [CHANGELOG](../CHANGELOG.md) in the order a maintainer reads them.
Start with [Upgrading from 1.0.14](#upgrading-from-1014) if you only want to know what changes for you.

The main theme is the [Decision Rulebook](decision-rulebook.md): levels, the applier and rules now work on parks
that already exist, and every park declares what kind of question it is. The [Upkeep](upkeep.md) guide covers the
merge changes.

## Upgrading from 1.0.14

Everything in the Rulebook stays off unless you turned it on. If you did not, nothing below applies to you. If the
applier is on, do these steps in order. `<dir>` is your `.sdlc` directory. Script paths are under
`skills/sigma-loop/scripts/`.

1. **Turn the applier off before you upgrade.** Set `rulebook.autonomy.applier.enabled` to `false`. Keep it off until
   step 9 is done. The first sync of the new applier (#1499), together with pooled levels (#1500), could otherwise
   arm many old parks at once.
2. **Back up `decisions/`, `autonomy/` and the rulebook file** (`rulebook.rules.path`, default `.sdlc/rulebook.json`)
   on every repository. The rekey in step 5 deletes watch files.
3. **Upgrade every machine that answers parks and the watcher host, then restart the watcher.** If a leftover
   `.sdlc/state/watch.stop` exists, remove it first, or the watcher exits at once (#1510).
4. **Run `/sigma-doctor` and fix every warning it names** (owners, unreachable levels, mis-kinded parks, local
   commits that never reached the default branch).
5. **Run the rekey on each repository.** `python3 autonomy.py rekey <dir>` is a dry run; add `--apply` to write
   (#1495). Answers, rules, comparisons and drafts now share one key, so comparisons stored under the old key are
   re-keyed. Agreement restarts from the evidence that remains.
6. **Declare the kind of every undeclared park.** List them with `python3 unpark.py list <dir> --undeclared`, then run
   `python3 unpark.py declare <dir> <issue> <kind>` for each (#1498). You can also answer them through
   `/sigma-unpark`. Tell the people who made those parks first: a rule may act after the window, and an owner's exact
   `no` vetoes it.
7. **Review every rule action.** Take each rule id from `.sdlc/rulebook.json` (`rule_draft.py list` shows
   learned patterns, not approved rules); `python3 rule_draft.py show <dir> <rule_id>` prints what one does: unpark, keep parked, or only recommend (#1495).
   Retire or reject proposed rules you will not approve, because they block drafting for their pair.
8. **Set the config explicitly:**
   - `rulebook.autonomy.owners`: list the logins. Do not rely on the `spend_approval.approvers` fallback.
   - `rulebook.autonomy.watch.retention_days`: raise it if you want older comparisons kept in full.
   - `rulebook.rules.reask_days`: choose it (default 90).
   - `rulebook.autonomy.applier.window_minutes` 1440, `max_auto_applied_per_day` 3, `allow_silent` `[]`.
9. **Dry-run the applier.** `python3 applier.py <dir> --dry-run` prints each planned action with its rule, level and
   window, and writes nothing (#1499); with the applier off it prints one line saying so, then the same plan as if it were on, still writing nothing (#1545, #1695). Review every line.
10. **Clear or re-arm the old pending asks** from before the upgrade, so none is applied the moment its pair reaches
    level 3. An ask opened below level 3 is now armed with one notice comment and a fresh window (#1499).
11. **Re-enable the applier** (`rulebook.autonomy.applier.enabled` `true`). Watch the first applied asks for a week:
    each comment names its rule, and a veto lowers the pair.

What stays unchanged:

- Your `.sdlc/config.json` is never rewritten by an update.
- With the Rulebook closed, output does not change (#1496).
- Upkeep's agent level still behaves as off until both spend keys are set (#1516), and a real run is still refused
  until the owner-gated probe.

## The Rulebook learns and acts

- **Every park declares a kind (#1498).** Sigma's own park sites pass an explicit kind. `loop.py record parked`
  without `--qkind` stays undeclared; one stderr line suggests a kind and never guesses from free text. One resolver,
  `qkind.resolve`, reads the newest kind line from a Sigma park comment or a later OWNER, MEMBER or COLLABORATOR
  declaration. New: `unpark.py declare`, `list --undeclared` and `resolve --qkind`.
- **Source-error and stale-worktree parks declare a kind (#1524).** The source-error downgrade-to-park declares
  `quota` for a quota or budget failure and `needs_decision` otherwise; the stale-worktree escalation park declares
  `needs_decision`.
- **One comparison key, and every option declares its meaning (#1495).** Answers, rules, comparisons and drafts share
  the route answer, so ruled pairs can reach level 2. Each bank option says unpark, keep parked or only recommend. A
  re-park is asked again and one park counts once. `autonomy.py rekey` and `rule_draft.py show` are new.
- **Levels pool machines, survive retention and recover from vetoes (#1500).** A pair's comparisons count together
  across machines. A machine with too few comparisons no longer caps it; a disagreeing one with enough still does.
  The ladder reads the newest `l4_min_answers` comparisons whatever their age, and pruning keeps them. A veto's
  one-level drop wears off after `veto_recover_after` agreeing answers. A missing area reads `repo`.
- **The applier asks for parks that already exist (#1499).** `applier.py` runs a bounded sync (20 reads per tick)
  before each tick. An ask in any state covers its park, so it never asks twice. A new park or an unpark supersedes
  or closes the old ask, and superseded asks are pruned after two days. The window counts from when a rule became
  eligible. The applied record carries its gesture and answer. `applier.py <dir> --dry-run` prints the plan and
  writes nothing.
- **An approved rule is shown as the recommendation, and agreement is explained (#1496).** `unpark.py brief` adds
  `recommendation` and `pair`, only with the rules part open and the option on offer. `resolve` records `rule_id`,
  `shown` and `accepted`. The autonomy row adds `misses` and `majority_share`, with shown and not-shown agreement
  apart; level 3 judges the not-shown. `rule_draft.py list --why` and `due` and the doctor explain skipped pairs and
  per-rule acceptance. The applier needs a rule's own live comparisons at level 3.
- **`rule_draft.py suggest` ranks which pairs deserve a rule (#1513).** It scores each question kind and area pair
  from parks, hours parked, goals blocked and answer agreement. It names the majority option and whether one
  any-area rule covers it, and never lists hard-stop or unknown kinds. `--propose N` drafts the top N as proposals,
  never approved. The doctor's rulebook row lists the top uncovered pairs when rules are on.
- **A closing answer is normalised through the bank (#1521).** A `needs_decision` answer is stored after case,
  spacing and a trailing recommended marker are normalised. Other text is `free` and never learnable.
- **Two applier guards (#1522).** An unstamped legacy answers block is no longer counted for a newer stamped park.
  A quota wait-it-out ask is applied only after its recorded reset time; an unknown time counts as not yet.
- **An optional shared spend cap (#1502).** `rulebook.spend.weekly_usd` and `rulebook.spend.monthly_usd` (both unset
  by default, which changes nothing) cap the real cost of the import classifier and the drift judge together over a
  rolling 7 and 30 days. A call that would pass a cap is refused with one line, counted in
  `.sdlc/state/spend-cap.jsonl`, and shown by the doctor with the reset time.
- **The classifier charges a cut-off call its real cost (#1471).** The CLI checks `--max-budget-usd` only after a
  turn, so the old default 0.05 cut off every call yet billed about three times the charge. A failed call now costs
  its envelope cost, the first cut-off stops the run with a message naming `rulebook.import.max_budget_usd_per_call`,
  the drift judge charges known cost on an invalid verdict, and the default is 0.50.
- **Import changes.**
  - The `folders read:` line lists only folders actually found (#1523).
  - Docs show a project-folder form that works in bash and zsh, and a scan prints the folders it read (#1503).
  - A missing project folder reads `skipped`, not `refused`, and `plan` and `apply` print scan notices. `apply` adds
    a `partial import: N files not read` line when a limit cut the import short (#1488).
  - Each imported record and comparison carries an `origin` (hashed on a public repository). `--only-in-repo` or
    `rulebook.import.require_repo_cwd` keeps only answers whose cwd is inside the repository.
    `decision_import.py remove --project-dir DIR [--apply]` deletes one folder's imports (#1477).
- **Headless judge calls no longer read the caller's project (#1472).** The import classifier, drift judge and unit
  assigner and validator run `claude -p` in a fresh empty temporary directory, without `CLAUDE_PROJECT_DIR`, and
  with `--setting-sources user`, `--strict-mcp-config` and `--no-session-persistence` when the installed CLI lists
  them.
- **The drift judge records which documents it did not see (#1481).** A document past the `max_total_chars` budget
  is named in `documents_clipped_out`, a cut-short one in `documents_truncated`, and the doctor warns when the
  documents exceed the budget.

## Hard stops

- **Deny-list filings are hard stops (#1498).** Filing triage declares the first hard-stop kind for deny-word,
  spend, secret and class matches and writes `sigma-hardstop: <class>` on the park. The decision record copies it
  into `hardstop_class`.
- **A deny word holds only in an action clause (#1519).** A clause that describes redaction, detection, gating or
  documentation before the deny word no longer parks, and the park reason quotes the matched clause. Secret-shaped
  text and spend amounts still hold.
- **A plain money amount for compute is spend (#1511).** A body that names a dollar amount near GPU, instance,
  cluster, credits, compute, pod or run time (in digits or words, either order) parks with the spend class.
  Amounts inside fenced blocks stay a miss. An empty `triage.spend_phrases` still disables the check.
- **A hand-typed blocker on a third-party issue is reported, not raised (#1468).** With hard stops on, the
  blocker-priority writer checks ownership before any write. A foreign issue gets no priority label, park or
  comment; one stderr line names it. A Sigma follow-up label now counts as Sigma's own.

## Upkeep

- **The mechanical level unions two append-only shapes (#1515).** A dated `## YYYY-MM` log and a bullet-list file
  that both sides only added to are unioned: per section in date order, then base order and the unit's new items. An
  edit or removal on either side still parks.
- **The agent conflict level is fed from configuration (#1516).** `upkeep.conflicts.agent` adds model, per-unit
  budget, daily spend ceiling, timeout, sweep unit limit and reviewer keys. The level behaves as off until both
  spend keys are set, and a reached ceiling parks the unit as `spend ceiling reached`. The doctor reports
  configuration, spend today and the last resolution.
- **The agent level is hardened (#1536).** A sweep child that finds its sweep counter missing, inactive or from
  another run fails closed. A resolver result that drops a line the integration side added is refused as
  `base-line-dropped`, naming a few lines. The doctor's `resolver configured` row fails while the model is still the
  placeholder id.
- **The sweep says what it cannot see (#1493).** The job record gains `units_listed`, `unregistered` (remote
  `feature/*` branches with no registry record) and the registry reader's `notes`. The doctor shows an upkeep units
  row naming each unmaintained remote feature branch, also when no `.sdlc/features/` exists.
- **Sweep note and units row (#1535).** The note ends `:units=<n>` (`0` for an empty sweep). The doctor units row
  skips branches named in `upkeep.units.exclude` and its remedy names that list. A test pins that a unit differing
  from its branch only in case is registered.
- **The goal-replay conflict finding says who caused it (#1494).** A read-only `git merge-tree` of the goal tip
  against the unit's previous tip reports the conflict as pre-existing (with the previous tip, backup ref and
  conflicted paths), introduced by the pass, or unknown. Only the introduced case says the pass brought it forward.
- **The conflict probe has a time budget (#1537).** Three guard timeouts per pass, after which findings keep the
  original wording. The rebase text in the finding is sanitised and capped. A probe and replay disagreement is
  documented as unknown.
- **Findings are filed at the lowest AI bucket priority (#1484).** P4 by default, not the human default P1. A triage
  park writes that priority too, and `promote.py migrate-confirmation` swaps a follow-up's priority label for the
  bucket priority when it arms it.
- **A rebase-upkeep finding is filed parked when triage is on (#1490).** It carries a declared question kind
  (`merge_conflict` or `needs_decision`), a park event and the lowest triage priority, and no longer carries the
  legacy confirmation label. With triage off nothing changes.
- **The unit state file is written at the single exit of every pass (#1492).** `.sdlc/state/upkeep/units/<unit>.json`
  is recorded under the unit lock, and the job no longer records the same outcome twice.
- **The per-unit record survives an unreadable base tip (#1532).** The base tip is stored as null, and a `current`
  pass whose report has no usable tip refreshes the stored unit tip from the checkout.
- **A merge or empty commit's ack survives the replay (#1470).** The ack also stores a rewrite-stable `rewrite_key`
  (author, author date, message, parent count), matched after `--rebase-merges` re-creates the commit.
- **A goal cut from a unit while another pass rewrites it no longer carries the old unit commits (#1483).** The cut
  tip is read from the new worktree's own HEAD and recorded for every unit goal. When the pick-time pass finds the
  rebase lock held, the start re-checks the unit tip after the cut and moves the still-empty goal branch onto it if
  it moved (provisional).
- **`define.py open` cuts the unit branch from the remote base and records the owner (#1504).** The branch is cut
  from `<remote>/<base>` after a fetch. A base missing on the remote is refused with one line. The unit record
  carries the active login as owner plus one repository entry; if the login cannot be read the owner stays empty and
  one line says so.
- **A follow-up is not folded into a duplicate from another unit (#1505).** The file-time duplicate search keeps
  such a match as related and names it in the new issue's body. Same-unit duplicates still become a comment.
- **The acceptance record reaches the goal branch (#1474).** `work.py commit` copies the root acceptance record into
  the worktree, `work.py pr` refuses a branch missing it, and a confirmed merge removes untracked root plan,
  research and acceptance files byte-identical to what landed. Differing or unlanded ones are reported, never
  removed. Doctor reports old untracked records and modified registry files.
- **The upkeep session line shows a held window (#1507).** It says `window open, retry after HH:MM (last attempt
  HH:MM)`. The line and the trigger share one decision function, `feature_upkeep_window.decide`.

## Loop and claims

- **Session end and dead sessions release goals they claimed but never started (#1529).** On a GitHub source,
  `loop.py session-end` hands back each goal in its own session marker that has no work record and no agent marker,
  through the same release as the over-capacity path (comment `claimed but not started: session ended`, ledger
  `release`, action-log `released`). A dead session's entry does the same on the next pick or start after
  `session_release.grace_minutes` (default 15). Started and live-session goals, a mismatched generation and the
  local source are untouched.
- **A live session can no longer sit on claimed goals it never starts (#1531).** Any pick on a GitHub source releases
  a goal a live session has held longer than `session_release.hold_minutes` (default 30, `0` turns it off) with no
  work record and no agent marker. A started goal is never released.
- **Blocker repair respects state and edges (#1475).** The four blocker-priority writers drop any blocker that is not
  OPEN and never raise a pull request. Whole-body scans use only blocked by, depends on and depends upon. The dry
  pass writes nothing, and outside blockers beyond the 10-read cap are reported on stderr and in the result
  (`unread`).
- **The two automatic blocker writers read explicit edges only (#1467).** The reconcile-tick repair and the
  board-pass promotion read only "blocked by", "depends on" and "depends upon" (unpark Q&A text excluded) plus
  ledger hand-offs. Weak-word mentions such as "after #N lands" are reported on stderr and never written.
- **Blocker priority mode `"smart"` is explained (#1489).** The reconcile tick prints a one-time skip line and the
  doctor shows a not-OK "blocker priority rule inactive" row naming the one-word fix to `"always"`.
- **Switching from `"smart"` to `"always"` shows the first-run preview (#1526).** The smart skip marker no longer
  swallows it, and the doctor row reads "first repair has not run" until the first `always` tick.
- **Confirmation migration (#1517, #1518, #1473, #1469).**
  - An item labelled epic, story, dossier or spec has the retired label removed and is not armed (#1517).
  - An item whose only hold is an open blocker is armed with the `sdlc:blocked` overlay, reason `armed (held by
    #N)` (#1518).
  - A rebase upkeep finding carries a `<!-- sigma:upkeep-finding -->` marker and is parked as `merge_conflict` or
    `needs_decision`, not armed (#1473).
  - `--apply` no longer reports complete when an armed card stayed off the Ready lane. Each such row carries a note,
    a board-queue repo exits non-zero, and a migrated park moves its card to Parked (falling back to Blocked)
    (#1469).
- **`board_migrate.py --apply` is safer (#1491, #1525).** It snapshots every card by project item id, fails if a
  card's Status changed during the option write, and prints counts that add up to the board. A card deleted before
  the final snapshot prints a failure, asks you to run it again and exits 1; null-content cards count as `other`.
- **A merge parked for a missing approval says so (#1506).** It says the PR is not approved and how to approve it,
  instead of blaming GraphQL. The failed-read park keeps its wording.

## Ledger privacy

- **Ledger entry file names can use an alias or a salted host token (#1514).** `ledger.host_alias` and
  `ledger.salt_host_token` (both off by default, which changes no file name) replace or salt the hash of the host
  name in `entries/<actor>-<host>.<pid>.jsonl`. Older files stay read and attributed. The doctor row
  `ledger entries carry no raw host name` reads the local copy of the ledger branch and warns on a public repository.
- **Changing either key re-roots this machine (#1528).** It is one-time, and shared aliases are unsupported. Doctor
  warns when `salt_host_token` is true but `discovery.github.repo` is unset.

## Doctor rows added or changed

- `resolver configured` fails while the model is the placeholder id (#1536).
- Upkeep agent level: configuration, spend today and last resolution (#1516).
- Upkeep units row: unmaintained remote feature branches, `upkeep.units.exclude` in the remedy (#1493, #1535).
- Rulebook row: top uncovered pairs (#1513); skipped pairs and per-rule acceptance (#1496).
- Drift judge: warns when documents exceed the budget (#1481).
- Spend cap: shown with the reset time (#1502).
- `ledger entries carry no raw host name` (#1514), and the `salt_host_token` without repository warning (#1528).
- `no leftover watcher stop file`, only when `.sdlc/state/watch.stop` exists and no watcher is running (#1510).
- `blocker priority rule inactive` (#1489) and `first repair has not run` (#1526).
- `gh auth`, `gh project scope` and pinned-board rows read "(cannot verify)" with wait-for-the-reset advice when a
  read is rate limited (#1485) or the network fails (#1527). No `gh auth login` or create-a-board command is offered.
- The `blocking_priority_override` row describes the within-tier order (#1509).
- Old untracked records and modified registry files (#1474).

## Wording and docs fixes

- **The `blocking_priority_override` doctor row (#1509).** It said a blocking goal sorts ahead of every other issue
  regardless of tier. It now says a blocking goal goes first within its own tier and a blocker is raised to the tier
  of what it blocks.
- **The branching model states that a merged goal branch is deleted on the remote (#1512).** It names the guards that
  refuse the delete and says it cannot be turned off.
- **The Rulebook and upkeep enable steps (#1510)** say to remove a leftover `.sdlc/state/watch.stop` before starting
  the watcher.
- **The `archive_after_days` note (#1508)** says `archive` is a library function nothing calls automatically, not a
  command.
- **Import docs and help (#1503)** show one folder per flag, or a quoted array.
