"""When the stored dependency map is refreshed, and how its death is made visible (#1612).

`refresh_if_due(sdlc_dir, config, run, now)` is the one policy. Three triggers call it, none trusted
alone: the reconcile tick's duty (`reconcile_tick.py`), the first pick of a process (`loop.py`) and the
explicit verb `dag.py refresh [--force]`. Nothing here starts a process, and nothing here runs unless
the operator wrote `discovery.dag.mode` (`shadow` or `on`) into the config: the automatic triggers
check `active(config)` first, so a repository that never opted in pays nothing, and mode `off` reads
and writes nothing at all.

Due means: no usable map file, or the map's last good stamp is older than the time to live
(`discovery.dag.ttl_minutes`, 60) plus a stable per-clone offset of up to 10 percent of it (a hash of
the clone path, so clones that run side by side on one machine do not refresh together). A failed
refresh is retried no sooner than `RETRY_SECONDS` later, so a broken `gh` is not hammered every tick.
One collection per call, under the refresh lock inside `dag_collect.refresh`; a sibling holding the
lock means `busy` and nothing is waited on.

Bounds, derived from the watcher call timeout (`SIGMA_WATCH_CALL_TIMEOUT`, default 120, the bound the
tick itself is killed by): a time budget of half of it and a call cap of one and a half calls per
second of it (180 at the default; a worst-case full read is about 125). Over either, the next call
raises `BudgetExceeded`, the read is recorded as an error and the old map stays as it was.

Liveness: every real attempt records `state/dag-refresh.meta.json` (last attempt, last status, and the
count of consecutive errors), written atomically; the doctor row and `line` read it and the map header.
Scale: one small file read per check; a due check at 10x or 100x the repository costs the same, the
refresh itself is `dag_collect`'s cost.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


dag_store = _load("dag_store")
dag_collect = _load("dag_collect")
dag_lease = _load("dag_lease")                          # #1957: one refresher per repository
dag_cycles = None                                       # #1615: loaded on first use after a saved refresh
dag_insert = None                                       # #1616: loaded on first use after a saved refresh

META = "dag-refresh.meta.json"
RETRY_SECONDS = 300
OFFSET_FRACTION = 0.10
TIMEOUT_ENV = "SIGMA_WATCH_CALL_TIMEOUT"
TIMEOUT_DEFAULT = 120.0
CALLS_PER_SECOND = 1.5
STALE_TTLS = 3                  # the doctor's bound: last good stamp older than this many TTLs
ERROR_RUN = 2                   # the doctor's other bound: this many consecutive errors


class BudgetExceeded(Exception):
    """The refresh used its call cap or its time budget."""


def active(config):
    """True only when the operator wrote `discovery.dag.mode` as `shadow` or `on`. The default
    (nothing written) reads as shadow in `dag_store.mode`, but starts no automatic read."""
    v = dag_store._key(config, "mode")
    return isinstance(v, str) and v in ("shadow", "on")


def clone_path(sdlc_dir):
    return os.path.realpath(os.path.dirname(os.path.abspath(str(sdlc_dir))))


def offset_minutes(path, ttl_minutes):
    """A stable value in [0, 10 percent of the TTL] minutes from a hash of the clone path."""
    h = int(hashlib.sha256(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:8], 16)
    return (h % 1001) / 1000.0 * OFFSET_FRACTION * ttl_minutes


def call_timeout():
    try:
        v = float(os.environ.get(TIMEOUT_ENV) or TIMEOUT_DEFAULT)
    except ValueError:
        v = TIMEOUT_DEFAULT
    return max(5.0, v)


def bounds():
    """`(call_cap, budget_seconds)`."""
    t = call_timeout()
    return int(CALLS_PER_SECOND * t), t / 2.0


def _meta_path(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "state" / META


def read_meta(sdlc_dir):
    try:
        d = json.loads(_meta_path(sdlc_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                   # noqa: BLE001 - absent or unreadable is empty
        return {}


def _write_meta(sdlc_dir, record):
    try:
        p = _meta_path(sdlc_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        dag_store.state.atomic_write_text(p, json.dumps(record, sort_keys=True))
    except Exception:                                   # noqa: BLE001 - costs one extra attempt, never a crash
        pass


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def is_due(sdlc_dir, config, now):
    """`(due, why)`; reads the config's TTL, the map header and the retry stamp. Touches no network."""
    if dag_store.mode(config) == "off":
        return False, "off"
    meta = read_meta(sdlc_dir)
    last = _number(meta.get("last_attempt"))
    if meta.get("last_status") in ("error", "folded") and last is not None and 0 <= now - last < RETRY_SECONDS:
        return False, "retry-wait"
    m, why = dag_store.load(sdlc_dir, config)
    if m is None:
        return True, why
    t = dag_store.stamp(m)
    if t is None:
        return True, "no stamp"
    ttl = dag_store.ttl_minutes(config)
    limit = (ttl + offset_minutes(clone_path(sdlc_dir), ttl)) * 60
    if now - t >= limit:
        return True, "stale"
    return False, "fresh"


def _bounded(run, cap, budget, clock=time.monotonic):
    run = run or dag_collect.gh_api._default_run
    used = [0]
    start = clock()

    def wrapped(args):
        if used[0] >= cap:
            raise BudgetExceeded("call cap")
        if clock() - start > budget:
            raise BudgetExceeded("time budget")
        used[0] += 1
        return run(args)
    return wrapped


