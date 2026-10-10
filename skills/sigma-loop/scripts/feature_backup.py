"""Backup refs for a rewritten unit tip: create one in the same atomic push, restore one, prune old ones.

WHAT IT IS. The unit branch `feature/<unit>` is force-pushed by the rebase pass. This module makes that rewrite
undoable. The OLD tip is kept under `refs/sigma/backup/<unit>/<UTC stamp>` (stamp: `20261010T031500Z`), created in
the SAME `git push --atomic` as the leased branch update, so both land or neither does. A person can put a backup
back (`restore_backup`), and old backups are removed by `prune_backups`: the newest `backup.keep_last` of each unit
are always kept, and only a backup older than `backup.keep_days` is ever removed.

WHO CALLS WHAT. The pick-time rebase pass (`_pushed` in feature_rebase.py) calls `push_unit`, and only when its own query
of the upkeep gate (the project door, through `feature_upkeep.evaluate`) is open; with the gate closed the pass pushes
exactly as before and this module is never loaded. The verbs `restore` and `prune` of feature_rebase.py call
`restore_backup` and `prune_backups`, on a person's request; each asks the gate itself first and returns
`{"closed": True, ...}` before it spawns anything. No function here is a decorated entry point: that would have to be
registered with a trap driver, which rewrites two slice-1 pins; the pass is registered by the slice that builds it.

SAFETY RULES, each proved by a test, none of them given by git itself:
  * THE PREFIX IS ENFORCED HERE. `ls-remote` patterns match from the END of a name, so a listing for the backup
    prefix also returns a branch, a tag or another namespace whose name merely ends with it. Every listed name is
    parsed strictly (`parse_ref`): the exact prefix, ONE unit segment that satisfies the unit-name rule, ONE
    fixed-width valid stamp. Anything else is ignored, never deleted. The delete step re-checks every name again.
  * AGE COMES FROM THE NAME. A backup of an old commit made today has an old commit date and a new stamp.
  * A STAMP IN THE FUTURE (any writer to the remote can create one) takes no keep slot and is kept.
  * AN UNREADABLE LISTING IS NOT AN EMPTY ONE: it deletes nothing and says so.
  * GIT RESOLVES A SHORT NAME BY TAIL-MATCHING (`refs/heads/<name>`, `refs/tags/<name>`, `refs/<name>`,
    `refs/remotes/<name>`), and `push --delete` and a lease use the same rules. A candidate whose name also appears
    in the listing in one of those forms is KEPT (counted `skipped_ambiguous`), and just before each delete chunk the
    exact refs are read again: a ref that vanished or moved, or that has a lookalike, is skipped. The small window
    between that read and the push is a named ceiling, not zero.
  * THE DELETE USES THE `--delete` FLAG, one lease per ref (`--force-with-lease=<ref>:<sha seen>`), never a bare
    colon refspec and never `--atomic`: a ref that changed after it was listed survives and the rest go.
  * A NAME COLLISION (same unit, same second) is REFUSED, never overwritten: the push carries an expect-absent lease
    on the backup name, and `--atomic` makes the refusal take the branch update with it.
  * THE FORMER-PREFIX LIST SHIPS EMPTY. `backup.former_prefixes` (read through the gate only) names namespaces an
    earlier product wrote; a name is accepted only if it has the shape `refs/<word>/backup/`. Nothing is deleted
    under a namespace nobody listed.
  * AN OVER-LONG UNIT NAME (more than UNIT_LIMIT bytes) is refused before any git call; the pass reports it and
    pushes nothing, never a push without a backup.

CEILINGS, measured on a local bare remote with git 2.49 (not on a hosting service, and not on an older git): the plan
is linear (2 ms at 1,000 refs, 22 ms at 10,000, 253 ms at 100,000); one capped call (list, plan, up to MAX_DELETES
deletes, list again) took 0.4 s at 1,000 refs and 2.3 s at 10,000; draining 9,500 candidates took 48 calls. The
argument size of one delete is derived from the machine (`arg_budget`), never a constant.

Library-only (no `__main__`). It writes no file of its own; `restore` runs `git fetch`, which adds objects (and may
update remote-tracking refs) in the repository it runs in, and never writes FETCH_HEAD or creates a local branch or tag.
The runner is `run(cwd, argv) -> stdout`, raising on a non-zero exit, the same contract as `feature_sync._run`.
"""
import datetime
import importlib.util
import os
import pathlib
import re
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


