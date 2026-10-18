"""The map-backed pick-time hold and its shadow comparison (#1619).

`map_hold(sdlc, config, goal, cache, now)` answers `(blockers, reason)` from the stored dependency map
(`dag_store`): `blockers` is the sorted list of open blocker numbers of `goal` (possibly empty, meaning the
map says free), or `None` when the map cannot answer, with `reason` naming the one clause that failed. The
clauses, each on its own: the map file is missing or unreadable, `dag_store.usable` refuses it (schema,
not built, partial, future, stale), no change check ran within `dag_store.CHANGE_CHECK_SECONDS` (600 s,
read from the header's `checked_at`), the goal is not in the map, the goal or one of its blockers has an
`unknown` state. Open follows `dag_core.Graph.resolved`: an issue is resolved when closed, a pull request
only when merged (a closed-unmerged pull request never landed, so it still holds). A `comment`-source
edge never holds: only edges written in an issue body or found on the timeline count.

The map is loaded and the graph built ONCE per pick call and kept in `cache["dag_pick"]`; every later
candidate is a dict lookup. Scale: one file read and one graph build, linear in nodes plus edges (measured
by the map's own tests at 30,000 nodes: 0.05 s); nothing else here is per-node.

The shadow log, `.sdlc/state/dag-shadow.jsonl`, one JSON line per compared candidate, numbers and codes only:
`{"t": epoch, "g": goal, "c": class, "k": [causes], "o": [old blockers], "m": [map blockers]}`.
`c` is `agree-hold`, `agree-free`, `map-only` (the map holds something the old path did not) or
`old-only`. `k` holds, per differing blocker number, the first matching cause: `pr` (a pull request node in
the map), `comment-only` (the map has a comment-source edge from it to the goal), `cross-repo` (absent from
the map's nodes), else `unknown`. The file rotates at `ROTATE_BYTES` (1 MiB): it is renamed to
`dag-shadow.jsonl.1`, replacing the previous one, and the record goes to a fresh file, so the ceiling is
two files of about 1 MiB. At about 80 bytes a record that is 13,000 compared picks per file; at 10x and
100x the picks the log rotates faster and the oldest are dropped, which the days-in-shadow count survives
because `dag-shadow.meta.json` (`{"since": epoch}`, written once at the first record) is never rotated.
Logging never raises and never blocks a pick; an unwritable `.sdlc` loses the record silently.

`shadow_stats(sdlc, now)` reads both log files (local only) for the doctor row: compared picks,
disagreements, differing blockers by cause, whole days in shadow and whether the flip criterion holds:
at least 7 days, at least 200 compared picks and no `unknown` cause. Not measured against a real board:
the disagreement rate is what the shadow measures.
"""
import importlib.util
import json
import os
import pathlib
import re
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


dag_store = _load("dag_store")
dag_chain = _load("dag_chain")
state = dag_store.state

LOG = "dag-shadow.jsonl"
META = "dag-shadow.meta.json"
ROTATE_BYTES = 1024 * 1024
CLASSES = ("agree-hold", "agree-free", "map-only", "old-only")
CAUSES = ("pr", "comment-only", "cross-repo", "unknown")
FLIP_DAYS = 7
FLIP_PICKS = 200
_NUM = re.compile(r"#(\d+)")


# ------------------------------------------------------------------ the map's answer

def _entry(sdlc, config, cache, now):
    """The per-call `(map, graph, reason)`, loaded once."""
    hit = cache.get("dag_pick")
    if hit is not None:
        return hit
    m, why = dag_store.load(sdlc, config)
    graph = None
    if m is not None:
        ok, why = dag_store.usable(m, now, dag_store.ttl_minutes(config))
        if not ok:
            m = None
        else:
            h = m.get("header") or {}
            c = dag_store.parse_ts(h.get("checked_at"))
            if c is None or not (-dag_store.SKEW_SECONDS <= now - c <= dag_store.CHANGE_CHECK_SECONDS):
                m, why = None, "no change check within %d seconds" % dag_store.CHANGE_CHECK_SECONDS
            else:
                graph = dag_chain._graph(m)
    cache["dag_pick"] = hit = (m, graph, why if m is None else "ok")
    return hit


