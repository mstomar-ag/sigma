"""Where the dependency map came from, how old the shared snapshot is, and who refreshes it (#1959).

Library. `facts(sdlc, config, now=None, live=False)` returns None when `discovery.dag.mode` is `off`, else
`{ledger, source, built_by, snapshot_age, holder, holder_expires_in, me, am_refresher, ttl_seconds, lease_dead}`.
`line(facts)` renders it for the doctor row and `dag.py report`.

Read-only and local: the source from `dag_store.load_with_source`, the snapshot's own stamp and builder token from
`dag_store.read_snapshot_at`, the holder from `dag_lease.holder`. No network, no write, never raises (a failed
read leaves the field None).

`lease_dead` is the one new failing verdict: ledger on, a snapshot older than `dag_refresh.STALE_TTLS` TTLs, no
unexpired lease and `live` (a watcher heartbeat or loop session is live, the doctor's own test). A snapshot that
old with a live lease holder is a slow but live refresher and is not failed here.
Scale: one snapshot read and one directory listing of at most one file per writer.
MEASURED: offline real-git fixtures only.
"""
import importlib.util
import pathlib
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


dag_store = _load("dag_store")
dag_lease = _load("dag_lease")
dag_refresh = _load("dag_refresh")


def facts(sdlc, config, now=None, live=False):
    now = time.time() if now is None else now
    if dag_store.mode(config) == "off":
        return None
    ttl = dag_store.ttl_minutes(config) * 60
    out = {"ledger": False, "source": "local", "built_by": None, "snapshot_age": None, "holder": None,
           "holder_expires_in": None, "me": None, "am_refresher": False, "ttl_seconds": ttl, "lease_dead": False}
    try:
        root = dag_store._ledger_root(sdlc, config)
    except Exception:                                   # noqa: BLE001
        root = None
    if root is None:
        return out
    out["ledger"] = True
    try:
        if dag_store.load_with_source(sdlc, config)[2] == "ledger":
            out["source"] = "ledger"
    except Exception:                                   # noqa: BLE001
        pass
    try:
        snap, built_by = dag_store.read_snapshot_at(root)
        t = dag_store.stamp(snap) if snap is not None else None
        out["built_by"] = built_by if snap is not None else None
        out["snapshot_age"] = None if t is None else max(0, int(now - t))
    except Exception:                                   # noqa: BLE001
        pass
    try:
        out["me"] = dag_lease.writer(config)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        h = dag_lease.holder(root, now, ttl)
    except Exception:                                   # noqa: BLE001
        h = None
    if h is not None:
        out["holder"] = h["writer"]
        out["holder_expires_in"] = max(0, int(h["expires_at"] - now))
        out["am_refresher"] = out["me"] is not None and h["writer"] == out["me"]
    out["lease_dead"] = bool(live and out["snapshot_age"] is not None
                             and out["snapshot_age"] > dag_refresh.STALE_TTLS * ttl and h is None)
    return out


def line(f):
    if not f:
        return ""
    if f["source"] != "ledger" and not f["ledger"]:
        return "source local (ledger off)"
    parts = ["source " + f["source"]]
    if f["snapshot_age"] is not None:
        s = "snapshot %s old" % dag_refresh._age(f["snapshot_age"])
        if f["built_by"]:
            s += " built by %s" % f["built_by"]
        parts.append(s)
    if f["holder"]:
        s = "lease held by %s for %s" % (f["holder"], dag_refresh._age(f["holder_expires_in"]))
        if f["am_refresher"]:
            s += " (this machine refreshes)"
        parts.append(s)
    else:
        parts.append("no lease holder")
    return ", ".join(parts)