features = _load("features")
sync = _load("feature_sync")
feature_upkeep = _load("feature_upkeep")

PREFIX = "refs/sigma/backup/"
BRANCH_PREFIX = features.BRANCH_PREFIX
STAMP_LEN = 16
REF_LIMIT = 255
UNIT_LIMIT = REF_LIMIT - len(PREFIX) - 1 - STAMP_LEN        # 220: the longest unit name a backup ref can carry

MAX_DELETES = 200          # per invocation; the rest waits for the next run
CHUNK_REFS = 100           # refs per delete push, whatever the byte budget says
ARG_FLOOR, ARG_CEILING, ARG_FALLBACK = 4096, 64000, 32768
MAX_FORMER = 8             # former prefixes read at most
MAX_LISTED = 50            # backups shown by `restore --list`
_WHY_CHARS = 600
#: said on every successful restore (the pass is not stopped from undoing it, and a person should hear it)
NOT_STICKY = ("a restore is not sticky: while upkeep is enabled the next pick of a goal in this unit brings the branch "
              "forward again, keeping the restored tip as a backup; set work.rebase_upkeep to off first to keep it")
_SECONDS_A_DAY = 86400

#: names under refs/ that git or a host uses itself; never accepted as a former product word
RESERVED_WORDS = frozenset({"heads", "tags", "remotes", "pull", "notes", "replace", "changes", "stash", "bisect",
                            "original", "for", "merge-requests", "keep-around"})

PUSHED, RESTORED, LISTED = "pushed", "restored", "listed"
PRUNED, NOTHING, DRY_RUN, PARTIAL = "pruned", "nothing-to-prune", "dry-run", "partial"
TOO_LONG, BAD_INPUT, BUSY = "name-too-long", "bad-input", "busy"
LEASE_REFUSED, COLLISION, FAILED = "lease-refused", "name-collision", "failed"
NO_BACKUP, NO_BRANCH, MOVED, NO_CHANGE = "no-such-backup", "no-branch", "remote-moved", "no-change"
UNREADABLE = "unreadable"
#: outcomes that mean "done as asked"; a command-line caller exits 0 for these and 1 for the rest
DONE = (PUSHED, RESTORED, LISTED, PRUNED, NOTHING, DRY_RUN, NO_CHANGE)

_STAMP_RE = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FORMER_RE = re.compile(r"refs/([A-Za-z0-9][A-Za-z0-9._-]{0,63})/backup/")


class Refused(ValueError):
    """An input the module will not act on. `code` is one of the outcome words above."""

    def __init__(self, code, why):
        ValueError.__init__(self, why)
        self.code, self.why = code, why


def _why(exc):
    return " ".join(str(exc).split())[:_WHY_CHARS] or exc.__class__.__name__


def _result(outcome, **fields):
    fields["outcome"] = outcome
    return fields


def _closed(config):
    """None when the upkeep gate is open for the project door, else the closed result a gated entry point returns.
    The query is pure: it reads the config through the gate module and does nothing else."""
    verdict = feature_upkeep.evaluate(config, "project")
    if verdict["open"]:
        return None
    return {"closed": True, "door": "project", "missing": verdict["missing"], "problems": verdict["problems"]}


# --------------------------------------------------------------------------- names and stamps


