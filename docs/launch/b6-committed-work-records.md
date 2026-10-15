# Retention: committed work records, plans / research / acceptance (B6, #462)

Host-agnostic. Everything below is Sigma's own Python and git; nothing needs a hook, a process pause
or a human to keep running. The machine-readable claims are `docs/launch/dispositions/462.json`
(13 rows); `tests/test_b6_462.py` runs each claim against the real tree, the real scan or a scratch
git repository.

## Decision

These records are **intentionally unbounded in git, with a tested ceiling, and have no pruner.**
They are the project's own history: the plan a goal was built from, the research behind it and its
acceptance record. Deleting them deletes the audit trail, and the loop and review gates re-read them
by goal stem (below), so a Sigma-side pruner would be a way to lose history, not to bound it. What
Sigma does instead is make growth visible: a test fails when a record or the average per goal passes
the ceiling.

## Ownership (from the writer, not from the path name)

Every path is under the project's own `.sdlc/`. None is under a host configuration root (a
home-directory agent or editor config), so none is out of scope on that ground and nothing here reaches
outside the repository.

| Store | Writer | Owner | In git |
| --- | --- | --- | --- |
| `.sdlc/plans/<goal-stem>.md` | the Plan phase (`sigma-plan`), committed by `work.py commit` | the project's maintainers | yes |
| `.sdlc/research/<goal-slug>.md`, and `.sdlc/research/235.md` (a citation in `skills/sigma-init/references/board.md`, not a writer) | the Research phase (`sigma-research`, `sigma-goal-design`) | the project's maintainers | yes |
| `.sdlc/acceptance/<goal-stem>.md` | `acceptance.py record` | the project's maintainers | yes, but only through the branch: `work.py commit` copies the root record into the worktree and `work.py pr` refuses a branch without it (#1474) |
| `.sdlc/plans/<epic>-plan.md` | `sigma-scope`'s `assign.py` (one file per executed scope run) | the project's maintainers | yes |
| `.sdlc/plans/<goal-stem>.slices.json` | the Plan phase, read by `slices.py` | the project's maintainers | yes |
| `.sdlc/plans/scope/<slug>.plan.json` | the `sigma-scope` skill prose | the operator | no, gitignored |
| `.sdlc/plans/triage/<UTC-date>-<slug>.json`, `.md` | `triage.py plan` | the operator | no, gitignored |

`tests/test_b6_462.py` (`test_gitignored`) checks the two gitignored rows against `git check-ignore`
and that the tracked directories are not ignored. The `<epic>-plan.md`, `.slices.json` and scope-plan
files are absent from the measured tree, so their sizes were not measured. The tracked ones are under
the same per-file cap anyway because the guard reads every tracked file in the three directories.

## Who reads them (checked before concluding anything)

All readers look a goal up by its stem; none walks the whole directory except the two hooks.

- `work.py`: `pr` refuses a branch with no plan; `record-plan-review` and the `pr` gate bind the plan's
  sha256 (`review_context.plan_sha256`) to the plan as published; the research dossier must be on the
  branch. These read the goal's own file, once, before its PR merges.
- `review_context.py`: `phase_doc_file` finds a goal's plan, research or acceptance record (exact name,
  else a glob over a numeric stem) and builds the review briefs from the branch copy.
- `acceptance.py`: `refusal` and the `MAX_BYTES` read/write cap (32,768 B) on one goal's record.
- `hooks/plan_gate.sh` and `hooks/completion_gate.sh`: `find .sdlc/plans -name '*.md' -mmin -N` on a
  source edit or a stop, a directory scan.

Nothing reads another goal's file after its PR has merged. That is why the files could be reclaimed
without breaking a gate; it is not a reason to delete them, because they are the audit trail.

## Measured

Producing revision: origin/main `27390dd`, a clean worktree on Darwin 25.6.0 / APFS, one developer
machine. The audit snapshot's own sizes (plans 341,761 B, research 208,519 B, acceptance 10,318 B)
were taken at an older revision; the live counts below are the larger ones and are used.

| Measured | Result |
| --- | --- |
| plans, tracked | 77 files, 551,352 B; median 2,661, p95 43,076, max 89,108 |
| research, tracked | 75 files, 320,755 B; median 3,012, p95 17,896, max 25,656 |
| acceptance, tracked | 39 files, 24,539 B; median 521, max 1,553 |
| per goal (77 goals) | 896,646 B total, mean 11,645 B, median 6,179, p95 59,306, max 108,681 |
| largest not-yet-committed files, same checkout | plan 92,603 B, research 82,252 B; mean per goal 14,318 B counting them |
| history of these paths | 243 blobs, 1,744,317 B raw, 654,626 B packed (plans are rewritten after review, so about 1.9x the checkout raw, 0.73x packed) |
| span | first commit 2026-09-29: 77 goals in 5 days, about 15 per day on a heavily agent-driven machine |
| a `triage.py plan` run (5 picks, 3 waves, scratch) | 924 + 1,218 B plus a 156 B `active.json` |
| `find .sdlc/plans -name '*.md' -mmin -360` with no fresh file, 770 / 7,700 / 77,000 files of 100 B | 4.9 / 9.4 / 73.8 ms (median of 3) |
| `phase_doc_file` numeric-stem glob miss, same sizes | 0.3 / 2.9 / 31.6 ms; an exact hit is 0.01 ms |

Commands, from the repository root: `git ls-files -z .sdlc/plans | xargs -0 wc -c` (and for `research`,
`acceptance`); `git rev-list --objects --all -- .sdlc/plans .sdlc/research .sdlc/acceptance | git
cat-file --batch-check='%(objecttype) %(objectsize) %(objectsize:disk) %(rest)'`; for the scans, a
scratch directory of N files aged 30 days, timing the `find` above and `review_context.phase_doc_file`.
A goal's stem is its leading digits, else its name up to the first dot, so a `<n>-benchmark` record
counts with goal `<n>`.

