"""One refresher per repository for the dependency map, by a lease on the ledger branch (#1957).

Without this every machine that has the map on reads the whole repository from GitHub once per TTL.
With the ledger on and `discovery.dag.mode` not `off`, the machine that is due first takes a lease, reads,
and publishes the snapshot (`dag/map.json`, #1956); every other machine folds the snapshot in and makes
no GitHub call.

A lease is one small file per writer, `dag/lease/<writer>.json` in the ledger worktree:
`{"writer", "taken_at", "expires_at"}` (epoch seconds). One file per writer means two machines never write
the same path, so a lease never conflicts in git. `expires_at` is `taken_at` plus the TTL plus a margin
(the larger of 300 seconds and 10 percent of the TTL), so a holder that refreshes on schedule renews before
its lease lapses. A lease whose holder died simply expires and the next due machine takes over: no person.

`holder(root, now)` is the unexpired lease with the smallest `(taken_at, writer)`. It is a pure function of
files every machine can read after a pull, so when two machines write before seeing each other, both read
the same two files after the next pull and agree on one holder; the other reads the snapshot.

`acquire(sdlc, config, now, force, git)` returns `local` (ledger off, mode off or no worktree: read as before,
no ledger path touched), `fold` (read nothing; the snapshot is, or will be, someone else's) or `holder`
(this machine reads). `force` (the explicit `refresh --force` and `rebuild`) skips the two fold checks,
writes its lease and reads on this machine. Any failure on a ledger-on repository folds: at worst one extra
read per TTL later, never a read storm, because `dag_refresh.RETRY_SECONDS` still applies.

Untrusted input: lease files come from the shared branch. A lease whose name does not match its writer, whose
stamps are not numbers, that starts more than `SKEW_SECONDS` in the future or spans more than twice a
legitimate lease is ignored, so a forged or clock-skewed file cannot park the refresh for longer than the TTL.
Scale: one directory listing of at most one small file per writer; writers are bounded by machines, not
issues. A foreign expired file is ignored on read and never deleted here (deleting another writer's file would
dirty the worktree for the next rebase); a writer's own file is overwritten, so nothing grows.
MEASURED: offline real-git tests only; the live number of reads is not measured.
"""
import importlib.util
import json
import pathlib
import re

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


DIR = "dag/lease"
MIN_MARGIN_SECONDS = 300
MARGIN_FRACTION = 0.10
SKEW_SECONDS = 300
_WRITER = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def margin_seconds(ttl_seconds):
    return max(MIN_MARGIN_SECONDS, MARGIN_FRACTION * ttl_seconds)


def writer(config):
    """This machine's lease name: the ledger actor and the host token."""
    ledger = _load("ledger")
    return "%s-%s" % (ledger.actor(config), ledger._host_token(config))


def lease_dir(root):
    return pathlib.Path(root) / DIR


def mine(root, config):
    """The ledger-relative path of my lease file when it exists, as a list (for `sync.publish`)."""
    try:
        name = "%s.json" % writer(config)
        return ["%s/%s" % (DIR, name)] if (lease_dir(root) / name).is_file() else []
    except Exception:                                   # noqa: BLE001
        return []


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _read_all(root, now, ttl_seconds):
    out = []
    d = lease_dir(root)
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        return out
    for p in files:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                               # noqa: BLE001 - damaged: ignored
            continue
        w = doc.get("writer") if isinstance(doc, dict) else None
        t, e = (_num(doc.get("taken_at")), _num(doc.get("expires_at"))) if isinstance(doc, dict) else (None, None)
        if not isinstance(w, str) or not _WRITER.match(w) or p.name != w + ".json" or t is None or e is None:
            continue
        if e <= now or t > now + SKEW_SECONDS:          # expired, or from the future
            continue
        if ttl_seconds is not None and e - t > 2 * (ttl_seconds + margin_seconds(ttl_seconds)):
            continue
        out.append({"writer": w, "taken_at": t, "expires_at": e})
    return out


def holder(root, now, ttl_seconds=None):
    """The unexpired lease with the smallest `(taken_at, writer)`, or None."""
    leases = _read_all(root, now, ttl_seconds)
    return min(leases, key=lambda x: (x["taken_at"], x["writer"])) if leases else None


def _snapshot_fresh(root, now, ttl_seconds):
    dag_store = _load("dag_store")
    snap, _by = dag_store.read_snapshot_at(root)
    t = dag_store.stamp(snap) if snap is not None else None
    return t is not None and -SKEW_SECONDS <= now - t < ttl_seconds


def _exchange(sdlc, config, git, do_publish):
    """Publish my lease (when asked) and pull. Statuses are not trusted: the files decide."""
    sync = _load("sync")
    try:
        if do_publish:
            sync.publish(sdlc, config, run=git)
        sync.pull(sdlc, config, run=git)
    except Exception:                                   # noqa: BLE001
        pass


def _write(root, me, now, ttl_seconds):
    state = _load("state")
    p = lease_dir(root) / (me + ".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"writer": me, "taken_at": now, "expires_at": now + ttl_seconds + margin_seconds(ttl_seconds)}
    state.atomic_write_text(p, json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")


def verdict(root, me, now, ttl_seconds=None):
    """`holder` when my lease is the holder, else `fold`."""
    h = holder(root, now, ttl_seconds)
    return "holder" if h is not None and h["writer"] == me else "fold"


def acquire(sdlc, config, now, force=False, git=None):
    """`local`, `fold` or `holder`; never raises."""
    dag_store = _load("dag_store")
    root = dag_store._ledger_root(sdlc, config)
    if root is None:
        return "local"
    try:
        ttl = dag_store.ttl_minutes(config) * 60
        me = writer(config)
        if not force:
            if _snapshot_fresh(root, now, ttl):
                return "fold"
            _exchange(sdlc, config, git, False)
            if _snapshot_fresh(root, now, ttl):
                return "fold"
            h = holder(root, now, ttl)
            if h is not None and h["writer"] != me:
                return "fold"
        _write(root, me, now, ttl)
        _exchange(sdlc, config, git, True)
        got = verdict(root, me, now, ttl)
        return "holder" if force else got
    except Exception:                                   # noqa: BLE001 - conservative: never a read storm
        return "fold"
