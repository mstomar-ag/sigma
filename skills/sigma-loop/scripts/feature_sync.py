#!/usr/bin/env python3
"""The pick-path registry sync -- `.sdlc/features/` reconciled on every goal pick (#1473, epic
#1464, story #1427).

`feature_registry` (#1469) is the store and `feature_doc` (#1470) is its human-readable half. Both
are pure; neither is ever called. THIS is the thing that runs, and the design's own sentence is the
whole specification of what it must do:

    live state comes from the REMOTE; history survives LOCALLY; NEITHER IS TRUSTED ALONE.

So one pass does three things, in that order: record the goal under the unit its issue declared,
cross-check what the registry CLAIMS against the branches that actually EXIST, and regenerate the
managed block from the result.

WHERE THIS RUNS, AND WHY IT IS `work.start()` RATHER THAN `loop._next`. Two call sites could be
called "the pick", and #1472 already put the cross-repo landing check on the other one. This one is
chosen for three reasons that are all about not asking a second time:

  1. `start()` has ALREADY resolved the unit. `_declared_unit` returns it as a local variable, so
     the sync is handed the answer in-process and reads no issue of its own. That is the whole of
     requirement 1 and it is satisfied structurally rather than by discipline -- there is no code
     path here that could make the read even if a later author wanted to. #1472 had to DELETE its
     own reader to reach the same property; this one never has one.
  2. `start()` is the only layer holding a git runner scoped to the project root, and the
     cross-check against real branches is a git question about a remote, not a `gh` question about
     an issue.
  3. `work._declared_unit`'s own docstring defers exactly this question -- "whether the branch
     exists at all (L2's registry)" -- from that function, on that path.

THE HONEST LIMIT OF THAT CHOICE, stated rather than discovered later: a project with `work.enabled`
false never calls `start()`, so it never syncs. That project also cuts no branches and opens no PRs,
so the branching model has nothing to record about it -- but the two facts are connected by
coincidence, not by construction, and a future goal that gives such a project feature branches has
to move this call rather than assume it followed.

ORDER MATTERS, AND IT IS THE ONE ORDERING DECISION HERE. The sync runs BEFORE `start()`'s `git
fetch`. A brand-new unit has no branch yet, and that fetch correctly fails closed on exactly that --
so a sync placed after it would miss the first goal of every new unit, which is the one goal that
creates the entry. Running first means the registry learns about the unit, the missing branch is
surfaced, and the fetch's own failure arrives as a diagnosis instead of a mystery.

--------------------------------------------------------------------------------------------------
SAME-UNIT CONCURRENCY, WHICH IS THE REAL PROBLEM THIS MODULE SOLVES

#1469 gave each unit its own shard, so two picks on DIFFERENT units are structurally incapable of
colliding. It also measured, and documented, what that does not buy: two picks on the SAME unit are
a read-modify-write with no lock, so the last writer wins the whole file. Measured again here at
real process level before writing a line of the fix -- 10 processes, one shard, one goal number
each, five runs: 5 to 6 of 10 goal numbers LOST per run. At 4 goals each: 17 to 19 of 40. Never
corrupt -- `os.replace` holds and every read parses -- just silently absent, which is worse than a
crash because nothing looks wrong.

Two goals on one unit picked at once is not an exotic case. It is what a unit of work IS, and what a
parallel drain produces by design. And a lost goal number defeats the registry's entire purpose: it
exists so that when a remote branch is deleted, what belonged to that unit is still knowable, and a
silently short list is that failure dressed as success.

THE ANSWER IS A PER-UNIT `flock`, AND THE ALTERNATIVES WERE REAL:

  - an append-only shard per writing process (the ledger's answer, one axis further) is the most
    robust of the three and was rejected on blast radius, not on merit: it would change the layout
    `read()` reads, which the 2026-08-22 amendment settled and #1477 is about to copy verbatim
    between repos. Re-opening a settled layout ruling from the goal that consumes it is the wrong
    direction of travel;
  - a compare-and-swap retry cannot be built honestly on POSIX. `os.replace` is unconditional, so
    "write only if nothing changed" has no primitive under it, and a re-read-after-write loop
    converges only probabilistically -- A can verify its own write and be clobbered by B a
    microsecond later, having already returned success;
  - `flock` is the one that is exact. It is kernel-mediated, so it has no stale-lock problem at all:
    a holder that crashes releases it, which is the property a lockfile-by-existence never has.
    `loop.py`'s claim lock is the same primitive, so this is the codebase's own idiom rather than a
    new one.

The retry loop stays anyway, as a BACKSTOP rather than the mechanism -- see `amend`. And the lock
file lives under `.sdlc/state/`, never beside the shards: `.sdlc/features/` is deliberately NOT
gitignored (a gitignored backup is not a backup), so a lock there would be untracked litter in every
adopter's `git status`, while `.sdlc/state/` is already covered by `setup.RUNTIME_IGNORES`.

WHAT THE FAIL-OPEN PATH CAN AND CANNOT DO, MEASURED, BECAUSE AN EARLIER VERSION OF THIS FILE CLAIMED
MORE THAN IT COULD KEEP. When no lock can be taken at all -- no `fcntl`, a filesystem that cannot
flock, a wedged holder that timed out -- the retry is all there is, and the objection this module
raises against CAS three paragraphs up applies to it VERBATIM: a writer can verify its own write and
be clobbered a microsecond later, having already returned. Measured on that path, 10 processes x 4
goals: 40 of 40 writes reported success and 11 to 16 of them were absent afterwards. The retry does
NARROW the loss (bare loses more), and it is exact for a write that never landed at all -- but it
cannot see a clobber that arrives after it looked.

So the honesty is structural rather than promised, in three parts:

  1. `landed` MEANS `written AND serialised` -- the only combination in which "recorded" is a fact
     rather than a likelihood. An unserialised write reports `written: True, landed: False`, which
     is the true statement: the bytes went in, and nothing can prove they stayed;
  2. a RE-READ AT THE END OF THE PASS catches the clobbers that arrived while the rest of the pass
     was running, which is most of them in practice and none of them in principle. What it finds is
     `LOST`, and `LOST` is a measurement, not an inference;
  3. both `UNSERIALISED` and `LOST` reach the operator through the clause `work.start()` prints, so
     the signal has a consumer rather than sitting in a field nobody reads.

The residual is named rather than left to be discovered: a clobber that lands after the final
re-read is invisible to this process, and no lock-free scheme on POSIX can see it. That is a reason
to have the lock, not a reason to pretend the fallback is one.

--------------------------------------------------------------------------------------------------
WHAT THIS DELIBERATELY DOES NOT DO

`index.json` IS NEVER WRITTEN ON THE PICK PATH, and this is a ruling, not an omission. #1469 left
the fold to this goal -- "whether that pass then removes the shards it folded in is #1473's call".
The call is: fold on demand (`fold()`, the CLI verb), never on a pick, and NEVER remove a shard.

  - not on a pick, because `index.json` is the one file every unit would share, and the whole point
    of the amendment was to stop concurrent picks rewriting one file. Folding it back in on every
    pick would restore exactly the merge conflicts on exactly the file that exists to be a backup;
  - never removing a folded shard, because a shard written by a concurrent process between this
    pass's `read` and its delete would be deleted having never been folded. `read`'s union is
    already correct with both present, so removal buys tidiness and risks the data.

Cross-repo propagation (#1477) and ownership enforcement (#1479) are not here either. This records
the goal, reconciles against reality, and stops.

Module shape follows `feature_registry.py` and `feature_doc.py`: zero third-party dependencies,
module-level constants, loaded by siblings via `_load("feature_sync")`.
"""
import copy
import importlib.util
import json
import os
import pathlib
import random
import re
import sys
import time

try:
    import fcntl                          # POSIX only -- see `_acquire`
except ImportError:                       # pragma: no cover - Windows; the fail-open path is tested
    fcntl = None

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


registry = _load("feature_registry")     # the store: schema, read, write_unit, write_index
doc = _load("feature_doc")               # the managed block in <name>.md
features = _load("features")             # BRANCH_PREFIX, and the one rule for a unit name
ledger = _load("ledger")                 # team record (config-gated, default OFF; fail-open)

#: WHY `except ValueError` AND NOT `except registry.InvalidUnitName` ANYWHERE BELOW. `_load` executes
#: a fresh module OBJECT on every call, so the `InvalidUnitName` raised by `doc`'s own copy of
#: `feature_registry` is a DIFFERENT class from the one `registry` (this module's copy) raises, and
#: neither `except` catches the other. Both are `ValueError` subclasses precisely so the broad form
#: holds across instances -- `feature_doc` re-exports the name for the same reason. The rule
#: generalises: never compare classes, compiled patterns or sentinels by identity across modules
#: loaded through `_load`.

#: Where the per-unit locks live: `.sdlc/state/features/<name>.lock`. `state/` and not `features/`
#: -- see the module docstring; `setup.RUNTIME_IGNORES` already covers the former and deliberately
#: does not cover the latter.
LOCK_DIRNAME = "features"
LOCK_SUFFIX = ".lock"

#: How long a pick will wait for another process's write to finish. A BOUND rather than a blocking
#: `flock`: a wedged holder must cost this pick its serialisation and nothing else, because a pick
#: that hangs forever on a lock is a worse outcome than the loss the lock exists to prevent.
LOCK_TIMEOUT = 10.0
LOCK_POLL = 0.005

#: How many times a write that did not land is retried. See `amend` for what "did not land" means
#: and why the retry is a backstop rather than the mechanism.
WRITE_ATTEMPTS = 4

#: The refspec pattern the live branch set is fetched with, and the prefix `ls-remote` prints.
#: Server-side filtering, so the answer is small on a repo with thousands of branches -- and the
#: pattern is why `reconcile` refuses to judge a recorded branch outside the `feature/` namespace:
#: a question that was never asked has no answer to draw a conclusion from.
BRANCH_PATTERN = features.BRANCH_PREFIX + "*"
REF_PREFIX = "refs/heads/"