def stamp_of(epoch):
    """The 16-character UTC stamp for a time in seconds."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))


def stamp_epoch(text):
    """Seconds since the epoch for a well-formed stamp, else None. Fixed width, ASCII digits, a real date."""
    if not isinstance(text, str):
        return None
    got = _STAMP_RE.fullmatch(text)
    if got is None:
        return None
    try:
        when = datetime.datetime(*[int(part) for part in got.groups()], tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return when.timestamp()


def unit_problem(unit):
    """None when a backup ref can be named for `unit`; else (code, reason). A pure string test: no git call."""
    if not (isinstance(unit, str) and unit.isascii() and features._is_unit_name(unit)):
        return BAD_INPUT, "not a unit name"
    size = len(unit.encode("utf-8"))
    if size > UNIT_LIMIT:
        return TOO_LONG, ("unit name is %d bytes; its backup ref would be %d bytes, over the %d-byte limit "
                          "(the longest unit name is %d bytes)"
                          % (size, len(PREFIX) + size + 1 + STAMP_LEN, REF_LIMIT, UNIT_LIMIT))
    return None


def ref_name(unit, stamp):
    """`refs/sigma/backup/<unit>/<stamp>`, or Refused for an unusable unit or stamp."""
    problem = unit_problem(unit)
    if problem is not None:
        raise Refused(*problem)
    if stamp_epoch(stamp) is None:
        raise Refused(BAD_INPUT, "not a backup stamp")
    return PREFIX + unit + "/" + stamp


def parse_ref(ref, prefixes=(PREFIX,)):
    """(prefix, unit, stamp) for a name that is EXACTLY `<prefix><unit>/<stamp>` with one of `prefixes`, else None."""
    if not isinstance(ref, str):
        return None
    for prefix in prefixes:
        if ref.startswith(prefix):
            unit, sep, stamp = ref[len(prefix):].partition("/")
            if (sep and unit and "/" not in stamp and unit.isascii() and features._is_unit_name(unit)
                    and stamp_epoch(stamp) is not None):
                return prefix, unit, stamp
            return None
    return None


def _former_problem(item):
    got = _FORMER_RE.fullmatch(item) if isinstance(item, str) else None
    if got is None:
        return "not of the form refs/<name>/backup/"
    if got.group(1).lower() in RESERVED_WORDS:
        return "a namespace git or a host uses itself"
    if item.lower() == PREFIX:
        return "reads as the current prefix"
    return None


def usable_prefixes(configured):
    """(prefixes, rejected): the current prefix first, then each well-formed former prefix, at most MAX_FORMER.
    `rejected` lists {position, reason}; a configured value is never echoed."""
    prefixes, rejected = [PREFIX], []
    for position, item in enumerate(configured if isinstance(configured, (list, tuple)) else (), 1):
        why = _former_problem(item)
        if why is None and item in prefixes:
            why = "repeats a prefix already read"
        if why is None and len(prefixes) > MAX_FORMER:
            why = "more than %d former prefixes" % MAX_FORMER
        if why is not None:
            rejected.append({"position": position, "reason": why})
        else:
            prefixes.append(item)
    return prefixes, rejected


def _now(clock):
    value = time.time() if clock is None else (clock() if callable(clock) else clock)
    return float(value)


def _remote_problem(remote):
    if not (isinstance(remote, str) and remote.strip() and not remote.startswith("-") and not any(c.isspace() for c in remote)):
        return BAD_INPUT, "not a usable remote name"
    return None


# --------------------------------------------------------------------------- reading the remote


def _list(run, cwd, remote, prefixes, unit=None):
    """-> ([(sha, ref)], "") or (None, reason). `ls-remote` patterns match from the end of a name, so callers filter."""
    patterns = [prefix + (unit + "/" if unit else "") + "*" for prefix in prefixes]
    try:
        out = run(cwd, ["git", "ls-remote", remote] + patterns)
    except Exception as exc:              # noqa: BLE001 - not knowing is never "there is nothing"
        return None, _why(exc)
    found = []
    for line in str(out or "").splitlines():
        sha, _, ref = line.partition("\t")
        if sha.strip() and ref.strip():
            found.append((sha.strip(), ref.strip()))
    return found, ""


def _remote_ref(run, cwd, remote, ref):
    """What exactly `ref` points at on the remote: its sha, "" when absent, None when it could not be read."""
    try:
        out = run(cwd, ["git", "ls-remote", remote, ref])
    except Exception:                     # noqa: BLE001
        return None
    for line in str(out or "").splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref:
            return sha.strip()
    return ""


# --------------------------------------------------------------------------- the push: leased branch update plus backup


def _atomic_leased_push(run, cwd, remote, branch, unit, expect, source, stamp):
    """THE ONE PLACE a leased branch update and a new backup ref travel together, in one `--atomic` push.

    `source` is pushed to `branch` under a lease on `expect`; `expect` (the tip being replaced) is pushed to the new
    backup name under an expect-absent lease. Both refspecs have non-empty sources by construction: `source` is the
    literal HEAD or a full object name, `expect` a full object name, and the unit and stamp are validated by
    `ref_name`. An empty source would turn a colon refspec into a delete, so it is refused before argv exists.
    -> (True, "") or (False, reason). Raises Refused for an input it will not push."""
    if branch != BRANCH_PREFIX + unit:
        raise Refused(BAD_INPUT, "the branch is not feature/<unit>")
    if not (isinstance(source, str) and (source == "HEAD" or _SHA_RE.fullmatch(source))):
        raise Refused(BAD_INPUT, "the source of the branch update must be HEAD or a full object name")
    if not (isinstance(expect, str) and _SHA_RE.fullmatch(expect)):
        raise Refused(BAD_INPUT, "the expected tip must be a full lowercase object name")
    backup = ref_name(unit, stamp)
    push_argv = ["git", "push", "--atomic",
                 "--force-with-lease=refs/heads/%s:%s" % (branch, expect),
                 "--force-with-lease=%s:" % backup,
                 remote,
                 "%s:refs/heads/%s" % (source, branch),
                 "%s:refs/sigma/backup/%s/%s" % (expect, unit, stamp)]
    try:
        run(cwd, push_argv)
    except Exception as exc:              # noqa: BLE001
        return False, _why(exc)
    return True, ""


def _classify(run, read_cwd, remote, branch, unit, expect, stamp, why):
    """Why an atomic push failed, decided by MEASURING the remote, never by git's wording."""
    tip = _remote_ref(run, read_cwd, remote, "refs/heads/" + branch)
    backup = ref_name(unit, stamp)
    if tip is not None and tip != expect:
        return _result(LEASE_REFUSED, tip=tip, backup=backup, why=why)
    held = _remote_ref(run, read_cwd, remote, backup)
    if held and held != expect:
        return _result(COLLISION, tip=tip, backup=backup, holds=held,
                       why="the backup ref %s is already taken; nothing was pushed, retry after a second" % backup)
    return _result(FAILED, tip=tip, backup=backup, why=why)


