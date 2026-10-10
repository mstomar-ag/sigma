---
name: sigma-doctor
description: Check project setup, dependencies, and host support with fix commands. Use for setup problems or /sigma-doctor.
allowed-tools: Bash(python3 *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-doctor

Detailed selection triggers: [selection](references/selection.md).

Setup, self-diagnosed. The powerful features each need a small step (a `gh` permission, a
`pip install`, a filled north-star); this checks them all at once and hands you the exact fix, so
nothing fails silently.

Run the check-up and report it:
`python3 "${CLAUDE_SKILL_DIR}/scripts/doctor.py" check .sdlc`

It only checks what `.sdlc/config.json` makes relevant — a zero-dep local project sees just the one
**project layer** check; turn on the board or the KG and the matching checks appear. For each line:
- **OK** → that piece is ready.
- **MISSING** → run the printed one-liner (e.g. `gh auth refresh -s project`, `pip install graphifyy`,
  `/sigma-init`, `/sigma-vision`).

With `work.enabled` on or github discovery, it also runs `/sigma-init`'s preflight (#229): rows
`git repository`, `git remote '<name>'`, `base branch '<b>' on '<remote>'`, `gh installed`,
`gh auth`, `gh token scopes` and, with a board, `gh project scope`. Each fix is the failing check's
own: `gh` absent says install it (never `gh auth login`); a missing scope names
`gh auth refresh -s <scope> -h <host>`. A row ending `(cannot verify)` -- a fine-grained or GitHub App
token reports no scopes, or an ssh alias remote that `ssh -G` cannot resolve -- is not a pass, and
is never turned into a session-start setup step. A row whose prerequisite failed is not shown; fix the one above
it first. The network checks (`git ls-remote`, `gh auth status`, the owner lookup) are each limited
to the smaller of `SIGMA_WATCH_CALL_TIMEOUT` and 15s, so a dead host shows as `(cannot verify)`
with "timed out". The session-start setup check never runs `ls-remote` or the owner lookup; only
this full run does.

Present the checklist plainly. Offer to run a fix that's safe to run for the user, but **never run an
interactive login (`gh auth …`) or a package install on their behalf** — hand them the command.

That rule governs acting *unprompted* — silently, or on an assumed yes. It does **not** apply to
`sigma-wizard`'s consent flow, which asks the user a real yes/no question in the moment and runs the
install only on an explicit yes (`gh auth …` stays off-limits there too, since interactive OAuth
needs their own browser). Asked-and-answered is a different thing from done-on-their-behalf.

**One fix IS safe to run for them: `team ledger initialized → MISSING`.** The ledger is switched on
in config but its shared branch hasn't been created yet. Just run the one-command setup — it creates
the ops branch, seeds their own entries file + the `.sdlc/ledger/TEAM.md` rollup, and pushes, so the branch exists
for the whole team the moment the ledger is turned on:

`python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/sync.py" bootstrap .sdlc`

It's idempotent (safe to re-run), and each teammate runs it once in their own clone to join. After
that, claiming/recording is automatic inside `/sigma-loop`; point them at **`/sigma-ledger`** to read
the ledger or hand work off.

## Upgrading from the plugin's previous name

A repo adopted under the plugin's previous name keeps working, because Sigma reads the old spellings.
The explicit cutover is a dry run by default, `--apply` writes, and it is safe to re-run:
`python3 "${CLAUDE_SKILL_DIR}/scripts/migrate.py" .sdlc [--apply]`. It prints every file it changes,
refuses anything it cannot rewrite with certainty (exit 2) -- including a unit record the old plugin
wrote after the conversion, which it leaves for `feature_sync.py repair` -- and never runs on the user's behalf
without being asked. See [docs/upgrading.md](../../docs/upgrading.md).

The `coexistence` row reports whether the old plugin is also active on this repository. When it is,
the row is a WARN, never a failure: Sigma runs normally beside it and is handling the repository
(one notice line per run names the uninstall command). Relay the cut-over steps
`python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/coexist.py" check .sdlc` prints, IN ORDER --
stop the old plugin on this repository first (`claude plugin disable <id> --scope local`, run by
the user), migrate dry run, `--apply` only on the user's explicit yes, then uninstall the old
plugin. The old plugin cannot read Sigma's registry, so while it can still run here `--apply`
refuses (exit 2, dry run shown) unless `--replace-old-plugin` is added: never add that flag, disable,
uninstall, or set `SIGMA_ALLOW_COEXIST=1` (which only silences the notice) on the user's behalf. A
registry line saying a record `still declares the schema id` of the old plugin means it already
wrote a unit record after the conversion (Sigma merges it as a delta; a partial migrate never leaves
one, because `migrate.py --apply` keeps `index.json` in the old schema until every record has
converted): relay the recovery it prints. The `legacy delta records` row counts such records and how
many would be refused. `feature_sync.py repair` lists every record value it discards and refuses a
record newer than `index.json`; never work around that refusal on the user's behalf.

## Secret-file coverage

One row deserves naming because it is the only check whose MISSING state is a live exposure rather
than an unfinished setup step: **`secret-shaped files are ignored`**. `work.py commit` stages the
goal's worktree with `git add -A`, which honours `.gitignore` and nothing else — so any
secret-shaped file the repo has not already ignored (`.env`, `*.pem`, `*.key`, `id_rsa`,
`credentials.json`, service-account JSON) is one unattended goal away from a pull request.

`commit` refuses rather than commits it, so the failure mode is a stopped run, not a leak — but the
refusal arrives mid-goal, and this row arrives before the loop has run at all. **Report it, name the
paths, and offer to add them to `.gitignore`** (that one is safe to run for them). Only if a listed
file is a deliberate fixture does its exact path belong in `work.allow_secret_paths`. The row is
matched on NAMES; no file is ever read, so it can neither see nor report a secret's value.

## Install scoping — which version actually governs here

In a Codex session, the full check uses `codex plugin list --json` to verify that Sigma is
installed, enabled, and at or above the floor stated in the running plugin's `AGENTS.md`. An
unreadable inventory is MISSING, never a green check. It also checks Codex's companion plugins
from that inventory. This does not use Claude's scope file. The cheap wizard check omits the Codex
CLI query, so run the full `/sigma-doctor` before the first loop.

On Claude, the existing scope check below still applies.

The other row worth naming: **`sigma install scopes`**. `~/.claude/plugins/installed_plugins.json`
records one entry **per scope**. A `user` entry applies everywhere; a `project` (or `local`) entry
carries a `projectPath` and **shadows user scope for that path alone**. So `claude plugin update
--scope user` can report success while one directory sits on an old version indefinitely.

This row enumerates every scope sigma is installed under and judges each against the version
floor `AGENTS.md` states (parsed from that file, never a second copy of the number). Two things it
deliberately keeps apart:

- **Below the floor is a hard stop** — `AGENTS.md` forbids starting the loop there. This row fails.
- **Merely behind the marketplace** is the separate `sigma up to date` nudge. Informational. It
  reads the marketplace the plugin was installed from (the host's marketplace record) and falls
  back to the public repository when that record is unreadable.

When it fails it also says which entries **this run can fix** (`--scope user` works from anywhere; a
per-project scope only from its own `projectPath`) and which you must go to. **For a redundant
override the remedy is removal, not another update:** `claude plugin uninstall sigmaloop@sigmaloop
--scope project`, run from that directory, makes the path inherit user scope again. Never advise
keeping N installs in step. Doctor only ever names these gestures — **never run an install, update
or uninstall on the user's behalf.**

An entry whose `projectPath` no longer exists is counted, not flagged: it governs nothing, and the
directory you would run the uninstall from is gone. And if `installed_plugins.json` is missing,
unreadable or malformed, the row is **omitted entirely** rather than shown green — a row that means
"we did not look" must never read as an all-clear.

Old-plugin-id install row: [pre-launch id](references/pre-launch-id.md).
Resolver and reviewer readiness rows: [resolver readiness](references/resolver-readiness.md).

## What this check-up covers

This check-up reports on the core only. Anything installed alongside it reports its own health
through its own tooling.

## Standing-doc hygiene

The same run also scans the standing docs (`.sdlc/project.md`, `.sdlc/context/*.md`) for references
that no longer resolve — a cited path that moved or was deleted, a markdown link to a missing file.
Docs rot as the code moves, and a north-star pointing at a file that's gone quietly teaches the wrong
thing to every phase that reads it.

`python3 "${CLAUDE_SKILL_DIR}/scripts/doctor.py" hygiene .sdlc .` prints the detail on its own.

Report it as a **separate section** from the setup checks and never fold it into the ready score —
"is my setup working?" and "are my docs rotting?" are different questions with different fixes. This
half is deliberately mechanical: it only reports references that provably don't resolve, or that
resolve *outside* the repository — a leading `/`, a `..`, or a symlink that leaves the tree is the
same failure for a doc making claims about THIS repo. The judgment
half — demoting a rule that CI now enforces, archiving a superseded plan — belongs to **`sigma-retro`**,
which proposes standing-doc changes and parks them for your approval rather than editing them.

## Dispatch compliance — detection, never enforcement

**`dispatch compliance (agent_dispatch/agent_done vs ledger phase boundaries, #1779)`** answers a
question no host-side chokepoint can enforce (a prior goal, #1703, found there is none): when a
phase runs, did it actually run as its own **dispatched subagent** — the maker≠checker discipline
`sigma-loop`'s own SKILL.md relies on per-phase subagents for — or did it silently run inline in the
orchestrator instead? That regression has the exact "no errors, nothing happening" shape `AGENTS.md`'s
LIVENESS property already names as the dangerous one: nothing crashes, nothing logs an error, the
loop simply stops getting the independent check it was designed around.

It cross-references two stores that were never meant to be read together: the shared ledger's own
`phase` events (written by `phase_report.py` at every SDLC phase boundary, on every host, regardless
of how that phase ran) against each goal's **local** action log (`.sdlc/state/log/<goal>.jsonl`,
`agent_dispatch`/`agent_done`, written only when the orchestrator actually dispatched that phase as a
Task-tool subagent). Read the row like this:

- **`off (the journal is not enabled …)`** → nothing is being recorded on either side; there is
  nothing to check yet, and this is not a gap. Turn on `"journal": {"enabled": true}` first.
- **`no phase boundaries recorded in the last 30d …`** → the journal is on, but nothing has run
  recently enough to check. Also not a gap.
- **`READY — N/N phase boundaries …`** → every phase boundary the ledger recorded in the last 30
  days had a matching dispatch/done pair in that goal's own local log.
- **`PARTIAL — n/N …`** / **`MISSING — 0/N …`** → names each gap as `#<goal> (<phase>)`, with an
  `x<count>` suffix when the same goal re-ran that phase more than once with no dispatch either
  time. This is the row to check after a SKILL.md edit that touches how a phase is dispatched, or
  after noticing a phase's own review quality has quietly dropped with nothing else explaining why.

**This is diagnostic, not a gate** — matching #1703's own finding that a Task-tool dispatch cannot be
made code-driven on every host (Cursor's `.mdc` is text a model reads, not a hook chokepoint). A gap
never blocks a pick or a merge; it only tells you where to look. Report it, name the gaps, and leave
the fix (going back to dispatching that phase properly) to a human — never edit SKILL.md or retry the
phase on this row's say-so alone.

## Dispatch model log coverage — phase, the sibling row, #2514

**`dispatch model log coverage — phase (not observed host model, #2514)`** sits right below the
row above and answers a narrower, CONDITIONAL question: **given** a phase dispatch happened at all,
did its log carry a `--model` tier? It reports `LOGGED ONLY` even with full coverage; a log field
is not proof that Claude or Codex ran on that model. `phase_report.py end` checks observed models
when a readable host transcript exists. The two are deliberately separate rows — conflating "no
dispatch" with "dispatch, no model" would make them indistinguishable in the output.

Unlike the dispatch-compliance row above, this one now has a real enforcement half:
`actionlog.append()` refuses to write a `--role phase` `agent_dispatch` entry with no `--model` at
log time (`loop.py log` surfaces the refusal as a non-zero exit). This row is the DETECTION half,
catching anything that reached the log another way — a hand-forced log line, or a log written
before this rule existed. Read it the same way as the row above:

- **`off (the journal is not enabled …)`** / **`no phase boundaries recorded in the last 30d …`** —
  same meaning as the sibling row: nothing to check yet, not a gap.
- **`LOGGED ONLY — N/N phase boundaries … had a matching agent_dispatch carrying model`** → every
  phase boundary has a model field in its dispatch log; runtime model compliance remains separate.
- **`PARTIAL — n/N …`** / **`MISSING — 0/N …`** → names each gap the same `#<goal> (<phase>)`
  [`x<count>`] way as the sibling row. Since `--model` is now refused at log time, a gap here in a
  fresh install means something wrote directly to the action log, bypassing `loop.py log`.

Same posture as the sibling row: diagnostic, never a gate. Covers `--role phase` only — the slice
row below covers `--role slice` (#2557).

## Dispatch model log coverage — slice, issue #2557

**`dispatch model log coverage — slice (…, #2557)`** asks the same question as the phase row, for
`--role slice` dispatches, but as its OWN row: it has no ledger denominator to cross-reference. The
ledger's `"slice"` event fires only at `slices.py plan` time (one row per manifest-declared slice)
and has never fired in this repo, so it can't stand in for "did a slice actually run". This row
instead audits the local action log directly — every `agent_dispatch --role slice` entry in the
last 30 days, and whether it carries `model` — same enforcement (`actionlog.append()` refuses a
bare `--role slice` dispatch, #2514/#2521) and the same band names as the phase row (`LOGGED
ONLY`/`PARTIAL`/`MISSING`), with gaps named `#<goal> (slice <thread>)` [`x<count>`].

Two differences from the phase row: it gates on `action_log.enabled`, not `journal.enabled` (its
data is local-log-only, no ledger dependency); and it can't see a slice that ran with zero log
entries at all, or tell apart two slices that both omitted `--thread` (a `--thread` compliance gap
of its own, out of scope here) — named limitations, not silently equivalent to the phase row's
ledger-backed coverage.

`--role goal-slot` gets no row: #2514 deliberately excluded it from the `--model` requirement (its
tier resolves *inside* the dispatched subagent, after dispatch — nothing to pass at the call site),
so nothing requires `--model` there on `main` today and a row would only produce false gaps. #2557
tracks adding one once that requirement lands.

Same posture as both rows above: diagnostic, never a gate.

## Budget enforcement — the sibling row, #2515

**`budget enforcement (is max_tokens actually being fed? #2515)`** sits right below the existing
**`budgets`** row and answers the narrower, CONDITIONAL question that row does not: **given**
`budget.max_tokens` is configured, has the current run fed the counter that
enforces it? Before #2515, nothing did — `phase_report.py` priced every phase's real cost but never
called `state.add_tokens`, so `run_tokens` stayed at 0 and `budget.max_tokens` was
configured-but-inert on every host. `phase_report.py cmd_end` now credits the counter once per
phase attempt when the entire phase has a measured, priced cost-equivalent token count —
**unconditional on `"journal": {"enabled": true}`**, unlike the row above it. This row reads the
current run's durable credit identifiers and matching `phase`/`spend` events to corroborate that;
journal-off hosts have no event evidence to cross-reference:

- **`off (budget.max_tokens not configured …)`** → nothing to enforce; this is the ONLY "off" state
  this row uses (contrast the dispatch-compliance rows above, which read "off" when the journal is
  disabled — this row deliberately does not, since the mechanism it detects does not depend on the
  journal).
- **`… (the journal is off, so this check has no events to read — budget.max_tokens may still be
  enforced; check STATE.md's run_tokens directly to confirm)`** appended to any other line →
  `max_tokens` IS configured, but this check has nothing to cross-reference. Read `STATE.md`'s
  `run_tokens` cursor directly (or `loop.py`'s own status output) to see whether it's actually
  moving.
- **`no phase boundaries recorded in the last 30d …`** → the journal is on, but nothing has run
  recently enough to check.
- **`MISSING — N phase-end(s) recorded but NONE measured tokens …`** → this host's transcript
  source has been unavailable every time (see a phase banner's own `cost: unavailable` reason);
  `budget.max_tokens` is genuinely inert here, not merely undetected.
- **`PARTIAL — n/N current-run phase-end(s) …`** → some measured ends lack a matching priced spend
  event and durable budget credit. Older event pairs without a current-run credit cannot prove
  enforcement. Codex phases and partly unpriced Claude phases also remain uncredited.
- **`READY — n/N current-run phase-end(s) …`** → every observed end in the current run matches a
  priced spend event and a durable cursor credit.

Same posture as the two rows above: diagnostic, never a gate — this row never changes what the loop
does, it only tells you whether the mechanism above it in `budgets` is real or decorative.