#: The remote a direct caller gets when it names none. `work.start()` passes its own `s["remote"]`
#: in rather than letting this fire -- re-deriving `work.settings` here is not merely duplication,
#: it is impossible: `work.py` loads THIS module, so loading it back would recurse forever through
#: `_load`, which has no `sys.modules` cache to break the cycle.
DEFAULT_REMOTE = "origin"

#: The outcome of one pass.
NOT_ADOPTED = "not-adopted"       # no `.sdlc/features/` -- this project has not adopted the model
SYNCED = "synced"                 # the pass ran; see `divergences` for what it found
FAILED = "failed"                 # something went wrong; nothing here is claimed
OUTCOMES = (NOT_ADOPTED, SYNCED, FAILED)

#: What a pass can find. Each is a fact about the registry disagreeing with reality, with itself, or
#: with what this pass was able to establish.
BRANCH_MISSING = "branch-missing"          # the entry NAMED a branch and it is not there any more
BRANCH_ABSENT = "branch-absent"            # the picked unit's branch does not exist yet
CLOSED = "closed"                          # rule 5: no branch and no goals -> `open: false`
PICKED_WHILE_CLOSED = "picked-while-closed"  # a goal was picked onto a unit somebody closed
BLOCK_DIVERGED = "block-diverged"          # a hand-edit inside the managed block was overwritten
NO_REPO = "no-repo"                        # no slug resolved, so no goal could be filed anywhere
REMOTE_UNREADABLE = "remote-unreadable"    # the live branch set could not be read; nothing was judged
SHARD_UNREADABLE = "shard-unreadable"      # #1565: the unit's own file is there and cannot be read
LEGACY_DELTA_CONFLICT = "legacy-delta-conflict"  # #327: a write would drop a newer legacy record's value
LEGACY_DELTA_DISCARD = "legacy-delta-discard"    # #327: a write replaced an older legacy record, dropping values
UNSERIALISED = "unserialised"              # a write went in without a lock, so it cannot be PROVED
LOST = "lost"                              # re-read at the end of the pass, and the goal was gone
SCOPE_EXPANSION = "scope-expansion"        # #1477: the pick's repo is not one this unit names
DIVERGENCES = (BRANCH_MISSING, BRANCH_ABSENT, CLOSED, PICKED_WHILE_CLOSED, BLOCK_DIVERGED, NO_REPO,
               REMOTE_UNREADABLE, UNSERIALISED, LOST, SCOPE_EXPANSION, LEGACY_DELTA_CONFLICT,
               LEGACY_DELTA_DISCARD)

#: WHICH DIVERGENCES REACH THE LEDGER, AND THE RULE BEHIND THE LIST: only the ones that record a
#: STATE CHANGE this pass made. Those are one-shot by construction -- a branch is reconciled away
#: once, a unit is closed once -- so an owner hears about each exactly once.
#:
#: The ones left off are left off for a reason each. `BRANCH_ABSENT` is the ORDINARY state of the
#: first pick of every new unit (the branch is cut after, not before), so ledgering it would put a
#: note in front of an owner for the normal case, which is how a channel gets ignored.
#: `PICKED_WHILE_CLOSED`, `NO_REPO`, `REMOTE_UNREADABLE` and `UNSERIALISED` are visible to whoever is
#: running the pick, on stderr and in the clause `start()` prints. `BLOCK_DIVERGED` is already
#: ledgered by `feature_doc._tell`, addressed to the same owner -- writing a second entry for one
#: event would be this module double-counting it.
#:
#: `LOST` IS THE ONE ADDITION THAT IS NOT A CHANGE THIS PASS MADE, and it earns the exception: it is
#: a goal number this pass wrote and then MEASURED to be gone, which is the exact failure the whole
#: registry exists to prevent. It is also rare by construction -- it can only happen on the
#: unserialised path -- so it cannot become noise.
#: `SCOPE_EXPANSION` IS THE OTHER EXCEPTION TO "a change this pass made", and it earns it the
#: opposite way round: it is a change this pass REFUSED to make, and the refusal is only useful if
#: the one person who could authorise it hears about it. Left off, a goal would sit unrecorded with
#: nothing anywhere naming why (#1477, §7.1).
#: `LEGACY_DELTA_DISCARD` (#327) IS A STATE CHANGE THIS PASS MADE, and one-shot by construction:
#: the record it replaced is in Sigma's schema afterwards, so it is never a delta again. The values
#: it dropped were the previous plugin's post-conversion edits, which the unit's owner may still
#: want -- so the owner hears about it, not only whoever ran the pick.
LEDGERED = (BRANCH_MISSING, CLOSED, LOST, SCOPE_EXPANSION, LEGACY_DELTA_DISCARD)

#: `owner/name`, the shape a registry repo key and a `gh` `--repo` argument both have. Borrowed in
#: SPIRIT from `cross_repo._REPO_RE` and not by import: `cross_repo` loads `work`, `work` loads this
#: module, and `_load` has no cycle-breaking cache -- so importing it back would recurse forever.
#: The one thing this must get right is refusing `{owner}/{repo}`, `gh`'s own placeholder, which
#: would otherwise become a literal registry key and a literal branch name.
_SLUG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _note(message):
    """One stderr line, never an exception -- the shape `features._note` and `feature_registry._note`
    both hold, for the same reason: a diagnostic must never be the thing that breaks a pick."""
    try:
        sys.stderr.write(message)
    except Exception:                     # noqa: BLE001 - a diagnostic must never break a pick
        pass


def _run(cwd, argv):
    """The default runner, matching `work._run`'s `(cwd, argv) -> stdout` contract exactly: raise on
    a non-zero exit, because every caller here reads an exception as "the question went unanswered"
    and a silent empty string as "the answer is nothing". Collapsing those two is what turns an
    unreachable remote into "every branch was deleted"."""
    import subprocess                     # local: the injected runner is the real path; this is the
    proc = subprocess.run([str(a) for a in argv], cwd=str(cwd),   # fallback for a direct CLI caller
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or
                           "%s exited %s" % (argv[0], proc.returncode))
    return (proc.stdout or "").strip()


# --------------------------------------------------------------------------- the lock


def lock_path(sdlc_dir, name):
    """`.sdlc/state/features/<name>.lock` -- the one place a unit NAME becomes a LOCK path.

    Refuses the same names `feature_registry.unit_path` refuses, through the same predicate and with
    the same exception type, because this path is derived from the same string and an escape here
    would be an escape there. It is a separate function rather than a reuse of `unit_path` only
    because the two live in different directories on purpose."""
    if not (isinstance(name, str) and registry.is_unit_name(name)):
        raise registry.InvalidUnitName(
            "%r is not a unit name, so it cannot be locked -- a unit name is one git branch segment"
            % (name,))
    # #1566: folded, for the same reason and by the same rule as `feature_registry.unit_path` --
    # same string in, same address out. This half matters more than the shard: two shards lose data,
    # but two LOCKS lose the serialisation the shard write depends on, so neither casing excludes the
    # other and both read-modify-write the unit at once.
    return pathlib.Path(sdlc_dir) / "state" / LOCK_DIRNAME / (name.lower() + LOCK_SUFFIX)


def _acquire(path, timeout=LOCK_TIMEOUT):
    """An exclusive `flock` on `path` -> the open fd, or None if it could not be taken.

    FAIL-OPEN, EXACTLY LIKE `loop._try_acquire_claim_lock`, and for the same reason: a platform with
    no `fcntl` (Windows), a filesystem that cannot flock, or a directory this process cannot write
    must cost SERIALISATION, never the write. The caller is told which it got, so "recorded" is
    never silently confused with "recorded safely".

    BOUNDED, NOT BLOCKING. `LOCK_EX` without `LOCK_NB` would wait forever on a wedged holder; a
    spin with a deadline gives up and lets the caller proceed unserialised, which is strictly better
    than a pick that never returns. Contrast `loop.py`, which gives up IMMEDIATELY -- there, losing
    the race means another process owns the goal and this one should move on; here it means another
    process is mid-write on the same unit and this one should wait its turn.

    The fd is the lock. Closing it is what releases it, and the FILE is deliberately left behind for
    the next acquisition -- deleting it would reopen a create/delete race that `flock` otherwise
    does not have."""
    if fcntl is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    except (OSError, ValueError):
        return None                       # ValueError: an embedded NUL raises before any OSError can
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(LOCK_POLL)


def _release(fd):
    """Best-effort. Releasing must never raise into a pick."""
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:                       # noqa: S110 - a failed close is not worth a failed pick
        pass


# --------------------------------------------------------------------------- the serialised write


