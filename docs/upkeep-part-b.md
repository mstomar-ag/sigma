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
| agent | Level 2: a source conflict goes to a capped, headless resolver; a second, independent session reviews the result; a failed proof, a dropped unit line or a blocking verdict parks and pushes nothing. It behaves as `off` until the two spend keys below are set and the model resolves |

A parked conflict is one finding. Before filing, the pass reads the open issues once and, if one already carries the same
conflict id in its title, files nothing and comments on it at most once a day (a counter and the time; the count is kept in
the per-unit filed store, so a second clone may comment once more). A changed conflict id files a new issue that names the
one it supersedes and leaves that one open. When a later pass replays the unit cleanly it comments on an issue found this
way and does not close it. The lookup needs a resolvable repository and the gate open; a failed read just files as before.

Level 1 also covers the release-heading case: when the base moved the old Unreleased entries under a new dated
version heading, the unit's new entries are filed under Unreleased above that release section, the base's sections stay
as they are, and an entry the base already has is dropped by its text; an edit to an existing line, a link footer or an
unknown heading still parks.

Level 1 also takes two append-only files (#1515): a dated log with `## YYYY-MM` sections (lines unioned per section, sections in
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

With either spend key null, or a tier missing from the model catalog, or no `claude` on the path, the agent level behaves
exactly as `off` and the doctor row `resolver configured` gives the reason. A reached ceiling parks the unit with the text
`spend ceiling reached` and calls no model. A resolver result that no longer holds a line the unit added is refused
(`unit-line-dropped`), parked, and the finding shows the attempt as a diff against the unit's side. PROVISIONAL: the
reviewer share, the strict line rule and the sweep counter are defaults for the owner to ratify. The launcher's design flags
stay unverified and its model catalog is still the placeholder, so the real CLI run is refused at run time at no cost until
the owner-gated probe of the real flags and model id; see `skills/sigma-doctor/references/resolver-readiness.md`.

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
The model may edit, build and run tests there (`acceptEdits` with Read, Edit, Write, Glob, Grep and Bash), so the
before/after discard of the plain export does not apply; the copy is thrown away. The session is NOT network
isolated: a wall around the model command would cut its own API access, so the shipped plan sets none and the doctor row
`resolver session network` says "not isolated on this platform" (informational, never failing). Its protection is the
credential-free copy: no token, no agent socket, no credential helper, a temporary home and no remote. The launcher can
still prefix a wall when a plan supplies one (`network_prefix` builds the macOS and Linux forms); that wiring is UNVERIFIED
against a real model run.

What does not change: Sigma alone writes a file back, and only a conflicted file that is a regular, non-link file inside
the session directory with no marker left; the dropped-unit-line and dropped-integration-line proofs, the stamp, the
authorship check, the proof gate, the independent review and the single atomic backup push all run as before. The
whole session shares one wall clock (`timeout_minutes`): each stop gets what is left, and none left parks as
`session-timeout`. Session limits are PROVISIONAL (30 files, 100 hunks, 4000 conflict lines, 50 stops). A plan without
the `session` key runs the plain export exactly as before. Files a later stop's tree no longer holds are not removed
from the session copy; the whole directory is removed when the pass ends.

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

- The real-model run. Reasons: the model catalog entry is a placeholder, the flags beyond the launcher's confirmed table
  are unverified and refused, and no credential or spend cap has been chosen.
- A real-model resolver and reviewer.
- The chat door against a chat service.
- Branch rulesets and bypass actors (a private repository without a paid plan has none).

Until an operator approves a kept throwaway repository and its spend, treat Level 2 as proven against a fake only.
