"""Committed, repository-based store of recorded decisions (library only; no main).

One record per file, `<store_dir>/<id>.json`, created exclusively: parallel worktrees add distinct
new files and never edit a shared one, and the files ride the goal's own branch through the normal
commit flow. A generated index (`_index.json`, never committed) is a rebuildable cache. Every
function is inert unless `rulebook.records.enabled` is the JSON boolean true.

Records are about the repository and its areas, never about a person: an audit field the record
carries is written and read by nothing here (a test scans for it). Scale: about 1 KB per record;
the doctor warns above `max_files_warn` and `archive` folds old records into one file per year.
A crash mid-write can leave a truncated file; readers skip files that do not parse.
"""
import contextlib
import datetime
import importlib.util
import json
import os
import re
import subprocess
import sys
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
HOW = ("autonomous", "human", "imported")
#: `imported` marks a record read back from past work (decision_import.py), never a live answer.
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ARCHIVE_PREFIX = "archive-"
_INDEX, _LOCK = "_index.json", "_index.lock"


def _records_cfg(config):
    try:
        sub = config["rulebook"]["records"]
        return sub if isinstance(sub, dict) else {}
    except Exception:
        return {}


def enabled(config):
    return _records_cfg(config).get("enabled") is True and _open(config)


def _open(config):
    try:
        import importlib.util
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rulebook_cfg.py")
        spec = importlib.util.spec_from_file_location("rulebook_cfg", here)
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
    elif isinstance(record.get("id"), str) and record["id"].startswith(ARCHIVE_PREFIX):
        bad.append("id must not start with %r" % ARCHIVE_PREFIX)
    return bad


def redact(record, visibility):
    """Public removes the free-text keys; ids and enumerations stay."""
    out = dict(record)
    if visibility == "public":
        for key in FREE_TEXT:
            out.pop(key, None)
    return out


def _scrub_mod():
    spec = importlib.util.spec_from_file_location(
        "scrub", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrub.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scrub_record(record):
    """Secret-shape scrub (the leak scan's shape set) over the free-text keys and string values under
    `origin`. -> (new record, number of substitutions); a clean record comes back equal."""
    sc, out, n = _scrub_mod(), dict(record), 0

    def one(text):
        nonlocal n
        new = sc.scrub(text)
        n += max(0, new.count("[REDACTED") - text.count("[REDACTED"))
        return new

    for key in FREE_TEXT + ("shape",):
        if isinstance(out.get(key), str):
            out[key] = one(out[key])
    if isinstance(out.get("origin"), dict):
        out["origin"] = {k: one(v) if isinstance(v, str) else v for k, v in out["origin"].items()}
    return out, n


def append(sdlc_dir, record, config, now=None):
    """Create the record's own file exclusively; returns its id, or None when the part is closed."""
    if not enabled(config):
        return None
    problems = validate(record)
    if problems:
        raise ValueError("; ".join(problems))
    rid = record.get("id") or new_id(now)
    record, _n = scrub_record(record)
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
                      if n.endswith(".json") and not n.startswith("_") and not n.startswith(ARCHIVE_PREFIX))
    except OSError:
        return []


def _archives(store):
    try:
        return sorted(n for n in os.listdir(store) if n.startswith(ARCHIVE_PREFIX) and n.endswith(".json"))
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


def get(sdlc_dir, rid, config):
    """One record by id, or None; None as well when the part is closed."""
    if not enabled(config) or not isinstance(rid, str) or not _SAFE.match(rid):
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


def list_by(sdlc_dir, config, qkind=None, area=None):
    """Records matching the filters. Uses the index when it names exactly the files on disk; else scans. Empty when the part is closed."""
    if not enabled(config):
        return []
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


def reindex(sdlc_dir, config):
    """Rebuild the uncommitted index; {"indexed": n} or {"skipped": reason}. Writes nothing when closed."""
    if not enabled(config):
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
            path = os.path.join(store, ARCHIVE_PREFIX + "%d.json" % year)
            kept = {r.get("id"): r for r in (_read(path) or {}).get("records", []) if isinstance(r, dict)}
            for n, rec in items:
                stem = n[:-5]
                kept[stem] = dict(rec, id=stem)
            _publish(path, json.dumps({"records": [kept[k] for k in sorted(kept, key=str)]}, sort_keys=True, indent=1) + "\n")
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
        return "%d decision records exceed the configured ceiling of %d; fold old ones with the archive library function (no command runs it)" % (n, cap)
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


_MSG = "chore: publish recorded decisions"


def _git(root, *args):
    return subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=True, timeout=30)


def _unit_branches(root):
    names, units = set(), os.path.join(root, ".sdlc", "features", "units")
    try:
        files = os.listdir(units)
    except OSError:
        return names
    for n in files:
        try:
            with open(os.path.join(units, n), encoding="utf-8") as fh:
                rec = json.load(fh)
            found = [rec.get("branch")]
            for entry in rec.get("features", {}).values():
                found += [r.get("branch") for r in entry.get("repos", {}).values()]
        except (OSError, ValueError, AttributeError):
            continue
        names.update(b for b in found if isinstance(b, str) and b)
    return names