def amend(sdlc_dir, name, mutate, timeout=LOCK_TIMEOUT):
    """Read one unit's entry, let `mutate` change it, write it back. -> a report.

    THE WHOLE READ-MODIFY-WRITE HAPPENS UNDER THE UNIT'S OWN LOCK, which is the entire point of this
    function and the answer to the loss #1469 measured. `mutate` is handed a NORMALISED copy of the
    effective entry (`read`'s union, so a fresh clone carrying only `index.json` amends the sheet's
    entry rather than erasing it) and mutates it in place. The WHOLE entry goes back -- never a
    delta, which `write_unit` documents as the caller's non-optional obligation, since `read`
    replaces a unit's entry with its shard rather than merging the two.

    NOTHING IS WRITTEN WHEN NOTHING CHANGED. A re-pick recording a goal already recorded produces an
    identical entry, and rewriting it would make every pass a diff in a directory that is committed
    on purpose. The comparison is on the NORMALISED forms, which is also what makes de-duplication
    free: `_goals` already de-dupes and coerces `"2879"` to `2879`, so `mutate` may append blindly
    and the "did anything change?" question answers itself correctly.

    THE RETRY IS A BACKSTOP, NOT THE MECHANISM, and the distinction is worth being exact about. With
    the lock held the write cannot be clobbered mid-section, so the verify never fires. It exists
    for the fail-open case -- no `fcntl`, an unlockable filesystem, a wedged holder that timed out,
    or a writer from a version of this plugin that predates the lock -- where it converts a silent
    loss into a retried write. It is BOUNDED, because an adversarial or simply broken co-writer must
    not be able to spin a pick forever.

    AND IT IS NOT A GUARANTEE, WHICH THIS DOCSTRING USED TO SAY IT WAS. The verify reads back a write
    it has just made; a co-writer holding stale state can clobber that write a microsecond later,
    after the read and before this function returns. That is the same objection the module docstring
    uses to reject compare-and-swap, and it applies here unchanged -- measured on the fail-open path
    at 40 of 40 writes reporting success with 11 to 16 of them absent afterwards. The verify is exact
    for a write that never landed AT ALL (a sabotaged `write_unit` reports `landed: False` after
    `WRITE_ATTEMPTS`); it is blind to a clobber that arrives after it looked.

    SO THE REPORT SEPARATES THE TWO CLAIMS RATHER THAN CONFLATING THEM:

      - `written` -- the shard on disk said what we wrote, at the moment we looked. Provable, and
        this is what "the goal was recorded" is entitled to rest on;
      - `serialised` -- the read-modify-write happened under an exclusive lock;
      - `landed` -- `written and serialised`, and NOTHING ELSE. It is the only field that means
        "recorded, and nothing could have taken it away", so it is the only one that must never
        overstate. On the fail-open path it is False even when the write plainly went in, because
        False there is the true statement.

    `sync_at_pick` re-reads the shard at the END of the pass for any unserialised write and reports
    `LOST` for a goal that is no longer there. That is a measurement rather than an inference, and it
    catches the clobbers that arrive while the rest of the pass runs -- not the ones that arrive
    after it, which nothing lock-free can catch.

    A LEGACY DELTA IS NEVER REWRITTEN SILENTLY (#327). When `name`'s own file is a record in the
    previous plugin's schema read as a delta (`feature_registry.legacy_delta_record`), the write
    replaces it with Sigma's -- and drops each record value the entry already had another value
    for. `_delta_verdict` is the rule, shared with `repair`: every such value is printed and put in
    `discarded`; and when there is one and the record is at least as new as `index.json` (an edit
    that plugin made after the conversion), nothing is written -- `refused` says why and
    `delta_conflict` is True.

    RAISES `InvalidUnitName` on a name that is not a unit name (via `lock_path`), and whatever the
    filesystem raises on a write that genuinely failed. `sync_at_pick` is the layer that turns those
    into a report; a direct caller gets the exception, the same division `feature_registry.write_unit`
    and `feature_doc.sync` both draw."""
    features_dir = registry.registry_dir(sdlc_dir)
    fd = _acquire(lock_path(sdlc_dir, name), timeout)
    report = {"name": name, "entry": None, "changed": False, "existed": False, "written": False,
              "landed": False, "serialised": fd is not None, "attempts": 0, "path": None,
              "refused": None, "delta_conflict": False, "discarded": []}
    try:
        # #1565: AN UNREADABLE SHARD IS NOT A SHARD WE MAY REPLACE.
        #
        # `registry.read` drops an unreadable shard's unit outright -- deliberately, and it argues for
        # it. But that makes `known.get(name)` below return nothing, so `before` becomes `{}`, and
        # `write_unit` REPLACES the file: title, owner, tracking issue, every hand-written
        # `authorized` grant, every sibling repo and every recorded goal, gone, with the report saying
        # it synced. §12 requires humans to hand-edit this file, so malformed JSON is precisely what
        # hand-editing produces -- and there is no committed pre-image to restore from, because
        # nothing stages `.sdlc/features/`.
        #
        # `feature_propagate._their_entry` already refuses on the REMOTE side, "whenever a file is
        # there and does not yield this unit". Same rule, applied locally, using the same discriminator
        # -- the file's EXISTENCE, which is what tells an unreadable shard apart from an absent one.
        #
        # REFUSED IN THE REPORT RATHER THAN RAISED. A raise here reaches a broad outer guard that
        # returns "" for the whole pass, which would make this silent -- and a guard whose whole
        # subject is silent data loss must not itself be silent.
        shard = registry.unit_path(features_dir, name)
        if shard.exists() and registry.read_unit(features_dir, name) is None:
            report["refused"] = (
                "unit %s has a record at %s that cannot be read, so it was left exactly as it is "
                "rather than replaced by a one-goal stub. Repair or delete that file; nothing else "
                "about this unit can be recorded until then." % (name, shard))
            return report

        for attempt in range(1, WRITE_ATTEMPTS + 1):
            report["attempts"] = attempt
            known = registry.read(features_dir)
            report["existed"] = name in known
            before = registry.normalise_entry(known.get(name) or {})
            entry = copy.deepcopy(before)
            mutate(entry)
            entry = registry.normalise_entry(entry)
            report["entry"] = entry
            if entry == before:
                # NOTHING TO WRITE IS NOT AN UNPROVEN WRITE. There is no window for a clobber to
                # arrive in, so this is `written` in the only sense that applies, and `landed`
                # follows the same rule as every other path: it needs the lock to be a fact.
                report["written"] = True
                break
            # #327: a legacy delta record is rewritten through ONE rule (`_delta_verdict`), the
            # same one `repair` applies -- never by a pick that silently drops what it carries.
            verdict = _delta_verdict(features_dir, name, entry)
            if verdict is not None and verdict[1] and verdict[2]:
                report["refused"] = _conflict_text(features_dir, verdict)
                report["delta_conflict"] = True
                _note("sigma: features: %s\n" % report["refused"])
                break
            try:
                report["path"] = registry.write_unit(features_dir, name, entry)
            except registry.LegacyDeltaConflict as exc:
                # `write_unit` applies the same rule itself: the record changed since the verdict
                # above (the previous plugin takes no Sigma lock). Same refusal, never a raise.
                report["refused"] = str(exc)
                report["delta_conflict"] = True
                _note("sigma: features: %s\n" % report["refused"])
                break
            # THE VERIFY IS A READ OF THE SHARD, not of `read`'s union: the shard is what this write
            # produced, and it is the only thing this write can be held to. `read_unit` returns the
            # normalised entry, and `entry` is already normalised, so equality here is exactly "the
            # bytes on disk still say what we wrote" -- AT THE MOMENT OF THE READ, which is the whole
            # of what it establishes.
            if registry.read_unit(features_dir, name) == entry:
                report["written"] = True
                report["changed"] = True
                if verdict is not None and verdict[1]:
                    report["discarded"] = verdict[1]
                    _note("sigma: features: unit %s: rewrote %s (a record in the previous plugin's "
                          "schema, read as a delta) in Sigma's schema, and discarded what it held "
                          "that %s already had a value for: %s\n"
                          % (name, verdict[0], registry.INDEX_NAME, "; ".join(verdict[1])))
                break
            time.sleep(random.uniform(0, LOCK_POLL))
    finally:
        _release(fd)
    # `landed` IS `written and serialised`, and it is computed HERE, once, rather than assigned at
    # each break -- so no future branch can set it without going through the rule. Without the lock,
    # a write that was observed on disk still cannot be shown to have stayed there.
    report["landed"] = report["written"] and report["serialised"]
    return report


# --------------------------------------------------------------------------- reality


#: How far the reason for an unreadable remote is quoted. Bounded and flattened for `work
#: ._READ_NOTE_CHARS`'s reason: git's stderr is multi-line and this text is spliced into a ONE-LINE
#: result an agent reads back.
_REASON_CHARS = 120


def live_branches(run, cwd, remote):
    """The `feature/*` branches that ACTUALLY EXIST on `remote` -> `(branches, reason)`.

    `branches` is a frozenset, or None when the remote could not be read; `reason` is "" on success
    and the flattened error text otherwise. EXACTLY ONE OF THEM IS SET, which is the same
    `(value, note, ok)` shape `work._declared_unit` uses one function along on this very pick path,
    and for the identical reason it gives: "the read failed" is NOT the same fact as "the answer is
    nothing", and a function that returns only the value forces its caller to collapse them.

    `None` AND `frozenset()` ARE NOT THE SAME ANSWER AND MUST NEVER BE COLLAPSED. `None` is "the
    remote could not be read"; the empty set is "the remote was read and has no feature branches".
    Reading the first as the second is the single most destructive bug available here -- it deletes
    every recorded branch and closes every unit, in one pass, silently, on a laptop that happened to
    be offline. Every caller checks for `None` first, and `reconcile` refuses outright.

    THE REASON IS RETURNED RATHER THAN SWALLOWED, and that is a correction: this used to `except`
    into a bare `None`, so a pass that cross-checked NOTHING was indistinguishable from a pass that
    cross-checked everything and found it all correct -- while still reporting `outcome: "synced"`,
    which actively asserts the second. The registry's own definition of done requires a divergence
    to be "surfaced rather than silently trusted", and on that path local was trusted alone, in
    silence. `_sync_at_pick` turns this reason into a `REMOTE_UNREADABLE` divergence.

    `ls-remote`, not `for-each-ref`: the question is about the REMOTE's branches, and a local
    remote-tracking ref is a cache of the last fetch, which is precisely the stale local state this
    pass exists to reconcile against. The pattern filters server-side, so the reply stays small on a
    repo with thousands of branches -- and it is also the reason `reconcile` will not judge a
    recorded branch outside the `feature/` namespace."""
    try:
        out = run(cwd, ["git", "ls-remote", "--heads", remote, BRANCH_PATTERN])
    except Exception as exc:              # noqa: BLE001 - not knowing is never "there are none"
        return None, (" ".join(str(exc).split())[:_REASON_CHARS] or exc.__class__.__name__)
    names = set()
    for line in str(out or "").splitlines():
        ref = line.split("\t")[-1].strip()
        if ref.startswith(REF_PREFIX):
            names.add(ref[len(REF_PREFIX):])
    return frozenset(names), ""


