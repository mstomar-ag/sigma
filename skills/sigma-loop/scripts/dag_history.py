"""The dependency map's local history (#1622): append-only, monthly, numbers only.

The stored map (`dag_store`) is replaced on every refresh, so what it said yesterday is gone.
`dag_store.save`, the single writer of the map, hands the map it is replacing and the one it wrote to
`record_save`; `diff` turns the difference into records and `append` adds them to
`.sdlc/state/dag-history/YYYY-MM.jsonl`, the month of the record's own timestamp.

Records (`EVENTS`), each one JSON object on one line, `t` an ISO UTC stamp and `ev` the event name:
  * `edge-added` / `edge-removed`   `a` blocker, `b` blocked
  * `node-resolved`                 `n` (open in the old map, closed / merged / closed-unmerged in the new)
  * `node-deleted`                  `n` (in the old map, absent in the new: marked, never forgotten)
  * `chain-completed`               `chain` (lowest member), `n` (members); every member of a chain of two
                                    or more is resolved now and was not all resolved before
  * `chain-superseded`              `chain` (lowest member of the OLD chain), `to` (the lowest members of
                                    the chains that took its place; may be empty when it dissolved). A
                                    chain is a weak component of two or more nodes. It is superseded when
                                    it is merged with another, is split, or dissolves; a chain that only
                                    grows or shrinks and stays one-to-one is NOT superseded.
Nothing else is ever written: no title, label, name, url or comment text; the first map ever saved (no
old map) records nothing.

`append(sdlc, config, records)` is silent when `discovery.dag.mode` is `off`, writes one line per record
with `open(..., "a")` (one write each, so a concurrent writer interleaves whole lines), never raises and
prints one `dag history:` line to stderr on failure. No function here deletes, truncates or rewrites a
history path; `tests/test_dag_history_1622.py` scans this file for that. History is never pruned by
Sigma: the doctor warns when one month passes `WARN_BYTES` and says what a person may do.

`record_save` is `diff` + `append`, called by `dag_store.save` after a successful write, loaded lazily
and wrapped so a history failure never fails or delays a save. The same map saved twice diffs empty
and appends nothing (headers are not compared). Cost: one extra read and parse of the previous map per
save, plus the diff.

`read_month(sdlc, month)` opens only `<month>.jsonl`, `month` a strict `YYYY-MM`; anything else raises
`ValueError`. `size_bytes` and `health` use `stat`, never a read.

Growth: about 100 bytes a record. 50 changes a day for a year is 50 * 365 * 100 = 1.8 MB in total over 12
monthly files, about 150 KB a month, far below `WARN_BYTES` (1 MB); a burst month is the case the warning
exists for. At 10x and 100x the changes the year holds are 18 MB and 180 MB in total, still 1.5 MB and
15 MB per month file, so the warning fires and a person archives a closed month; reads stay one month.
`diff` is linear in nodes plus edges: measured by the test at 30,000 nodes and 29,999 edges: 0.13 s for the whole test on one development
machine, fixture build included (a single run, not a benchmark); the cost is paid once per save, inside the map lock the refresh already holds.
"""
import importlib.util
import json
import os
import pathlib
import re
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DIRNAME = "dag-history"
WARN_BYTES = 1_000_000
YEAR_BOUND_BYTES = 2_000_000            # 50 * 365 * 100 = 1.8 MB, with margin
EVENTS = ("edge-added", "edge-removed", "node-resolved", "node-deleted", "chain-completed",
          "chain-superseded")
RESOLVED = ("closed", "merged", "closed-unmerged")
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ------------------------------------------------------------------ the pure side

def _comps(m):
    """Weak components of two or more nodes: `{lowest member: frozenset(members)}`, and the node->id map
    over every component (singletons included)."""
    dag_core = _load("dag_core")
    nodes = [dag_core.Node(n["number"], n.get("kind", "issue"), n.get("state", "unknown"),
                           n.get("life", ""), n.get("prio")) for n in m["nodes"]]
    g = dag_core.build(nodes, [(e["blocker"], e["blocked"], e.get("source", "other")) for e in m["edges"]])
    chains, owner = {}, {}
    for c in dag_core.components(g):
        for n in c:
            owner[n] = c[0]
        if len(c) >= 2:
            chains[c[0]] = frozenset(c)
    return chains, owner


def _states(m):
    return {n["number"]: n.get("state") for n in m["nodes"]}


def _chain_records(old, new, st_old, st_new, ts):
    out = []
    old_chains, old_owner = _comps(old)
    new_chains, new_owner = _comps(new)
    for cid in sorted(new_chains):
        members = new_chains[cid]
        if all(st_new.get(n) in RESOLVED for n in members) and not all(st_old.get(n) in RESOLVED for n in members):
            out.append({"t": ts, "ev": "chain-completed", "chain": cid, "n": len(members)})
    for cid in sorted(old_chains):
        members = old_chains[cid]
        succ = sorted({new_owner[n] for n in members if n in new_owner and new_owner[n] in new_chains})
        sources = {old_owner[n] for s in succ for n in new_chains[s] if n in old_owner and old_owner[n] in old_chains}
        if len(succ) == 1 and sources == {cid}:
            continue                                    # one-to-one: grew or shrank, same chain
        out.append({"t": ts, "ev": "chain-superseded", "chain": cid, "to": succ})
    return out