def _rulebook_rel(sdlc_dir, config):
    try:
        spec = importlib.util.spec_from_file_location("rulebook", os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "rulebook.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.settings(config)["path"]
    except Exception:
        return ".sdlc/rulebook.json"


def _default_ref(root):
    """The remote default branch ref (origin/HEAD, else origin/main, origin/master), or None."""
    head = _git(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").stdout.strip()
    for ref in ([head] if head else []) + ["origin/main", "origin/master"]:
        if _git(root, "rev-parse", "-q", "--verify", ref + "^{commit}").returncode == 0:
            return ref
    return None


def evidence_paths(sdlc_dir, config):
    """(root, [paths relative to root]) of the records folder, the autonomy folder and the rulebook file."""
    sdlc = os.path.abspath(str(sdlc_dir))
    root = os.path.dirname(sdlc)
    rel = os.path.relpath(sdlc, root)
    return root, [os.path.join(rel, settings(config)["store_dir"]), os.path.join(rel, "autonomy"),
                  _rulebook_rel(sdlc, config)]


def commits_differing(sdlc_dir, config, behind=True):
    """Commits touching the evidence paths: those on the default branch not in HEAD (`behind`), or those in
    HEAD not on the default branch. Read-only. `(count, [path names touched])`; `(0, [])` when it cannot
    be told (not a repository, no remote default branch, git missing): never a reason to refuse by itself."""
    try:
        root, paths = evidence_paths(sdlc_dir, config)
        ref = _default_ref(root)
        if ref is None:
            return 0, []
        span = "HEAD..%s" % ref if behind else "%s..HEAD" % ref
        count = _git(root, "rev-list", "--count", span, "--", *paths)
        if count.returncode != 0 or not count.stdout.strip().isdigit() or int(count.stdout) == 0:
            return 0, []
        files = _git(root, "log", "--name-only", "--format=", span, "--", *paths).stdout.split()
        names = sorted({next((p for p in (("decisions" if x.startswith(paths[0] + "/") else None),
                                          ("autonomy" if x.startswith(paths[1] + "/") else None),
                                          ("rulebook" if x == paths[2] else None)) if p), "evidence")
                        for x in files})
        return int(count.stdout), names
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0, []


def publish(sdlc_dir, config):
    """Commit (never push) the records folder, the autonomy folder and the rulebook file, and nothing else.

    Returns `{"committed", "reason"}`; never raises. Skips: gate closed, `records.publish` not the JSON
    boolean true, a goal worktree, a registered unit branch or a detached HEAD, nothing changed."""
    def skip(reason):
        return {"committed": False, "reason": reason}
    if not enabled(config):
        return skip("records-closed")
    if _records_cfg(config).get("publish") is not True:
        return skip("publish-off")
    try:
        sdlc = os.path.abspath(str(sdlc_dir))
        root = os.path.dirname(sdlc)
        if os.sep + os.path.join(".sdlc", "work") + os.sep in root + os.sep:
            return skip("goal-worktree")
        gd, cd = (_git(root, "rev-parse", "--path-format=absolute", x).stdout.strip()
                  for x in ("--git-dir", "--git-common-dir"))
        if not gd or gd != cd:
            return skip("goal-worktree")
        branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
        # A `feature/` branch is a unit branch even before its record names it (a unit just opened has
        # empty `repos`): nobody commits directly to a unit branch, so never publish onto one.
        if not branch or branch.startswith("feature/") or branch in _unit_branches(root):
            return skip("unit-branch")
        rel = os.path.relpath(sdlc, root)
        paths = [os.path.join(rel, settings(config)["store_dir"]), os.path.join(rel, "autonomy"),
                 _rulebook_rel(sdlc, config)]
        paths = [p for p in paths if os.path.exists(os.path.join(root, p))]
        if not paths or not _git(root, "status", "--porcelain", "--", *paths).stdout.strip():
            return skip("nothing-changed")
        if _git(root, "add", "--", *paths).returncode != 0:
            return skip("add-failed")
        done = _git(root, "commit", "-q", "-m", _MSG, "--", *paths)
        return {"committed": True, "reason": "committed"} if done.returncode == 0 else skip("commit-failed")
    except (OSError, subprocess.SubprocessError, ValueError):
        return skip("git-unavailable")


def main(argv):
    if len(argv) == 3 and argv[1] == "publish":
        try:
            with open(os.path.join(argv[2], "config.json"), encoding="utf-8") as fh:
                config = json.load(fh)
        except (OSError, ValueError):
            config = {}
        print(json.dumps(publish(argv[2], config)))
        return 0
    help_asked = len(argv) > 1 and argv[1] in ("-h", "--help")
    print("usage: decision_store.py publish <state-folder>", file=sys.stdout if help_asked else sys.stderr)
    return 0 if help_asked else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
