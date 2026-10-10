"""Committed, repository-based store of recorded decisions (library only; no main).

One record per file, `<store_dir>/<id>.json`, created exclusively: parallel worktrees add distinct
new files and never edit a shared one, and the files ride the goal's own branch through the normal
commit flow. A generated index (`_index.json`, never committed) is a rebuildable cache. Every
function is inert unless `decision_rubric.records.enabled` is the JSON boolean true.

Records are about the repository and its areas, never about a person: an audit field the record
carries is written and read by nothing here (a test scans for it). Scale: about 1 KB per record;
the doctor warns above `max_files_warn` and `archive` folds old records into one file per year.
A crash mid-write can leave a truncated file; readers skip files that do not parse.
"""
import contextlib
import datetime
import json
import os
import re
import subprocess
import tempfile
import time
import uuid

try:
    import fcntl
except ImportError:  # no kernel lock on this host: reindex refuses rather than half-guarding
    fcntl = None

DEFAULTS = {"store_dir": "decisions", "lock_timeout_s": 20, "archive_after_days": 180,
            "max_files_warn": 5000}
FREE_TEXT = ("choice", "reason")
HOW = ("autonomous", "human")
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_INDEX, _LOCK = "_index.json", "_index.lock"


def _records_cfg(config):
    try:
        sub = config["decision_rubric"]["records"]
        return sub if isinstance(sub, dict) else {}
    except Exception:
        return {}


def enabled(config):
    return _records_cfg(config).get("enabled") is True and _open(config)


def _open(config):
    try:
        import importlib.util
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "decision_rubric_cfg.py")
        spec = importlib.util.spec_from_file_location("decision_rubric_cfg", here)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.load(config).part_enabled("records")
    except Exception:
        return False


def _visibility(config):
    return "public" if _records_cfg(config).get("repo_visibility") == "public" else "private"


def settings(config):
    """Store settings; any wrong-typed or out-of-range value reads as its documented default."""
    sub, out = _records_cfg(config), dict(DEFAULTS)
    name = sub.get("store_dir")
    if isinstance(name, str) and _DIR.match(name):
        out["store_dir"] = name
    for key in ("lock_timeout_s", "archive_after_days", "max_files_warn"):
        v = sub.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            out[key] = v
    return out


def _store(sdlc_dir, config):
    return os.path.join(str(sdlc_dir), settings(config)["store_dir"])