def _slug_from_url(url):
    """`owner/name` out of a git remote URL, or None.

    A faithful port of `setup.detect_repo`'s parse, including the `github.com-<alias>:owner/repo`
    host-alias form, and a port rather than a call for one structural reason: `setup.py` lives in
    `skills/sigma-setup/scripts/`, and `_load` resolves siblings of THIS directory only. The
    duplication is named here so a later change to either is looked for in both.

    The result is validated, which the original does not do, because here it becomes a registry KEY
    and a filename rather than a display string."""
    if not url:
        return None
    for sep in ("github.com:", "github.com/"):
        if sep in url:
            url = url.split(sep, 1)[1]
            break
    else:
        if ":" in url and "/" in url:     # host-alias, e.g. github.com-work:owner/repo
            url = url.split(":", 1)[1]
    if url.endswith(".git"):
        url = url[:-4]
    parts = [p for p in url.split("/") if p]
    slug = "/".join(parts[-2:]) if len(parts) >= 2 else ""
    return slug if _SLUG_RE.match(slug) else None


def slug_from_remote_url(url):
    """Public wrapper over the validated slug parser: `owner/name` from a git remote URL, or None."""
    return _slug_from_url(url)


def repo_slug(config, run, cwd, remote):
    """Which repo this goal belongs to -> `owner/name`, or None.

    `discovery.github.repo` FIRST, because that is where the goal NUMBER came from: the issue was
    read from that repo, so that is the repo the goal is in, and `_declared_unit` makes the same
    call for the same reason. A checkout that is a fork of somewhere else does not change which
    board filed the work.

    THE FALLBACK IS THE REAL INSTALL PATH, NOT A THEORETICAL ONE -- the config template ships `repo`
    empty. `gh`'s `{owner}/{repo}` placeholder is what `_declared_unit` substitutes there, and it is
    the one thing this must refuse: unlike a `gh` argument, a registry key is written to disk, so a
    placeholder would become a literal key, a literal `.md` filename and a literal branch name that
    nothing could ever reconcile. `_SLUG_RE` rejects it, and the local git remote answers instead --
    locally, with no network call, which is why it is the fallback rather than `gh repo view`."""
    discovery = config.get("discovery") if isinstance(config, dict) else None
    github = discovery.get("github") if isinstance(discovery, dict) else None
    declared = github.get("repo") if isinstance(github, dict) else None
    if isinstance(declared, str) and _SLUG_RE.match(declared.strip()):
        return declared.strip()
    try:
        url = str(run(cwd, ["git", "remote", "get-url", remote]) or "").strip()
    except Exception:                     # noqa: BLE001 - no remote at all is simply no answer
        return None
    return _slug_from_url(url)


# --------------------------------------------------------------------------- the reconcile


def _div(kind, unit, repo, detail):
    return {"kind": kind, "unit": unit, "repo": repo, "detail": detail}


def _is_gone(branch, live):
    """Is this RECORDED branch one the measurement proved absent?

    Three conditions, and the middle one is the whole point. The live set was fetched with a
    `feature/*` pattern, so it CANNOT contain `main` -- measuring a recorded `main` against it would
    report a deleted branch on the strength of a question that was never asked. The guard sits on
    the RECORDED value rather than on the answer, because that is where the mismatch actually is."""
    return (isinstance(branch, str)
            and branch.startswith(features.BRANCH_PREFIX) and branch not in live)


def reconcile(entry, name, repo, live):
    """Bring ONE normalised entry into line with the branches that exist. -> the divergences found.

    Mutates `entry` in place and returns what it changed, which is the shape a caller needs to both
    write it back and report it.

    NOTHING IS RECONCILED AGAINST IGNORANCE. `live is None` means the remote could not be read, and
    the only correct action on an unanswered question is none at all.

    ONLY THIS REPO'S HALF IS JUDGED. §7.1 requires every participating repo to hold the WHOLE entry,
    branches on remotes this checkout has never heard of included. `ls-remote` answered about ONE
    remote, so a sibling repo's branch is UNMEASURED -- and unmeasured is not "gone". That is also
    what keeps discovery one-directional (§12.2): this pass can only ever narrow its own end.

    AND ONLY INSIDE THE `feature/` NAMESPACE. The live set was fetched with a `feature/*` pattern,
    so it CANNOT contain `main` -- and measuring a recorded `main` against it would report a deleted
    branch on the strength of a question that was never asked. The guard is on the recorded value,
    not on the answer, because that is where the mismatch actually is.

    RULE 5 IS AN `AND`, AND THE SECOND HALF IS THE LOAD-BEARING ONE. "No branch and no open goals"
    closes a unit; a unit whose branch merged away but whose goals are recorded is exactly what the
    backup exists to preserve, and closing it would be the registry discarding the one fact it was
    built to keep. "Open goals" is read as "goals recorded", deliberately and with the gap named:
    knowing whether issue 2871 is still open costs one network read per goal per pick, which this
    path will not spend. The approximation errs towards leaving a unit open, which is the direction
    #1469 already chose for the same field -- closing is what makes a unit's goals stop being
    recorded, so the safe guess is that nobody closed anything.

    `open` IS NEVER SET BACK TO TRUE HERE. Only a human (or `open`'s own default) opens a unit;
    a pick that disagrees with a closure is reported by `_record_goal`, not adjudicated."""
    if live is None:
        return []
    found = []
    repos = entry.get("repos") or {}
    mine = repos.get(repo_key(repos, repo)) if isinstance(repo, str) else None
    if isinstance(mine, dict):
        branch = mine.get("branch")
        if _is_gone(branch, live):
            mine["branch"] = None
            found.append(_div(BRANCH_MISSING, name, repo, branch))
    branches = [one.get("branch") for one in repos.values()
                if isinstance(one, dict) and one.get("branch")]
    goals = [g for one in repos.values() if isinstance(one, dict) for g in (one.get("goals") or [])]
    if entry["open"] and not branches and not goals:
        entry["open"] = False
        found.append(_div(CLOSED, name, repo, ""))
    return found


def same_repo(a, b):
    """Are these two strings the same GitHub repository? CASE-INSENSITIVELY (#1477 F2).

    `owner/name` is case-insensitively unique on GitHub -- `Org/Repo` and `org/repo` cannot both
    exist -- so two casings were never two repos, exactly as `feature_registry._read_unit_file`
    already rules for two casings of a unit NAME, in this same subsystem and for this same reason.

    IT MATTERS HERE MORE THAN IT DID BEFORE THIS GOAL, and that is why it is a function rather than
    a `.lower()` at each site. `repos` keys used to be written by the machine (`_record_goal`'s old
    unconditional `setdefault`), so both sides of every comparison came from one derivation. This
    goal STOPS the machine widening `repos`, which makes every key after the first a HUMAN-TYPED
    one -- exact-matched against a slug derived from config or from a git remote. A capitalisation
    difference then reads as a different repo, and the consequences are no longer cosmetic: the
    pick is refused as a scope expansion and the goal is made inert."""
    return isinstance(a, str) and isinstance(b, str) and a.lower() == b.lower()


def repo_key(repos, repo):
    """The key `repos` ALREADY uses for this repo, else `repo` itself.

    The write-side half of `same_repo`: having decided that two casings are one repo, a write must
    land on the key that is already there rather than minting a second one beside it. Returns the
    EXISTING spelling because that is what its author wrote -- `_read_unit_file`'s ruling again."""
    if not isinstance(repos, dict):
        return repo
    for key in repos:
        if same_repo(key, repo):
            return key
    return repo


def is_scope_expansion(entry, repo):
    """Would recording a goal for `repo` WIDEN this unit into a repo its owner never named? (#1477)

    THE PREDICATE IS ONE DEFINITION WITH TWO CALLERS, and they sit in different layers on purpose:
    this module refuses the WRITE (reached by a bare `work.py start` as well as by the loop), and
    `feature_propagate.gate_at_pick` refuses the PICK (which needs a backlog source and therefore
    only exists inside the loop). Either alone leaves a hole; two spellings of the question would
    leave a worse one.

    AN ENTRY NAMING NO REPOS IS NOT AN EXPANSION, and that line is the whole of the judgement here.
    `repos` is filled by propagation, so an empty block is the ordinary state of every freshly
    recorded unit -- `cross_repo.decide` groups exactly that case as "not cross-repo" rather than
    flagging it, for the same reason -- and treating it as a positive claim would make the FIRST
    pick of every new unit inert, which is the one pick that creates the entry. An entry naming
    `a/b` and not `c/d` HAS made a statement about scope, and §7.3 makes widening it the unit
    owner's decision rather than a pick's.

    A repo that could not be resolved (`repo is None`) is NOT an expansion either: `_record_goal`
    already has `NO_REPO` for that, and reporting the same gap twice under two names would tell an
    owner about a scope decision nobody was trying to take."""
    repos = (entry or {}).get("repos") if isinstance(entry, dict) else None
    if not isinstance(repos, dict) or not repos:
        return False
    return isinstance(repo, str) and not any(same_repo(key, repo) for key in repos)


