# Branch upkeep, part B: conflict levels, the opt-in audit and what was proven

Part B of branch upkeep decides what the unit pass does when bringing a unit branch forward hits a conflict. Part A (the
pass, the backup refs, restore, prune and the scheduler) is in `docs/branching-model.md`, sections 13c to 13e. Everything
here is **off by default**: nothing happens until `upkeep.enabled` is the boolean true in `.sdlc/config.json`.

## The conflict levels

`upkeep.conflicts.resolve` picks how far a conflict may be resolved. A conflict that the chosen level cannot take is parked
exactly as before: the unit branch is left untouched, nothing is pushed, and an issue records why.

| Value | What happens |
| --- | --- |
| off | the default; a conflict parks, as it did before part B |
| mechanical | Level 1: a conflict confined to the changelog is resolved by the heading-aware union, proved, stamped and pushed once through the atomic backup push; anything else parks |
| agent | Level 2: a source conflict goes to a capped, headless resolver; a second, independent session reviews the result; a failed proof, a dropped unit line or a blocking verdict parks and pushes nothing. With no spend key set it runs with no spend limit |

A parked conflict is one finding. Before filing, the pass reads the open issues once and, if one already carries the same
conflict id in its title, files nothing and comments on it at most once a day (a counter and the time; the count is kept in
the per-unit filed store, so a second clone may comment once more). A changed conflict id files a new issue that names the
one it supersedes and leaves that one open. When a later pass replays the unit cleanly it comments on an issue found this
way and does not close it. The lookup needs a resolvable repository and the gate open; a failed read just files as before.

Level 1 also covers the release-heading case: when the base moved the old Unreleased entries under a new dated
version heading, the unit's new entries are filed under Unreleased above that release section, the base's sections stay
as they are, and an entry the base already has is dropped by its text; an edit to an existing line, a link footer or an
unknown heading still parks.

Level 1 also takes two append-only files (#1515): a dated log with `## YYYY-MM` or `## YYYY-MM-DD ...` sections (#1949: a day-form heading is one entry, so two entries on one date both stay; lines unioned per section, sections in
date order) and a bullet-list file (the base side's order, then the unit's new items). Only when both sides purely added lines and
no line of either side is lost; an edit or removal on either side parks exactly as before. The record, backup ref and
`sigma-resolution:` trailer are the changelog path's.

Level 1 needs a verify command. With none set the pass parks, unless `upkeep.conflicts.mechanical_without_verify` is the
boolean true. That key IS acted on by Level 1 (the config table in `docs/branching-model.md` still says it is validated
only; this page is the correction). `upkeep.verify.clean_rebase` is validated and not acted on.

