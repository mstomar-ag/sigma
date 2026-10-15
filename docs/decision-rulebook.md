# The Decision Rulebook

The Rulebook is an optional layer that remembers how the people who run a repository answer the questions the loop
parks on, and lets the loop earn the right to answer the repeated ones itself. Everything in it is off until you
open a gate, and each gate opens on its own. Back to the [README](../README.md).

In one paragraph: when the loop parks a goal because it needs a decision, a person answers through
`/sigma-unpark` (or approves through `/sigma-promote`). With the Rulebook on, that answer is stored as a
**decision record** in the repository. When enough answers of one **kind** in one **area** agree, the kind earns
a higher **autonomy level**. At low levels the loop only records and recommends. At the top two levels, and only
when you open the applier, it may answer a question nobody answered in time, using a **rule** an owner approved,
and tells you it did, with a window in which you can say `no`.

This page is the full reference. The short summary is in the [README](../README.md#decision-rulebook-and-upkeep);
every key is also listed in the [configuration reference](configuration.md); the data the Rulebook sends anywhere
is in [privacy.md](privacy.md).

## Contents

- [Words used here](#words-used-here)
- [The parts and how they fit](#the-parts-and-how-they-fit)
- [Turning it on, step by step](#turning-it-on-step-by-step)
- [What it will never do](#what-it-will-never-do)
- [Decision records](#decision-records)
- [Question kinds](#question-kinds)
- [Autonomy levels](#autonomy-levels)
- [Rules: drafting and approval](#rules-drafting-and-approval)
- [The applier and the veto window](#the-applier-and-the-veto-window)
- [Hard stops and the record-done net](#hard-stops-and-the-record-done-net)
- [The drift judge](#the-drift-judge)
- [Importing past decisions](#importing-past-decisions)
- [Privacy on public repositories](#privacy-on-public-repositories)
- [Spend ceilings](#spend-ceilings)
- [Migrating the old confirmation label](#migrating-the-old-confirmation-label)
- [What closed gates mean](#what-closed-gates-mean)
- [Configuration reference](#configuration-reference)
- [Verbs and where each lives](#verbs-and-where-each-lives)
- [What the doctor shows](#what-the-doctor-shows)
- [Failure modes and recovery](#failure-modes-and-recovery)
- [Rollout advice](#rollout-advice)

## Words used here

| Term | Meaning |
|---|---|
| question kind (`qkind`) | the class of a parked question, declared by the agent that parks (see [Question kinds](#question-kinds)) |
| area | the first `area:<x>` label on the issue, else `repo` (the whole repository) |
| decision record | one small JSON file describing one answer: kind, area, choice, reason, how it was given |
| how | `human` (a person's gesture), `autonomous` (the automatic sweep or the applier), or `imported` (read back from past work) |
| level | 0 to 4, derived from records and from watch comparisons, never stored |
| watch comparison | what the rulebook would have chosen, next to what the person chose |
| rule | a committed statement "for this kind in this area, recommend this option" |
| ask | one pending question the applier is tracking |
| owner | a login listed in `rulebook.autonomy.owners`, the only place a login is read as authority |

Records and rules describe the repository and its areas, never a person. A rule with any person field is invalid.

## The parts and how they fit

The block `rulebook` in `.sdlc/config.json` has five **parts**, plus two settings blocks. A part opens only when
its own `enabled` is the JSON boolean `true` (the text `"true"`, `"yes"` and the number `1` all stay off).

| Part | Key | What it adds | Reads |
|---|---|---|---|
| records | `rulebook.records` | stores each answer; counts them | nothing else |
| hard stops | `rulebook.hard_stops` | classifies concrete actions and parks the dangerous ones | nothing else |
| autonomy | `rulebook.autonomy` | derives levels from records and comparisons | records |
| rules | `rulebook.rules` | a committed file of approved rules | records, autonomy for drafting |
| drift | `rulebook.drift` | judges a parked question against reference documents, in shadow only | a model |
| applier (inside autonomy) | `rulebook.autonomy.applier` | levels 3 and 4: answers an unanswered question | autonomy, rules |
| import | `rulebook.import` | bounds for reading past decisions; not a gate | |

A wrong-typed block, part or `enabled` closes the **whole** Rulebook, not just that part, and `/sigma-doctor` names
the key. The hard-stop net follows the same rule. Keys beginning with an underscore are notes and are ignored.
The retired key `decision_rubric` is not read; the doctor says so if it is still present.

## Turning it on, step by step

Nothing below is needed for the rest of Sigma. Do the steps in order and stop wherever you are comfortable.

1. **Copy the block in.** A repository scaffolded earlier does not receive the `rulebook` block. Copy it from
   `skills/sigma-init/templates/config.json.tmpl` in the installed plugin, with every `enabled` still `false`.
   Run `/sigma-doctor`; the rows `rulebook`, `autonomy levels` and `applier tick` should read off.
2. **Set visibility.** If the repository is public, set `rulebook.records.repo_visibility` to `"public"`
   before you open anything (see [Privacy](#privacy-on-public-repositories)).
3. **Open records.** Set `rulebook.records.enabled` to `true`. From now on `/sigma-unpark`, `/sigma-promote`
   and the automatic sweep each write a record and one comment carrying its id. Commit the `decisions/` folder
   (with `records.publish` on, a person-run gesture commits it for you; it never pushes).
4. **Look at what you have.** Run `python3 skills/sigma-loop/scripts/decision_import.py plan .sdlc` to see what
   past decisions could be imported, then `apply` if you want them (see [Importing](#importing-past-decisions)).
5. **Open autonomy (read-only effect).** Set `rulebook.autonomy.enabled` to `true`. Levels are now derived.
   Levels 0 to 2 only record and recommend. Read them with `python3 skills/sigma-doctor/scripts/doctor.py features .sdlc`.
6. **Name your owners.** Set `rulebook.autonomy.owners` to the list of logins who may approve rules. If you leave the key
   out, `spend_approval.approvers` is used instead; if both are empty, owner confirmation is refused.
7. **Open rules and draft one.** Set `rulebook.rules.enabled` to `true`. When a kind and area reach level 2, run
   `rule_draft.py list`, then `rule_draft.py draft`, then an owner runs `rule_draft.py approve`
   (see [Rules](#rules-drafting-and-approval)). A drafted rule is only ever proposed.
8. **Optional: hard stops.** Set `rulebook.hard_stops.enabled` to `true` to park dangerous actions
   (see [Hard stops](#hard-stops-and-the-record-done-net)). This only adds stops.
9. **Optional: drift judge.** Needs a model name and a daily ceiling. Shadow only.
10. **Last, and only if you want it: the applier.** Set `rulebook.autonomy.applier.enabled` to `true`, with the
    conservative defaults. It needs the ledger watcher running, because the watcher runs its tick.

## What it will never do

- It never decides on its own below level 3. Levels 0 to 2, and every rule at any level, only recommend.
- It never applies an answer for a **hard-stop kind** (default: `irreversible`). That cap is fixed and not configurable.
- It never approves a rule. A drafted or imported rule starts `proposed`, and only an owner approval makes it count.
- It never lets a person's answer lose. A person's answer inside the window always wins, and an owner's `no`
  after an automatic apply puts the goal back and lowers the level.
- It never applies more than `max_auto_applied_per_day` answers in a rolling 24 hours.
- It never acts silently unless the kind is listed in `allow_silent` and the rule is owner-approved; even then
  every `spot_check_one_in`-th apply still comments.
- It never writes a login into a record or a rule. The one place a login is read as authority is the owner check.
- It never raises a level from disagreement: when machines disagree the lowest level wins.
- It never pushes. Publishing records is a local commit.
- It never calls a model from the drift judge without a model name and a positive daily ceiling.
- It never changes a label, parks, or blocks on the drift judge's verdict. That judge is shadow-only.
- It never rewrites an approved rule. A rule edited by hand after approval stops counting.

## Decision records

A record is one file `<store_dir>/<id>.json` (default folder `.sdlc/decisions`), created exclusively, so parallel
worktrees add distinct files and never edit a shared one. The files travel on the goal's own branch through the normal
commit flow. About 1 KB each.

Fields: `id`, `time`, `gesture`, `qkind`, `area`, `choice`, `reason`, `how`, `issue`, and `rule_id` when a rule was
involved. `choice` and `reason` are the only free-text fields.

Who writes a record, by gesture:

| Gesture | Writer | `how` |
|---|---|---|
| `unpark`, `keep-parked` | `unpark.py resolve` | human |
| `promote`, `demote` | `promote.py` | human |
| `ack` (a ledger acknowledgement) | `handoff.py` | human; stores a record only, touches no issue and posts no comment |
| automatic sweep (blocker closed) | the auto-unpark sweep | autonomous, kind `dependency` |
| applier apply | `applier.py` | autonomous |
| import | `decision_import.py apply` | imported |

Each gesture also posts **one comment** on the issue with the same record id, inside a fenced span the blocker
scanner strips, so free text in an answer cannot plant a blocker edge. A store error never blocks the comment (the
error is reported), and a failed comment never loses the record. A failed label write writes no record, and
`keep-parked` or `unpark` on an issue that is not parked is refused with nothing recorded.

Housekeeping:

- A generated `_index.json` is a rebuildable cache and is never committed.
- `archive_after_days` (default 180) is the age at which the `archive` function folds old records into one file
  per year. `reindex` and `archive` are library functions in `decision_store.py`; the only command-line verb it has
  is `publish`.
- The doctor warns above `max_files_warn` files (default 5000) and when records sit untracked for more than a day.
- A truncated file left by a crash is skipped by readers.
- `records.pointer` (off by default; needs the ledger) writes one short pointer per decision to the team ledger,
  batched to `max_writes_per_day` (default 1) with `compact_after_days` (default 14) folding old pointers. A pointer
  carries only `id|kind|how`, never the record, so a lost pointer loses nothing.
- `records.publish` (the config template sets it to `true`; a config that omits the key leaves it off): a person-run unpark or rule approval **commits, never
  pushes,** the records folder, the `autonomy` folder and the rulebook file, and nothing else. It skips goal
  worktrees, unit branches and a detached HEAD.

## Question kinds

The agent that parks a goal declares the kind with `loop.py record <sdlc_dir> <goal> parked "<text>" --qkind <kind>`,
which adds one machine line (`sigma-qkind: <kind>`) to the park comment. Without the flag the kind is guessed from the
text, and the Rulebook treats an undeclared kind as not learnable. The closed list:

`irreversible`, `needs_decision`, `merge_conflict`, `failing_check`, `no_evidence`, `dependency`, `review_cap`,
`quota`, `unknown`, `scope_hold`, `owner_hold`, `reviewer_down`, `review_post_ambiguous`.

`scope_hold` and `owner_hold` are written by the two pick gates (scope expansion and ownership). With AI-filed
triage on (`ai_filed.triage.enabled`, default true) those gates park a held goal with that kind instead of writing the
old confirmation label. `/sigma-unpark` lists them, and the automatic sweep never resumes them.

## Autonomy levels

A level from 0 to 4 is derived per kind and area. It is never stored. The thresholds, with the defaults:

| Level | Answers needed | Agreement needed (over at least as many comparisons) | What the loop may do |
|---|---|---|---|
| 0 | fewer than level 1 | | ask every time |
| 1 | 10 (`l1_min_answers`) | | ask; counting has started |
| 2 | 30 (`l2_min_answers`) | 0.90 (`l2_agreement`) | ask, with a rule offered as a recommendation |
| 3 | 60 (`l3_min_answers`) | 0.95 (`l3_agreement`) | the applier may act and tell |
| 4 | 300 (`l4_min_answers`) | 0.99 (`l4_agreement`) | the applier may act silently for kinds in `allow_silent` |

Rules of the ladder:

- **Answers** are human records plus imported records of that kind and area.
- **Agreement** is how often the rulebook's pick matched the person's. It is measured **per machine**, over at
  least as many comparisons as answers for that level, and **the lowest machine wins**.
- **Imported evidence** counts toward levels 1 and 2 and never above level 2. Above that only live answers and live
  comparisons count.
- **A veto** (an owner's `no` after an applied answer) drops the kind one level, once. `veto_pause_consecutive`
  (default 3) vetoes since the last agreeing comparison, or `veto_pause_total` (default 20) in all, pause the kind at level 0.
- **Hard-stop kinds** are capped at level 2, whatever the evidence.
- Missing, unreadable or malformed files never raise a level.
- Watch files older than `watch.retention_days` (default 30) are pruned by the time inside them; vetoes are kept.
- Public visibility writes no watch comparisons, because they would carry the free-text choice.

State lives in `.sdlc/autonomy/` as one small file per fact (`watch-<id>.json`, `veto-<id>.json`, plus the applier's
`ask-<id>.json`, `applier-heartbeat.json` and `spot-counter.json`). The machine tag is a short hash of the host name.

### Per-category thresholds

An optional file `.sdlc/categories.json` (schema `sigma/categories@1`) groups areas into categories, each with its own
`levels` block using the same keys. Precedence per key is the global value, then the first category (file order)
whose `areas` contain the pair's area and whose `qkinds`, when given, contain its kind. A bad entry is dropped on its
own; a bad file reads as no categories. The doctor and the question list show the thresholds in force. Full schema:
[configuration.md](configuration.md#per-category-autonomy-thresholds).

## Rules: drafting and approval

A rule says: for a question kind in an area, recommend this option. The file is `.sdlc/rulebook.json` (`rulebook.rules.path`,
relative to the repository root, no parent escapes). Rule fields are a closed set: `id`, `qkind`, `area`, `option`,
`reason`, `status`, `created`, `confirmed`, `seal`. A rule outside that set is invalid and never acts.

- A rule starts **proposed**. Approval stamps a time and a **seal**, a hash of the rule's content. The approver's
  login is not stored. A rule whose status says approved but whose seal does not match its content (a hand edit)
  is read as proposed and never acts.
- **Matching.** Exactly one sealed approved rule for a kind and area is a recommendation. None, or two or more, mean the
  question is asked. A hard-stop kind never matches.
- **Re-ask.** Approved rules older than `reask_days` (default 90) are listed as due for confirmation again.

Drafting is deliberate and conservative:

```
python3 skills/sigma-loop/scripts/rule_draft.py list    .sdlc                 # pairs at level 2 or more whose answers agree
python3 skills/sigma-loop/scripts/rule_draft.py draft   .sdlc <qkind> <area>  # proposes; never approves
python3 skills/sigma-loop/scripts/rule_draft.py approve .sdlc <rule_id>       # owner only
```

`draft` splits the pair's human answers deterministically by a hash of the record id. The majority option of one part
drafts the rule, and the other part is an exam: the draft passes only when the held-out answers agree with it at
`rulebook.rules.draft.min_agreement` (default 0.90), with at least `min_held_out` (default 5) held-out answers. It
fails closed on a small held-out set, a tie, a missing option, a hard-stop kind, a level below 2, or a pair that already
has a rule. It needs the rules, autonomy and records parts all open. On a public repository it refuses (the free-text
choice it needs was never stored).

`approve` refuses, with one line, for an unknown id, an already approved rule, a non-owner, an unresolved login or a
closed gate. For a login that is not an owner it prints the line that asks an owner to approve, and the rule stays proposed.

## The applier and the veto window

The applier is the one part that answers for a person. It is a separate gate, `rulebook.autonomy.applier.enabled`,
and it runs as one more timed call in the ledger watcher's tick (`applier.py`). Closed, it spawns nothing and writes nothing.

How an ask is born: when a goal is parked with a declared kind, and an approved rule matches the issue's area, the
loop opens an **ask** (reusing a pending one for the same issue). A hard-stop kind or an undeclared kind opens none.

What the tick does, in order:

1. Reads owner comments. An owner's exact `no` on a **pending** ask cancels it. An owner's `no` on an **applied** ask,
   posted after it was applied and inside the window, records a veto. The veto is judged by the comment's own time, so
   a late tick still honours it, up to `stale_after_intervals` watch intervals past the window.
2. Plans actions. An ask is applied only when all hold: nobody answered, the window (`window_minutes`, default 1440)
   has passed, the kind is not a hard-stop kind, the derived level is at least 3, exactly one sealed approved rule
   matches, and fewer than `max_auto_applied_per_day` (default 3, 0 stops it) were applied in the last 24 hours.
3. Applies. It re-reads the issue's labels first: an issue that is no longer parked closes the ask as already unparked
   and applies nothing, and a failed read leaves the ask pending. Then one atomic gesture: an unpark label swap
   (card to Ready on a board-active repository) or `promote`. A rule whose option reads as leaving the issue parked
   (a leading "no" or "do not", or keep/leave/stay/remain/hold with "park") touches no label and no card. After the
   gesture lands it writes the record and posts the comment naming the rule id.
4. Handles vetoes. A vetoed unpark is parked again (labels, and the board card on a board-active repository), a
   comment says so, and the kind drops a level.

Level 3 comments every time. Level 4 is silent only for a kind in `allow_silent` on an owner-approved rule, and every
`spot_check_one_in`-th silent-eligible apply per kind and area (default 5) still comments. That counter advances only
for applies that landed, and it is decided per action at apply time.

A person's gesture on the same issue (unpark, keep-parked, promote, demote) closes the pending ask with their text.
A keep-parked or demote on an issue whose ask was applied inside its window records the veto.

The tick writes `applier-heartbeat.json`; the doctor reads a tick as dead when it is missing or older than
`stale_after_intervals` (default 3) watch intervals. A dead tick is harmless: the goal stays parked. Applied asks with no
pending veto are deleted after `prune_after_days` (default 30); answered and vetoed asks after two days.

## Hard stops and the record-done net

`rulebook.hard_stops` classifies one concrete action text into a class, or says it cannot tell. An unreadable action is
never treated as permitted: a parse failure, a bad pattern or a raising detector reads as "cannot tell", which blocks.

Classes: `spend`, `destroy`, `rewrite_history`, `production_change`, `send_outside`, `access_secrets` are on by default
when the part is open; `tamper_oversight` and `unvetted_execution` are off by default. Toggle one with
`hard_stops.classes.<name>`. Patterns are shape-based and deliberately conservative. The legacy keys
`gates.irreversible_actions` map to class toggles while this part is open, and `gates.on_block` other than `park` is
ignored: a block parks.

Own-branch carve-out: Sigma's own lease force-push of a branch under `own_branch_prefixes` (default `["sdlc/"]`) or of
`feature/<unit>` for a registered unit (`own_registered_units`, default true) is not classified, and a merge Sigma performs
under `work.auto_merge` is own by chokepoint, not by pattern.

Where it applies: the commit, pull-request and rebase-push steps, and the issue writes Sigma performs itself. A command
an agent runs by itself on a host without hooks stays advice. Kinds in `hard_stops.hardstop_kinds` (default
`irreversible`) are capped at autonomy level 2 whether or not this part is open.

**The record-done net.** With `action_log.enabled` and this part open, `loop.py log --kind action --action "<text>"`
records a concrete action. `loop.py record <goal> done` is refused (exit 4) when an `action` entry classifies to a
hard-stop class and no `permission_use` or park entry carries the same text. With the action log off the net reads
nothing.

## The drift judge

The drift judge asks one model call whether a parked question is consistent with reference documents you name. It is
shadow-only and has two parts that must both be configured before anything is called.

- It runs once per issue and park, when a question is parked with a declared, non-hard-stop kind.
- It reads the files listed in `rulebook.drift.documents` (repository-relative; an absolute path, an escape or an
  unreadable file is reported as unreachable), clipped to `max_total_chars` (default 40000) together.
- Verdicts: `conflicts` (counts only with a quote the code finds verbatim in a document, and the document is named),
  `consistent`, `not_covered`, or `not_judged` with a typed reason (`disabled`, `no_model`, `no_ceiling`,
  `ceiling_reached`, `call_failed`, `lock_unusable`, `spend_unreadable`, `no_documents_configured`, `documents_unreachable`).
- It stores the verdict in `.sdlc/state/drift-shadow.json` and **never parks, blocks or changes a label**.
- A call that returns no valid verdict (a timeout, a kill) is `not_judged` (`call_failed`), is charged the per-call
  budget against the daily ceiling, and is not stored, so the next park judges again.
- `rounds` (default 1, at most 3) repeats the call; the verdict is `consistent` only when every round says so.

The model name is `rulebook.drift.model` and the daily ceiling `spend_ceiling_usd_per_day`; both default to `null`,
which means no call. When the rolling 24-hour spend reaches the ceiling, the doctor says
`drift judge paused: daily ceiling reached at HH:MM`. The real cost of one run has not been measured; the defaults are provisional.

## Importing past decisions

`decision_import.py` turns decisions you already made into records, so a repository does not start at zero.

```
python3 skills/sigma-loop/scripts/decision_import.py scan  .sdlc [--profile DIR ...] [--project-dir DIR ...]
python3 skills/sigma-loop/scripts/decision_import.py plan  .sdlc [same options]
python3 skills/sigma-loop/scripts/decision_import.py apply .sdlc [same options]
```

- `scan` reports candidates and writes nothing. `plan` shows per kind and area what `apply` would write, how many are
  unknown, and the level the evidence alone would reach (capped at 2). `apply` writes records marked `how: imported`
  (with a source fingerprint and the original time) plus replayed watch comparisons. It is idempotent and writes
  nothing while the records part is closed.
- **Sources.** Transcript files of an `AskUserQuestion` call paired with its answer (no other tool result is read), and,
  when `rulebook.import.comments` is `true`, `sigma:unpark-qa` answers and decision-record lines in issue comments of
  goal-labelled and parked-labelled issues (at most `max_issues`, default 50). Each answer takes the kind of the nearest
  preceding `sigma-qkind` line.
- **Bounds.** `max_file_bytes` (500 MB, streaming), `time_budget_s` (60, a total per scan; a file cut short is reported
  as partial), `max_candidates` (500). A dash-leading folder name takes two words: `--project-dir -srv-demo-work-repo`.
- **Classifier.** `rulebook.import.classifier` is `"table"` (default: a fixed keyword table in code) or `"model"`. With
  `"model"` and the records gate open, `apply` sends only the clipped question and option labels to the named small model
  (`model`, default `haiku`), accepts only a listed kind and area (else `unknown`), caches by fingerprint so a second run
  calls nothing, and stops at the per-call and per-run ceilings. `plan` sends nothing. A call that times out or fails is
  neither cached nor stored, so a re-run retries it; `apply` reports the count. Each call is bounded by `timeout_s`
  (default 60) and a timeout kills the whole process group.
- **Agreement shape.** Replay compares a normalised shape (resume-or-keep, accepted-recommended, option role) when a
  decision has options, else the exact choice. Records of kind `unknown` never feed rule drafts.
- Imported records show in the doctor and the question list as `N imported`. `/sigma-init` mentions the import as an
  optional, never-run task when records are on.

## Privacy on public repositories

Records are committed to the repository, so on a public repository their free text would be public. Set
`rulebook.records.repo_visibility` to `"public"` (any other value reads as private) and the following apply:

- Stored records drop `choice` and `reason`; ids, kinds and enumerations stay.
- The unpark answer block on the issue records each answer as `recorded privately`; keys and question text stay.
- The keep-parked marker drops its reason.
- The applier's comment omits the rule option.
- No watch comparisons are written, so a public repository learns only from counts and cannot earn trust above
  what `rule_draft` can verify (and `rule_draft` refuses there).
- The doctor warns when records are on in a public repository. It makes one REST read of the repository's private
  flag and warns whatever the configured value says; without `gh` or on a failed read it uses the configured value and
  says so.

With records closed, or visibility private, every byte is as before. Nothing in the Rulebook leaves the machine except
the model calls named above (the drift judge and the optional import classifier) and the issue comments, labels and
board moves the gestures already made. See [privacy.md](privacy.md).

## Spend ceilings

| Where | Per call | Over time | Default |
|---|---|---|---|
| drift judge | `drift.max_budget_usd_per_call` (0.10) | `drift.spend_ceiling_usd_per_day` (rolling 24 hours) | no ceiling means no call |
| import classifier | `import.max_budget_usd_per_call` (0.50) | `import.max_budget_usd_per_run` (1.0) | `"table"` calls nothing |
| applier | | `autonomy.applier.max_auto_applied_per_day` (3) | a count of applies, not money |

The per-call figure is passed to the CLI as `--max-budget-usd`, which the CLI checks only after a model turn: one call can
bill more than it, and the answer is then discarded. A failed call is charged its real cost, and the first budget
cut-off stops the import run with a message naming `rulebook.import.max_budget_usd_per_call`.

The import ceiling applies to **one run** only. There is no daily or cross-run cap, so repeated runs add up. All dollar
defaults are provisional.

## Migrating the old confirmation label

Nothing writes the legacy `sdlc:needs-confirmation` label any more: a filed follow-up is already a goal, armed or
parked with a declared question. Boards that still carry leftovers are cleared with:

```
python3 skills/sigma-loop/scripts/promote.py migrate-confirmation .sdlc                    # dry run (the default)
python3 skills/sigma-loop/scripts/promote.py migrate-confirmation .sdlc --apply [--unit NAME] [--only N,N] [--limit K]
```

The dry run prints each issue's destination, population and reason. `--unit` (case-insensitive), `--only` and
`--limit` (open issues only) let you run a board in slices; an empty filter, or one that matches nothing, exits non-zero
with one line. Writes are paced by `ai_filed.migration.max_writes_per_minute` (default 30); a rate-limit answer pauses
(`rate_limit_pause_s`, default 60, up to `rate_limit_max_pauses`, default 3) and resumes. An apply re-reads the labelled
set and fails if an open issue in the slice still carries the label; an already parked issue stays parked.
`/sigma-promote` and `/sigma-doctor` count leftovers.

## What closed gates mean

"Closed gates are byte-identical" means this, and tests assert it part by part:

- A part whose `enabled` is not the JSON boolean `true` reads nothing it would otherwise read, writes no file,
  makes no network call, spawns no process, and produces the same output as a release without the feature.
- The applier step in the watcher tick is not even spawned.
- The hard-stop guard returns an allowing verdict before reading anything.
- Records closed means gestures keep their old code path and their old comment text.
- The one exception is the doctor, which always prints its rows (they read `off`).

Two things are not gated by `enabled`, on purpose: the fixed hard-stop-kind cap on autonomy levels, and
`decision_import.py scan`, which only reports.

## Configuration reference

Defaults are those scaffolded by `/sigma-init`. A wrong-typed value reads as its default unless it is an `enabled`
flag, which closes the whole block.

| Key | Default | Meaning |
|---|---|---|
| `records.enabled` | `false` | open the records part |
| `records.publish` | `true` | a person-run gesture commits (never pushes) records, autonomy files, rulebook |
| `records.repo_visibility` | `"private"` | `"public"` keeps free text out of records and issue text |
| `records.store_dir` | `"decisions"` | plain folder name under the state folder |
| `records.lock_timeout_s` | `20` | wait for the index lock before a rebuild is skipped |
| `records.archive_after_days` | `180` | age at which archiving folds records into a yearly file |
| `records.max_files_warn` | `5000` | doctor warns above this many record files |
| `records.pointer.enabled` | `false` | write ledger pointers (needs the ledger) |
| `records.pointer.max_writes_per_day` | `1` | pointer batches per writer per UTC day |
| `records.pointer.compact_after_days` | `14` | age at which a writer's own pointers fold into a rollup |
| `hard_stops.enabled` | `false` | open the hard-stop net |
| `hard_stops.own_branch_prefixes` | `["sdlc/"]` | branches whose lease push is Sigma's own |
| `hard_stops.own_registered_units` | `true` | `feature/<unit>` of a registered unit is own |
| `hard_stops.classes.tamper_oversight` | `false` | default-off class |
| `hard_stops.classes.unvetted_execution` | `false` | default-off class |
| `hard_stops.classes.<name>` | on | toggle any default-on class off with `false` |
| `hard_stops.hardstop_kinds` | `["irreversible"]` | kinds capped at level 2 and never applied |
| `autonomy.enabled` | `false` | derive levels |
| `autonomy.levels.l1_min_answers` ... `l4_min_answers` | `10`, `30`, `60`, `300` | answers per level (whole numbers of at least 1) |
| `autonomy.levels.l2_agreement` ... `l4_agreement` | `0.90`, `0.95`, `0.99` | agreement per level (0 to 1) |
| `autonomy.veto_pause_consecutive` | `3` | vetoes since the last match that pause a kind |
| `autonomy.veto_pause_total` | `20` | vetoes in all that pause a kind |
| `autonomy.watch.retention_days` | `30` | age at which watch records are pruned |
| `autonomy.owners` | unset | logins who may approve; unset falls back to `spend_approval.approvers` |
| `autonomy.applier.enabled` | `false` | open levels 3 and 4 |
| `autonomy.applier.window_minutes` | `1440` | how long a person has to answer or veto |
| `autonomy.applier.max_auto_applied_per_day` | `3` | rolling 24-hour cap; `0` stops it |
| `autonomy.applier.allow_silent` | `[]` | kinds that may be applied silently at level 4 |
| `autonomy.applier.spot_check_one_in` | `5` | every N-th silent-eligible apply still comments |
| `autonomy.applier.stale_after_intervals` | `3` | watch intervals without a tick that the doctor reads as dead |
| `autonomy.applier.prune_after_days` | `30` | age at which an applied ask with no veto is deleted |
| `rules.enabled` | `false` | open the rules part |
| `rules.path` | `".sdlc/rulebook.json"` | where the rule file lives |
| `rules.reask_days` | `90` | age at which an approved rule is due for confirmation |
| `rules.draft.min_agreement` | `0.90` | agreement of held-out answers needed to draft |
| `rules.draft.min_held_out` | `5` | smallest held-out set |
| `drift.enabled` | `false` | open the drift judge (shadow) |
| `drift.documents` | `[]` | repository-relative reference documents |
| `drift.max_total_chars` | `40000` | clip for all documents together |
| `drift.model` | `null` | model name; without it nothing is called |
| `drift.rounds` | `1` | calls per park (at most 3) |
| `drift.max_budget_usd_per_call` | `0.10` | per-call ceiling |
| `drift.spend_ceiling_usd_per_day` | `null` | rolling 24-hour ceiling; without it nothing is called |
| `import.max_file_bytes` | `500000000` | per transcript file |
| `import.time_budget_s` | `60` | total per scan |
| `import.max_candidates` | `500` | scan stops here |
| `import.comments` | `false` | also read issue comments |
| `import.max_issues` | `50` | issues read when comments are on |
| `import.profile_dirs`, `import.project_dirs` | `[]` | same as `--profile`, `--project-dir` |
| `import.classifier` | `"table"` | `"table"` or `"model"` |
| `import.model` | `"haiku"` | model for the classifier |
| `import.max_budget_usd_per_call` | `0.50` | classifier per call |
| `import.max_budget_usd_per_run` | `1.0` | classifier per run |
| `import.timeout_s` | `60` | classifier per call |

Related keys outside the block: `ai_filed.triage.enabled` (gate holds), `ai_filed.migration.*` (the label migration),
`ledger.watch.interval_seconds` (the interval the dead-tick check is measured in), `action_log.enabled` (the record-done
net), `spend_approval.approvers` (owner fallback), `gates.irreversible_actions` and `gates.on_block` (hard-stop legacy aliases).

## Verbs and where each lives

All scripts are in `skills/sigma-loop/scripts/` unless noted.

| Verb | Script | What it does |
|---|---|---|
| declare a kind | `loop.py record <sdlc_dir> <goal> parked "<text>" --qkind <kind>` | adds the machine line to the park comment |
| log an action | `loop.py log ... --kind action\|permission_use --action "<text>"` | feeds the record-done net |
| answer a park | `unpark.py list`, `brief`, `resolve ... --decision keep-parked\|unpark` | records the decision (skill: `/sigma-unpark`) |
| approve or return | `promote.py list`, `apply`, `demote` | the approval gesture (skill: `/sigma-promote`) |
| migrate the old label | `promote.py migrate-confirmation` | see [above](#migrating-the-old-confirmation-label) |
| draft and approve a rule | `rule_draft.py list`, `draft`, `approve` | see [Rules](#rules-drafting-and-approval) |
| import | `decision_import.py scan`, `plan`, `apply` | see [Importing](#importing-past-decisions) |
| publish records | `decision_store.py publish <sdlc_dir>` | the local commit that `records.publish` does for you |
| run the applier once | `applier.py [sdlc_dir]` | the watcher runs it each tick |
| read levels | `skills/sigma-doctor/scripts/doctor.py features [sdlc_dir]` | the `rulebook`, `autonomy levels` and `applier tick` rows |

Library modules (no command line): `rulebook.py` (rule file, seal, match), `rulebook_cfg.py` (the total config reader),
`decision_store.py` (records), `decision_record.py` (record plus comment), `autonomy.py` (levels, watch, veto,
owners), `categories.py`, `hard_stop.py` and `hard_stop_guard.py`, `drift_judge.py` and `drift_shadow.py`,
`qkind.py`, `gate_hold.py`, `ledger_pointer.py`.

## What the doctor shows

`/sigma-doctor` prints three rows in its feature dashboard (`doctor.py features`):

- **`rulebook`**: `off (default)`, `on: records, autonomy, ...`, or `closed (<reason naming the key>)`. It appends warnings
  when records are on in a public repository, when the record folder is above `max_files_warn`, when records are
  untracked for over a day, the ledger pointer counts, `drift judge paused: daily ceiling reached at HH:MM`, and a note
  if the retired `decision_rubric` key is still present.
- **`autonomy levels`**: one clause per kind and area with the level, the answers and comparisons, what the next level
  needs, `(paused)`, `(N imported)`, and the category in force when a categories file exists.
- **`applier tick`**: `off (default)`, `on: last tick <time>`, or a WARNING that the tick looks dead.

Separate check rows count leftover legacy confirmation labels and name `promote.py migrate-confirmation`.

## Failure modes and recovery

| What went wrong | Effect | Recovery |
|---|---|---|
| a wrong-typed `enabled` or block | the whole Rulebook, hard-stop net included, closes | fix the key the doctor names |
| the watcher is not running | the applier tick looks dead; parked questions stay parked | restart the watcher; nothing is lost |
| a record write fails | the comment is still posted with `(not stored)` | re-run the gesture's record by hand; check the folder is not a symbolic link |
| a label write fails | no record is written | retry the gesture |
| the index lock times out | the index rebuild is skipped | it is a rebuildable cache |
| a truncated record file | readers skip it | delete or repair the file |
| the applier applied something wrong | | reply `no` as an owner inside the window; the goal is parked again and the kind loses a level |
| an applied unpark's board card move fails | the label swap is the gate; the ask is marked applied with `board_failed` | move the card by hand |
| the classifier or drift call times out | nothing is stored, the call is charged, the next run retries | none |
| the daily drift ceiling is reached | verdicts are `not_judged` (`ceiling_reached`) until the window rolls | raise the ceiling or wait |
| an unreadable action in the hard-stop net | treated as blocked | read the log line it prints |
| an approved rule edited by hand | it stops counting | re-propose and re-approve it |
| machines disagree on agreement | the lowest machine's level wins | converge by answering more questions |
| `require_review: "approval"` on a host that cannot answer the approval query | the goal is parked rather than merged | use an approval the REST API can see |

## Rollout advice

1. **Start with all gates closed.** That is the default and it changes nothing.
2. **Open records first.** It is the least risky gate (it writes files and one comment per gesture) and every
   other part reads it. Live with it for a while and commit the records.
3. **Import** only after reading the `plan` output. Prefer the `"table"` classifier first; it costs nothing.
4. **Autonomy next.** It adds a readout, not behaviour.
5. **Rules after you see a pair reach level 2.** Approve few rules, and read each one.
6. **Hard stops are independent** of the rest and only add stops. Open them early if you run unattended; expect some
   parks and watch the first ones.
7. **Drift judge only with a reference document set you trust** and a small daily ceiling.
8. **The applier last, if ever.** Keep `window_minutes` at a day, `max_auto_applied_per_day` at 3, `allow_silent` empty.
   Stay at level 3 (act and tell) before you consider any silent kind.

Turn off any part by setting its `enabled` to `false`; the files it wrote stay in the repository and are read again
if you reopen it.
