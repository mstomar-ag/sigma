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
| agent | Level 2: a source conflict goes to a capped, headless resolver; a second, independent session reviews the result; a failed proof or a blocking verdict parks and pushes nothing |

Level 1 needs a verify command. With none set the pass parks, unless `upkeep.conflicts.mechanical_without_verify` is the
boolean true. That key IS acted on by Level 1 (the config table in `docs/branching-model.md` still says it is validated
only; this page is the correction). `upkeep.verify.clean_rebase` is validated and not acted on.

Level 2 is **unreachable in the shipped engine**: its plan is supplied by a seam that is unset, because the binary, the
credential route and the spend caps are not read from configuration. Setting `conflicts.resolve` to `agent` therefore
changes nothing today beyond making the doctor show its resolver and reviewer rows (see
`skills/sigma-doctor/references/resolver-readiness.md`), which fail by design until those are chosen.

Every resolution leaves a record under `.sdlc/state/upkeep/resolutions`, and the rewritten commits carry a
`sigma-resolution:` trailer naming the level and the run.

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
