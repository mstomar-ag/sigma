"""The chain query (#1613): every unresolved transitive blocker of one goal, in order, as `dag-chain/1`.

Read-only: it reads the stored map (`dag_store.load`, `dag_store.usable`) and walks it with the pure
core (`dag_core`). It writes nothing: no file, no state directory, no lock. With `discovery.dag.mode`
off it reads nothing either.

Contract for the Chain's author. The object `chain()` returns is schema `dag-chain/1`, and changes to
it are ADDITIVE ONLY: a key may be added, a key is never renamed, removed or re-typed, and a closed
vocabulary (`why`, `life`, `state`, `role`) may gain a value but never lose or re-mean one. A consumer
must ignore keys it does not know. Keys:

  schema, goal, mode (shadow|on), source (map|map+verified), map_built_at, map_age_seconds,
  closure_id, complete,
  members   [{number, role blocker|goal, kind issue|pr, state, life, prio 0-5, level, waits_on, unit}]
  waves     [[number]] members grouped by level; the members of one wave have no path between them
  decisions [{number, why parked|unarmed|unmanaged|cross-repo|unknown|cycle, hint}]
  cycles    [[number]]
  counts    {open, parked, unarmed, unknown, resolved_skipped}
  verify    only when `verify` was asked: {read, failed, capped}

Members are the unresolved transitive blockers, blockers first and the goal last, in topological
order; among members ready together the more urgent priority rank comes first, then the lower number
(`dag_core.order`). A shared blocker is listed once. Membership looks at state and edges only: priority,
unit and every label except the life decide nothing. A walk stops at a resolved node. A cycle's members
are listed under `cycles` and `decisions` and are not in the order; `complete` is then false, and the
goal still comes last among the members it can be ordered with. `level` is the longest run of listed
unresolved blockers below a member; resolved members (`include_resolved`) sit first with level 0 and
are not in `waves` (there is nothing left to do for them).

`closure_id` is a hash of the sorted `(number, state, life)` of every unresolved member (cycle members
included, resolved ones not, so the flag does not move it). A chain calls once per step and compares:
a member added, resolved or changed life changes it; nothing else does.

`verify` re-reads each open member live through `reader(number)` -> `{"state", "kind"}` (default: the
REST helper `gh_api` through `dag_collect._outside`), at most `VERIFY_CAP` of them, blockers first. A
member that is now resolved is dropped and the walk redone on the new states. Past the cap, or on an
unreadable member, `complete` is false and `verify` says so.

Scale: linear in nodes plus edges per call, one map read; `--verify` costs at most 50 REST calls.
"""
import hashlib
import importlib.util
import json
import pathlib
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


dag_core = _load("dag_core")
dag_store = _load("dag_store")

SCHEMA = "dag-chain/1"
VERIFY_CAP = 50
EXIT = {"ok": 0, "unknown_goal": 2, "unusable": 3, "off": 4}

_LIFE_OUT = {"goal": "goal", "blocked": "blocked", "blocking": "blocking", "in-progress": "in-progress",
             "parked": "parked", "needs-label": "followup", "needs-unit": "followup"}