def _record_goal(entry, name, repo, goal, live):
    """Record `goal` under `name`'s entry for `repo`. -> the divergences found. Mutates in place.

    NOTHING IS RECORDED WHEN THIS REPO IS NOT ONE THE UNIT NAMES (#1477, §7.1/§12.2). Discovery
    flows registry -> repos and never repo -> registry: `setdefault` here USED to add the picking
    repo to `repos` unconditionally, which is precisely the silent registry edit the one-directional
    rule forbids -- any issue in any repo could have widened a unit into a codebase its owner never
    agreed to touch, through the one field the registry exists to be authoritative about. The
    divergence is reported and reaches the unit owner through the ledger; the entry is untouched.

    THE GOAL IS APPENDED BLINDLY, AND THAT IS CORRECT. `feature_registry._goals` de-duplicates in
    first-seen order and coerces `"2879"` to `2879`, and #1469's docstring says why in as many
    words -- "de-duplicated because #1473 re-picks a goal onto a unit it is already recorded under".
    Re-deriving that rule here would be a second opinion about what a goal number is; `amend`'s
    normalised before/after comparison then makes idempotence fall out rather than be arranged.

    THE BRANCH IS FILLED IN ONLY WHEN THERE IS NOTHING THERE AND IT WAS MEASURED, and both halves of
    that are deliberate:

      - only when EMPTY, because overwriting a recorded branch with the one this unit's name implies
        would be a silent registry edit over somebody's hand-written value -- §12.2's ruling, that
        the registry narrows its own end and never rewrites what it did not establish. A recorded
        branch is `reconcile`'s business, not this function's;
      - only when MEASURED, because with `live` unknown, writing `feature/<name>` would be the
        registry asserting a branch exists on the strength of a naming convention. That is trusting
        local alone, which the design's own sentence forbids.

    With it known and the branch absent the entry keeps saying nothing and the absence is surfaced:
    that is the ordinary first pick of a new unit, and it is also precisely what `start()`'s next
    `git fetch` is about to fail on."""
    found = []
    if is_scope_expansion(entry, repo):
        # REPORTED AND NOT RECORDED. The goal number is the `detail` because the wording is about
        # THIS goal arriving from an unlisted repo, and `_surface` addresses it to the unit's owner
        # -- the only person §7.3 lets widen the unit.
        return [_div(SCOPE_EXPANSION, name, repo, str(goal))]
    # THE KEY THAT IS ALREADY THERE, never a second casing beside it (#1477 F2). `same_repo` has
    # just decided this repo IS one the unit names; writing under our own spelling would then add
    # the duplicate key that decision exists to deny.
    mine = entry["repos"].setdefault(repo_key(entry["repos"], repo),
                                     {"branch": None, "owner": None, "authorized": False,
                                      "goals": []})
    mine["goals"] = list(mine.get("goals") or []) + [goal]
    branch = features.BRANCH_PREFIX + name
    if live is not None and not mine.get("branch"):
        if branch in live:
            mine["branch"] = branch
        else:
            found.append(_div(BRANCH_ABSENT, name, repo, branch))
    if entry.get("open") is False:
        # Somebody closed this unit and somebody else picked a goal onto it. Two humans disagree,
        # and the registry's job is to say so rather than to settle it by overwriting one of them.
        found.append(_div(PICKED_WHILE_CLOSED, name, repo, ""))
    return found


# --------------------------------------------------------------------------- the ledger edge


def _tell(sdlc_dir, name, goal, why, to=None):
    """One stderr line always, one ledger entry when the ledger is on -- the same shape and the same
    reasoning as `feature_doc._tell`, including the capped, `.sdlc`-relative `ref` (`ledger.append`
    truncates `ref` at 120 characters from the left, which is the end that matters least).

    `kind="note"`, never `handoff`: `backlog_check._ledger_signals` reads a hand-off as a real block,
    so reporting a reconciled branch that way would PARK the very goal whose pick just found it."""
    _note("sigma: features: %s\n" % why)
    fields = {"why": why, "area": registry.REGISTRY_DIRNAME,
              "ref": "%s/%s" % (registry.REGISTRY_DIRNAME, name)}
    if isinstance(to, str) and to.strip():
        fields["to"] = to
    ledger.safe_append(sdlc_dir, "note", goal, **fields)


#: ONE SENTENCE PER DIVERGENCE. Every kind gets wording whether or not it is `LEDGERED`, because
#: the two questions are separate: this says WHAT was found, `LEDGERED` decides who is told.
#:
#: THIS COMMENT USED TO CALL THE TABLE TOTAL, AND THE ARGUMENT FOR SAYING SO WAS ITSELF THE BUG
#: (#1645) -- recorded rather than deleted, because it is the reason the code below reads this
#: table the way it does. The claim was that a partial table is self-correcting: widen `LEDGERED`
#: over a kind with no row and the `KeyError` fails visibly instead of quietly. It failed visibly
#: in the wrong place. #1565 added `SHARD_UNREADABLE` and no row, and the `KeyError` surfaced out
#: of `_surface`'s LOOP -- so the pass reported `failed`, and every other divergence found on that
#: pass was dropped along with the shard path and the repair gesture the operator needed.
#:
#: SO NOTHING RESTS ON TOTALITY ANY MORE. `_surface` reads this with `.get()` and degrades an
#: unworded kind to `<kind>: <detail>`, which costs one ugly line and loses nothing else. Add the
#: row anyway: the fallback is a floor under a mistake, not a substitute for saying what was found.
#:
#: `DIVERGENCES` ABOVE IS NOT THE SET TO CHECK A NEW KIND AGAINST. It is missing `SHARD_UNREADABLE`,
#: which two call sites emit, and nothing in the tree reads the tuple -- so it can go stale without
#: anything noticing, which is exactly what it did. `tests/test_docs.py` measures the kinds this
#: module actually hands `_div`, and that is the set a new row is owed to.
_WORDING = {
    SHARD_UNREADABLE: ("unit %(unit)s has a record on disk that cannot be read, so nothing about it "
                       "was recorded. %(detail)s"),
    LEGACY_DELTA_CONFLICT: ("unit %(unit)s was not written, so nothing about it was recorded: "
                            "%(detail)s"),
    LEGACY_DELTA_DISCARD: ("unit %(unit)s: its record in the previous plugin's schema (read as a "
                           "delta, older than index.json) was rewritten in Sigma's schema, and "
                           "these values it held were DISCARDED because index.json already had "
                           "another: %(detail)s"),
    BRANCH_MISSING: "the registry recorded %(detail)s for unit %(unit)s in %(repo)s, and that "
                    "branch no longer exists on the remote -- the entry no longer claims it, and "
                    "every goal recorded against it is kept",
    CLOSED: "unit %(unit)s has no branch and no recorded goals, so it is marked open: false -- "
            "the entry and its file are kept, because the registry is a record, not a cache",
    BRANCH_ABSENT: "unit %(unit)s is recorded, but %(detail)s does not exist on the remote yet, so "
                   "the entry claims no branch",
    PICKED_WHILE_CLOSED: "a goal was picked onto unit %(unit)s, which is marked open: false -- the "
                         "goal is recorded and the unit is left closed, because reopening it is "
                         "somebody's decision, not this pass's",
    NO_REPO: "no `owner/name` could be resolved from `discovery.github.repo` or from the `%(detail)s`"
             " remote, so goal work on unit %(unit)s was not recorded anywhere. Set "
             "discovery.github.repo",
    BLOCK_DIVERGED: "the managed block in %(unit)s.md was regenerated over an edit it could not "
                    "vouch for",
    REMOTE_UNREADABLE: "the branches on %(detail)s could not be read, so NOTHING was cross-checked "
                       "against reality this pass -- no branch was judged missing and no unit was "
                       "closed, because an unanswered question is not an answer",
    UNSERIALISED: "unit %(unit)s was written without a lock (%(detail)s), so the write went in but "
                  "cannot be shown to have stayed -- a concurrent writer on the same unit can still "
                  "overwrite it",
    SCOPE_EXPANSION: "goal %(detail)s declares unit %(unit)s from %(repo)s, which that unit's "
                     "repos do not list -- the goal was NOT recorded and `repos` is unchanged, "
                     "because adding a repo is an expansion of the unit's scope and that is its "
                     "owner's decision, never a pick's",
    LOST: "goal %(detail)s was written under unit %(unit)s and was gone when the pass re-read it: a "
          "concurrent unserialised writer overwrote it. Re-pick the goal, or run on a filesystem "
          "that supports flock",
}

#: WHAT AN UNADOPTED REGISTRY IS TOLD, AND THE ONE CONDITION IT IS TOLD UNDER (#1598).
#:
#: Measured live on 1.4.1, not inferred: `docs/branching-model.md` §14's steps opened a real unit,
#: two goals were cut from its branch, and NEITHER was recorded -- `_sync_at_pick`'s opt-out returned
#: `not-adopted` and swallowed it, with no error, no warning and no stderr line, while §14 promised
#: "recorded against it" a paragraph below. The opt-out's SEMANTICS were never wrong. Its silence
#: was, in exactly the way `REMOTE_UNREADABLE` above records for a remote that could not be read.
#:
#: ONLY WHEN A UNIT IS DECLARED, and that condition is the whole design rather than a filter on
#: noise. Declaring a unit is the adopter asking for a record, so answering it with silence is the
#: failure; declaring none is a project that never asked, whose pick stays byte-identical -- same
#: report, same output, same calls -- which is what keeps the model adoptable without disturbing
#: anyone who never wanted it.
#:
#: SAID HERE AND NOWHERE ELSE. Five modules open with the same `is_dir()` opt-out
#: (`feature_propagate`, `feature_rebase`, `feature_owner`, `unit_completion` and `cross_repo`), and
#: a pick that printed one correction five times is a pick whose output nobody reads. This is the
#: one that promises the recording and the one `work.start()` calls first, so it is the one that
#: speaks.
#:
#: WHAT IT HAS TO CARRY, each part earned by what was missing on the day: WHICH directory (the trial
#: ran inside a worktree, so several `.sdlc` trees were in play and "`.sdlc/features/` is absent"
#: would not have said which), the command that creates it, and the limit -- goals already picked
#: are not recovered (§15), which is worth knowing at the one moment the correction is still free.
#: ASCII ONLY, like every other line this module writes: a `§` here would raise on a `LC_ALL=C`
#: console and `_note` would swallow the whole message, which is the failure this constant exists
#: to end.
_NOT_ADOPTED_NOTE = (
    "sigma: features: goal %(goal)s declares unit %(unit)s, but there is no %(dir)s directory, "
    "so this project has not adopted the branching registry: the goal still runs and NOTHING "
    "records it under the unit. `mkdir -p %(dir)s` is the whole gesture (docs/branching-model.md, "
    "`Opening a unit`); goals picked before that directory exists are never backfilled.\n")

