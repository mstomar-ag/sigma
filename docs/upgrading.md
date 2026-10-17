# Upgrading a repository adopted under the plugin's previous name

Sigma was published under a different name before its 1.0.0 release. A repository adopted under
that name (any 1.4.x release) carries the old name in its state. Sigma reads that state as it is, so
the files need no migration to be understood. One thing is not carried over, because it is deliberately
kept out of the repository: [each checkout grants verify trust once](#verify-commands-need-a-one-time-per-checkout-trust). Fresh installs also differ in a few defaults: [What changes when you switch](#what-changes-when-you-switch) lists each one with its cost.
When you want the files themselves to stop carrying the old name, run the one-shot migration.

The old name is not spelled out in this document. `migrate.py` prints it at the top of every run,
and the table below uses `<old>` for the name and `<OLD>_` for its environment-variable prefix.

## Updating the plugin between releases

An ordinary plugin update replaces skills, docs, hooks and scripts. It never rewrites your `.sdlc/config.json`, so a
block added in a later release (`rulebook`, `upkeep`) does not appear in a repository adopted earlier. Copy the block
from `skills/sigma-init/templates/config.json.tmpl` in the installed plugin, with every `enabled` left `false`, and
open one gate at a time. Restart the session after updating, and stop any running watcher so the next trigger starts it
on the new build (`touch .sdlc/state/watch.stop`). What each release changes by default is listed in the
[CHANGELOG](../CHANGELOG.md); for 1.0.16 see [what's new in 1.0.16](whats-new-1.0.16.md). The two optional features are
documented in [the Decision Rulebook](decision-rulebook.md) and [Upkeep](upkeep.md).

## From the pre-launch name

Before launch the plugin and its marketplace were both named `sigma`, so the install id was `sigma@sigma`.
Sigma Loop's plugin and marketplace are named `sigmaloop` (install id `sigmaloop@sigmaloop`). Claude Code and
Codex key updates on that id, so **an install recorded under the old id keeps working and silently stops
receiving updates**. Nothing removes it for you: `/sigma-doctor` prints the commands and runs none of them.

**What is kept.** `.sdlc/` data is kept as is: goals, plans, state, the action log, labels and the board are not
touched by any of these steps. Only the plugin install changes.

**Claude Code.** Run each line yourself, in this order. Add `--scope project` or `--scope local` (and run it from that project's
directory) for an install recorded under that scope; `/sigma-doctor` lists every recorded scope. The reinstall is at user
scope. If `sigmaloop@sigmaloop` is already installed too, run only the uninstall (and the marketplace removal): adding the
old source again would repoint the working install at it.

```
claude plugin uninstall sigma@sigma
claude plugin marketplace remove sigma          # only if no other plugin you use comes from that marketplace
claude plugin marketplace add <the repository your old install came from>
claude plugin install sigmaloop@sigmaloop
```

`/sigma-doctor` prints these lines for your install, with the repository your install recorded filled in (a private
copy of the repository carries the plugin under its new name too). A fresh install from the public repository uses
`claude plugin marketplace add https://github.com/Agrim-Intelligence/sigmaloop` instead. Then restart the session, so
the version in use is resolved afresh. With a marketplace shared by other plugins, the uninstall and marketplace-removal steps were run
on 2026-10-14 in a scratch profile against a two-plugin directory marketplace (a stand-in, not a real old release): uninstalling
one plugin left the other installed. The marketplace removal is the step to skip: `claude plugin marketplace
remove` on that marketplace also uninstalled the second plugin ("Also uninstalled 1 plugin from this marketplace"), and the CLI added that the removal "also deletes their saved
options, secrets and data where it can", so the guard above is not a courtesy. The sequence has not been run against a real pre-launch install.

**Codex.**

```
codex plugin remove sigma@sigma
codex plugin marketplace add <the repository your old install came from>
codex plugin add sigmaloop@sigmaloop
```

The `codex plugin remove` verb is the one codex-cli 0.154 lists; confirm it with `codex plugin --help` on your host
before running it (the install lines were last run before the rename, not for `sigmaloop`). The other way to remove
a Codex plugin is to delete its table from `config.toml` and restart Codex.

**Rule files and the Codex block.** Cursor's `.cursor/rules/*.mdc` files and the Codex block in `AGENTS.md` carry
the skill names as they were when they were scaffolded: re-run `/sigma-init` (with `--cursor` or `--codex`) after
deleting the two Cursor rule files, as the changelog's migration notes say.

**An install under the old id cannot warn you.** It runs the OLD doctor, which asks the marketplace for a plugin
named `sigma`, finds none after the rename, and shows no row at all. The row described above reaches only an install
that already runs the new code (a checkout, or a fresh install). The owner announces the rename; this document and
the changelog are where the steps live.

## Verify commands need a one-time per-checkout trust

This applies to **every repository whose committed `.sdlc/config.json` already has a `verify.command`** (a repository
migrated from the previous plugin, or one adopted by a teammate) and to **every new clone of it**.

`loop.py verify` runs the verify command through a shell, and that command comes from a file in the repository, so Sigma
refuses it until the person at this checkout says it may run. That decision is stored in this checkout's Git-local
configuration (`.git/config`), which is not committed and is not copied by `git clone`. It is therefore not inherited
from the repository: **each checkout grants it once.** Without it the first `loop.py verify` exits 2 with `REFUSED:
repository-configured shell command requires explicit operator trust`, and with `verify.enforce` on, `record done`
needs a green verify, so every goal stops until trust is granted.

Inspect the command in `.sdlc/config.json` (`verify.command`), and if you trust the project, run this once in the
checkout:

```
git -C <project> config --local sigma.allowRepositoryShellCommands true
```

It covers the checkout's linked goal worktrees. Check it with `python3 <installed-sigma>/skills/sigma-loop/scripts/loop.py
verify .sdlc <goal>`: it runs the command instead of refusing.

What tells you it is missing:

- `/sigma-init`, re-run on the repository, prints a `[trust] verify` line with the command and this gesture instead
  of `[ok] verify`. It does not set the trust for you, and no init flag answers it: run the gesture yourself.
- `/sigma-doctor` shows a `verify command trusted in this checkout` row, with the same gesture, while a command is
  configured and trust is absent.

`/sigma-init`'s explicit `verify_detect.py confirm` and `set` gestures record the same trust for the command you confirm,
which is why a fresh adoption never meets the refusal. The policy itself (Git-local, default no) is unchanged; see
[the threat model](threat-model.md#repository-configured-shell-commands).

## What changes when you switch

A repository that carries its own `.sdlc/config.json` keeps every value in it, so a carried-over repository does not change
behaviour in the rows below that name a key. The differences reach a **fresh** adoption (`/sigma-init` writes the Sigma template)
or a config that never set the key. Each row gives the previous plugin's behaviour, Sigma's, what it costs where that was
measured, and how to get the previous behaviour back.

| Key or behaviour | Previous plugin | Sigma | Cost | Restore |
|---|---|---|---|---|
| `work.require_review` | `off`: the PR is merged without a loop review pass | `changes`: after the PR opens, one fresh author-blind review runs; each `block` adds a fix, a verify and a re-review, capped by `work.max_review_cycles` (3) | Tokens: [measured below](#what-the-review-pass-costs) | Set `work.require_review` to `off` in `.sdlc/config.json` |
| `action_log.enabled` | `false`: no action log | `true`: a local trace in `.sdlc/state/log/`, git-ignored, bounded by retention | No model tokens; local disk only | Set `action_log.enabled` to `false` |
| `work.enabled` | `null`: reads as off, unless setup resolved it to on during a GitHub adoption | `true`: one worktree, branch and PR per goal from the first run | No model tokens; one worktree on disk per goal | Set `work.enabled` to `false` to keep the loop writing in your checkout |
| `verify.command` | Runs when configured | Runs only after this checkout is trusted, once per checkout; see [the trust section](#verify-commands-need-a-one-time-per-checkout-trust) | None | Grant the trust as that section says |
| `discovery.reconcile.mode` | A missing key reads `on`: stale `sdlc:in-progress` labels on closed issues are cleaned up | The template writes `off`, and a missing key reads `off` today. The owner decided on 2026-10-01 that a missing key reads `on`; that change is pending and not in this release | Label writes on closed issues only; about 13 `gh` calls per sweep, one sweep per `ttl_minutes` (default 60), so roughly 13 an hour while on (`loop.py` census; an earlier figure of about 21 came from the owner's decision on #393) (rate-limited, not billed) | The missing-key default is what differs, so there is no value to restore: set `discovery.reconcile.mode` to `on` explicitly |
| Git hooks | Adoption leaves git hooks alone | Adoption never writes `core.hooksPath`. Earlier Sigma releases wrote it, pointing at the old hooks path, a directory nothing creates, so git ran no hooks. `/sigma-init` unsets a value that matches it when no worktree of the repository has that directory, and prints one line saying so; a `core.hooksPath` you set yourself is kept | None | Nothing to restore. To clear a stale value yourself: `git config --local --unset core.hooksPath`; `/sigma-doctor` shows a failing row naming it, and [the uninstall guide](uninstall.md) has the same step |
| Setup wizard and decision gate outside adopted repositories | The session-start wizard prompts in any repository | Silent until the repository has `.sdlc/`; the decision-gate hook also stays inert without `.sdlc/config.json` | None | Run `/sigma-init` in the repository |

### What the review pass costs

`work.require_review: changes` is the one default that changes your bill. The owner decided to keep it (decision D-2) and to state
its cost here. Two measurements exist, and neither is a measured per-goal total:

- **One review pass: 139,235 tokens** (about 214 seconds), measured once (n=1) on the previous plugin's own goals, against a mean
  goal of **168,174 tokens** over its ten most recent goals. One pass was therefore about 83% of a mean goal's whole cost.
- **Passes per goal:** this repository's action log holds 130 review verdicts over 73 goals (82 pass, 48 block), so it averages
  1.78 passes per goal.

Multiplying the two gives about 248,000 tokens of review per goal. That is an **estimate** from an n=1 pass and a different
repository's mean, not a measurement. The cycle count also reflects the loop's own habits, not only this switch. A per-PR cost
receipt is opt-in and has not landed yet, so today the bill shows up in your provider usage, not on the PR.

## What Sigma reads without any migration

| State the old release wrote | Where | What Sigma does |
|---|---|---|
| `<old>/features@1`, `<old>/landing@1`, `<old>/withheld@1`, `<old>/propagation@1` schema ids | `.sdlc/features/`, `.sdlc/state/landing/`, `.sdlc/state/withheld/`, `.sdlc/state/propagation/` | Reads them as the Sigma ids. The next write uses the Sigma id. A different version (for example `@2`) is refused, exactly as Sigma's own `@2` is. |
| `<!-- <old>:begin managed … -->` / `<!-- <old>:end managed -->` | `.sdlc/features/<unit>.md` | Manages the block and respells its markers on the next sync. If the file also has a Sigma block, Sigma manages only its own block and leaves the other one alone. |
| `<!-- <old>:codex:start/end -->` | `AGENTS.md` | `/sigma-init --codex` replaces the old block in place. |
| `<OLD>_*` environment variables | your shell or launcher | Reads each operator setting under the old prefix when the `SIGMA_*` name is unset or empty. **`SIGMA_*` wins when both are set.** `SIGMA_RUN_ID` and `SIGMA_AUTOWATCH_HOP` are Sigma's own hand-off variables, so they are never read under the old prefix. |
| `*_env` config values naming `<OLD>_*` | `.sdlc/config.json` | Reads the `SIGMA_*` spelling first, then the named one. |
| `drift_watch.channels.<old>` | `.sdlc/config.json` | Reads it when `channels.sigma` is unset or blank. |
| `<old>:approve` / `block` / `unblock`, `keep-parked`, `dismissed-finding`, decomposition and design markers, Q&A blocks, flag watermarks | GitHub issue and PR bodies and comments | Recognises them. A `<old>:block` comment on an open PR still blocks it. |

The single reader for all of these is `skills/sigma-loop/scripts/legacy.py`.

## The one-shot migration

```
python3 skills/sigma-doctor/scripts/migrate.py .sdlc            # dry run: lists every change, writes nothing
python3 skills/sigma-doctor/scripts/migrate.py .sdlc --apply    # writes, then prints what it changed
```

The migration rewrites only these:
- the schema ids above;
- the feature-doc markers;
- the Codex block, which it regenerates with Sigma's text;
- in `config.json`, `*_env` values (to `SIGMA_*`) and the `drift_watch.channels` key.

Each rewrite swaps exact text in place. Line endings, key order and comments all survive. Each result
is checked by reading it back: JSON must parse to the original with only the listed values changed,
and a feature doc must keep the same body and digest. A file that fails that check, or that the
migration does not understand, is refused: it is left untouched and listed, and the command exits 2.

Running it again is safe in these states, and does exactly this:
- **nothing changed since the last `--apply`**: it prints `nothing to migrate`;
- **a file was refused as changed after it was read** (or a unit record kept `index.json` back):
  rerun it; it converts what is left, unit records first and `index.json` last;
- **the old plugin wrote a unit record after the conversion** (a goal it started, an owner claim,
  `set-priority`): that record is **refused, not converted**, and the refusal names
  `feature_sync.py repair`. Rewriting only its schema id would make it replace the `index.json`
  entry, so Sigma would serve the old plugin's near-empty record instead of the unit (see below).
  Run `repair`, not `migrate`, for it.

Each write is atomic. `--apply` refuses to run while this
`.sdlc`'s watcher is running, because that watcher may belong to the old plugin and be writing the
old spellings. Stop it first. It also waits while the old plugin can still run on this repository:
see [Switching over from the previous plugin](#switching-over-from-the-previous-plugin).

Some things are **left as is and listed**, on purpose:
- history: ledger entries and timing sessions;
- schema ids of kinds or versions Sigma does not know;
- a feature doc or `AGENTS.md` that carries both spellings, since the other block belongs to a
  teammate still on the old plugin;
- the old journal config block, because turning the journal on is an explicit opt-in that
  `/sigma-doctor` reports.

Environment variables are listed by **name only, never value**. Rename them where you set them.

## Switch the team together

The old plugin cannot read Sigma's spellings. Every file the migration changes is one the old plugin
also reads. Committed files (`config.json` and `.sdlc/features/`) reach every teammate through git.
Not migrating does not keep a mixed team safe. Sigma reads the old spellings, but on normal use it
rewrites registry files (`.sdlc/features/index.json` and the unit records under `units/`) with its
own schema id, printing a one-line `migrated legacy ... on use` notice when it does. The old plugin
cannot read those files: it reads an `index.json` in Sigma's schema as empty, and its own guard
refuses to touch a unit record in Sigma's schema (that unit is then stuck for it, not overwritten).
So either switch every machine to Sigma and migrate together, or keep the old plugin off the
branches Sigma writes to until you do.

Two things protect the registry meanwhile. Before Sigma's first write to `.sdlc/features` on a
machine where the old plugin can still run on the repository, it saves one copy of `.sdlc/features`
to `.sdlc/state/backup/features-<UTC time>/` and says so on stderr (once per repository; `state/` is
machine-local and ignored by git; a registry over 5,000 files or 64 MB is not copied, and that is
said instead). That copy is taken ONCE, ever, per repository: it is the state before Sigma's
first write, so restoring it later discards every write made since. Restore it only within the
cut-over window, and prefer the repair below.

And Sigma's registry reader never lets a record the old plugin wrote replace an entry it could not
see. The rule is structural, keyed on the two schema ids and nothing else: a unit record that still
carries the old plugin's schema id, next to an `index.json` in Sigma's schema that has an entry for
the same unit, is a post-conversion delta: the old plugin wrote it after `index.json` became
Sigma's, starting from nothing (a goal it started, the owner claim its next pick makes, `define.py
set-priority`). A partial migrate does not leave one: the old plugin writes its unit record first
and rebuilds its index only on demand, so `migrate.py --apply` converts every unit record before
`index.json`, converts the index only once none is left in the old schema (a refused or symlinked
record, or one the old plugin wrote during the run, keeps the index in the old schema, where the
record is read whole; rerun once the refusal is fixed), and puts the index back if such a record
appears just after it replaced it. Sigma reads such a record as a delta onto the `index.json` entry. The entry wins on every field it has a value for
(title, owner, parent, tracking issue, priority, `open`, each repository's branch and owner). The
record only fills blanks, its goals are added to the entry's, and a repository only it names is
added without a grant. `authorized` is never taken from that record. The reader prints the
recovery once. `feature_sync.py fold` writes the merged view, and first checks that the result
loses nothing the current `index.json` records (no goal of any unit; no field, repository or grant
of a merged unit). If anything would be lost, it refuses (exit 2, nothing written) and names the
loss and the repair:

```
python3 skills/sigma-loop/scripts/feature_sync.py repair .sdlc   # rewrites each such record in Sigma's schema: the index entry plus what the record adds
```

`migrate.py --apply` does not convert such a record: it refuses it and names `repair`, because
rewriting only the schema id would make the record replace the entry. Two Sigma steps rewrite it,
by one rule: `repair`, and a Sigma write to that unit (a pick recording a goal, an owner claim,
`set-priority`, or automatic classification reopening a closed unit). The rule is enforced where
every unit-record write passes, so no Sigma step can skip it. Neither step is lossless: a non-empty
record value that differs from the entry (an owner, a priority, a branch, a grant) is dropped, and
each one is printed; a pick also reports it as a `legacy-delta-discard` divergence, on its result
line and in the ledger for the unit's owner. When there is such a value and the record is at least
as new as `index.json`, both refuse that unit and write nothing (a pick reports a
`legacy-delta-conflict` divergence; classification attaches nothing and refuses the pick that pass):
the value is most likely an edit the old plugin made after the conversion, which Sigma never applied
and you may still want. Copy the values you want into `index.json`, or delete them from the record,
then rerun `repair`.

You can tell such records exist without reading stderr: `/sigma-doctor` shows a `legacy delta
records` row, and `status.py` adds a `legacy delta records: N (M would be refused)` segment, each
naming the `repair` command. Both are silent when there are none.

"At least as new" is file time: the registry carries no timestamp of its own, equal times count as
newer, and a `git checkout`, `clone` or `pull` stamps every file it writes with the checkout's time,
not the edit's. After one, the order only says which file git wrote last. A Sigma `feature_sync.py
fold` that changes `index.json` also makes it the newer file: after it, a delta record written
before the fold is older, so a later `repair` or pick DISCARDS its differing values (printed, and a
`legacy-delta-discard` divergence) instead of refusing. A fold that would write identical bytes
leaves `index.json` and its time untouched. So check the doctor row, and settle any delta record,
before you fold. Either answer stays safe: a refusal writes nothing, and a rewrite prints what it
drops.

Once `index.json` is in Sigma's schema, do not run the old plugin's own `feature_sync.py fold`
(the copy under its loop skill's `scripts/`, run by hand) on the repository. That is true however
the index got there: `migrate.py --apply`, or ANY Sigma `feature_sync.py fold` -- Sigma's own
fold writes the index in Sigma's schema, so it converts the index even if you never migrated. The
old fold cannot read Sigma's `index.json` and rebuilds it from its own records alone, in its own
schema -- run on a converted repository with no such records, its 1.4.25 release wrote an empty
index. If it happened, `git checkout` the committed `index.json`.

**Known edge.** Because the `index.json` entry wins, an ownership, title or priority change the old
plugin makes after the conversion is not applied while the entry already has a value for that
field. Disable the old plugin, then make the change with Sigma (or edit `index.json`).

Cross-repository propagation follows the same rule. Sigma will not overwrite a sibling repository's
registry file while that file still carries the old schema id. Run the migration in that repository
first.

## Switching over from the previous plugin

Sigma replaces the old plugin in place. You can install Sigma next to it and use every Sigma skill,
the loop and the watcher straight away; the old plugin being installed is a notice, never a
refusal. Once you trust Sigma on a repository, stop the old plugin there, migrate its state, and
uninstall the old plugin -- in that order.

**Why the order matters.** The old plugin cannot read Sigma's registry: it reads a
`.sdlc/features/index.json` carrying Sigma's schema id as empty. So if it starts a goal in a unit
that exists only in that index, it writes a new record for the unit in its own schema, starting
from nothing (no title, priority or tracking issue, `authorized` false, only its own goal; its next
pick then claims an owner on it). Sigma merges that record only as a delta onto the index entry
(above), so nothing the index knew is lost, but whatever the old plugin changes on such a unit
reaches Sigma only where the entry is blank. It also writes other state Sigma owns:

- **Withheld-findings indexes** (`.sdlc/state/withheld/`). Its `upstream._remember` treats an index
  in Sigma's schema as unreadable and then writes a fresh one over it, erasing that goal's dedup
  history and its count toward the per-goal cap of upstream issues (read in its 1.4.25 code; not
  run here).
- **Landing and propagation records.** It cannot read Sigma's, so it writes its own again:
  duplicate work and duplicate records, not loss (#319 review finding; not re-measured here).

These are why the order is: stop the old plugin on the repository **before** converting anything.

The cut-over, per machine:

1. **Install Sigma** next to the old plugin (README Quickstart). Nothing needs disabling first.
2. **Run it.** `/sigma-init`, `/sigma-loop`, `/sigma-goal` and the watcher all work. Each run
   prints one line, once:
   `sigma: notice: the plugin previously published as '<old>' is also enabled here; Sigma is
   handling this repository -- uninstall it when ready: claude plugin uninstall <old>@<marketplace>;
   the old plugin cannot read Sigma's registry; ...`.
   In a repository the old plugin adopted, init and `loop.py start` also print one
   `sigma: takeover:` line with the exact migration dry-run command. Until you migrate, Sigma reads
   the old state as it is (the table above).
3. **Stop the old plugin on this repository.** Use the exact step `coexist.py check .sdlc` or the
   migration's refusal prints:
   - Claude Code, this repository only: run `claude plugin disable <old>@<marketplace> --scope local`
     in the repository. It writes `"<old>@<marketplace>": false` under `enabledPlugins` in the
     repository's `.claude/settings.local.json`, which wins over the project and user settings on
     this machine (checked on 2026-10-14 in a scratch project: the file held exactly that one entry, and
     `claude plugin enable <old>@<marketplace> --scope local` set it back to `true`). Or uninstall it now (step 5). A machine-wide managed setting that enables it
     wins over both: ask whoever manages it.
   - Codex: set `enabled = false` in the `[plugins."<old>@<marketplace>"]` table of `config.toml`
     (`CODEX_HOME`, else `~/.codex`), or remove the table. Codex has no per-repository switch, so
     this stops it on every repository on this machine.
   - A hook registered by hand that runs the old plugin: remove that entry from the settings file
     the notice names.
   - A watcher it left running: the polite lever under "What stays impossible" below.
4. **Migrate** (see above): the dry run first, then `--apply` only when you say yes.
   `migrate.py .sdlc --apply` refuses (exit 2, nothing written, the dry run shown) while the old
   plugin can still run on this repository, and prints the exact step 3 command and the rerun.
   If you must convert with it still running, add `--replace-old-plugin`: a copy of
   `.sdlc/features` is saved under `.sdlc/state/backup/` first, and the registry reader's guard
   (above) still stands. `--apply` also refuses while any watcher is running for this `.sdlc`.
5. **Uninstall the old plugin**, with the exact command the notice printed:
   - Claude Code: `claude plugin uninstall <old>@<marketplace>` (add `--scope project` or
     `--scope local` for an install in that scope -- the notice says which). Then, optionally and
     only if nothing else you use comes from it, `claude plugin marketplace remove <marketplace>`,
     where `<marketplace>` is the part after `@` in the id the notice printed (`coexist.py check`
     prints this command with the real name; it is not always the plugin's own name).
   - Codex: remove the `[plugins."<old>@<marketplace>"]` table from `config.toml`. This is the edit
     Sigma names; Codex's own plugin commands were not available to verify here.
   - A hook registered by hand: as in step 3.

`SIGMA_ALLOW_COEXIST=1` (exactly `1`, read under the Sigma name only) silences the notice. It is
not needed for anything to run.

### What Sigma does while both are installed

The detector is `skills/sigma-loop/scripts/coexist.py`. `coexist.py check .sdlc` prints what it
found and the cut-over steps, and exits 0.

| Surface | When the old plugin is active |
|---|---|
| `/sigma-init` (`init_flow.py`, `sdlc_init.py`) | Proceeds, prints the notice, records Sigma as the owner, and offers the migration dry run in an adopted repository. |
| `loop.py start` | The same. |
| `loop.py claim` / `record` (the `/sigma-goal` path) and the automatic watcher start | Proceed; the notice at most once per run (below). |
| `watch_daemon.py` | Proceeds to the shared lock; the notice goes to `.sdlc/state/watch.log`. |
| `migrate.py` | The dry run proceeds with the notice. `--apply` waits for the old plugin to be stopped on this repository (exit 2, dry run shown, the exact disable step) unless `--replace-old-plugin` is given, which takes the `.sdlc/features` backup first. `--apply` still refuses while a watcher is live. This is the only step that waits: it is the one that converts the registry the old plugin cannot read. |
| Registry writes (`.sdlc/features`) | Proceed. The first one saves the one-time copy to `.sdlc/state/backup/features-<time>/`. |
| Registry reads, `feature_sync.py show` / `fold` | A unit record in the old plugin's schema next to a Sigma `index.json` entry is merged as a delta (the entry wins every field it has; goals added; never a grant). `fold` writes that and refuses any result that would lose something `index.json` records; `feature_sync.py repair` (and any Sigma write to that unit: a pick, a claim, `set-priority`, a classification reopen) rewrites such records in Sigma's schema, lists each record value it discards, and refuses when there is one and the record is at least as new as `index.json`; the doctor row and the status segment `legacy delta records` count them. `migrate.py --apply` converts `index.json` only once every unit record has converted (and puts it back if one appears during the run), and refuses to convert such a delta record, naming `repair`. |
| `/sigma-doctor` | A `coexistence: WARN` row naming the uninstall command. Never a failure. |
| `status.py` | The notice on stderr. The status line still prints. |
| Session-start hook (Claude Code) | Adds the one notice line to the session, then runs its other checks as usual. Read-only, so it says the same thing every time. It is an accelerator only: every behaviour above is in Sigma's Python, on every host, including Cursor, which has no hooks. |

### A fold by the old plugin (#514)

**What happens.** The old plugin's own `feature_sync.py fold <sdlc_dir>` cannot read a registry in Sigma's
schema. Measured against its installed scripts, run only from a temporary copy: it says the document declares
a schema other than its own so none of it was read, **exits 0**, and writes `.sdlc/features/index.json` anyway,
an empty registry under its own schema id; Sigma then reads no units. With a unit record of its own beside the
sheet, its fold writes exactly that unit and the Sigma units are lost the same way. Sigma cannot stop another
program's write, and this is a mitigation, not a sheet that fold leaves alone.

**What Sigma does.**

- It keeps a copy of the sheet at `.sdlc/state/backup/index-sigma.json`, a path the old plugin never reads or
  writes: the bytes of the last Sigma-schema `index.json` this checkout wrote (`write_index`) or saw (every
  `loop.py` verb that runs the coexistence notice looks at the sheet; a sheet that arrives by pull is
  copied then; a fresh clone has no `state/` yet, so it is copied only by a later verb, once `state/` exists). The copy is the union of every unit it has held, the latest sheet's entry winning a shared name: a sheet
  holding fewer units (an older branch, a pull, or a truncated one that Sigma's own `fold` re-wrote) adds its units
  and removes none, so the copy neither shrinks nor stops tracking new units, and a unit removed on purpose stays in
  it until `recover --discard`. It is
  written for every user, with or without the old plugin, so one installed later is covered, but only inside an
  initialised repository (an existing `.sdlc/state/`: neither a write nor a check creates it). One file the size of
  the sheet, rewritten only when its bytes change. Measured once on one machine (a single run each, not a
  benchmark): at 1,000 units (196 KB) a check that finds nothing to do took under a millisecond, and about 7 ms when
  the sheet is smaller than the copy (the union is rebuilt each time); at 20,000 units (3.9 MB), 15 ms and 162 ms.
  It runs at each `loop.py` start, claim and record, and grows linearly with the sheet. It is local untracked state: a fresh clone, or a linked worktree without its own `state/`, has none.
- When `index.json` is in the old plugin's schema and no longer holds units the copy has, every such verb says so
  on stderr, loudly, naming the units and both levers. It repeats while the sheet stays that way; it is not
  silenced by `SIGMA_ALLOW_COEXIST=1`, which silences only the coexistence notice.
- **Nothing restores the tracked sheet unprompted.** The lever is explicit:
  `python3 skills/sigma-loop/scripts/feature_sync.py recover .sdlc` adds the missing units (whole units only; the
  sheet's own entries and any unit record win on a shared name) and writes the sheet in Sigma's schema, normalised
  like a fold. Review the change with `git diff`. If the sheet is complete on purpose (an older branch, a unit
  dropped deliberately), `recover .sdlc --discard` renames the copy aside (it is never deleted) and the
  refusals below stop.
- Until then Sigma's own `feature_sync.py fold` refuses (exit 2, nothing written) and `migrate.py` does not
  convert that sheet's schema id (listed as refused, in the dry run too), so neither can make the emptied
  registry look complete.

**Not covered, plainly.** The old plugin still rewrites `index.json`; between its write and the next `loop.py`
verb the registry is wrong, and `feature_registry.read` itself is unchanged, so a bare read before `recover`
returns what the file holds. The watcher's polling does not run the check. A unit record the old plugin
overwrites directly (its module-level `write_unit` replaced a Sigma-schema record in one measurement; no
command-line path to that was run) is not covered. A sheet that is truncated, corrupt or deleted, one emptied
before Sigma ever saw it, and one that kept a unit but lost its goals are not detected (only whole missing units
are restored). A complete old-schema sheet that arrives from an older branch is also reported, and `recover`
would add the copy's units to it: read the diff, or use `--discard`. A unit a teammate dropped on purpose can
return from a stale copy if no Sigma verb ran between the pull and the old plugin's fold (units are closed, not
dropped, in practice). `recover` rewrites the sheet's file time, so a later `repair` of a legacy record prints
what it discards instead of refusing. While the old plugin keeps running, its next fold empties the sheet again
and the signal returns: stop it first (above). Two Sigma processes refreshing the copy at once can leave it
missing a unit one of them held, until a later verb sees a sheet that has it (the refresh is a compare-and-write on
the sheet, not a lock). Nothing caps the copy's size, so a copy tampered with locally is restored in full by
`recover`. The lever is spelled for a POSIX shell (`python3`, single-quoted paths).

The controls run on the real fold (from a temporary copy; skipped with a named reason where the old plugin is not
installed) and on an in-tree model of it (always): `python3 -m pytest tests/test_registry_survives_predecessor_fold.py`.

**Once per run.** `init`, `loop.py start` and `migrate.py` always print the notice. The per-verb
surfaces (claim, record, the watcher and its automatic start) stay quiet while
`.sdlc/state/coexist.notice` is less than 6 hours old; every printed notice refreshes it. So a
session hears it once, not once per `loop.py` call.

**Ownership.** `.sdlc/state/owner.json` records Sigma as the owner on init and loop start. A
marker naming another plugin is part of the notice, never a lock: Sigma replaces it.

### What stays impossible

**Two watchers on one `.sdlc`.** Both plugins' watchers take the same lock files
(`state/watch.pid`, `state/watch.heartbeat`, `state/watch.decide.lock`), whatever you set. When the
old plugin's watcher holds the lock, a Sigma watcher start logs that the running watcher was not
started by Sigma and exits without starting a second one. Sigma never signals another plugin's
process. To hand the lock over, use the polite lever: uninstall the old plugin first (or its
triggers start its watcher again), create `.sdlc/state/watch.stop`, wait one watcher tick for it to
exit, then delete the file (no watcher starts while it exists). Sigma's own watcher starts on the
next trigger and records itself in `state/watch.owner`.

**Migrating under a live watcher.** `migrate.py --apply` refuses (exit 2, nothing written) while any
watcher is running for this `.sdlc`, because it may be the old plugin's, still writing the old
spellings. The refusal names the same lever.

### How the old plugin is detected

It counts as **active** on this repository when any of these holds:
- Claude Code has `<old>@…` enabled in `enabledPlugins` and the plugin is installed for this
  repository on this machine (`plugins/installed_plugins.json`). The managed, local, project and
  user settings files are read in that order of precedence; `CLAUDE_CONFIG_DIR` is honoured, and
  comments and trailing commas in them are accepted. The managed file is
  `/Library/Application Support/ClaudeCode/managed-settings.json` on macOS,
  `/etc/claude-code/managed-settings.json` on Linux and
  `%ProgramData%\ClaudeCode\managed-settings.json` on Windows. When the install list cannot be
  read, an entry in the managed, local or user settings still counts as active, while an entry
  only in the committed project settings is a note.
- Codex's `config.toml` sets `enabled = true` for `plugins."<old>@…"`, in any TOML spelling:
  `[plugins."…"]` or `[plugins.'…']` tables, dotted keys, or `"…" = { enabled = true }` under
  `[plugins]`. `CODEX_HOME` is honoured. Python 3.11+ reads the file with `tomllib`; Python 3.10
  uses a small built-in reader for these shapes.
- A hook whose command runs the old plugin is registered by hand in one of those settings files:
  a path in the command has a directory named exactly the old name, or contains the old plugin's
  recorded install path. A script of your own whose file name merely contains the name does not
  count.
- A live watcher holds this `.sdlc`'s watcher lock and Sigma did not start it (Sigma records its
  own in `.sdlc/state/watch.owner`). This counts as active when the path of the watcher's script
  (`watch_daemon.py`) has a directory named exactly the old name (read from `/proc` on Linux; only
  that script path is checked, never the `.sdlc` argument). Where the command line cannot be read
  (macOS, Windows), it counts as active only when another signal in this list is also present.
- `.sdlc/state/owner.json` names a plugin other than Sigma.

These are notes only, and print no notice: the old plugin installed but not enabled here, enabled
in settings but not installed for this repository on this machine, old schema ids in `.sdlc/` (at
most 200 files are read), an old-name Cursor rule or Codex `AGENTS.md` block (committed text), and a
watcher Sigma did not start with no active signal beside it -- which is also what a Sigma watcher
started before `watch.owner` existed looks like. `log.py` has no coexistence check.
