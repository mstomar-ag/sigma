"""The read-back classifier and the pending-landing record for a unit landing (upkeep part C, slice 5).

A library, loaded by path; no CLI, and NOTHING CALLS IT YET. Every entry point that writes or reports asks the upkeep
gate (`feature_upkeep.enabled`, the only reader of the config block) first, and a closed gate means no file, no
directory, no row and no reader call: existing paths are untouched.

THE CLASSIFIER. `classify(pre, call, pr, parents)` is pure. Success is decided from the read-back, never from an exit
status. `pre` is what was seen BEFORE the call (the pull request open and unmerged, its head T, the base tip B). The
outcomes are:
  merged               the PR is merged, the merge commit has exactly two parents, the second is T and the first is B.
  merged-with-warning  merged and the second parent is T, but the first is not B (the base moved after it was read;
                       it can be detected and not prevented) or the parent count is not two.
  refused              the call returned an error AND a read shows the PR still open and unmerged.
  unconfirmed          a timeout, a lost acknowledgment, a failed read, no pre-merge observation, parents that could
                       not be read, or a merge whose second parent is not T. The pending record stays.
  armed                only on a POSITIVE read-back: an open, unmerged PR whose `auto_merge` is a non-empty object.
                       Whether REST shows this reliably is unverified, so nothing else ever yields it.
`merged_at` is a recorded fact and is never compared with a local clock.

THE RECORD. One small JSON document per unit at `state/unit-landings/<unit key>.json`, written BEFORE the call, rewritten
with the outcome and deleted on merged. The next landing for the unit settles a pending one with one PR read and one
commit read through injected readers (no reconcile-tick re-read). Stale records are aged out by `prune`; the existing
goal-state pruner is keyed by terminal goals and cannot own a unit-keyed store.

THE DOCTOR ROW. `doctor_row` only reads: the age of the oldest pending record and the exact re-run gesture. Age is the
tell: a landing that died is told from none by the record's presence and age.
"""
import collections
import importlib.util
import json
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


_LOADED = {}


def _sibling(name):
    """A sibling module, loaded once per process."""
    if name not in _LOADED:
        _LOADED[name] = _load(name)
    return _LOADED[name]


SCHEMA_ID = "upkeep-landing/1"
STORE_REL = "state/unit-landings"
SUFFIX = ".json"
MAX_RECORD_CHARS = 8192
MAX_PRUNE_SCAN = 100000
MAX_LIST_SCAN = 1000
DEFAULT_KEEP_DAYS = 14
RERUN_GESTURE = "python3 skills/sigma-rebase/scripts/verify_merge.py land .sdlc <unit>"

MERGED = "merged"
MERGED_WARNING = "merged-with-warning"
REFUSED = "refused"
UNCONFIRMED = "unconfirmed"
ARMED = "armed"
OUTCOMES = (MERGED, MERGED_WARNING, REFUSED, UNCONFIRMED, ARMED)
PENDING = "pending"
CALL_KINDS = ("ok", "error", "lost")

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_RECORD_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*\.json|\.[A-Za-z0-9_]{8}\.tmp")

Verdict = collections.namedtuple("Verdict", "outcome reason")
WriteResult = collections.namedtuple("WriteResult", "ok reason path")
Landing = collections.namedtuple("Landing", "outcome reason called")


def _sha(value):
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def _open(config):
    """The gate. A closed or unreadable config is closed; this module reads no config of its own."""
    try:
        return bool(_sibling("feature_upkeep").enabled(config))
    except Exception:  # noqa: BLE001 - a gate that cannot answer is closed
        return False


# --------------------------------------------------------------------------- the classifier


def classify(pre, call, pr, parents):
    """The outcome of one landing attempt -> Verdict(outcome, reason). Pure and total; never raises.

    pre      {"unmerged": True, "head": T, "base": B} seen before the call, or anything else (unproven).
    call     {"kind": "ok" | "error" | "lost", "status": int | None}, what the call itself did.
    pr       the PR read-back ({"merged", "state", "merge_commit_sha", "auto_merge", ...}) or None when it failed.
    parents  the merge commit's parent shas, or None when they could not be read.
    """
    try:
        return _classify(pre, call, pr, parents)
    except Exception:  # noqa: BLE001 - total: a malformed input is unconfirmed, never merged
        return Verdict(UNCONFIRMED, "malformed-input")


def _classify(pre, call, pr, parents):
    if not (isinstance(pre, dict) and pre.get("unmerged") is True and _sha(pre.get("head")) and _sha(pre.get("base"))):
        return Verdict(UNCONFIRMED, "no-pre-merge-observation")
    kind = call.get("kind") if isinstance(call, dict) else None
    if kind not in CALL_KINDS:
        kind = "lost"
    if not isinstance(pr, dict) or type(pr.get("merged")) is not bool:
        return Verdict(UNCONFIRMED, "readback-failed")
    if pr["merged"]:
        return _merged(pre, pr, parents)
    if isinstance(pr.get("auto_merge"), dict) and pr["auto_merge"] and pr.get("state") == "open":
        return Verdict(ARMED, "auto-merge-read-back")
    if kind == "error" and pr.get("state") == "open":
        status = call.get("status")
        return Verdict(REFUSED, "status-%s" % status if type(status) is int else "call-error")
    if pr.get("state") != "open":
        return Verdict(UNCONFIRMED, "closed-unmerged")
    return Verdict(UNCONFIRMED, "lost-acknowledgment" if kind == "lost" else "ok-but-unmerged")