def _post_cycles(sdlc_dir, config, run):
    """#1615: after a saved refresh, one comment per new dependency cycle through the same bounded
    runner. Loaded only here (never on the pick path); can never fail the refresh."""
    try:
        global dag_cycles
        if dag_cycles is None:
            dag_cycles = _load("dag_cycles")
        dag_cycles.post(sdlc_dir, config, run)
    except Exception:                                   # noqa: BLE001
        pass


def _read_unchecked(sdlc_dir):
    """#1847: the pairs on file before collection starts; the saved map covers exactly these. []"""
    try:
        global dag_insert
        if dag_insert is None:
            dag_insert = _load("dag_insert")
        return dag_insert.unchecked(sdlc_dir)
    except Exception:                                   # noqa: BLE001
        return []


def _clear_unchecked(sdlc_dir, seen):
    """#1616/#1847: edges written without a cycle verdict before this refresh began are examined by the
    map it just saved (and by `dag_cycles` after it), so those are dropped; a pair recorded during the
    refresh stays for the next one, so the list cannot grow without bound but nothing is lost. Never raises."""
    try:
        if seen:
            dag_insert.drop_unchecked(sdlc_dir, seen)
    except Exception:                                   # noqa: BLE001
        pass


def refresh_if_due(sdlc_dir, config, run=None, now=None, force=False):
    """Refresh the map when due (or `force`). Never raises. -> `{status, why, calls}` where status is
    `off`, `fresh` (not due, no read), `retry-wait`, `folded` (another machine holds the lease; no read), `busy`, `kept`, `saved` or `error`."""
    now = time.time() if now is None else now
    try:
        if config is None:
            config = dag_store.state.load_config(sdlc_dir)
    except Exception:                                   # noqa: BLE001
        config = {}
    if dag_store.mode(config) == "off":
        return {"status": "off", "why": "off"}
    if not force:
        due, why = is_due(sdlc_dir, config, now)
        if not due:
            return {"status": why if why == "retry-wait" else "fresh", "why": why}
    else:
        why = "forced"
    if dag_lease.acquire(sdlc_dir, config, now, force=force) == "fold":     # #1957: another machine reads
        prior = read_meta(sdlc_dir)
        _write_meta(sdlc_dir, {"last_attempt": now, "last_status": "folded", "last_error": None,
                               "errors": int(_number(prior.get("errors")) or 0)})
        return {"status": "folded", "why": why}
    seen = _read_unchecked(sdlc_dir)
    cap, budget = bounds()
    counted = _bounded(run, cap, budget)
    try:
        rep = dag_collect.refresh(sdlc_dir, config, run=counted, now=now)
        status, err = rep["status"], rep.get("last_error")
    except Exception as exc:                            # noqa: BLE001 - a refresh never fails its caller
        status, err = "error", type(exc).__name__
    if status == "busy":
        return {"status": "busy", "why": why}
    prior = read_meta(sdlc_dir)
    failed = status != "saved"
    streak = (int(_number(prior.get("errors")) or 0) + 1) if failed else 0
    _write_meta(sdlc_dir, {"last_attempt": now, "last_status": "error" if failed else "saved",
                           "last_error": err if failed else None, "errors": streak})
    if not failed:
        _clear_unchecked(sdlc_dir, seen)
        _post_cycles(sdlc_dir, config, counted)
    return {"status": "saved" if not failed else "kept", "why": why, "last_error": err}


# ------------------------------------------------------------------ the age line and the doctor row

def _age(seconds):
    s = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def line(sdlc_dir, config=None, now=None):
    """The one-line age report: "" when mode is off or there is no readable map. Reads the config
    and one file; no network, no lock, nothing created."""
    now = time.time() if now is None else now
    try:
        if config is None:
            config = dag_store.state.load_config(sdlc_dir)
    except Exception:                                   # noqa: BLE001
        config = {}
    if dag_store.mode(config) == "off":
        return ""
    m, _why = dag_store.load(sdlc_dir, config)
    if m is None:
        return ""
    t = dag_store.stamp(m)
    age = _age(now - t) if t is not None else "unknown"
    return "dependency map: age %s, %d nodes, %d edges" % (age, len(m["nodes"]), len(m["edges"]))


def health(sdlc_dir, config, now=None, live=False):
    """The doctor's facts, or None when mode is off. `ok` is False when the last good stamp is older
    than `STALE_TTLS` TTLs while `live` (a watcher heartbeat or loop session is live), or when the
    last `ERROR_RUN` refreshes errored. Each clause is separate: `stale` and `erroring`."""
    now = time.time() if now is None else now
    if dag_store.mode(config) == "off":
        return None
    ttl = dag_store.ttl_minutes(config)
    m, why = dag_store.load(sdlc_dir, config)
    t = dag_store.stamp(m) if m is not None else None
    stale = bool(live and t is not None and now - t > STALE_TTLS * ttl * 60)
    errors = int(_number(read_meta(sdlc_dir).get("errors")) or 0)
    erroring = errors >= ERROR_RUN
    out = {"mode": dag_store.mode(config), "ttl": ttl, "why": why, "age": None if t is None else now - t,
           "nodes": 0, "edges": 0, "cycles": 0, "errors": errors, "stale": stale, "erroring": erroring,
           "ok": not (stale or erroring)}
    if m is not None:
        out["nodes"], out["edges"] = len(m["nodes"]), len(m["edges"])
        try:
            core = dag_collect.dag_core
            nodes = [core.Node(n["number"], n["kind"], n["state"], n.get("life", ""), n.get("prio"))
                     for n in m["nodes"]]
            g = core.build(nodes, [(e["blocker"], e["blocked"], e["source"]) for e in m["edges"]])
            out["cycles"] = len(core.cycles(g))
        except Exception:                               # noqa: BLE001 - a count that cannot be made stays 0
            pass
    return out