class ChainError(Exception):
    """An answer that is not a chain. `code` is the exit code (2, 3 or 4); `str(self)` is the line."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _config(sdlc):
    try:
        return dag_store.state.load_config(sdlc)
    except Exception:                                   # noqa: BLE001
        return {}


def _open_map(sdlc, now):
    """`(map, mode, config)` or raises ChainError 4 (off) / 3 (unusable)."""
    config = _config(sdlc)
    md = dag_store.mode(config)
    if md == "off":
        raise ChainError(EXIT["off"], "dag: off")
    m, why = dag_store.load(sdlc, config)
    if m is None:
        raise ChainError(EXIT["unusable"], "dag: map unusable (%s)" % why)
    ok, reason = dag_store.usable(m, now, dag_store.ttl_minutes(config))
    if not ok:
        raise ChainError(EXIT["unusable"], "dag: map unusable (%s)" % reason)
    return m, md, config


def _graph(m, states=None):
    states = states or {}
    nodes = []
    for n in m["nodes"]:
        st = states.get(n["number"], {})
        nodes.append(dag_core.Node(n["number"], st.get("kind", n.get("kind", "issue")),
                                   st.get("state", n.get("state", "unknown")),
                                   n.get("life", "unknown"), n.get("prio")))
    return dag_core.build(nodes, [(e["blocker"], e["blocked"], e.get("source", "other")) for e in m["edges"]])


def _why(node, in_cycle, foreign):
    if node.number in in_cycle:
        return "cycle", "break the cycle: drop one blocker line among %s" % ", ".join(
            "#%d" % n for n in sorted(in_cycle))
    if node.number in foreign:
        return "cross-repo", "resolve #%d in its own repository, then refresh the map" % node.number
    if node.state == "unknown" or node.life == "unknown":
        return "unknown", "read #%d and refresh the map: dag.py refresh --force" % node.number
    if node.life == "parked":
        return "parked", "/sigma-unpark %d" % node.number
    if node.life in ("needs-label", "needs-unit"):
        return "unarmed", "/sigma-promote %d" % node.number
    if node.life == "none":
        return "unmanaged", "/sigma-promote %d" % node.number
    return None, None


def _walk(g, m, goal, include_resolved, foreign):
    """The chain object minus the header fields (`_head` adds them)."""
    unit = {n["number"]: n.get("unit") for n in m["nodes"]}
    closure = dag_core.closure(g, goal)
    cyc = dag_core.cycles(g)
    cyc_members = {n for c in cyc for n in c}
    listed = set(closure) | {goal}
    in_cycle = {n for n in listed if n in cyc_members}
    own_cycles = [list(c) for c in cyc if any(n in listed for n in c)]
    sub_nodes = [g.nodes[n] for n in sorted(listed - in_cycle)]
    sub_edges = [(a, b, s) for a, b, s in g.edges if a in listed - in_cycle and b in listed - in_cycle]
    sub = dag_core.build(sub_nodes, sub_edges)
    order = dag_core.order(sub)
    levels = {}
    waves = dag_core.waves(sub)
    for i, w in enumerate(waves):
        for n in w:
            levels[n] = i
    resolved_seen = sorted({b for n in listed for b in g.blockers[n] if g.resolved(b)})
    resolved_in = resolved_seen if include_resolved else []
    seq = list(resolved_in) + [n for n in order if n != goal]
    if goal in sub.nodes:
        seq.append(goal)
    members = []
    for n in seq:
        node = g.nodes[n]
        res = n in resolved_in
        members.append({
            "number": n, "role": "goal" if n == goal else "blocker", "kind": node.kind,
            "state": node.state, "life": _LIFE_OUT.get(node.life, "unmanaged"),
            "prio": g.rank(n), "level": 0 if res else levels.get(n, 0),
            "waits_on": [] if res else [b for b in sub.blockers.get(n, [])],
            "unit": unit.get(n)})
    decisions = []
    for n in sorted(listed):
        why, hint = _why(g.nodes[n], in_cycle, foreign)
        if why:
            decisions.append({"number": n, "why": why, "hint": hint})
    ident = sorted((n, g.nodes[n].state, g.nodes[n].life) for n in listed)
    closure_id = hashlib.sha256(json.dumps(ident).encode()).hexdigest()[:16]
    by_why = lambda w: sum(1 for d in decisions if d["why"] == w)      # noqa: E731
    return {
        "closure_id": closure_id, "complete": not own_cycles,
        "members": members, "waves": waves, "decisions": decisions, "cycles": own_cycles,
        "counts": {"open": sum(1 for n in listed if g.nodes[n].state == "open"),
                   "parked": by_why("parked"), "unarmed": by_why("unarmed"),
                   "unknown": by_why("unknown"), "resolved_skipped": 0 if include_resolved else len(resolved_seen)},
    }, closure


def _default_reader(config):
    dc = _load("dag_collect")
    repo = dc._gh(config).get("repo") or ""

    def read(number):
        node, ok = dc._outside(dc.gh_api._default_run, repo, number, time.time())
        if not ok:
            raise RuntimeError("unreadable")
        return {"state": node["state"], "kind": node["kind"]}
    return read


def chain(sdlc, goal, *, include_resolved=False, verify=False, reader=None, now=None, foreign=()):
    """The `dag-chain/1` object for `goal`. Raises `ChainError` (code 2 goal unknown, 3 map unusable,
    4 mode off). `reader` is the live-read seam for `verify`; `foreign` names other-repository numbers
    (the stored map does not record a repository, so the command line passes none)."""
    now = time.time() if now is None else now
    m, md, config = _open_map(sdlc, now)
    g = _graph(m)
    if goal not in g.nodes:
        raise ChainError(EXIT["unknown_goal"], "dag: goal %d is not in the map" % goal)
    foreign = set(foreign)
    vinfo = None
    if verify:
        first, _ = _walk(g, m, goal, False, foreign)
        todo = [x["number"] for x in first["members"] if x["state"] in ("open", "unknown")]
        read = reader or _default_reader(config)
        states, failed = {}, []
        for n in todo[:VERIFY_CAP]:
            try:
                got = read(n)
                states[n] = {"state": got["state"], "kind": got.get("kind", g.nodes[n].kind)}
            except Exception:                           # noqa: BLE001 - unreadable stays as mapped
                failed.append(n)
        vinfo = {"read": len(states), "failed": failed, "capped": len(todo) > VERIFY_CAP}
        g = _graph(m, states)
    body, _ = _walk(g, m, goal, include_resolved, foreign)
    if vinfo and (vinfo["capped"] or vinfo["failed"]):
        body["complete"] = False
    t = dag_store.stamp(m)
    out = {"schema": SCHEMA, "goal": goal, "mode": md,
           "source": "map+verified" if verify else "map",
           "map_built_at": int(dag_store.parse_ts(m["header"].get("built_at")) or 0) or None,
           "map_age_seconds": max(0, int(now - t)) if t is not None else None}
    out.update(body)
    if vinfo is not None:
        out["verify"] = vinfo
    return out


def _ends_and_sets(g):
    """Weak components with two or more unresolved members: `[(members, ends)]`."""
    out = []
    for comp in dag_core.components(g):
        live = [n for n in comp if not g.resolved(n)]
        if len(live) < 2:
            continue
        # edges between unresolved members only: a component joined through a resolved node is split
        sub = dag_core.build([g.nodes[n] for n in live],
                             [(a, b, s) for a, b, s in g.edges if a in live and b in live])
        for part in dag_core.components(sub):
            if len(part) >= 2:
                ends = [n for n in part if not sub.dependents[n]]
                depth = max(dag_core.depth(sub, n) for n in part)
                out.append({"members": list(part), "ends": ends, "depth": depth})
    return out


def chains(sdlc, rank="biggest", now=None):
    """Weak components with at least two open members, `[{members, ends, depth}]`, ranked
    `biggest` (most members) or `longest` (deepest); ties by lowest member number. ChainError as `chain`."""
    now = time.time() if now is None else now
    m, _md, _c = _open_map(sdlc, now)
    found = _ends_and_sets(_graph(m))
    key = (lambda c: (-c["depth"], -len(c["members"]), c["members"][0])) if rank == "longest" \
        else (lambda c: (-len(c["members"]), -c["depth"], c["members"][0]))
    return sorted(found, key=key)


def stuck(sdlc, now=None):
    """Decision roots that hold other work: `[{root, why, hint, waiting}]`, most freeing first. A root
    is an unresolved member with no unresolved blocker that needs a human; `waiting` is every open
    node that transitively waits on it. Cycles are listed with `why: cycle` and their members. `kind` is
    the root's kind (issue|pr), added with #1614; a cycle entry also carries `cycle`, its sorted members."""
    now = time.time() if now is None else now
    m, _md, _c = _open_map(sdlc, now)
    g = _graph(m)
    cyc = dag_core.cycles(g)
    in_cycle = {n for c in cyc for n in c}
    out = []
    for n in dag_core.leaves(g):
        why, hint = _why(g.nodes[n], in_cycle, set())
        waiting = dag_core.open_dependents(g, n)
        if why and waiting:
            out.append({"root": n, "why": why, "hint": hint, "waiting": waiting, "kind": g.nodes[n].kind})
    for c in cyc:
        waiting = sorted({d for n in c for d in dag_core.open_dependents(g, n)} - set(c))
        out.append({"root": c[0], "why": "cycle", "hint": _why(g.nodes[c[0]], set(c), set())[1],
                    "waiting": sorted(set(c[1:]) | set(waiting)), "kind": g.nodes[c[0]].kind,
                    "cycle": sorted(c)})
    return sorted(out, key=lambda s: (-len(s["waiting"]), s["root"]))