def push_unit(run, cwd, read_cwd, remote, unit, expect, *, source="HEAD", clock=None):
    """Push `source` (run in `cwd`) to `feature/<unit>` under a lease on `expect`, and keep `expect` as a backup ref in
    the same atomic push: both land or neither does. `read_cwd` is where the remote is read back after a refusal.
    -> {outcome: PUSHED | LEASE_REFUSED | COLLISION | FAILED | TOO_LONG | BAD_INPUT, ...}. Never raises.
    THE CALLER HOLDS THE GATE: the pass builds the descriptor that reaches here only when the upkeep gate is open."""
    problem = unit_problem(unit) or _remote_problem(remote)
    if problem is not None:
        return _result(problem[0], why=problem[1])
    stamp = stamp_of(_now(clock))
    branch = BRANCH_PREFIX + unit
    try:
        ok, why = _atomic_leased_push(run, str(cwd), remote, branch, unit, expect, source, stamp)
    except Refused as refused:
        return _result(refused.code, why=refused.why)
    if ok:
        return _result(PUSHED, backup=ref_name(unit, stamp), stamp=stamp, expected=expect)
    return _classify(run, str(read_cwd), remote, branch, unit, expect, stamp, why)


# --------------------------------------------------------------------------- restore


def _backups_of(run, cwd, remote, unit):
    """-> ([{stamp, sha}] newest first, "") or (None, reason), only names that parse as this unit's backups."""
    entries, why = _list(run, cwd, remote, [PREFIX], unit)
    if entries is None:
        return None, why
    rows = []
    for sha, ref in entries:
        got = parse_ref(ref)
        if got is not None and got[1] == unit and _SHA_RE.fullmatch(sha):
            rows.append({"stamp": got[2], "sha": sha})
    rows.sort(key=lambda row: row["stamp"], reverse=True)
    return rows, ""


