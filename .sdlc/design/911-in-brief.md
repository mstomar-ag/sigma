# Automatic, optional branch upkeep: the design in brief

**This mapping is incomplete.** The sweep used its whole three-round budget, and the last round was still finding new
parts of the code. Because of that, the work was split. This design now covers part A: the engine, the scheduler and the
backups. Conflict resolution (part B) and landing (part C) get their own designs, which run in parallel while part A is
built. The areas not yet traced are listed at the end, each next to the part that will trace it.

**What was checked.** Sigma 1.0.4's own code, read by nine read-only research passes over three rounds. They found 108
places the change touches, across 35 parts of the product: the rebase engine, the background watcher, the shared team
log, the chat and hand-run entry points, landing, the doctor, the setup files, the rule documents and the automated
guards. A "unit" here means a long-lived feature branch plus its record.

**Findings that need no ruling.**
- **Upkeep already exists, in four places.** Sigma rebases a unit when someone starts work in it, from the command line,
  from the hand-run rebase skill and from chat. What it lacks is:
  - a schedule;
  - a way to resolve conflicts at the unit level;
  - a landing that can be repeated and that checks exactly what it merges;
  - one shared set of safety checks for every way in.
- **Everything new fits behind one switch that is off by default.** Nothing changes for a project that does not turn it
  on, and the existing guards plus a new test can prove that.
- **The background job runs detached, with its output discarded.** Measured: a child that keeps the watcher's output
  open holds the watcher for its whole life. The job runs on Linux and macOS and refuses on Windows.
- **Two machines working the same unit cannot overwrite each other.** The protected push only overwrites a branch that
  is still where the job last saw it. The shared team log only records what happened, as plain unaddressed notes, and
  none of the existing readers acts on those, which were checked one by one. One cost was found: the read-marker that
  tracks the log remembers every process that ever wrote to it, so a job that is a new process on each run would grow
  it on each run, whoever it writes as. Step 7 must therefore choose a writer that adds nothing per run.
- **Before every rewrite, the old tip is saved as a backup copy on the remote.** It can be restored. Copies older than
  14 days are cleaned up automatically, always keeping the newest five; this is the feature's one automatic deletion.
- **Several automated guards must be widened or respected on purpose.** These are the "never delete" check, the
  inventory of places that write to GitHub, and the limits on instruction-file size.

**Decided by the owner.**
- **Scope:** build part A now, and design parts B and C in parallel.
- **Backup naming:** the copies live under `refs/sigma/backup/`. The clean-up still finds them after any future product
  rename. Unit names too long to fit in a ref are refused loudly.
- **Review:** a fresh, independent reviewer checks this design before anything is filed.

**Still open.**
- **Moved to part B:** which independent reviewer checks a machine-made conflict resolution.
- **Moved to part C:**
  - how a landing is approved when nobody is watching;
  - whether chat landing needs that same approval.
- **Needs approval:** any test against a real hosted repository, rather than a local scratch copy.

**The plan for part A.**

| Step | What it delivers | Needs first |
| --- | --- | --- |
| 1 | The off-by-default switch, and proof that nothing changes while it is off | none |
| 2 | Widened safety guards | none |
| 3 | Measuring how far a unit has drifted, and when it is due | 1 |
| 4 | A shared, time-limited test runner and a non-interactive git setup | 1 |
| 5 | A backup before every rewrite, plus restore and clean-up of old backups | 1, 2 |
| 6 | The upgraded rebase pass | 3, 4, 5 |
| 7 | The scheduler and its health checks | 6 |
| 8 | Documentation, the switch audit and one recorded end-to-end run on a scratch remote | 7 |

**Corrections to the original description.**
- Units are not rebased "only when a goal is picked": four triggers already exist. The schedule is what is missing.
- A conflict does not stop everything: it stalls only that unit's catch-up, and changelog conflicts inside individual
  goals already resolve themselves.
- Landing is not only hand-run: chat can already land a unit without approval.
- The background watcher already runs on Windows, so the new job must refuse there explicitly.

**Not yet traced, and where it will be traced.** An independent review checked this list and moved most of it back into
part A's own steps. Each step's research looks at its items first, before the step is planned.
- **In part A, before the step named:**
  - the limit on how much text each phase loads, which decides where the rule edits can go (step 5);
  - how findings get labelled with their unit, and how a pass with no goal files them (steps 6 and 7);
  - the doctor's time-limited checks and the trust check (steps 4 and 7), and the git-version check (step 7);
  - the registry's naming checks and the clean-up rule for new records (steps 1 and 3);
  - how the job's notes are published to the team (step 7);
  - the entry points into the existing rebase engine, and their locks (steps 6 and 7);
  - whether any shared helper could start a claim by accident (steps 4, 6 and 7).
- **Already answered:** nothing reacts to an unaddressed note in the team log, and the read-marker does not stay
  bounded (see above).
- **In parts B and C:** where exact model names come from (part B), and the landing read-back helpers (part C).
