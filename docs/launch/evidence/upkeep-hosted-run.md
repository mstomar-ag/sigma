# Upkeep: one hosted end-to-end run

This is the sanitized record of one run of the shipped upkeep code on 2026-10-10 against a private throwaway repository
on a hosting service, with a Projects board, using the real scripts. The repository, its owner and its identifiers are
left out on purpose. It is a hand-recorded result, not a generated transcript: unlike `docs/launch/evidence/upkeep-local-run.md`
no test regenerates it, so it can go stale, and it is one run on one day.

## What passed

- Init with a board.
- A unit with two goals, each through a pull request.
- The upkeep pass, with the old tip kept as a backup ref in the same atomic push, and the host accepted the push.
- Restore, and a refused restore when the expected tip was wrong.
- A restore is not sticky: the next pass rewrote the unit again.
- A prune that deleted exactly the old backups and kept the rest.
- A guarded, head-pinned landing of the unit: a rehearsal, then the merge, then `already-landed` on replay.
- The mechanical conflict level: a non-union changelog conflict was refused and parked with an issue, and a union conflict
  was resolved and pushed with a backup.
- The scheduler's detached job.

## What was not covered

- A real-model conflict resolver and a real-model reviewer. No model was called; the resolver and reviewer routes stay
  `verified: false`.
- The chat door against a chat service.
- Branch rulesets and bypass actors. A private repository without a paid plan cannot have them, so the advice to grant
  upkeep's actor a bypass on `feature/*` is untested here.
- Scale: nothing was measured at tens or hundreds of units. The figures in `docs/branching-model.md` section 13d are
  reasoned, not measured.
- Any machine other than the one that ran it, and Windows (which is refused by design).

## Host limit seen

Branch names starting with `refs/` are rejected by the host.