#: The one kind `_surface` skips outright: `feature_doc._tell` has ALREADY put it on stderr and in
#: the ledger, addressed to the same owner, at the moment it happened. Saying it again here would be
#: this module double-counting one event.
SURFACED_BY_THE_DOC = (BLOCK_DIVERGED,)


def _surface(sdlc_dir, goal, divergences, registry_now):
    """Say what the pass found out loud -- everything on stderr, state changes to the ledger.

    THE SPLIT IS THE `LEDGERED` RULE, and it is about who has to act. A state change this pass made
    is an event with an owner, and it is one-shot by construction, so it is addressed to that owner
    (§7.3) -- a note in a stream nobody reads is not surfacing. An observation that merely describes
    the ordinary world (a branch not cut yet) belongs on the console of whoever is running the pick,
    where it costs nobody's attention twice."""
    for one in divergences:
        if one["kind"] in SURFACED_BY_THE_DOC:
            continue
        # A kind with no row must not take the whole surfacing pass down with it. The table calls
        # itself total; it was not (#1565 added `shard-unreadable` and no row), and the `KeyError`
        # aborted the loop -- so every OTHER divergence found on that pass was dropped too, and the
        # operator got the exception text instead of the shard path and the repair gesture.
        template = _WORDING.get(one["kind"])
        why = (template % one) if template else (
            "%s: %s" % (one["kind"], one.get("detail") or "no further detail"))
        if one["kind"] not in LEDGERED:
            _note("sigma: features: %s\n" % why)
            continue
        entry = registry_now.get(one["unit"]) or {}
        _tell(sdlc_dir, one["unit"], goal, why, to=entry.get("owner"))


# --------------------------------------------------------------------------- the pass


def _report(goal, unit):
    return {"outcome": SYNCED, "goal": goal, "unit": unit, "repo": None, "live": False,
            "recorded": False, "created": False, "changed": [], "divergences": [], "doc": {},
            "serialised": True, "why": "", "note": ""}


def _sync_doc(features_dir, sdlc_dir, name, entry, goal, report):
    """Regenerate `<name>.md`'s managed block from the entry that was just written.

    #1470 built `sync()` as a pure function of `(features_dir, name, entry)` and left it unwired;
    this is the wire, and wiring it HERE -- on the same pass that changed the entry -- is what keeps
    the block a human reads and the shard a machine reads from ever being a pick apart.

    GUARDED, because the `.md` is the readable PROJECTION and the shard is the RECORD. A read-only
    checkout, a permission problem or a garbled file must cost the projection and never the record;
    `feature_doc` already refuses to write over what it cannot vouch for, and this refuses to let
    that refusal escalate."""
    try:
        result = doc.sync(features_dir, name, entry, goal=goal, sdlc_dir=sdlc_dir)
    except ValueError as exc:             # the naming contract, from `doc`'s own class -- see the
        _note("sigma: features: %s.md was not written: %s\n" % (name, exc))   # cross-module note
        return
    except Exception as exc:              # noqa: BLE001 - the projection must not cost the record
        _note("sigma: features: %s.md could not be written (%s); the registry entry is "
              "recorded and the file will be regenerated on the next pick.\n" % (name, exc))
        return
    report["doc"][name] = result["outcome"]
    if result["diverged"]:
        report["divergences"].append(_div(BLOCK_DIVERGED, name, None, str(result["path"])))


def sync_at_pick(sdlc_dir, config, goal, unit, run=None, cwd=None, remote=None):
    """THE PICK-PATH PASS. Record the goal, reconcile against reality, regenerate the block.

    NEVER RAISES. A registry is a record; losing one is bad, and losing the GOAL because the record
    could not be written is worse. Everything resolves to a report, and `FAILED` claims nothing --
    the same posture `cross_repo.check_at_pick` takes, and the same reason.

    `unit` IS HANDED IN, ALREADY RESOLVED. This function has no code path that reads an issue, which
    is what makes "no second network read" structural instead of a promise -- see the module
    docstring. `unit` may be None (the goal declares none), and the pass still runs: "local and
    remote reconcile on every pass" is about the PASS, not about the unit, and it is what keeps a
    registry from drifting merely because nobody happened to pick the unit that went stale."""
    report = _report(goal, unit)
    try:
        return _sync_at_pick(sdlc_dir, config, goal, unit, run, cwd, remote, report)
    except Exception as exc:              # noqa: BLE001 - "never raises" has to be total
        report["outcome"] = FAILED
        report["why"] = " ".join(str(exc).split())
        _note("sigma: features: the registry sync for %s did not run (%s); the pick carries on "
              "and the registry is unchanged for whatever it did not reach.\n" % (goal, report["why"]))
        return report


def _refusal(amended, name, repo):
    """-> the divergence for a unit `amend` refused to write, or []: #1565's unreadable record, or
    #327's legacy delta whose newer values the write would drop -- or, when it DID write over an
    older legacy delta, the `LEGACY_DELTA_DISCARD` naming each value dropped (#327 review block #2:
    stderr alone never reached the loop's result line or the owner)."""
    if amended.get("discarded") and amended.get("written"):
        return [_div(LEGACY_DELTA_DISCARD, name, repo, "; ".join(amended["discarded"]))]
    if amended.get("delta_conflict"):
        return [_div(LEGACY_DELTA_CONFLICT, name, repo, amended["refused"])]
    if amended.get("refused"):
        return [_div(SHARD_UNREADABLE, name, repo, amended["refused"])]
    return []


def _sync_at_pick(sdlc_dir, config, goal, unit, run, cwd, remote, report):
    features_dir = registry.registry_dir(sdlc_dir)
    if not features_dir.is_dir():
        # THE CHEAPEST STEP IS THE OPT-OUT, exactly as `cross_repo._check_at_pick` orders its own:
        # a project with no `.sdlc/features/` has not adopted the branching model, and pays no
        # `ls-remote`, no reconcile and no new failure mode it never asked for.
        report["outcome"] = NOT_ADOPTED
        if unit:
            _note(_NOT_ADOPTED_NOTE % {"goal": goal, "unit": unit, "dir": features_dir})
        return report
    run = run or _run
    cwd = str(cwd or pathlib.Path(sdlc_dir).parent)
    remote = remote or _remote(config)
    live, why = live_branches(run, cwd, remote)
    report["live"] = live is not None
    if live is None:
        # THE PASS STILL RUNS AND STILL RECORDS THE GOAL -- it just judges nothing. What it must not
        # do is stay quiet about that, which is what `outcome: "synced"` would otherwise assert on
        # its behalf. `unit` is None here as often as not, and the divergence is filed against it
        # either way so the clause can carry it whichever kind of pick this is.
        report["divergences"].append(_div(REMOTE_UNREADABLE, unit, None,
                                          "%s (%s)" % (remote, why)))
    repo = repo_slug(config, run, cwd, remote)
    report["repo"] = repo

    unserialised = []
    if unit and repo is None:
        # `repos.<slug>.goals` is the only place a goal number can live, so with no slug there is no
        # honest place to put it. Reporting the gap beats inventing a key that nothing can reconcile.
        report["divergences"].append(_div(NO_REPO, unit, None, remote))
    elif unit:
        found = []

        def _pick(entry, _found=found):
            del _found[:]                 # `amend` may retry, and one pass found it once
            _found.extend(_record_goal(entry, unit, repo, goal, live))
            _found.extend(reconcile(entry, unit, repo, live))

        amended = amend(sdlc_dir, unit, _pick)
        report["divergences"] += found
        report["divergences"] += _refusal(amended, unit, repo)   # never silent -- see `amend`
        # `written`, NOT `landed`: "was the goal recorded" is a question about the bytes going in,
        # and it is answerable. "Can that be proved to have stayed" is `landed`, is a different
        # question, and is reported separately rather than folded into this one.
        # #1477: a scope expansion writes NOTHING, and `amend` reports `written` True for a pass
        # that changed nothing -- correctly, since there was no window for a clobber. So the two
        # facts are combined here rather than conflated: "the goal was recorded" has to be False
        # when the goal was deliberately not recorded, or the field and the divergence disagree.
        expanded = any(d["kind"] == SCOPE_EXPANSION for d in found)
        report["recorded"] = amended["written"] and not expanded
        report["created"] = not amended["existed"]
        report["serialised"] = report["serialised"] and amended["serialised"]
        if amended["changed"]:
            report["changed"].append(unit)
        if amended["changed"] and not amended["serialised"]:
            unserialised.append(unit)
            report["divergences"].append(_div(UNSERIALISED, unit, repo, _WHY_UNSERIALISED))
        # ALWAYS, even when the entry did not change: the definition of done says a pick creates
        # both the entry and the file, so a `<name>.md` somebody deleted comes back on the next
        # pick. `feature_doc.sync` writes nothing when the block already says exactly this, so the
        # unconditional call costs a read and never a diff.
        _sync_doc(features_dir, sdlc_dir, unit, amended["entry"], goal, report)

    known = registry.read(features_dir)
    for name in sorted(known):
        # EVERY OTHER UNIT, EVERY PASS -- "so local and remote reconcile on every pass rather than
        # being allowed to diverge". A unit nobody is picking is exactly the one that goes stale, and
        # a reconcile that only ever ran on the picked unit would never find it.
        if name == unit:
            continue
        # THE DRY RUN IS WHAT KEEPS THE SWEEP LINEAR, and it is only ever a HINT. `reconcile`
        # mutates exactly when it returns a divergence, so running it on a throwaway copy of the
        # entry `known` already holds answers "could this unit possibly change?" for free -- and a
        # No means no lock is taken, no shard is re-read and nothing is written.
        #
        # WHY A HINT CANNOT BE WRONG IN A WAY THAT MATTERS: the authoritative decision is still made
        # inside `amend`, under the unit's lock, against a fresh read. The hint can only be stale in
        # one direction that costs anything -- a unit that became reconcilable between `known` and
        # here is skipped this pass and found by the next one, which is a delay of one pick on a
        # unit nobody is picking. Without it the sweep was O(N^2): `amend` re-reads the WHOLE
        # registry, so 100 units cost ~10,200 shard reads and 0.30s per pick, every pick, almost
        # always to write nothing at all.
        if not reconcile(copy.deepcopy(registry.normalise_entry(known[name])), name, repo, live):
            continue
        found = []

        def _pass(entry, _name=name, _found=found):
            del _found[:]
            _found.extend(reconcile(entry, _name, repo, live))

        amended = amend(sdlc_dir, name, _pass)
        report["divergences"] += found
        report["divergences"] += _refusal(amended, name, repo)   # never silent -- see `amend`
        report["serialised"] = report["serialised"] and amended["serialised"]
        if amended["changed"]:
            report["changed"].append(name)
            _sync_doc(features_dir, sdlc_dir, name, amended["entry"], goal, report)
            if not amended["serialised"]:
                unserialised.append(name)
                report["divergences"].append(_div(UNSERIALISED, name, repo, _WHY_UNSERIALISED))

    _recheck(features_dir, unit, repo, goal, unserialised, report)
    _surface(sdlc_dir, goal, report["divergences"], registry.read(features_dir))
    report["note"] = _clause(report)
    return report


