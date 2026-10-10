# Upkeep part A: one local end-to-end run

This is the record of one run of the shipped upkeep code (the rebase pass, the backup refs, the restore and the prune)
against a **local bare remote only**. The remote was a bare repository made in a temporary directory and the clones were
local checkouts. No hosting service was involved, so nothing here is a claim that the feature works on one: that is not
proven, and it needs a throwaway repository on a hosting service that the operator approves and keeps afterwards. No
model was called; the only thing set by the test is the clock, so the backup stamps are the test's fixed clock, not the
date of any real rewrite.

The test `tests/test_upkeep_local_run.py` makes this transcript and compares it with the block below, so the record
cannot drift from what the code does. Commit ids are labelled by role (`tip0` is the unit tip before pass 1, `tip1` the
tip pass 1 produced) because the real ids change on every run.

<!-- transcript:begin -->
```text
remote: a bare repository in a temporary directory; clone: one local checkout; no hosting service
setup: feature/billing has one commit on tip0; main then moved ahead, so the unit is behind
pass 1: outcome=rebased; branch tip0 -> tip1; backup refs/sigma/backup/billing/20260921T141320Z holds tip0
restore --list: branch at tip1; backups 20260921T141320Z holds tip0
restore 20260921T141320Z expecting tip1: outcome=restored; branch at tip0; new backup refs/sigma/backup/billing/20260921T141420Z holds tip1
pass 2 (a restore is not sticky): outcome=rebased; branch moved off tip0; backup refs/sigma/backup/billing/20260921T141520Z holds tip0
prune --dry-run at +30 days: nothing removed, 3 backup refs remain
prune at +30 days (keep_last 1, keep_days 14): 1 backup refs remain, the newest (20260921T141520Z)
```
<!-- transcript:end -->

What it shows: the pass rewrote the unit and kept the old tip as a backup in the same atomic push; the restore put the
old tip back from a fresh clone that had never had it, keeping the tip it replaced; the next pass rewrote the unit
again, because a restore is not sticky; and the prune removed the two older backups, kept the newest, and touched
neither the unit branch nor the base. What it does not show: hosting-service rulesets or bypass actors, limits on
ref names or counts, authentication, rate limits, real network cost, a different git version or operating system, or
the scheduler's detached job.
