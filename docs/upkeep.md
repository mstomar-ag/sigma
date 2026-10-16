# Upkeep: keeping unit branches current

Upkeep keeps long-lived unit branches (`feature/<name>`) rebased onto their base branch, so that the work in a
unit does not drift away from the code it will land on. It is opt-in, it is off until you turn it on, and with
it off nothing in this document happens. Back to the [README](../README.md).

This page is the introduction and the operating guide. The exact rules for units, the registry and the older
pick-time rebase are in [branching-model.md](branching-model.md) (sections 13c to 13e hold the backup and
scheduler reference this page summarises). The conflict levels and the landing audit are in
[upkeep-part-b.md](upkeep-part-b.md). The setting keys are listed in the [configuration reference](configuration.md).

## What it is for

A unit is a group of goals that share one long-lived branch. Goals reach the unit branch through `sdlc/*` goal
branches and pull requests. While that branch lives, its base keeps moving. If nobody brings the unit forward,
the gap grows, and the eventual merge back is one large conflict.

Upkeep does one thing about that: it replays the unit branch onto the current base, pushes the result with a
lease, and keeps the tip it replaced as a backup. It does this for every unit that is behind its base, at most
about twice a day per machine, in the background.

It does not do the following:

- It never deletes a `feature/*` branch. The only deletion it performs is pruning old backup refs, and only
  refs that match the backup namespace exactly.