#: Why a write could not be serialised. Deliberately not a diagnosis: `_acquire` fails open on three
#: different causes (no `fcntl`, a filesystem or directory that cannot be locked, a holder that did
#: not release inside `LOCK_TIMEOUT`) and telling them apart would mean reporting the OS error out of
#: a function whose whole contract is that it never raises one. Naming the three is more useful to a
#: reader than guessing between them.
_WHY_UNSERIALISED = "no flock available, or the lock timed out"


def _recheck(features_dir, unit, repo, goal, unserialised, report):
    """Re-read what this pass wrote WITHOUT a lock, at the end of the pass, and report what is gone.

    THIS IS THE HALF THAT MAKES `UNSERIALISED` MORE THAN A DISCLAIMER. An unserialised write is
    verified inside `amend` the instant it is made, which a co-writer holding stale state can undo a
    microsecond later -- so the reading there proves less than it looks like it does. Reading again
    once the rest of the pass has run puts real time between the write and the check, and a goal that
    is missing NOW is missing as a measurement rather than as a worry.

    WHAT IT DOES NOT CATCH, said plainly: a clobber that arrives after this read. Nothing lock-free on
    POSIX can catch that one, which is an argument for the lock and not for pretending the fallback
    is one. `recorded` is corrected to False when the goal is gone, so the field and the divergence
    can never disagree.

    Costs one shard read per unserialised unit, and nothing at all on the ordinary locked path --
    `unserialised` is empty there, so this returns before touching the disk."""
    if unit not in unserialised or repo is None:
        return
    number = _goal_number(goal)
    if number is None:
        return                            # nothing the registry could have stored; nothing to miss
    entry = registry.read(features_dir).get(unit) or {}
    repos = entry.get("repos") or {}
    goals = (repos.get(repo_key(repos, repo)) or {}).get("goals") or []
    if number not in goals:
        report["recorded"] = False
        report["divergences"].append(_div(LOST, unit, repo, str(number)))


def _goal_number(goal):
    """The goal as the registry stores it -- an int -- through `normalise_entry`, never a second
    opinion about what a goal number is. `_goals` is the ONE rule (`feature_registry._GOAL_RE`), and
    re-deriving it here to answer "is my goal in this list?" is exactly the drift the registry's own
    docstring refuses when it borrows `is_unit_name` rather than restating it."""
    got = registry.normalise_entry({"repos": {"x/y": {"goals": [goal]}}})["repos"]["x/y"]["goals"]
    return got[0] if got else None


def _remote(config):
    work_settings = config.get("work") if isinstance(config, dict) else None
    remote = work_settings.get("remote") if isinstance(work_settings, dict) else None
    remote = _load("state").safe_ref("work.remote", remote)       # #710
    return remote if isinstance(remote, str) and remote.strip() else DEFAULT_REMOTE


#: How many divergences the one-line clause names before it stops. A clause appended to a result an
#: agent reads back has to stay one line; the report carries the rest, and the clause says how many
#: it did not name rather than dropping them silently.
_CLAUSE_CAP = 2

#: The kinds a person reading `start()`'s own output can act on right now. `CLOSED` and
#: `BRANCH_MISSING` on OTHER units belong to their owners and go to the ledger instead; `CLOSED` on
#: any unit is a bookkeeping change nobody has to answer for. `UNSERIALISED` and `LOST` are here
#: because they are the fail-open path's honesty, and honesty in a field nobody reads is decoration
#: -- `report["serialised"]` and `report["recorded"]` had no consumer at all until this list did.
IN_CLAUSE = (BRANCH_MISSING, BRANCH_ABSENT, PICKED_WHILE_CLOSED, NO_REPO, REMOTE_UNREADABLE,
             UNSERIALISED, LOST, SCOPE_EXPANSION, LEGACY_DELTA_CONFLICT, LEGACY_DELTA_DISCARD)


def _clause(report):
    """The one-line clause `work.start()` appends to its own result, or "".

    ONLY THIS PICK'S OWN UNIT. The sweep reconciles every unit in the registry, and a clause naming
    another unit's findings on this goal's start line reads as though this goal caused them --
    `_surface` already routes those to their own owners through the ledger. `REMOTE_UNREADABLE` is
    filed against `report["unit"]` (which is None on a goal that declares nothing), so it matches
    this test whichever kind of pick this is.

    THE OVERFLOW IS COUNTED, NOT DROPPED. A clause that silently stopped at two would report the same
    text for two findings and for nine, which is the shape of under-reporting this whole review round
    was about."""
    seen = [d for d in report["divergences"]
            if d["kind"] in IN_CLAUSE and d["unit"] == report["unit"]]
    if not seen:
        return ""
    said = ", ".join("%s (%s)" % (d["kind"], d["detail"] or d["unit"]) for d in seen[:_CLAUSE_CAP])
    more = len(seen) - _CLAUSE_CAP
    return " — registry: %s%s" % (said, (" +%d more" % more) if more > 0 else "")


# --------------------------------------------------------------------------- the fold


class FoldRefused(Exception):
    """`fold` will not write an `index.json` that loses anything the current one records (#314).
    The message names each loss and the exact recovery."""


def fold(sdlc_dir):
    """Materialise `index.json` from the whole registry. -> the path written.

    A DELIBERATE MATERIALISATION, OFF THE PICK PATH -- see the module docstring for why it is not on
    it, and why the shards it folds are never removed. `read`'s shard-wins union is already correct
    with both present, so this changes what a fresh clone carries and nothing about what this
    checkout answers.

    It writes `read`'s view, which already merges a legacy-id record as a DELTA onto the index
    entry (`feature_registry.merge_legacy_delta`, #314) -- so a record the plugin under the previous
    name wrote cannot shrink the entry by construction. And it CHECKS that rather than trusting it:
    `feature_registry.monotonic_violations` compares the result with the current index, and any
    loss (a goal, or a field/grant of a merged unit) refuses the fold (`FoldRefused`, nothing
    written) with the repair command."""
    features_dir = registry.registry_dir(sdlc_dir)
    missing = registry.recoverable(features_dir)
    if missing:
        raise FoldRefused(
            "feature_sync: fold refused, nothing written: %s was rewritten in the previous plugin's "
            "schema and no longer holds %d unit(s) Sigma wrote (%s), so folding it would write the "
            "emptied registry as if it were complete. Restore them: %s ; or, if the sheet is "
            "complete on purpose, set Sigma's copy aside: %s --discard (docs/upgrading.md)"
            % (registry.index_path(features_dir), len(missing), ", ".join(missing[:8]),
               registry.recover_hint(sdlc_dir), registry.recover_hint(sdlc_dir)))
    merged = registry.read(features_dir)
    lost = registry.monotonic_violations(features_dir, merged)
    if lost:
        raise FoldRefused(
            "feature_sync: fold refused, nothing written: it would lose what %s records -- %s. "
            "Recover: %s. If a record in Sigma's schema dropped these on purpose, edit %s to match."
            % (registry.INDEX_NAME, "; ".join(lost), registry.delta_recovery(features_dir),
               registry.index_path(features_dir)))
    return registry.write_index(features_dir, merged)


class RecoverRefused(Exception):
    """`recover` had nothing safe to do; the message says why. Nothing was written."""