**Not measured:** any adopter's repository (only this one); a goal that wrote more than one plan name;
the sizes of `.slices.json`, `<epic>-plan.md` and scope plan files (absent here); clone, fetch or
checkout time at 10x or 100x; Linux or Windows scan times; hooks other than the two `find` calls.
`bytes per merged goal` here is bytes of records per goal stem, not per merged pull request.

## Scale: 10x and 100x

| Goals | At the measured mean (11,645 B) | At the ceiling (32,768 B per goal) |
| --- | --- | --- |
| 77 (now) | 0.9 MB | 2.5 MB |
| 770 (10x) | 9 MB working tree, about 6.5 MB packed history | 25 MB |
| 7,700 (100x) | 90 MB working tree, about 65 MB packed history | 252 MB |

At the current rate (about 15 goals a day, a five-day window, so a projection and not a trend) 10x is
reached in about 50 days and 100x in about 17 months. The pending files lift the mean to 14,318 B, so
the "measured mean" column is, if anything, low by about 20%.

The scans stay small: at 7,700 goals the directory holds about 23,000 files and the `find` costs
tens of milliseconds per source edit, and the numeric-stem glob a few milliseconds, both only when no
exact name matches. The ceilings that bind first are checkout and clone size, then the per-edit
`find`; neither was measured past the figures above.

## The ceiling and its guard

`tests/test_b6_462.py` holds the guard (`records_over_ceiling`), a read of `git ls-files`:

| Ceiling | Value | Why this number |
| --- | --- | --- |
| a plan file | 131,072 B (128 KiB) | largest tracked 89,108, largest pending 92,603: 1.4x clearance, and a plan that size is roughly 32k tokens (an estimate, bytes divided by four, not measured) the reviewer must read whole |
| a research file | 131,072 B (128 KiB) | largest tracked 25,656, largest pending 82,252: 1.6x clearance |
| an acceptance file | 32,768 B | the existing `acceptance.MAX_BYTES`, imported, so there is one source |
| average per goal | 32,768 B | 2.3x the worst measured mean (14,318 B) |

A record over its cap, or an average over the per-goal ceiling, turns the suite red. Raising a number
is a reviewed edit of the constant and this table together: `test_doc_constants` fails when they
disagree. Run it exactly as written:

```sh
python3 -m pytest tests/test_b6_462.py
```

It runs wherever the suite runs: `.github/workflows/ci.yml` runs `pytest tests/` on every supported
cell. A tree with no `.git`, a root that is not its own git toplevel, or no `git` makes the guard raise
rather than report clean; a tree that tracks no records at all (a snapshot) skips the real-tree node and
says why. Untracked and gitignored files are not counted, because only history is a record.

**What the guard does not do:** it does not cap the number of goals (that is the product's growth), it
does not cap history (rewrites of a plan are stored in git and are not capped), and it protects
Sigma's own repository only. An adopter's repository keeps whatever its agents write; the only
size limit that reaches adopters in code is the 32,768 B `acceptance.MAX_BYTES`.

## Public snapshot

Whether the public snapshot carries `.sdlc/` is not decidable from this repository: the file selection
(`tools/public-manifest.txt`) and the builder are private and absent here. `tests/public_surface.py`,
in the snapshot's own mode, treats every tracked path as shipped, and the committed exposure evidence (not re-run here)
reports findings in two tracked records, so these files are treated as in scope for review either way. If the
snapshot does carry them, the ceiling above is what an adopter inherits; if it does not, the ceiling
protects only the source repository.

## Owner and the reclaim lever

Owner: the project's maintainers, per goal that wrote the file. Reclaiming is a human git operation and
Sigma ships no script for it, deliberately: a reviewed commit that `git rm`s the records of a retired
range of goals (their plans, research and acceptance), after the maintainers agree that history is no
longer needed. Reclaiming bytes from clones also needs a history rewrite (for example `git filter-repo`),
which only a maintainer with push rights should run, with every clone re-fetched. The gitignored triage
and scope files are the operator's: delete them by hand.

## Recovery (no human needed, no process pausing)

- The guard is read-only. A crash, a lost run or a partial run leaves nothing to repair; a red result is
  fixed by trimming the offending record or by the reviewed cap raise above.
- A lost or damaged record is restored from git (`git checkout -- <path>`); nothing here holds state
  outside git, and the gates re-read the goal's own file each time.
- Restore from backup: a restored checkout is the same files; the guard reads the checkout, so it
  re-evaluates from scratch and needs no prior state.
- A partial reclaim (a half-finished manual `git rm`) is only an uncommitted deletion; `git restore`
  returns it, and a gate that needs a missing plan refuses `pr` loudly rather than proceeding.

## Disposition and the audit gesture

`docs/launch/dispositions/462.json` covers the 13 patterns the scan produces under these directories: nine tracked rows and four
local ones, each `intentionally unbounded` with the ceiling, owner and reclaim lever in its text. No
`unscanned` waiver is used; the scan produces every pattern. The issue listed 14 patterns from an older
snapshot: `.sdlc/plans/0007-fix-retry.md` and `.sdlc/plans/2521.md` are no longer produced (the prose that
named them was reworded) and a pattern the scan does not produce is refused, so they are not covered;
`.sdlc/research/235.md` is new and is. The committed `growth-audit.json` is not regenerated by this slice,
as with the sibling B6 slices, so it still lists the two stale rows. The documented gesture in
`growth-audit.md` is run on a scratch copy by `test_audit`, which asserts every row leaves
`b6_disposition.unresolved_patterns` and carries the entry's `pruner_or_cap` and `decision`.