- It never commits to a unit branch itself. It rewrites the branch by replaying existing commits.
- It does not land a unit unless a person asks for that (see [Landing a unit](#landing-a-unit)).
- It does not run a language model to resolve a conflict unless `upkeep.conflicts.resolve` is `agent` and both spend caps are set. See [Conflicts](#conflicts).

## The two ways a unit gets brought forward

1. **At pick time.** When a goal that declares a unit is picked, the pick-time pass brings that unit's branch
   (and the goal's own branch) up to date. If another pass holds the unit's rebase lock, the pick does not wait: the
   new goal is cut, the unit tip is re-checked right after the cut, and the still-empty goal branch is moved onto
   the new tip if it changed. The pick line says so, and the goal's recorded cut tip makes its later rebase replay
   only its own commits. This is the older mechanism. `work.rebase_upkeep` (on by default;
   `"off"` turns it off) governs it.
2. **On a window.** With the scheduler, one bounded sweep brings every unit that is behind its base up to
   date, once per window. This is the subject of the next sections.

Both use the same per-unit engine, so a backup, a lease, the direct-commit refusal and conflict handling behave
the same in either.

## How the window works

The scheduler no longer polls each unit. It works on a window, `upkeep.window_hours` (default 12).

- **What opens the window.** The window is open when this machine has no completed sweep on record, or when
  `window_hours` have passed since the last completed sweep ended.
- **Who starts the sweep.** Whichever of these comes first after the window opens:
  - the first session start on the machine (`upkeep_session.py start`, run by the session-start hook; it
    starts a detached sweep and returns at once, it never waits for it);
  - the watcher's next tick (the backstop, for a machine where no session starts);
  - a merge made through `work.py merge` on the machine that merged, which asks the same question once.
- **What a closed window costs.** One small file read (`.sdlc/state/upkeep/window.json`) and some arithmetic.
  No git call, no GitHub call, no unit listing.
- **The per-unit record.** `.sdlc/state/upkeep/units/<unit>.json` holds the last attempt and last success times, the unit
  and base tips after the last success, the last outcome, why the pass started, and the consecutive-failure count. The
  engine writes it at its single exit, under the unit's lock, on every pass (pick, CLI verb, job, sweep); a closed gate
  writes nothing. Delete the file and the unit reads as never attempted.
- **What the sweep does.** It takes the job lock, lists the units admitted by `upkeep.units.include` and
  `upkeep.units.exclude` (an exclude always wins), and then runs the per-unit engine for each unit, one at a
  time. A unit already at its base costs one bounded read and nothing else, which is how a user whose window
  opens right after a colleague's sweep changes nothing.
  The units come from the registry (`.sdlc/features/`) alone: a remote `feature/*` branch with no registry record
  is not maintained, and the job record (`units_listed`, `unregistered`, `notes`) and the doctor name it, even when
  the repository has no registry at all. The branching model's registry section says how a unit gets a record.
- **Failures do not spread.** A unit that is refused, parked or failed does not stop the others.
- **The time cap.** All units share one total wall-clock cap: `upkeep.verify.timeout_minutes` (default 60)
  plus a fixed 15-minute margin. A unit that the cap, `state/upkeep.stop` or `state/watch.stop` prevented from
  starting is listed as **unreached** in the job record and the stamp, and waits for the next window.
- **One sweep at a time.** If another sweep holds the job lock, the new one backs off and records `busy`.
  That is a refusal, not a failure.
- **Where it runs.** Detached, in its own session, with only an allowlisted environment (see the
  [reference](branching-model.md#13d-the-upkeep-scheduler-off-by-default-three-opt-ins)). Windows is refused;
  the status file says so once.

### Completed and attempted stamps

The window stamp holds two times, and the difference matters:

| Stamp | Written when | Effect |
|---|---|---|
| completed (`last_at`) | a sweep ran to the end, even if some units were refused, parked or left unreached | closes the window for `window_hours` |
| attempted (`attempt_at`) | a sweep was started, or was cut short before doing useful work: killed, cut off before its first unit, found busy, failed to spawn, or every unit failed | holds off a re-trigger for `upkeep.retry_minutes` (default 60) only; never closes the window |

So a sweep that dies does not cost you a day: the next session start or watcher tick after
`retry_minutes` starts it again. A sweep in which every unit failed counts as an attempt for the same
reason.

Two more rules protect the stamp:

- A missing or unreadable stamp reads as "window open".
- A completed time dated in the future (beyond a five-minute skew margin) counts as no completed sweep, so a
  clock set far ahead cannot silence upkeep. It is not copied into an attempt stamp either, and a recent
  attempt still holds off `retry_minutes`.

## Turning it on

Three opt-ins must all be present. Any other spelling of a value (the text `true`, the number `1`) leaves it
closed.

1. In `.sdlc/config.json`, set `upkeep.enabled` to the JSON boolean `true`. The scaffold writes the block with
   `enabled` false; a repository adopted earlier copies the block in from the config template.
2. Set `ledger.enabled` to exactly `true`. The watcher that runs the tick is the ledger watcher. Remove a leftover `.sdlc/state/watch.stop` first, or the watcher exits at once.
3. Set the environment variable `SIGMA_UPKEEP_JOB` to exactly `1` in the environment of the process that
   starts the sweep, on **one** machine. Opt in one machine for any given set of units: two opted-in machines
   would both start passes. The lease makes the second push refuse, but each wastes a fetch and a replay. To
   split units between machines on purpose, use `upkeep.units.include` and `exclude`.

Then set `work.base` to your default branch. With `work.base` empty every pass ends `no-base` and does
nothing; the doctor says so.

Before you rely on it, run one pass by hand on a unit you can afford to rewrite:

```
python3 skills/sigma-loop/scripts/feature_rebase.py show .sdlc <unit>      # reports, moves nothing
python3 skills/sigma-loop/scripts/feature_rebase.py upkeep .sdlc <unit>    # one pass
```

### The base must be the default branch

Upkeep refuses a unit branch as `work.base` and reports "base is a unit branch; upkeep needs the default
branch". The same applies when it is spelled with the remote (`origin/feature/x`) or as a full ref. If a unit
branch really is your integration branch, name it in `work.integration_base` (same spelling rules) and upkeep
accepts it. Undeclared unit bases are still refused.

## Backups

When upkeep is enabled, every time a pass force-pushes a unit branch it also keeps the old tip as
`refs/sigma/backup/<unit>/<UTC stamp>`, in the same `git push --atomic` as the leased update. Both land or
neither does. A unit name over 220 bytes is refused before the replay (`name-too-long`), because the ref would
pass 255 bytes. A pure fast-forward replaces nothing, so it keeps the lease but writes no backup ref.

Two commands manage backups. While the upkeep gate is closed they do nothing and exit 3.

```
python3 skills/sigma-loop/scripts/feature_rebase.py restore .sdlc <unit> --list
python3 skills/sigma-loop/scripts/feature_rebase.py restore .sdlc <unit> <stamp> --expect <tip>
python3 skills/sigma-loop/scripts/feature_rebase.py prune .sdlc --dry-run
python3 skills/sigma-loop/scripts/feature_rebase.py prune .sdlc
```

- **Restore** puts the branch back at the named backup. You state `--expect`, the tip the branch has now
  (`--list` prints it), and the push is leased on that tip: if the branch moved, nothing changes. The tip
  being replaced is kept as a new backup.
- **A restore is not sticky.** The next pick or sweep brings the branch forward again. Set
  `work.rebase_upkeep` to `off` first if you want the restored branch to stay.
- **Prune** removes backups older than `upkeep.backup.keep_days` (default 14) while keeping the newest
  `upkeep.backup.keep_last` (default 5) of each unit whatever their age. Age comes from the stamp in the name,
  so it follows the clock of the machine that made the backup. At most 200 are removed per run. It selects by
  the exact prefix and skips any candidate that has a lookalike ref, reported as `skipped_ambiguous`.
- **The sweep prunes by itself.** Each rewrite adds one ref; once per sweep the job runs this prune (at most 200 deletes, a failure is retried next sweep, never fails the sweep), and `/sigma-doctor` shows the backup count and last prune. The manual verb stays.
- Without the gate, or on a plain clone, restore by hand with plain git; the steps are in
  [branching-model.md](branching-model.md#13c-backups-of-a-rewritten-unit-tip-off-by-default).

## Direct commits are refused

The rule is: nobody commits directly to a feature branch. Before it force-pushes `feature/<name>`, the pass
asks of every commit on that branch whether a pull request accounts for it. If one is not accounted for, the
pass refuses with `direct-commits`, leaves the branch exactly as it was, and files the finding as tracked work.
A later pass that finds no unacknowledged direct commits closes that finding.

If a commit did arrive through a pull request but its subject carries no trace of it, acknowledge it:

```
python3 skills/sigma-loop/scripts/feature_rebase.py show .sdlc <unit>          # lists the direct commits
python3 skills/sigma-loop/scripts/feature_rebase.py ack .sdlc <unit> <sha>...
python3 skills/sigma-loop/scripts/feature_rebase.py ack .sdlc <unit> --all
```

`ack --all` acknowledges only the commits the filed issue listed. The acknowledgement is keyed by patch id, so
it survives the rewrite it unblocks (a merge or empty commit, which has no patch id, is keyed by author, author date, message and parent count), and it lives under `.sdlc/features/`, so it travels by pull request to
every teammate. Sigma only detects a violation; only host branch protection can prevent one, and ordinary
protection on `feature/*` blocks upkeep's own force push, so grant upkeep's actor a bypass.

## Conflicts

`upkeep.conflicts.resolve` decides how far a conflict may be resolved:

| Value | What happens |
|---|---|
| `off` (default) | the conflict is parked: the unit branch is left untouched, nothing is pushed, one issue records why |
| `mechanical` | a conflict confined to the changelog is resolved by a heading-aware union, proved, and pushed once through the atomic backup push; anything else parks |
| `agent` | a capped headless resolver plus an independent reviewer (#1516); it behaves as `off` until `upkeep.conflicts.agent.max_budget_usd_per_unit` and `spend_ceiling_usd_per_day` are both set; see [Upkeep part B](upkeep-part-b.md) |

Level `mechanical` needs a verify command unless `upkeep.conflicts.mechanical_without_verify` is `true`.
A parked conflict is one finding: before filing, the pass looks for an open issue with the same conflict id and
comments on it at most once a day instead of filing again. Upkeep findings are filed parked with a declared
question kind and the lowest AI bucket priority (the `unclear` bucket, P4 by default), never armed as goals (with triage off they keep the legacy
confirmation label instead). More detail is in [upkeep-part-b.md](upkeep-part-b.md).

A refused replay that would delete content the branch has (a `would-drop`) is a blocked pass, not a clean one.
Details are in branching-model.md section 3b.

## The lease

Every push of a unit branch is a `--force-with-lease` on the exact tip the pass read. If the remote moved
since, the push is refused and recorded as `lease-refused`; the other writer keeps the branch, and the next
pass tries again. The attended rebase door takes the same unit lock and the same lease. The per-unit rebase lock
means two passes cannot rewrite the same unit at once.

## Landing a unit

Landing a whole unit onto its base is a separate, guarded gesture, behind the same gate, and never automatic:

```
python3 skills/sigma-loop/scripts/feature_land.py land <unit> --user-requested <unit>             # rehearsal (default)
python3 skills/sigma-loop/scripts/feature_land.py land <unit> --user-requested <unit> --merge     # merge the verified head
```

It runs a pass, verifies the unit tip in a scratch worktree, opens or finds the landing pull request, and with
`--merge` merges only the exact head it verified. It refuses, with a named reason, when verify is not
configured, a merge queue guards the base, the repository slug does not match the remote, an unattended
request has no valid approval for the current tip, or the other half of a cross-repository unit has not landed.
A replay of a landed unit reports `already-landed`. A landing record is kept per unit until it is settled by a
read-back; the doctor names a pending one and the exact re-run gesture. When the tree has upkeep off but the
base has it on, landing prints one warning naming the disagreement.

## What you see

- **Session start.** One line while upkeep is enabled, read from the stamp only:
  `upkeep: last sweep 3h05m ago, next window opens in 8h55m`, or `upkeep: no completed sweep yet on this
  machine; the window is open`.
- **`/sigma-doctor`.** Rows appear only while the block is on or invalid:
  - `upkeep settings` (an invalid block, naming the key);
  - `upkeep scheduler readiness` (which opt-ins are missing in the doctor's own shell; it does not read the
    watcher's environment);
  - `upkeep integration branch` (`work.base` empty);
  - `upkeep scheduler liveness` (age of the last tick; older than the stale limit means the watcher may be down);
  - `upkeep window` (window hours, last sweep, units left unreached; fails with "older than 2 windows: no sweep
    is running" when no completed sweep has happened for more than two windows);
  - `upkeep last start` (started by tick, trigger or pick, and when);
  - `upkeep job` (running with heartbeat age, HUNG when the lock is held but the heartbeat is stale, or the
    last outcome);
  - a pending-landing row, and the resolver rows when `conflicts.resolve` is `agent`.
- **The ledger.** The watcher writes the sweep outcome as one unaddressed ledger note under the key `window`.

## Failure modes and recovery

| What happened | What it looks like | Recovery |
|---|---|---|
| the sweep was killed or the machine rebooted | attempt stamp, job record not `done` | automatic: re-triggered after `retry_minutes` |
| the sweep hit its time cap | units listed as unreached | automatic: they wait for the next window |
| every unit failed | attempt stamp; window stays open | automatic retry after `retry_minutes`; read the job record for the cause |
| another sweep is running | outcome `busy` | none needed |
| a job hangs | doctor row `upkeep job` says HUNG | create `.sdlc/state/upkeep.stop` or `watch.stop`; Sigma never deletes either |
| the watcher is down | `upkeep scheduler liveness` fails; session start still starts a sweep | restart the watcher; after updating the plugin stop old watchers with `touch .sdlc/state/watch.stop` |
| the remote moved during a push | `lease-refused` | automatic: next pass |
| direct commits on the branch | `direct-commits`, a filed finding | review, then `ack`, or move the work onto a goal branch |
| a conflict | `parked`, one issue | resolve by hand with the `sigma-rebase` skill, or set `conflicts.resolve` to `mechanical` for changelog conflicts |
| a bad rewrite | the unit looks wrong after a pass | `restore` from the backup (see above) |
| a bad stamp (clock far ahead) | window never seems to open | a future-dated completed time already reads as none; or delete `.sdlc/state/upkeep/window.json` |

To stop upkeep entirely: set `upkeep.enabled` to `false` (the plain push returns), or create
`.sdlc/state/upkeep.stop` for a running job.

When an idle goal branch cannot be replayed onto the rebased unit branch because of a real content conflict, the filed finding says whether the pass caused it, measured by read-only `git merge-tree` probes of the goal tip: it also conflicts with the unit's previous tip (pre-existing, with the backup ref and the conflicted paths), or it merges cleanly with the previous tip AND conflicts with the new tip (introduced by this pass, with the paths from the new-tip probe, capped at 20; the only wording that says it was brought forward). Every other case (a replay outcome that is not a conflict such as not started, refused, gated or deferred; a clean or erroring new-tip probe; old git; a missing tip) makes no causal claim and keeps the original finding body unchanged. Paths taken from branches are sanitised (backticks, newlines and control characters replaced) before they enter the body. The probe is a single three-way merge while the replay goes commit by commit, so on rare histories (a change added and then reverted in the goal's commits) they can disagree, and then the finding says the cause is unknown. Probes share a per-pass time budget of three guard timeouts; once it is spent later goals keep the original body, and the rebase's own reported text is sanitised and capped the same way.

## Scale and limits

- One sweep per window per machine: at most `24 / window_hours` a day, at the default 2.
- A sweep is serial, so N units that each need a rebase take the sum of their passes, bounded by the one total
  cap. The tail is reported as unreached rather than run past the cap.
- A closed window is a single file read.
- The scheduler has no cross-machine lock beyond the per-unit lock and the lease, hence one opted-in machine
  per set of units.
- It has been run end to end on a bare local remote and on one private throwaway repository on a hosting
  service. It has not been measured at tens of units, against branch rulesets with bypass actors, or with a
  real model resolving conflicts. See `docs/launch/evidence/upkeep-hosted-run.md`.

## Configuration reference

All keys live under `upkeep` in `.sdlc/config.json`. An unknown key, an out-of-range or wrong-typed value, a
section that is not an object, or a contradictory pair closes the **whole** block, and `/sigma-doctor` names the
key path. (`work.rebase_upkeep` has the opposite typo rule: an unrecognised value reads as on.)

| Key | Default | Range | What it does |
|---|---|---|---|
| `enabled` | `false` | boolean | project opt-in; only the boolean `true` opens it |
| `window_hours` | `12` | whole number 1 to 8760 | hours since the last completed sweep before the next one may start |
| `retry_minutes` | `60` | whole number 1 to 10080 | hold-off after an attempt that did not complete; never closes the window |
| `units.include` | `["*"]` | list of case-folded globs, at least one | units the sweep may pick |
| `units.exclude` | `[]` | list | units it never picks; wins over include |
| `verify.timeout_minutes` | `60` | 1 to 1440 | with a 15-minute margin, the sweep's total wall-clock cap |
| `verify.clean_rebase` | `false` | boolean | validated; not acted on |
| `backup.keep_days` | `14` | 1 to 3650 | prune removes only backups older than this |
| `backup.keep_last` | `5` | 1 to 1000 | newest backups of each unit kept whatever their age |
| `backup.former_prefixes` | `[]` | list of `refs/<name>/backup/` | namespaces an earlier product wrote, pruned the same way |
| `conflicts.resolve` | `"off"` | `off`, `mechanical`, `agent` | how far a unit conflict may be resolved; `agent` is not available yet |
| `conflicts.mechanical_without_verify` | `false` | boolean | lets the mechanical level run with no verify command |
| `triggers.*`, `auto.*` | see branching-model.md 13e | | still read and validated for one release; they no longer schedule anything |

Related keys outside the block: `work.base` (the default branch), `work.integration_base` (a unit branch that
is also your integration branch), `work.rebase_upkeep` (`"off"` turns the pick-time pass and the project door
off), `ledger.enabled`, and the environment variable `SIGMA_UPKEEP_JOB`. Files under `.sdlc/state/`:
`upkeep/window.json` (the stamp), `upkeep/scheduler.json` (tick status), `upkeep/job.json` (last job record),
`upkeep.stop` and `watch.stop` (stop levers).