def restore_backup(config, sdlc_dir, unit, stamp, expect, *, remote, run=None, cwd=None, clock=None, hold=None,
                   list_only=False):
    """Put the backup named by `stamp` back as `feature/<unit>`, under a lease on `expect` (the tip the caller
    states the branch now has), and keep `expect` as a new backup in the same atomic push. `list_only` instead
    lists the unit's backups and the branch tip. `hold(unit)` returns a release callable, or None when the unit is
    busy. Never raises."""
    closed = _closed(config)
    if closed is not None:
        return closed
    run = run or sync._run
    cwd = str(cwd or pathlib.Path(sdlc_dir).parent)
    problem = unit_problem(unit) or _remote_problem(remote)
    if problem is None and not list_only:
        if stamp_epoch(stamp) is None:
            problem = BAD_INPUT, "not a backup stamp"
        elif not (isinstance(expect, str) and _SHA_RE.fullmatch(expect)):
            problem = BAD_INPUT, "restore needs the tip the branch has now (--expect), as a full lowercase object name"
    if problem is not None:
        return _result(problem[0], why=problem[1])
    release = None
    if hold is not None and not list_only:
        try:
            release = hold(unit)
        except Exception as exc:          # noqa: BLE001 - "never raises" has to be total
            return _result(FAILED, why="the unit's lock could not be taken: " + _why(exc))
        if release is None:
            return _result(BUSY, why="the unit's lock is held by another pass or restore, or could not be taken; "
                                     "nothing was changed")
    try:
        return _restore(run, cwd, remote, unit, stamp, expect, clock, list_only)
    finally:
        if release is not None:
            release()


def _restore(run, cwd, remote, unit, stamp, expect, clock, list_only):
    branch = BRANCH_PREFIX + unit
    rows, why = _backups_of(run, cwd, remote, unit)
    if rows is None:
        return _result(UNREADABLE, why="the backup listing could not be read: " + why)
    tip = _remote_ref(run, cwd, remote, "refs/heads/" + branch)
    if list_only:
        return _result(LISTED, branch_tip=tip, backups=rows[:MAX_LISTED], total=len(rows))
    wanted = [row for row in rows if row["stamp"] == stamp]
    if not wanted:
        return _result(NO_BACKUP, why="no backup of this unit has that stamp", available=[r["stamp"] for r in rows[:MAX_LISTED]])
    if tip is None:
        return _result(UNREADABLE, why="the branch tip could not be read")
    if tip == "":
        return _result(NO_BRANCH, why="the branch is not on the remote; nothing to restore over")
    if tip != expect:
        return _result(MOVED, tip=tip, why="the branch is not at the tip you named; nothing was changed")
    target = wanted[0]["sha"]
    if target == expect:
        return _result(NO_CHANGE, tip=tip, why="the branch already is at that backup")
    try:
        run(cwd, ["git", "fetch", "--no-write-fetch-head", "--no-tags", remote, "refs/heads/" + branch,
                  ref_name(unit, stamp)])
        run(cwd, ["git", "cat-file", "-e", target + "^{commit}"])
        run(cwd, ["git", "cat-file", "-e", expect + "^{commit}"])
    except Exception as exc:              # noqa: BLE001
        return _result(FAILED, tip=tip, why="the objects could not be fetched: " + _why(exc))
    fresh = stamp_of(_now(clock))
    try:
        ok, why = _atomic_leased_push(run, cwd, remote, branch, unit, expect, target, fresh)
    except Refused as refused:
        return _result(refused.code, why=refused.why)
    if ok:
        return _result(RESTORED, branch=branch, restored_tip=target, restored_from=stamp,
                       backup=ref_name(unit, fresh), backed_up=expect, note=NOT_STICKY)
    return _classify(run, cwd, remote, branch, unit, expect, fresh, why)


# --------------------------------------------------------------------------- prune


#: Where git's name resolution would also look for a short name `<ref>` (it tries these in turn and can pick, or
#: refuse as ambiguous, one that is not the backup ref). `git push --delete <ref>` and a lease on `<ref>` both use it.
_TAIL_FORMS = ("refs/heads/%s", "refs/tags/%s", "refs/%s", "refs/remotes/%s", "refs/remotes/%s/HEAD")


def ambiguous_with(ref, names):
    """The names in `names` that git could resolve the short name `ref` to instead of (or as well as) the exact ref."""
    return sorted(form % ref for form in _TAIL_FORMS if (form % ref) in names)