def diff(old, new, ts):
    """The records that turn `old` into `new`, stamped `ts`. `old` None (the first map) -> `[]`."""
    if old is None or new is None:
        return []
    out = []
    # A map cut short by a read cap lacks nodes and edges that still exist, so it proves no removal, no
    # deletion and no chain change; history is append-only and a false record could never be erased.
    cut_short = bool((new.get("header") or {}).get("partial"))
    old_short = bool((old.get("header") or {}).get("partial"))
    e_old = {(e["blocker"], e["blocked"]) for e in old["edges"]}
    e_new = {(e["blocker"], e["blocked"]) for e in new["edges"]}
    for a, b in sorted(e_new - e_old):
        out.append({"t": ts, "ev": "edge-added", "a": a, "b": b})
    if not cut_short:
        for a, b in sorted(e_old - e_new):
            out.append({"t": ts, "ev": "edge-removed", "a": a, "b": b})
    st_old, st_new = _states(old), _states(new)
    for n in sorted(st_new):
        if n in st_old and st_new[n] in RESOLVED and st_old[n] not in RESOLVED:
            out.append({"t": ts, "ev": "node-resolved", "n": n})
    if not cut_short:
        for n in sorted(set(st_old) - set(st_new)):
            out.append({"t": ts, "ev": "node-deleted", "n": n})
    if not (cut_short or old_short):
        out.extend(_chain_records(old, new, st_old, st_new, ts))
    return out


# ------------------------------------------------------------------ the files

def iso(now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def month_of(now):
    return time.strftime("%Y-%m", time.gmtime(now))


def _dir(sdlc):
    return pathlib.Path(sdlc) / "state" / DIRNAME


def _check_month(month):
    if not isinstance(month, str) or not _MONTH.match(month):
        raise ValueError("month must be YYYY-MM, got %r" % (month,))
    return month


def _config(sdlc, config):
    if config is None:
        try:
            config = _load("dag_store").state.load_config(sdlc)
        except Exception:                               # noqa: BLE001
            config = {}
    return config


def _off(sdlc, config):
    return _load("dag_store").mode(_config(sdlc, config)) == "off"


def _append(sdlc, records):
    """Add each record to the file of its own month. One write per line."""
    by_month = {}
    for r in records:
        by_month.setdefault(_check_month(str(r["t"])[:7]), []).append(r)
    d = _dir(sdlc)
    os.makedirs(d, exist_ok=True)
    for month in sorted(by_month):
        with open(d / (month + ".jsonl"), "a", encoding="utf-8") as fh:
            for r in by_month[month]:
                fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")


def append(sdlc, config, records):
    """Append `records`. Nothing when mode is `off` or there is nothing to add. Never raises."""
    if not records:
        return
    try:
        if _off(sdlc, config):
            return
        _append(sdlc, records)
    except Exception as exc:                            # noqa: BLE001 - history never fails its caller
        print("dag history: not recorded (%s)" % type(exc).__name__, file=sys.stderr)


def record_save(sdlc, config, old_map, new_map, now):
    """`diff` + `append`, called by `dag_store.save` after a successful write (mode already not off)."""
    recs = diff(old_map, new_map, iso(now))
    if not recs:
        return
    try:
        _append(sdlc, recs)
    except Exception as exc:                            # noqa: BLE001
        print("dag history: not recorded (%s)" % type(exc).__name__, file=sys.stderr)


def read_month(sdlc, month):
    """The records of one month, oldest first. Opens `<month>.jsonl` and nothing else. A line that does
    not parse is skipped."""
    p = _dir(sdlc) / (_check_month(month) + ".jsonl")
    try:
        with open(p, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def size_bytes(sdlc, month=None, now=None):
    """Bytes of one month's file (default the current month); 0 when there is none."""
    month = _check_month(month or month_of(time.time() if now is None else now))
    try:
        return (_dir(sdlc) / (month + ".jsonl")).stat().st_size
    except OSError:
        return 0


def health(sdlc, now, config=None):
    """`{"month", "bytes", "warn"}` for the current month; None when mode is `off` or no file exists.
    `warn` is strictly above `WARN_BYTES`."""
    try:
        if _off(sdlc, config):
            return None
        month = month_of(now)
        p = _dir(sdlc) / (month + ".jsonl")
        if not p.is_file():
            return None
        size = p.stat().st_size
        return {"month": month, "bytes": size, "warn": size > WARN_BYTES}
    except Exception:                                   # noqa: BLE001
        return None