def _merged(pre, pr, parents):
    if not _sha(pr.get("merge_commit_sha")):
        return Verdict(UNCONFIRMED, "no-merge-commit")
    if not (isinstance(parents, list) and parents and all(_sha(p) for p in parents)):
        return Verdict(UNCONFIRMED, "parents-unread")
    if len(parents) < 2 or parents[1] != pre["head"]:
        return Verdict(UNCONFIRMED, "merged-other-head")
    if len(parents) != 2:
        return Verdict(MERGED_WARNING, "parent-count-%d" % len(parents))
    if parents[0] != pre["base"]:
        return Verdict(MERGED_WARNING, "base-moved")
    return Verdict(MERGED, "verified")


# --------------------------------------------------------------------------- the record


def record_path(sdlc_dir, unit):
    """The one place a unit NAME becomes the path of its pending-landing record. Refuses what `feature_registry.unit_path`
    refuses (same predicate, same exception) and folds the name the same way, AFTER the refusal."""
    registry = _sibling("feature_registry")
    if not (isinstance(unit, str) and registry.is_unit_name(unit)):
        raise registry.InvalidUnitName("%r is not a unit name, so it has no landing record" % (unit,))
    return pathlib.Path(sdlc_dir) / STORE_REL / (registry.unit_key(unit) + SUFFIX)


def _document(unit, number, pre, call, now, outcome, reason, last_read_at=None, merged_at=None, merge_commit=None):
    return {"schema": SCHEMA_ID, "unit": unit, "pr": number, "head": pre.get("head"), "base": pre.get("base"),
            "observed_unmerged": pre.get("unmerged") is True, "call": call, "started_at": now, "outcome": outcome,
            "reason": reason, "last_read_at": last_read_at, "merged_at": merged_at, "merge_commit": merge_commit}


def _write(sdlc_dir, unit, doc):
    state = _sibling("state")
    try:
        path = record_path(sdlc_dir, unit)
    except ValueError:
        return WriteResult(False, "bad-unit", None)
    text = json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n"
    if len(text) > MAX_RECORD_CHARS:
        return WriteResult(False, "too-large", None)
    try:
        state.refuse_symlinks(sdlc_dir, path.relative_to(pathlib.Path(sdlc_dir)), create_parents=True)
        state.atomic_write_text(path, text)
    except OSError:
        return WriteResult(False, "unwritable", None)
    return WriteResult(True, None, path)


def read_record(sdlc_dir, unit):
    """The record, or None when absent, too large, not JSON or not of this schema. Never raises on file content."""
    try:
        path = record_path(sdlc_dir, unit)
        if path.is_symlink() or path.stat().st_size > MAX_RECORD_CHARS:
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA_ID else None


def begin(sdlc_dir, config, unit, number, pre, now):
    """Write the pending record BEFORE the merge call -> WriteResult. Closed gate: nothing is written.
    A pending or unconfirmed record is refused; a settled one (refused, merged-with-warning) is overwritten in place."""
    if not _open(config):
        return WriteResult(False, "gate-closed", None)
    if type(number) is not int or type(now) is not int:
        return WriteResult(False, "bad-input", None)
    existing = read_record(sdlc_dir, unit)
    if existing is not None and existing.get("outcome") in (PENDING, UNCONFIRMED):
        return WriteResult(False, "unsettled-record", None)
    return _write(sdlc_dir, unit, _document(unit, number, pre, None, now, PENDING, "call-in-flight"))


def finish(sdlc_dir, config, unit, verdict, now, pr=None, call=None):
    """Settle the record with the verdict: delete it on merged, rewrite it with the outcome otherwise -> WriteResult."""
    if not _open(config):
        return WriteResult(False, "gate-closed", None)
    doc = read_record(sdlc_dir, unit)
    if doc is None:
        return WriteResult(False, "no-record", None)
    if verdict.outcome == MERGED:
        try:
            path = record_path(sdlc_dir, unit)
            os.unlink(path)
        except OSError:
            return WriteResult(False, "unwritable", None)
        return WriteResult(True, None, path)
    pr = pr if isinstance(pr, dict) else {}
    doc.update(outcome=verdict.outcome, reason=verdict.reason, last_read_at=now,
               call=call if call is not None else doc.get("call"))
    if verdict.outcome == MERGED_WARNING:
        doc.update(merged_at=pr.get("merged_at"), merge_commit=pr.get("merge_commit_sha"))
    return _write(sdlc_dir, unit, doc)


