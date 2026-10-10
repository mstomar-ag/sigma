# Upkeep part B: one local run with a fake resolver

This is the record of one run of the shipped part B code (the conflict levels of the unit pass) against a **local bare
remote only**, with a **fake** resolver and a **fake** reviewer: small scripts written into a temporary directory that
stand in for the model command. No real model was called, no network call was made and no hosting service was involved.
The verify command is a stand-in too. Nothing here is a claim that a real model resolves or reviews a conflict well.

The test `tests/test_upkeep_partb_run.py` makes this transcript and compares it with the block below, so the record cannot
drift from what the code does. Only outcomes and counts are recorded, because commit ids and times change on every run.

<!-- transcript:begin -->
```text
remote: a local bare repository per scenario; resolver and reviewer: a fake script; no model, no hosting service
1 gate closed: outcome=conflict; pushes=0; unit branch unchanged=True
2 level 1 (mechanical), changelog-only conflict: outcome=rebased; level=1; pushes=1; backup kept=True
3 level 2 (agent), source conflict, fake resolver then fake reviewer approves: outcome=rebased; level=2; calls=resolve+review; pushes=1; backup kept=True
4 level 2, fake reviewer blocks: outcome=parked; calls=resolve+review; pushes=0; unit branch unchanged=True; reason=reviewer-blocked
```
<!-- transcript:end -->

## What a hosted validation covered

On 2026-10-10 the shipped scripts were run against a private throwaway repository and a Projects board, by hand. It
passed: init with a board; a unit with two goals through pull requests; the upkeep pass with an atomic backup ref accepted
by the host; restore, and a refused restore given a wrong expected tip; a restore that is not sticky; a prune that deleted
exactly the old backups and kept the rest; a guarded, head-pinned landing of the unit (rehearsal, then merge, then
already-landed on replay); the mechanical conflict level (a non-union changelog conflict refused and parked with an issue,
and a union conflict resolved and pushed with a backup); and the scheduler's detached job. One host limit was seen: branch
names starting with `refs/` are rejected. That run is described here from its sanitized summary and is not reproducible
from this repository.

## What was NOT covered

- The real-model run is **not run**. Three things stand in the way: the model catalog entry is a placeholder, so no model
  identifier is confirmed; the command-line flags beyond the launcher's confirmed table are unverified and the launcher
  refuses them; and no credential and no spend cap have been chosen by the operator. Until an operator picks all three
  and approves a kept throwaway repository, Level 2 is proven only against the fake.
- A real-model resolver and a real-model reviewer: untested, so the quality of a resolution and the reviewer's
  judgement are unmeasured.
- The chat door against a chat service: only the local control that a closed gate leaves chat unchanged was run.
- Branch rulesets and bypass actors: not available on a private repository without a paid plan, so a ruleset that blocks
  the force push, and the bypass that would allow it, were not exercised.