Level 2 is fed from configuration (#1516). `upkeep.conflicts.agent` holds:

| Key | Meaning | Default |
| --- | --- | --- |
| `model` | resolver model tier: haiku, sonnet, opus or fable | sonnet |
| `max_budget_usd_per_unit` | per-run cap for the resolver; the reviewer gets half of it | null |
| `spend_ceiling_usd_per_day` | rolling 24 hour ceiling, per machine and per team ledger | null |
| `timeout_minutes` | wall clock for one resolver run, 1 to 240 | 30 |
| `max_units_per_sweep` | units that may reach the resolver in one sweep, 1 to 100; the rest park | 5 |
| `reviewer.enabled` | validated only: the independent review is mandatory, so false is ignored | true |
| `reviewer.model` | reviewer model tier | sonnet |

With no spend key set the agent level runs with no spend limit; set `max_budget_usd_per_unit` (and optionally `spend_ceiling_usd_per_day`) to cap it.

With a tier missing from the model catalog, or no `claude` on the path, the agent level behaves
exactly as `off` and the doctor row `resolver configured` gives the reason. A reached ceiling parks the unit with the text
`spend ceiling reached` and calls no model. A resolver result that no longer holds a line the unit added is refused
(`unit-line-dropped`), parked, and the finding shows the attempt as a diff against the unit's side. PROVISIONAL: the
reviewer share, the strict line rule and the sweep counter are defaults for the owner to ratify. The launcher's catalog holds real model ids, and the one design flag still unverified (`--bare`) is refused by name and
not requested by the resolver; see `skills/sigma-doctor/references/resolver-readiness.md`.

The same line rule applies to the integration side: a result that drops a line it added is refused as `base-line-dropped`, and the finding names up to three of the lines (#1536). A sweep hands its run id to each child it starts, and a child whose sweep counter is missing, inactive or from another run counts as over the limit; the single-unit pass sets no run id and is unchanged. The doctor row `resolver configured` fails while the chosen model still resolves to the placeholder id.
The doctor also shows `resolver spend today` (against the daily ceiling; unreadable spend keeps the launcher closed), `units parked on conflict` and `last resolution` (level, verdict, time), and `upkeep units` (registered units and remote feature branches nobody maintains).

A merge made outside the loop is noticed at the next watcher tick (up to the watch interval); only
`work.py merge` starts a pass at once, and only with `SIGMA_UPKEEP_JOB=1` in that shell. A pure fast-forward push keeps no
backup ref (nothing is replaced); the lease still applies.

Every resolution leaves a record under `.sdlc/state/upkeep/resolutions`, and the rewritten commits carry a
`sigma-resolution:` trailer naming the level and the run.

## The agent level as one scratch session (#1573)

With the shipped configuration the agent level keeps ONE session directory per unit, under the resolver root, and
re-enters it at every conflicted commit of the replay. It is a `git init` with no history, no remote and no credentials,
so it shares no objects with the real repository; the stopped tree is copied over it at each stop, and
`.sigma-context/stopN/` holds, for each conflicted file, the base, ours and theirs versions and the recent integration
commit subjects, plus the replayed commit's message, the files resolved at earlier stops, and PR texts when supplied.
The model may edit, build and run tests there (`acceptEdits` with Read, Edit, Write, Glob, Grep and, only when confined, Bash), so the
before/after discard of the plain export does not apply; the copy is thrown away. Confinement (#1693, PROVISIONAL: the live run is owner-gated). Where a wall helper exists
(macOS `sandbox-exec`), the launch argv starts with it: reads of the account's credential directories are denied,
writes outside the session and scratch directories are denied, and outbound network is limited to port 443. It cannot
filter by host name, so any host on port 443 stays reachable; it is not a proof against a determined session. What the wall does not cover: the profile allows everything by default and denies only the listed items, so the keychain and mach services stay reachable (only reads of the credential directories are denied, not a lookup through a system service); reads outside the listed credential directories stay open; and writes to the temp areas (`/private/tmp` and `/private/var/folders`, besides the device files) stay open. Where no
wall exists (Linux, or the helper missing) the session runs WITHOUT the Bash tool, with Read, Edit, Write, Glob and Grep
only, so it cannot run commands, read credentials or open connections. The doctor row `resolver session confinement`
says which applies. Besides this, the copy is credential-free: no token, no agent socket, no credential helper, a
temporary home and no remote.

What does not change: Sigma alone writes a file back, and only a conflicted file that is a regular, non-link file inside
the session directory with no marker left; the dropped-unit-line and dropped-integration-line proofs, the stamp, the
authorship check, the proof gate, the independent review and the single atomic backup push all run as before. The
whole session shares one wall clock (`timeout_minutes`): each stop gets what is left, and none left parks as
`session-timeout`. Session limits are PROVISIONAL (30 files, 100 hunks, 4000 conflict lines, 50 stops). A plan without
the `session` key runs the plain export exactly as before. Files a later stop's tree no longer holds are not removed
from the session copy; the whole directory is removed when the pass ends.

## Turn on agent resolution

The agent level (`upkeep.conflicts.resolve` `agent`) is off until you choose how the resolver and the reviewer log in. Pick one.

**Scrubbed (the default, `upkeep.conflicts.agent.login` `"scrubbed"`).** The session gets a throwaway home and a throwaway
configuration directory, so the CLI's own login is not found. Name one environment variable that holds the model login:

```json
{ "upkeep": { "enabled": true, "conflicts": { "resolve": "agent", "agent": { "credential_var": "MODEL_LOGIN_VAR" } } } }
```

**Host (`"login": "host"`, opt-in).** The resolver, the reviewer and the logged-in preflight run on the watcher's own `HOME` and
`CLAUDE_CONFIG_DIR`, so the login you already did with the CLI is used and no variable is needed:

```json
{ "upkeep": { "enabled": true, "conflicts": { "resolve": "agent", "agent": { "login": "host" } } } }
```

What host mode exposes: the session can read the watcher user's CLI profile, settings and memory, because they are in that
home. Every other wall stays: the edit-only tool allowlist with `acceptEdits`, the network prefix, the timeout, the per-sweep
cap, the spend keys, the confined scratch copy, and no git, gh or ssh credential variable (`GH_*`, `GITHUB_*`, `SSH_*`,
`GIT_*`). `credential_var` is still passed if you also set it. A host run is charged the per-unit cap, or nothing when no cap is
set, because the shared transcript store is never priced. The model tiers are capped by the repo's `model_selection_max_tier`
(default `opus`): a `model` or `reviewer.model` above it is lowered to it, never raised. The doctor rows `resolver auth` and
`resolver configured` read "host login: logged in as <account type>" and never print a credential. A unit whose preflight finds
no login parks with "resolver cli is not logged in under the host login". On macOS the CLI keeps its login in the keychain, which the session wall denies and keeps denying (it also holds the git host token). Host mode therefore passes `USER` and `LOGNAME`, and hands the walled session the CLI's own `CLAUDE_CODE_OAUTH_TOKEN` when the watcher has it (made once with `claude setup-token`); the unit that finds no login parks naming that command. Only that one variable travels, never a `GH_*`, `GITHUB_*`, `SSH_*` or `GIT_*` one. UNVERIFIED on a real macOS CLI: the recorded `claude auth status` inside the wall is owner-run. The recorded real-model run of this mode is owner-run
after landing; until it is recorded, host mode is proven against a fake CLI only.

## Landing a unit

A guarded landing of a whole unit, behind the same gate, is run by the landing engine
(`skills/sigma-loop/scripts/feature_land.py`, `feature_land_merge.py`). It verifies the unit tip in a scratch worktree,
opens or finds the landing pull request, and merges only the exact head it verified. A replay reports `already-landed`.
Its approval and write surface are listed in `docs/launch/write-surface.md`.

## The opt-in audit

The claim is that with the gate closed, none of part B runs, reads or writes. It is checked three ways:

- `tests/test_upkeep_audit.py` enumerates every reference to the gate by any spelling (an aliased import, an assignment
  form, a hand-written check), compares that with the registered entry points, and runs a near-enabled matrix on each
  real entry point: a config that is almost, but not quite, open must leave a trap that records process launches, model
  launches, network lookups and file writes empty. A second run opens the gate to show the same driver does act, and a
  third strips the decorator to show the check goes red.
- `KNOWN_READERS` in that file lists each shipped script that reads the gate by hand, with a reason. Part B's are the
  conflict resolver launcher, the independent reviewer route, the landing engine halves, the landing approval check, the
  chat landing route and the rebase landing route. An unlisted reader fails the test; a listed one that stops reading
  fails it too.
- `tests/test_upkeep_level1.py` and `tests/test_upkeep_level2.py` each carry a gate-closed test that asserts the closed
  pass is byte for byte what it was.

## What was proven, and what was not

Two runs are recorded. `docs/launch/evidence/upkeep-local-run.md` is part A on a local bare remote.
`docs/launch/evidence/upkeep-part-b-run.md` is part B on a local bare remote with a fake resolver and a fake reviewer, and
carries the full list of what it did not cover.

A hosted validation on 2026-10-10 ran the shipped scripts against a private throwaway repository and a Projects board. It
passed for init with a board, a unit of two goals through pull requests, the pass with a backup ref accepted by the host,
restore (and a refused restore given a wrong expected tip), a restore that is not sticky, a prune of exactly the old
backups, a guarded head-pinned landing (rehearsal, merge, then already-landed), the mechanical level (a non-union
changelog conflict refused and parked, a union conflict resolved and pushed with a backup) and the scheduler's detached
job. A host limit was seen: branch names that start with `refs/` are rejected.

**Not run, and so not claimed:**

- The real-model run. Reasons: the flags beyond the launcher's confirmed table
  are unverified and refused, and no credential route has been chosen.
- A real-model resolver and reviewer.
- The chat door against a chat service.
- Branch rulesets and bypass actors (a private repository without a paid plan has none).

The recorded real-model run, for both login modes, is owner-run after landing; until it is recorded, treat Level 2 as proven against a fake only.

**Model login (#1691).** The resolver and the reviewer run in a scrubbed environment, where the CLI's own login is not found.
Set `upkeep.conflicts.agent.credential_var` to the NAME of one environment variable that holds the model login (never a
value); that single variable is the only credential passed on. Git, gh and ssh credentials (`GH_TOKEN`, `GITHUB_TOKEN`,
`SSH_AUTH_SOCK`, any `GIT_*` name) are refused as the name and never forwarded. Before any spend is reserved, the launcher runs
a bounded `claude auth status` in that environment; when it reports logged out, the unit parks with "resolver cli is not
logged in in the scrubbed environment". The doctor row `resolver auth` runs the same probe.

**The default parks (#1713).** With `upkeep.conflicts.agent.credential_var` unset, which is the default, every agent-level launch
runs `claude auth status` in the scrubbed environment and parks as not logged in: the unit parks until
`upkeep.conflicts.agent.credential_var` is set to the name of a variable that holds the model login. This is intended, not a fault.
Where the platform has a session wall (macOS `sandbox-exec`), both the launcher preflight and the doctor row run the probe inside
that same wall; where none exists the probe runs plain. The wall itself is unverified against a live session (owner-gated).

The agent level has not been run
against a real CLI, and the shape of the real `auth status` output is unverified (only its exit code and a false `loggedIn`
are read). The live run on a throwaway repository: Awaiting (owner-gated).