LONGEST_TOP = 3


def longest(sdlc, by="length", now=None):
    """The end goals to work on first (#1591): `{"by", "ends": [top three], "excluded": [...]}`, from the
    stored map only. An end goal is an unresolved issue with no unresolved dependent and at least one
    unresolved blocker; its members are its unresolved closure plus itself. Default order: priority rank
    (P0 first), deeper chain, more members, earlier `updated_at` of any member (a map node carries no
    creation time; a member without one is last), lower number. `by="freeing"` puts the member count
    first. An end in or downstream of a dependency cycle is not offered; it is listed under `excluded`
    with the cycle. Writes nothing and reads no GitHub. ChainError as `chain`."""
    now = time.time() if now is None else now
    m, _md, _c = _open_map(sdlc, now)
    g = _graph(m)
    stamp = {n["number"]: dag_store.parse_ts(n.get("updated_at")) for n in m["nodes"]}
    cyc = [sorted(c) for c in dag_core.cycles(g)]
    in_cycle = {n for c in cyc for n in c}
    found, excluded = [], []
    for n, node in sorted(g.nodes.items()):
        if g.resolved(n) or node.kind != "issue" or any(not g.resolved(d) for d in g.dependents[n]):
            continue
        clo = dag_core.closure(g, n)
        if not clo and n not in in_cycle:
            continue
        reach = set(clo) | {n}
        hit = [c for c in cyc if any(x in reach for x in c)]
        if hit:
            excluded.append({"goal": n, "cycle": hit[0]})
            continue
        ts = [stamp[x] for x in reach if stamp.get(x) is not None]
        found.append({"goal": n, "prio": g.rank(n), "depth": dag_core.depth(g, n), "members": len(reach),
                      "oldest": min(ts) if ts else None})
    age = lambda e: (1, 0) if e["oldest"] is None else (0, e["oldest"])        # noqa: E731
    if by == "freeing":
        key = lambda e: (-e["members"], e["prio"], -e["depth"], age(e), e["goal"])      # noqa: E731
    else:
        key = lambda e: (e["prio"], -e["depth"], -e["members"], age(e), e["goal"])      # noqa: E731
    found.sort(key=key)
    return {"by": by, "ends": found[:LONGEST_TOP], "candidates": len(found), "excluded": excluded}