def map_hold(sdlc, config, goal, cache, now=None):
    """`(sorted open blocker numbers, "ok")` or `(None, reason)`. Never raises."""
    try:
        now = time.time() if now is None else now
        m, graph, why = _entry(sdlc, config, cache, now)
        if m is None:
            return None, why
        g = int(goal)
        node = graph.nodes.get(g)
        if node is None or not any(n["number"] == g for n in m["nodes"]):
            return None, "goal not in map"
        if node.state == "unknown":
            return None, "goal state unknown"
        out = []
        for e in graph.edges:
            blocker, blocked, source = e[0], e[1], e[2]
            if blocked != g or source == "comment":
                continue
            b = graph.nodes.get(blocker)
            if b is None or b.state == "unknown":
                return None, "blocker #%d state unknown" % blocker
            if not graph.resolved(blocker):
                out.append(blocker)
        return sorted(set(out)), "ok"
    except Exception as exc:                    # noqa: BLE001 - the old path is always safe
        return None, "map read failed (%s)" % type(exc).__name__


def hold_text(numbers):
    """The hold string the pick prints, in the old path's shape."""
    return ", ".join("#%d (blocked by)" % n for n in numbers)


# ------------------------------------------------------------------ the comparison

def numbers_of(hold):
    """The blocker numbers in an old-path hold string (`"#12 (blocked by), #15 (depends on)"`)."""
    return sorted({int(x) for x in _NUM.findall(hold or "")})


def _cause(m, goal, ref):
    if any(n.get("number") == ref and n.get("kind") == "pr" for n in m.get("nodes") or []):
        return "pr"
    for e in m.get("edges") or []:
        if e.get("blocker") == ref and e.get("blocked") == goal and e.get("source") == "comment":
            return "comment-only"
    if not any(n.get("number") == ref for n in m.get("nodes") or []):
        return "cross-repo"
    return "unknown"


def classify(old, new, m, goal):
    """`(class, causes)` for the old path's blocker numbers against the map's. Pure."""
    old, new = set(old), set(new)
    if old == new:
        return ("agree-hold" if old else "agree-free"), []
    kind = "map-only" if new - old else "old-only"
    causes = sorted({_cause(m, int(goal), r) for r in (old ^ new)})
    return kind, causes


def log_compare(sdlc, config, goal, old_hold, cache, now=None):
    """Append one record comparing the old decision with the map's. Nothing is written when the map
    cannot answer (nothing was compared). Never raises."""
    try:
        now = time.time() if now is None else now
        new, why = map_hold(sdlc, config, goal, cache, now)
        if new is None:
            return None
        m = cache["dag_pick"][0]
        old = numbers_of(old_hold)
        kind, causes = classify(old, new, m, goal)
        rec = {"t": int(now), "g": int(goal), "c": kind, "k": causes, "o": old, "m": new}
        _append(sdlc, rec, now)
        return rec
    except Exception:                           # noqa: BLE001 - logging never blocks a pick
        return None


def _append(sdlc, rec, now):
    try:
        d = pathlib.Path(sdlc) / "state"
        d.mkdir(parents=True, exist_ok=True)
        log = d / LOG
        meta = d / META
        if not meta.exists():
            state.atomic_write_text(meta, json.dumps({"since": int(now)}) + "\n")
        try:
            if log.stat().st_size >= ROTATE_BYTES:
                os.replace(log, d / (LOG + ".1"))
        except FileNotFoundError:
            pass
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
    except Exception:                           # noqa: BLE001 - an unwritable .sdlc loses the record
        pass


# ------------------------------------------------------------------ the doctor's numbers

def shadow_stats(sdlc, now=None):
    """`None` when there is no log, else a dict: compared, disagreements, causes, days, flip."""
    now = time.time() if now is None else now
    d = pathlib.Path(sdlc) / "state"
    recs = []
    for name in (LOG + ".1", LOG):
        try:
            with open(d / name, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        recs.append(r)
        except OSError:
            continue
    if not recs:
        return None
    causes = {c: 0 for c in CAUSES}
    dis = 0
    for r in recs:
        if r.get("c") in ("map-only", "old-only"):
            dis += 1
            for c in r.get("k") or []:
                if c in causes:
                    causes[c] += 1
    since = None
    try:
        v = json.loads((d / META).read_text(encoding="utf-8")).get("since")
        since = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    except (OSError, ValueError, AttributeError):
        pass
    if since is None:
        ts = [r["t"] for r in recs if isinstance(r.get("t"), (int, float))]
        since = min(ts) if ts else now
    days = max(0, int((now - since) // 86400))
    flip = days >= FLIP_DAYS and len(recs) >= FLIP_PICKS and causes["unknown"] == 0
    return {"compared": len(recs), "disagreements": dis, "causes": causes, "days": days, "flip": flip}