def plan_prune(entries, prefixes, now, keep_days, keep_last):
    """Pure. `entries` is [(sha, ref)] as listed. Returns {delete: [(ref, sha)] oldest first, kept, foreign,
    malformed, future, units}. A name that is not exactly `<prefix><unit>/<stamp>` is never planned. Per unit
    (the raw name, git refs being case-sensitive) the newest `keep_last` are kept whatever their age; of the rest,
    only a backup older than `keep_days` is planned. A stamp later than `now` is kept and takes no slot. A candidate
    whose name ALSO appears in the listing as a branch, tag or other ref that git's tail-matching would resolve it
    to is KEPT and counted in `ambiguous` (the delete could take the other ref, or git would refuse the whole chunk)."""
    names = {ref for _sha, ref in entries if isinstance(ref, str)}
    groups, foreign, malformed, future, ambiguous = {}, 0, 0, 0, 0
    for sha, ref in entries:
        got = parse_ref(ref, prefixes)
        if got is None or _SHA_RE.fullmatch(sha) is None:
            if isinstance(ref, str) and any(ref.startswith(p) for p in prefixes):
                malformed += 1
            else:
                foreign += 1
            continue
        prefix, unit, stamp = got
        epoch = stamp_epoch(stamp)
        if epoch > now:
            future += 1
            continue
        groups.setdefault((prefix, unit), []).append((stamp, ref, sha, epoch))
    cutoff = now - keep_days * _SECONDS_A_DAY
    doomed, kept = [], future
    for rows in groups.values():
        rows.sort(reverse=True)
        kept += min(keep_last, len(rows))
        for stamp, ref, sha, epoch in rows[keep_last:]:
            if epoch < cutoff and ambiguous_with(ref, names):
                ambiguous += 1
                kept += 1
            elif epoch < cutoff:
                doomed.append((epoch, ref, sha))
            else:
                kept += 1
    doomed.sort()
    return {"delete": [(ref, sha) for _epoch, ref, sha in doomed], "kept": kept, "foreign": foreign,
            "malformed": malformed, "future": future, "units": len(groups), "ambiguous": ambiguous}


