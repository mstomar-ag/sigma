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
Every record also carries `u` (the source issue's `updated_at`, when the map has one) and `id` (#1958: a short hash of
the event, its numbers and `u`; `fold` applies each id once and a record without one gets the same id on read).
Nothing else is ever written: no title, label, name, url or comment text; the first map ever saved (no
old map) records nothing.

`append(sdlc, config, records)` is silent when `discovery.dag.mode` is `off`, writes one line per record
with `open(..., "a")` (one write each, so a concurrent writer interleaves whole lines), never raises and
prints one `dag history:` line to stderr on failure. No function here deletes, truncates or rewrites a
history path; `tests/test_dag_history_1622.py` scans this file for that. History is never pruned by
Sigma: the doctor warns when one month passes `WARN_BYTES` and says what a person may do.

Ledger mirror (#1623): only when `ledger.enabled` is true and the ledger worktree exists, the same records
are also appended to `<ledger worktree>/dag/YYYY-MM/<actor>-<host>.<pid>.jsonl` (`ledger.dag_file`; the host
token honours `ledger.host_alias` and the salt setting). `sync.publish` stages only the actor's own files of
that stream and pushes a change that is only there; `.gitattributes` carries `dag/**/*.jsonl merge=union`.
One file per writing process, as for entries: a file a month per process, about 100 bytes a record.

`record_save` is `diff` + `append`, called by `dag_store.save` after a successful write, loaded lazily
and wrapped so a history failure never fails or delays a save. The same map saved twice diffs empty
and appends nothing (headers are not compared). Cost: one extra read and parse of the previous map per
save, plus the diff.

Ledger readers (#1624): `read_all(root, after=None)` reads every writer's `dag/YYYY-MM/*.jsonl` under a ledger worktree,
skipping corrupt lines, each change once by `id` (#1958; `after` is the newest id a snapshot's `history_through` names); `contributors(root)` counts the writers and finds the newest record stamp.

`read_month(sdlc, month)` opens only `<month>.jsonl`, `month` a strict `YYYY-MM`; anything else raises
`ValueError`. `size_bytes` and `health` use `stat`, never a read.

Snapshot cut (#1967): a map save names `history_through` as `marks(root)`, the size of every writer file by `stat` only,
not a read of the history (it used to read and fold every record to name the newest id, inside the map lock). A reader
applies `read_all(root, after=marks)`: the bytes past each mark and every file the marks do not name, so a record
published late with an older stamp is applied, and no writer clock is consulted. MEASURED once on one development
machine, one record per file, best of 5: 30 files (a busy month, 1x) 0.3 ms, 300 files (10x) 2.5 ms, 3,000 files (100x)
33 ms, against 0.9, 12 and 117 ms for the read it replaces (a single run, not a benchmark; the old cost also grows with
the records in each file, the new one does not). Linear in files, one `stat` each; the test bounds 100x at 2 s.
A change that repeats an earlier local one in event, numbers and `u` carries `k` (see `_disambiguate`), so add, remove,
add under one unmoved `updated_at` keeps all three.

Growth: about 100 bytes a record. 50 changes a day for a year is 50 * 365 * 100 = 1.8 MB in total over 12
monthly files, about 150 KB a month, far below `WARN_BYTES` (1 MB); a burst month is the case the warning
exists for. At 10x and 100x the changes the year holds are 18 MB and 180 MB in total, still 1.5 MB and
15 MB per month file, so the warning fires and a person archives a closed month; reads stay one month.
`diff` is linear in nodes plus edges: measured by the test at 30,000 nodes and 29,999 edges: 0.13 s for the whole test on one development
machine, fixture build included (a single run, not a benchmark); the cost is paid once per save, inside the map lock the refresh already holds.
"""
import hashlib
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
_ID = re.compile(r"^[0-9a-f]{12}$")
_ID_FIELDS = ("ev", "a", "b", "n", "chain", "to", "u", "k")


def change_id(rec):
    """#1958: the identity of one change, a short hash of its event, its numbers and the source issue's
    `updated_at` (`u`; a record without one uses its own stamp `t`). Pure over the record's own fields, so the
    same change seen by two writers, or a record written before ids existed (no `id`, no `u`), gets the same
    id wherever it is read."""
    body = {k: rec.get(k) for k in _ID_FIELDS if rec.get(k) is not None}
    if "u" not in body and rec.get("t") is not None:
        body["u"] = rec["t"]                     # no source stamp (an old record): its own time, so a re-add is never folded away
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]


def with_id(rec):
    """`rec` with its `id` (a well-formed stored one is kept, a missing or malformed one is computed)."""
    stored = rec.get("id")
    return rec if isinstance(stored, str) and _ID.match(stored) else dict(rec, id=change_id(rec))


def _sort_key(rec):
    return (str(rec.get("t", "")), rec["id"])


def fold(records, after=None):
    """#1958: each change once. Records get their `id` (computed when absent), a repeat of an id is dropped
    (the first seen wins), and with `after` (the newest id a snapshot includes) only entries strictly later
    than it, by `(t, id)`, are kept. An `after` that no record carries applies everything: the cut is an
    optimisation and never loses a change. Order: oldest first."""
    seen, out = set(), []
    for r in records:
        if not isinstance(r, dict):
            continue
        r = with_id(r)
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(r)
    out.sort(key=_sort_key)
    if after:
        for i, r in enumerate(out):
            if r["id"] == after:
                return out[i + 1:]
    return out


def newest_id(records):
    """The id of the newest record by `(t, id)`, or None with no record."""
    folded = fold(records)
    return folded[-1]["id"] if folded else None


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
    """The records that turn `old` into `new`, stamped `ts`. `old` None (the first map) -> `[]`. Every record
    carries `u` (the source issue's `updated_at`: the blocked issue for an edge, the node itself, the chain's
    lowest member) and its `id` (#1958)."""
    if old is None or new is None:
        return []
    out = []
    # A map cut short by a read cap lacks nodes and edges that still exist, so it proves no removal, no
    # deletion and no chain change; history is append-only and a false record could never be erased.
    cut_short = bool((new.get("header") or {}).get("partial"))
    old_short = bool((old.get("header") or {}).get("partial"))
    e_old = {(e["blocker"], e["blocked"]) for e in old["edges"]}
    e_new = {(e["blocker"], e["blocked"]) for e in new["edges"]}
    if not old_short:                                    # a partial old map would make every dropped edge look new
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
    return [_stamp(r, old, new) for r in out]


def _updated(m):
    return {n["number"]: n.get("updated_at") for n in m["nodes"] if isinstance(n.get("updated_at"), str)}


def _stamp(rec, old, new):
    """Add `u` and `id` to a fresh record: the source issue's `updated_at` from the new map, else the old."""
    u_new, u_old = _updated(new), _updated(old)
    ev = rec["ev"]
    src = rec.get("b") if ev in ("edge-added", "edge-removed") else rec.get("n") if ev in ("node-resolved", "node-deleted") \
        else rec.get("chain")
    u = u_new.get(src) or u_old.get(src)
    if u:
        rec = dict(rec, u=u)
    return with_id(rec)


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


def _mirror(sdlc, config, records):
    """Mirror `records` into the ledger branch's `dag/YYYY-MM/<writer>.jsonl` (#1623). Only when
    `ledger.enabled` and the ledger worktree exists; otherwise nothing is written under `.sdlc/ledger`.
    The file is this writer process's own (same name as an entries file, so the host alias and salt
    settings apply); `sync.publish` stages only the actor's own. One write per line, the same bytes as
    the local file. Never raises."""
    try:
        config = _config(sdlc, config)
        ledger = _load("ledger")
        if not ledger.enabled(config) or not _load("sync").is_worktree(sdlc):
            return
        who = ledger.actor(config)
        by_month = {}
        for r in records:
            by_month.setdefault(_check_month(str(r["t"])[:7]), []).append(r)
        for month in sorted(by_month):
            p = ledger.dag_file(sdlc, who, month, config)
            os.makedirs(p.parent, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                for r in by_month[month]:
                    fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
    except Exception as exc:                            # noqa: BLE001 - the mirror never fails the local record
        print("dag history: ledger mirror not written (%s)" % type(exc).__name__, file=sys.stderr)


def append(sdlc, config, records):
    """Append `records`. Nothing when mode is `off` or there is nothing to add. Never raises."""
    if not records:
        return
    try:
        if _off(sdlc, config):
            return
        _append(sdlc, records)
        _mirror(sdlc, config, records)
    except Exception as exc:                            # noqa: BLE001 - history never fails its caller
        print("dag history: not recorded (%s)" % type(exc).__name__, file=sys.stderr)


def _base_id(rec):
    return change_id({k: v for k, v in rec.items() if k not in ("k", "id")})


def _disambiguate(sdlc, recs):
    """#1967: a change that repeats an earlier change of THIS writer's local history in event, numbers and `u` (a
    dependency edit that did not move the issue's `updated_at`: add, remove, add) gets `k`, the number of earlier
    records with that same base identity, folded into its id, so `fold` keeps all of them. Two writers with the same
    local history agree on `k`; one that missed the first add reads k = 0 and folds into it (documented, never
    loses a change it saw). Reads the local history only when a diff is non-empty."""
    want = {_base_id(r) for r in recs}
    seen = {}
    d = _dir(sdlc)
    for p in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                b = _base_id(r)
                if b in want:
                    seen[b] = seen.get(b, 0) + 1
    out = []
    for r in recs:
        n = seen.get(_base_id(r), 0)
        if n:
            r = dict(r, k=n)
            r["id"] = change_id(r)
        out.append(r)
    return out


def record_save(sdlc, config, old_map, new_map, now):
    """`diff` + `append`, called by `dag_store.save` after a successful write (mode already not off)."""
    recs = diff(old_map, new_map, iso(now))
    if not recs:
        return
    try:
        recs = _disambiguate(sdlc, recs)
        _append(sdlc, recs)
        _mirror(sdlc, config, recs)
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


# ------------------------------------------------------------------ every writer's files (#1624)

def _stream_files(root):
    base = pathlib.Path(root) / "dag"
    if not base.is_dir():
        return []
    return sorted(p for m in sorted(base.iterdir()) if m.is_dir() and _MONTH.match(m.name)
                  for p in sorted(m.glob("*.jsonl")) if p.is_file())


_MARK_KEY = re.compile(r"^\d{4}-\d{2}/[A-Za-z0-9._-]+\.jsonl$")


def marks(root):
    """#1967: `{"YYYY-MM/<file>.jsonl": size}` of every writer file under the ledger worktree `root`, by `stat` only
    (no read, no parse): what a snapshot names as `history_through`. Writer files are append-only, so everything past
    a mark, and every file a mark does not name, is newer than the snapshot whatever any writer's clock said."""
    out = {}
    for p in _stream_files(root):
        try:
            out[p.parent.name + "/" + p.name] = p.stat().st_size
        except OSError:
            continue
    return out


def valid_marks(value):
    """`value` when it is a well-formed marks dict (strict keys, non-negative int sizes), else None."""
    if isinstance(value, dict) and value and all(
            isinstance(k, str) and _MARK_KEY.match(k) and type(v) is int and v >= 0 for k, v in value.items()):
        return value
    return None


def _read_after_marks(root, after):
    out = []
    for p in _stream_files(root):
        start = after.get(p.parent.name + "/" + p.name, 0)
        try:
            with open(p, "rb") as fh:
                fh.seek(start)
                data = fh.read()
        except OSError:
            continue
        for line in data.decode("utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return fold(out)


def read_all(root, after=None):
    """Every record of every writer under the ledger worktree `root` (`dag/YYYY-MM/*.jsonl`), each change
    once (`fold`, #1958: an old record gets its computed `id`), oldest first. With `after`, the newest id a
    snapshot includes, only the entries later than it. `after` may also be the `marks` dict a snapshot names (#1967):
    then only the bytes past each file's mark and the files it does not name are read, independent of every writer's
    clock (a record published late with an older `t` is applied); the id form orders by `(t, id)` and is kept for
    snapshots written before marks. A line that does not parse, or is not an object,
    is skipped; an unreadable file is skipped."""
    if isinstance(after, dict):
        return _read_after_marks(root, valid_marks(after) or {})
    out = []
    for p in _stream_files(root):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return fold(out, after)


def contributors(root):
    """`(writers, newest)`: how many distinct `<actor>-<host>` writers have a dag file under `root` (the
    `.pid` of a process file does not make a new writer) and the newest record stamp (`t`) in any of them,
    '' when there is none. (PROVISIONAL: a writer is actor plus host token.)"""
    files = _stream_files(root)
    writers = {p.name[:-len(".jsonl")].rpartition(".")[0] or p.name for p in files}
    newest = max((str(r.get("t", "")) for r in read_all(root)), default="")
    return len(writers), newest