def _read_back(read_pr, read_commit, number):
    """One PR read, and one commit read when the PR is merged -> (pr or None, parents or None). Never raises."""
    try:
        pr = read_pr(number)
    except Exception:  # noqa: BLE001 - a failed read is unconfirmed, not an error
        return None, None
    if not (isinstance(pr, dict) and pr.get("merged") is True and _sha(pr.get("merge_commit_sha"))):
        return pr if isinstance(pr, dict) else None, None
    try:
        return pr, read_commit(pr["merge_commit_sha"])
    except Exception:  # noqa: BLE001
        return pr, None


def settle(sdlc_dir, config, unit, read_pr, read_commit, now):
    """Bounded re-read of a pending record: one PR read and at most one commit read -> Verdict, or None when there is no
    record (or the gate is closed). A reader that fails leaves the record, with its last-read time updated."""
    if not _open(config):
        return None
    doc = read_record(sdlc_dir, unit)
    if doc is None:
        return None
    pre = {"unmerged": doc.get("observed_unmerged") is True, "head": doc.get("head"), "base": doc.get("base")}
    call = doc.get("call") if isinstance(doc.get("call"), dict) else {"kind": "lost", "status": None}
    pr, parents = _read_back(read_pr, read_commit, doc.get("pr"))
    verdict = classify(pre, call, pr, parents)
    finish(sdlc_dir, config, unit, verdict, now, pr=pr, call=call)
    return verdict


def run_landing(sdlc_dir, config, unit, number, pre, do_merge, read_pr, read_commit, now):
    """Record, call, read back, classify, settle -> Landing(outcome, reason, called). The record is written BEFORE
    `do_merge`; if it cannot be written the call is never made. `do_merge()` returns the call dict, or raises (a raise
    is a lost acknowledgment and leaves the pending record for the next landing)."""
    settle(sdlc_dir, config, unit, read_pr, read_commit, now)
    started = begin(sdlc_dir, config, unit, number, pre, now)
    if not started.ok:
        return Landing(REFUSED, "record-%s" % started.reason, False)
    try:
        call = do_merge()
    except Exception:  # noqa: BLE001
        call = {"kind": "lost", "status": None}
    pr, parents = _read_back(read_pr, read_commit, number)
    verdict = classify(pre, call, pr, parents)
    finish(sdlc_dir, config, unit, verdict, now, pr=pr, call=call)
    return Landing(verdict.outcome, verdict.reason, True)


# --------------------------------------------------------------------------- pruning, listing, the doctor row


def prune(sdlc_dir, config, keep_days=DEFAULT_KEEP_DAYS, now=None):
    """Remove record files (and a crashed write's temporary) older than `keep_days` -> how many were removed. Touches only
    names of the record shape, never follows or removes a symlink, scans at most MAX_PRUNE_SCAN entries. Closed gate: 0."""
    if not _open(config):
        return 0
    if type(keep_days) is not int or keep_days < 1:
        raise ValueError("keep_days must be a positive whole number")
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    removed = 0
    try:
        with os.scandir(pathlib.Path(sdlc_dir) / STORE_REL) as entries:
            for seen, entry in enumerate(entries):
                if seen >= MAX_PRUNE_SCAN:
                    break
                if not _RECORD_NAME.fullmatch(entry.name):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
    except OSError:
        return 0
    return removed


def pending(sdlc_dir):
    """[(unit, started_at)] of the readable records, oldest first. READ ONLY; bounded by MAX_LIST_SCAN entries."""
    found = []
    try:
        with os.scandir(pathlib.Path(sdlc_dir) / STORE_REL) as entries:
            for seen, entry in enumerate(entries):
                if seen >= MAX_LIST_SCAN:
                    break
                if not entry.name.endswith(SUFFIX) or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    if entry.stat(follow_symlinks=False).st_size > MAX_RECORD_CHARS:
                        continue
                    with open(entry.path, encoding="utf-8") as handle:
                        doc = json.loads(handle.read())
                except (OSError, ValueError, RecursionError):
                    continue
                if isinstance(doc, dict) and doc.get("schema") == SCHEMA_ID and type(doc.get("started_at")) is int \
                        and isinstance(doc.get("unit"), str):
                    found.append((doc["unit"], doc["started_at"]))
    except OSError:
        return []
    return sorted(found, key=lambda item: item[1])


def doctor_row(sdlc_dir, config, now):
    """One doctor check for the oldest pending landing, or None (closed gate, or nothing pending). Never writes."""
    if not _open(config):
        return None
    records = pending(sdlc_dir)
    if not records:
        return None
    unit, started = records[0]
    age = max(0, now - started)
    return {"name": "unit landing not left pending", "ok": False,
            "fix": "the landing of unit %s has been pending for %d minute(s) (%d record(s) in all); a landing that died "
                   "leaves this record. Re-run: %s" % (unit, age // 60, len(records), RERUN_GESTURE.replace("<unit>", unit))}