def arg_budget():
    """Bytes of arguments one delete push may carry: a fraction of the machine's own limit, clamped."""
    try:
        limit = os.sysconf("SC_ARG_MAX")
    except (AttributeError, ValueError, OSError):
        limit = ARG_FALLBACK
    if not isinstance(limit, int) or limit <= 0:
        limit = ARG_FALLBACK
    return max(ARG_FLOOR, min(ARG_CEILING, limit // 8))


def _cost(ref, sha):
    return 2 * len(ref) + len(sha) + 22


def chunks(rows, budget):
    """Split [(ref, sha)] into groups of at most CHUNK_REFS whose argument bytes stay within `budget`."""
    out, current, used = [], [], 0
    for ref, sha in rows:
        cost = _cost(ref, sha)
        if current and (used + cost > budget or len(current) >= CHUNK_REFS):
            out.append(current)
            current, used = [], 0
        current.append((ref, sha))
        used += cost
    if current:
        out.append(current)
    return out


def _recheck(run, cwd, remote, rows):
    """Immediately before a delete: read the remote again for exactly these names. -> (rows still safe to delete,
    vanished_or_moved, ambiguous), or None when the read failed (then nothing is deleted). A row is safe only when
    the EXACT ref is present at the sha that was listed and no tail-matching lookalike is present: with the exact ref
    gone, `push --delete` would resolve the short name to a branch, tag or other ref carrying the same name."""
    try:
        out = run(cwd, ["git", "ls-remote", remote] + [ref for ref, _sha in rows])
    except Exception:                     # noqa: BLE001 - not knowing is never "safe"
        return None
    seen = {}
    for line in str(out or "").splitlines():
        sha, _, name = line.partition("\t")
        if sha.strip() and name.strip():
            seen[name.strip()] = sha.strip()
    safe, gone, ambiguous = [], 0, 0
    for ref, sha in rows:
        if ambiguous_with(ref, seen):
            ambiguous += 1
        elif seen.get(ref) != sha:
            gone += 1
        else:
            safe.append((ref, sha))
    return safe, gone, ambiguous


def _delete_chunk(run, cwd, remote, rows, prefixes):
    """One push removing `rows` [(ref, sha)] by name, each under a lease on the sha that was listed.
    Every name is checked once more against the exact-prefix shape: this is the only line that deletes."""
    for ref, sha in rows:
        if parse_ref(ref, prefixes) is None or _SHA_RE.fullmatch(sha) is None:
            raise Refused(BAD_INPUT, "refusing to delete a name that is not one of ours")
    delete_argv = (["git", "push", "--delete"]
                   + ["--force-with-lease=%s:%s" % (ref, sha) for ref, sha in rows]
                   + [remote] + [ref for ref, _sha in rows])
    try:
        run(cwd, delete_argv)
    except Exception as exc:              # noqa: BLE001
        return False, _why(exc)
    return True, ""


def prune_backups(config, sdlc_dir, *, remote, run=None, cwd=None, clock=None, dry_run=False, budget=None):
    """Remove backups older than `backup.keep_days`, keeping the newest `backup.keep_last` of each unit, by exact
    prefix. At most MAX_DELETES per call. `dry_run` plans and deletes nothing. Never raises; an unreadable
    listing deletes nothing. Reports what it measured, including how many of the planned deletions the remote
    confirms gone on a second listing."""
    closed = _closed(config)
    if closed is not None:
        return closed
    run = run or sync._run
    cwd = str(cwd or pathlib.Path(sdlc_dir).parent)
    problem = _remote_problem(remote)
    if problem is not None:
        return _result(problem[0], why=problem[1])
    settings = feature_upkeep.read(config).settings
    prefixes, rejected = usable_prefixes(settings["backup.former_prefixes"])
    entries, why = _list(run, cwd, remote, prefixes)
    if entries is None:
        return _result(UNREADABLE, deleted=0, why="the backup listing could not be read; nothing was deleted: " + why)
    plan = plan_prune(entries, prefixes, _now(clock), settings["backup.keep_days"], settings["backup.keep_last"])
    todo = plan["delete"][:MAX_DELETES]
    report = {"prefixes": len(prefixes), "rejected_prefixes": rejected, "listed": len(entries), "kept": plan["kept"],
              "foreign": plan["foreign"], "malformed": plan["malformed"], "future": plan["future"],
              "units": plan["units"], "skipped_ambiguous": plan["ambiguous"], "skipped_vanished": 0,
              "candidates": len(plan["delete"]), "remaining": len(plan["delete"]) - len(todo),
              "deleted": 0, "failed_chunks": 0, "confirmed": None}
    if dry_run:
        report["would_delete"] = [ref for ref, _sha in todo]
        return _result(DRY_RUN, **report)
    if not todo:
        return _result(NOTHING, **report)
    not_pushed = set()
    for group in chunks(todo, budget or arg_budget()):
        checked = _recheck(run, cwd, remote, group)
        if checked is None:
            report["failed_chunks"] += 1
            report.setdefault("why", "the refs could not be re-read just before deleting; that chunk was not deleted")
            continue
        safe, gone, ambiguous = checked
        not_pushed.update(ref for ref, _sha in group if (ref, _sha) not in safe)
        group = safe
        report["skipped_vanished"] += gone
        report["skipped_ambiguous"] += ambiguous
        if not group:
            continue
        try:
            ok, reason = _delete_chunk(run, cwd, remote, group, prefixes)
        except Refused as refused:
            ok, reason = False, refused.why
        if not ok:
            report["failed_chunks"] += 1
            report.setdefault("why", reason)
    again, _why_again = _list(run, cwd, remote, prefixes)
    if again is None:
        report["confirmed"] = False
        report["deleted"] = None
        return _result(FAILED, why="the deletions could not be confirmed: the second listing failed", **report)
    present = {ref for _sha, ref in again}
    # a ref skipped at the re-read was never pushed by us (gone before, or kept as ambiguous): not "deleted"
    report["deleted"] = sum(1 for ref, _sha in todo if ref not in present and ref not in not_pushed)
    report["confirmed"] = True
    return _result(PRUNED if not report["failed_chunks"] else PARTIAL, **report)