def recover(sdlc_dir, discard=False):
    """The lever for #514: the previous plugin's own fold left `index.json` in ITS schema without
    units Sigma's copy holds. -> the unit names restored (or, with `discard`, the path the copy was
    set aside to).

    EXPLICIT, NEVER AUTOMATIC. It adds only WHOLE missing units: the sheet's own entries and the shards
    `read` serves win over the copy on every shared name, so what the previous plugin legitimately
    changed is kept, and a unit it dropped a goal from is NOT repaired (the copy's older entry is
    not applied over it). The result is written through `write_index` (the one-time backup hook, the
    legacy-id notice and the unchanged-bytes rule all apply), in Sigma's schema, normalised like any
    fold. Refuses, writing nothing, when there is nothing to restore or the sheet changed since it
    was read (a compare against the bytes read: the remaining window is the instant before the
    replace).

    `discard` is the way out when the sheet is complete on purpose (an older branch, a unit dropped
    deliberately): the copy is RENAMED, never deleted, so `fold` and `migrate` stop treating its units
    as missing and nothing is lost."""
    features_dir = registry.registry_dir(sdlc_dir)
    path = registry.mirror_path(features_dir)
    if discard:
        if path.is_symlink() or not path.is_file():
            raise RecoverRefused("recover: no recovery copy to set aside at %s" % path)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        dest = None
        for n in range(1000):             # a second-resolution name must never replace an earlier copy
            candidate = path.with_name("%s-%s-%d.json" % (registry.MIRROR_DISCARDED, stamp, n))
            if not candidate.exists():
                dest = candidate
                break
        if dest is None:
            raise RecoverRefused("recover: too many discarded copies under %s; remove some" % path.parent)
        try:
            os.replace(str(path), str(dest))
        except OSError as exc:
            raise RecoverRefused("recover: the copy at %s could not be set aside (%s); nothing was "
                                 "changed" % (path, exc))
        return dest
    sheet = registry.index_path(features_dir)
    try:
        seen = sheet.read_text(encoding="utf-8")
    except (OSError, ValueError):
        seen = None
    missing = registry.recoverable(features_dir)
    if not missing:
        raise RecoverRefused(
            "recover: nothing to restore in %s -- it is not in the previous plugin's schema, or the "
            "recovery copy holds no unit it lacks (or there is no copy: it covers only sheets this "
            "checkout wrote or saw)" % registry.index_path(features_dir))
    copy_units = registry.mirror_units(features_dir)
    merged = dict(registry.read(features_dir))
    for name in missing:
        merged[name] = copy_units[name]
    try:
        now = sheet.read_text(encoding="utf-8")
    except (OSError, ValueError):
        now = None
    if now != seen:
        raise RecoverRefused("recover: %s changed while it was being read; nothing written -- run "
                             "it again" % sheet)
    try:
        registry.write_index(features_dir, merged)
    except OSError as exc:
        raise RecoverRefused("recover: %s could not be written (%s); nothing was changed"
                             % (sheet, exc))
    return missing


#: #327: THE rule for rewriting a legacy delta record lives in the registry beside `write_unit`,
#: which enforces it itself (review block #2) -- these names are the same functions, not copies.
_newer_than_index = registry.newer_than_index
_delta_verdict = registry.delta_verdict
_conflict_text = registry.delta_conflict_text


def repair(sdlc_dir, timeout=LOCK_TIMEOUT):
    """Rewrite each legacy-id record `read` merges as a delta (`feature_registry.legacy_deltas`)
    in Sigma's schema, as exactly what `read` serves for it -- the index entry plus what the record
    adds -- under the unit's own lock, re-checked inside it. Idempotent: a second run finds nothing.
    The plugin under the previous name cannot read the rewritten record (Sigma's schema id) and its
    own #1565 guard then refuses that unit rather than replacing it -- so disable it first anyway.

    NOT LOSSLESS, AND IT SAYS SO (#326). The rewrite DISCARDS every non-empty record value the delta
    merge does not keep (`feature_registry.delta_discards`: a differing owner, priority, branch, a
    grant). Each is returned. And when there is anything to discard and the record is at least as
    new as `index.json` (`_newer_than_index`: file mtime, a hint), those values are most likely an
    edit the previous plugin made after the conversion -- one Sigma never applied, and a human may
    still want: that unit is REFUSED, nothing is written, and the caller names the ways forward.
    `_delta_verdict` is the rule, and `amend` applies the same one. -> (done [(name, path,
    discarded)], refused [(name, path, discarded)])."""
    features_dir = registry.registry_dir(sdlc_dir)
    done, refused = [], []
    for name, _path in registry.legacy_deltas(features_dir):
        fd = _acquire(lock_path(sdlc_dir, name), timeout)
        try:
            now = [r for r in registry.legacy_delta_records(features_dir) if r[0] == name]
            if not now:
                continue
            verdict = _delta_verdict(features_dir, now[0][0])
            if verdict is None:
                continue
            path, discarded, newer = verdict
            if discarded and newer:
                refused.append((name, path, discarded))
                continue
            entry = registry.read(features_dir).get(name)
            if entry is not None:
                try:
                    done.append((name, registry.write_unit(features_dir, name, entry), discarded))
                except registry.LegacyDeltaConflict as exc:   # rewritten since the verdict above
                    refused.append((name, exc.path, exc.discarded))
        finally:
            _release(fd)
    return done, refused


def _live_deltas(sdlc_dir):
    """-> (records, how many the one rule would refuse now). Local reads only: the unit files and
    `index.json` once (`legacy_delta_records`), one stat per record. Never raises."""
    features_dir = registry.registry_dir(sdlc_dir)
    try:
        found = registry.legacy_delta_records(features_dir)
        refused = sum(1 for _n, path, base, record in found
                      if registry.delta_discards(base, record)
                      and registry.newer_than_index(features_dir, path))
    except Exception:                     # noqa: BLE001 - a health surface must never raise
        return 0, 0
    return len(found), refused


def _repair_command(sdlc_dir):
    return "python3 %s repair %s" % (pathlib.Path(__file__).resolve(), sdlc_dir)


def legacy_delta_row(sdlc_dir):
    """#327 review (e), LIVENESS: the `doctor.py check` row for live legacy delta records -- unit
    records the previous plugin wrote after the conversion, which Sigma merges but never applies.
    None when there are none, so a healthy project's check list is unchanged. A WARN (`ok` True):
    nothing is lost while the record stands, but a pick on a unit whose record would be refused
    records nothing until a human resolves it."""
    count, refused = _live_deltas(sdlc_dir)
    if not count:
        return None
    return {"name": "legacy delta records: WARN -- %d legacy delta record(s) under %s (the previous "
                    "plugin wrote them after the conversion; index.json wins, they only fill blanks "
                    "and add goals); %d would be refused (a value newer than index.json)"
                    % (count, registry.registry_dir(sdlc_dir) / registry.UNITS_DIRNAME, refused),
            "ok": True,
            "fix": "disable the previous plugin on this repository, then `%s` (docs/upgrading.md)"
                   % _repair_command(sdlc_dir)}


def legacy_delta_segment(sdlc_dir):
    """The one `status.py` segment for the same fact, or "" when there is none."""
    count, refused = _live_deltas(sdlc_dir)
    if not count:
        return ""
    return ("legacy delta records: %d (%d would be refused): %s"
            % (count, refused, _repair_command(sdlc_dir)))


# --------------------------------------------------------------------------- CLI


USAGE = ("usage: feature_sync.py fold <sdlc_dir> | feature_sync.py show <sdlc_dir> | "
         "feature_sync.py repair <sdlc_dir> | feature_sync.py recover <sdlc_dir> [--discard]")


def main(argv):
    """`feature_sync.py fold <sdlc_dir>` | `feature_sync.py show <sdlc_dir>`.

    THERE IS NO `sync` VERB, and the omission is the same one `cross_repo.main` makes for the same
    reason: the sync belongs to the pick, and a second way to run it is a second answer. `fold` is
    here because it is explicitly NOT part of a pick, and `show` because a record nobody can read is
    not much of a record. `repair` (#314) rewrites a legacy-id record merged as a delta in Sigma's
    schema. `recover` (#514) restores units the previous plugin's fold dropped from the sheet."""
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "fold":
        try:
            print(fold(argv[2]))
        except FoldRefused as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    if len(argv) >= 3 and argv[1] == "repair":
        done, refused = repair(argv[2])
        for name, path, discarded in done:
            print("repaired %s: %s" % (name, path))
            for what in discarded:
                print("  discarded %s: %s" % (name, what))
        for name, path, discarded in refused:
            print("feature_sync: repair refused %s, nothing written: %s is at least as new as %s "
                  "(file time) and rewriting it would discard %s -- most likely an edit the "
                  "previous plugin made after the conversion, which Sigma reads but does not "
                  "apply. To keep those values, copy them into %s; to drop them, delete them from "
                  "the record; then rerun repair."
                  % (name, path, registry.INDEX_NAME, "; ".join(discarded),
                     registry.index_path(registry.registry_dir(argv[2]))), file=sys.stderr)
        if not done and not refused:
            print("feature_sync: nothing to repair in %s" % argv[2])
        return 2 if refused else 0
    if len(argv) >= 3 and argv[1] == "recover" and argv[3:] in ([], ["--discard"]):
        try:
            done = recover(argv[2], discard=argv[3:] == ["--discard"])
        except RecoverRefused as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if argv[3:]:
            print("recover: the recovery copy was set aside at %s" % done)
        else:
            shown = ", ".join(done[:8]) + ((" and %d more" % (len(done) - 8)) if len(done) > 8 else "")
            print("recover: restored %d unit(s) into %s: %s -- review with git diff"
                  % (len(done), registry.index_path(registry.registry_dir(argv[2])), shown))
        return 0
    if len(argv) >= 3 and argv[1] == "show":
        print(json.dumps(registry.read(registry.registry_dir(argv[2])), indent=2, sort_keys=True))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


def _symlink_guard(argv):
    """#708: refuse a committed symlink under .sdlc/state or .sdlc/journey before any write."""
    import importlib.util as _u
    import pathlib as _p
    spec = _u.spec_from_file_location("_guard_state", _p.Path(__file__).resolve().parent / "state.py")
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.guard_argv(argv, _p.Path(__file__).name)


if __name__ == "__main__":
    raise SystemExit(_symlink_guard(sys.argv) or main(sys.argv))