def new_id(now=None):
    """Time-ordered and writer-unique: UTC second plus a random suffix."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return "%s-%s" % (now.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:8])


def validate(record):
    """List of problems; empty means the record is acceptable."""
    if not isinstance(record, dict):
        return ["record must be an object"]
    bad = []
    if record.get("how") not in HOW:
        bad.append("how must be one of %s" % (HOW,))
    for key in ("qkind", "choice"):
        if not isinstance(record.get(key), str) or not record[key]:
            bad.append("%s must be non-empty text" % key)
    for key in ("area", "reason", "status", "hardstop_class", "rule_id", "issue"):
        if key in record and record[key] is not None and not isinstance(record[key], (str, int)):
            bad.append("%s must be text" % key)
    if "id" in record and not (isinstance(record["id"], str) and _SAFE.match(record["id"])):
        bad.append("id must be a plain name")
    return bad


def redact(record, visibility):
    """Public removes the free-text keys; ids and enumerations stay."""
    out = dict(record)
    if visibility == "public":
        for key in FREE_TEXT:
            out.pop(key, None)
    return out


def append(sdlc_dir, record, config, now=None):
    """Create the record's own file exclusively; returns its id, or None when the part is closed."""
    if not enabled(config):
        return None
    problems = validate(record)
    if problems:
        raise ValueError("; ".join(problems))
    rid = record.get("id") or new_id(now)
    body = redact(dict(record, id=rid, time=(now or datetime.datetime.now(datetime.timezone.utc)).isoformat()),
                  _visibility(config))
    store = _store(sdlc_dir, config)
    if os.path.islink(store):
        raise ValueError("records folder is a symbolic link")
    os.makedirs(store, exist_ok=True)
    fd = os.open(os.path.join(store, rid + ".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(body, sort_keys=True, indent=1) + "\n")
    return rid


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _record_names(store):
    try:
        return sorted(n for n in os.listdir(store)
                      if n.endswith(".json") and not n.startswith("_") and not n.startswith("archive-"))
    except OSError:
        return []


def _archives(store):
    try:
        return sorted(n for n in os.listdir(store) if n.startswith("archive-") and n.endswith(".json"))
    except OSError:
        return []


def _all(sdlc_dir, config):
    store = _store(sdlc_dir, config)
    out = []
    for n in _record_names(store):
        rec = _read(os.path.join(store, n))
        if rec is not None:
            out.append(rec)
    for n in _archives(store):
        data = _read(os.path.join(store, n)) or {}
        out.extend(r for r in data.get("records", []) if isinstance(r, dict))
    return out


def get(sdlc_dir, rid, config=None):
    if not isinstance(rid, str) or not _SAFE.match(rid):
        return None
    store = _store(sdlc_dir, config)
    rec = _read(os.path.join(store, rid + ".json"))
    if rec is not None:
        return rec
    for n in _archives(store):
        for r in (_read(os.path.join(store, n)) or {}).get("records", []):
            if isinstance(r, dict) and r.get("id") == rid:
                return r
    return None


def list_by(sdlc_dir, qkind=None, area=None, config=None):
    """Records matching the filters. Uses the index when it names exactly the files on disk; else scans."""
    store = _store(sdlc_dir, config)
    idx = _read(os.path.join(store, _INDEX))
    recs = None
    if idx and isinstance(idx.get("entries"), list):
        live = sorted(e["id"] + ".json" for e in idx["entries"] if not e.get("archived"))
        if live == _record_names(store) and sorted(idx.get("archives", [])) == _archives(store):
            recs = idx["entries"]
    if recs is None:
        recs = _all(sdlc_dir, config)
    return [r for r in recs if (qkind is None or r.get("qkind") == qkind)
            and (area is None or r.get("area") == area)]


@contextlib.contextmanager
def _lock(store, timeout):
    """Bounded-wait exclusive lock; yields True when held, False on timeout or unsupported host."""
    if fcntl is None:
        yield False
        return
    os.makedirs(store, exist_ok=True)
    fd = os.open(os.path.join(store, _LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    held = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield held
    finally:
        if held:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _publish(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="._", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def reindex(sdlc_dir, config=None):
    """Rebuild the uncommitted index; {"indexed": n} or {"skipped": reason}. Writes nothing when closed."""
    if config is not None and not enabled(config):
        return {"skipped": "closed"}
    store = _store(sdlc_dir, config)
    if not os.path.isdir(store):
        return {"skipped": "no-store"}
    with _lock(store, settings(config)["lock_timeout_s"]) as held:
        if not held:
            return {"skipped": "lock-timeout" if fcntl is not None else "lock-unsupported"}
        entries = []
        for n in _record_names(store):
            rec = _read(os.path.join(store, n))
            if rec is not None:
                entries.append(rec)
        arch = _archives(store)
        for n in arch:
            for r in (_read(os.path.join(store, n)) or {}).get("records", []):
                if isinstance(r, dict):
                    entries.append(dict(r, archived=True))
        _publish(os.path.join(store, _INDEX), json.dumps({"entries": entries, "archives": arch}, sort_keys=True))
        return {"indexed": len(entries)}


def _id_time(rid):
    try:
        return datetime.datetime.strptime(str(rid)[:15], "%Y%m%dT%H%M%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def archive(sdlc_dir, older_than_days, config, now=None):
    """Person-run: fold records older than the age into one `archive-<year>.json` each; returns the count."""
    if not enabled(config):
        return 0
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=older_than_days)
    store = _store(sdlc_dir, config)
    folded = 0
    with _lock(store, settings(config)["lock_timeout_s"]) as held:
        if not held:
            return 0
        by_year = {}
        for n in _record_names(store):
            when = _id_time(n[:-5])
            rec = _read(os.path.join(store, n))
            if when is not None and when < cutoff and rec is not None:
                by_year.setdefault(when.year, []).append((n, rec))
        for year, items in sorted(by_year.items()):
            path = os.path.join(store, "archive-%d.json" % year)
            kept = {r.get("id"): r for r in (_read(path) or {}).get("records", []) if isinstance(r, dict)}
            for _, rec in items:
                kept[rec.get("id")] = rec
            _publish(path, json.dumps({"records": [kept[k] for k in sorted(kept)]}, sort_keys=True, indent=1) + "\n")
            for n, _ in items:
                os.unlink(os.path.join(store, n))
                folded += 1
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(store, _INDEX))
    return folded


def ceiling_warning(sdlc_dir, config):
    n = len(_record_names(_store(sdlc_dir, config)))
    cap = settings(config)["max_files_warn"]
    if n > cap:
        return "%d decision records exceed the configured ceiling of %d; run the archive verb" % (n, cap)
    return None


def untracked_old(sdlc_dir, now=None, config=None, min_age_days=1):
    """Ids of record files not yet known to git and older than a day (a record written outside a goal branch)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    store = _store(sdlc_dir, config)
    try:
        out = subprocess.run(["git", "-C", str(sdlc_dir), "ls-files", "--others", "--exclude-standard", "--",
                              settings(config)["store_dir"]], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    ids = []
    for line in out.stdout.splitlines():
        name = os.path.basename(line)
        if name in _record_names(store):
            age = now.timestamp() - os.path.getmtime(os.path.join(store, name))
            if age > min_age_days * 86400:
                ids.append(name[:-5])
    return sorted(ids)
