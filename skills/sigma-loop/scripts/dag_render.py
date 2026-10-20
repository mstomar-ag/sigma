"""#1624: DAG.md, a pure function of the merged dependency-map history records.

`render(records)` takes every record of every writer (`dag/YYYY-MM/*.jsonl` on the ledger branch) and
returns the text of `DAG.md`. Same record set in any order -> identical bytes: records are sorted by
`(t, ev, a, b, n, chain)` before folding, so the latest observation of an edge wins (`edge-added` and
`edge-removed` at the same instant: removed wins, it sorts later). Nothing but ticket numbers (`#N`) is ever
written: the records hold no title or name, and this module reads only `t`, `ev`, `a`, `b`, `n`, `chain`.

Sections: Stuck chains (PROVISIONAL, owner to ratify: history carries no parked state, so a stuck root is an
unresolved node that holds other unresolved nodes and has no unresolved blocker of its own; most nodes held
first, ties by number), Active chains, Cycles, Completed this month (`chain-completed` records in the month
of the newest record), Counts. A resolved or deleted node is gone from every section but its edges still join
chains. Cost: sort plus linear folds in records, nodes and edges.
"""
import collections


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _key(r):
    return (str(r.get("t", "")), str(r.get("ev", "")), _int(r.get("a")) or 0, _int(r.get("b")) or 0,
            _int(r.get("n")) or 0, _int(r.get("chain")) or 0)


def _nums(ns):
    return ", ".join("#%d" % n for n in ns)


def _sccs(nodes, out):
    """Strongly connected components of size >= 2 (iterative Tarjan), each sorted, list sorted."""
    index, low, on, stack, res, counter = {}, {}, set(), [], [], [0]
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(sorted(out.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            adv = False
            for w in it:
                if w not in nodes:
                    continue
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(out.get(w, ())))))
                    adv = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if adv:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) >= 2:
                    res.append(sorted(comp))
    return sorted(res)


def _snapshot_records(snap):
    """The snapshot as the facts `render` folds: every edge present, every node not `open` resolved."""
    h = snap.get("header") if isinstance(snap.get("header"), dict) else {}
    t = h.get("built_at") or ""
    out = [{"t": t, "ev": "edge-added", "a": e["blocker"], "b": e["blocked"]} for e in snap.get("edges") or []]
    out += [{"t": t, "ev": "node-resolved", "n": n["number"]} for n in snap.get("nodes") or []
            if n.get("state", "open") != "open"]
    return out


def _age(built, now):
    import calendar
    import time
    try:
        secs = int(now - calendar.timegm(time.strptime(built, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None
    # whole days, on purpose: a finer age would change DAG.md on every publish tick and so commit and
    # push it forever; the exact build time is on the line above
    return "%d day(s)" % (max(secs, 0) // 86400)


def render(records, snapshot=None, built_by=None, now=None):
    """With `snapshot` (a cleaned map), chains, stuck, cycles and counts come from it and only
    `chain-completed` history records are used; provenance lines follow the title (#1956)."""
    recs = [r for r in records if isinstance(r, dict) and isinstance(r.get("ev"), str)]
    if snapshot is not None:
        recs = [r for r in recs if r["ev"] == "chain-completed"] + _snapshot_records(snapshot)
    recs.sort(key=_key)
    edges, resolved, completed, newest = {}, set(), [], ""
    for r in recs:
        ev, t = r["ev"], str(r.get("t", ""))
        newest = max(newest, t)
        a, b, n, ch = _int(r.get("a")), _int(r.get("b")), _int(r.get("n")), _int(r.get("chain"))
        if ev in ("edge-added", "edge-removed") and a is not None and b is not None:
            edges[(a, b)] = ev == "edge-added"
        elif ev in ("node-resolved", "node-deleted") and n is not None:
            resolved.add(n)
        elif ev == "chain-completed" and ch is not None:
            completed.append((t, ch, n))
    present = sorted(e for e, on in edges.items() if on)
    if not recs and snapshot is None:
        return "# DAG\n"
    allnodes = {x for e in present for x in e}
    # weak components over every present edge
    parent = {n: n for n in allnodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in present:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    comps = collections.defaultdict(list)
    for n in sorted(allnodes):
        comps[find(n)].append(n)
    openn = {n for n in allnodes if n not in resolved}
    out, inn = collections.defaultdict(set), collections.defaultdict(set)
    for a, b in present:
        if a in openn and b in openn:
            out[a].add(b)
            inn[b].add(a)
    stuck = []
    for n in sorted(openn):
        if inn[n] or not out[n]:
            continue
        seen, todo = set(), [n]
        while todo:
            for w in out[todo.pop()]:
                if w not in seen:
                    seen.add(w)
                    todo.append(w)
        stuck.append((len(seen), n))
    stuck.sort(key=lambda x: (-x[0], x[1]))
    lines = ["# DAG", ""]
    if snapshot is not None:
        built = (snapshot.get("header") or {}).get("built_at") or "unknown"
        lines += ["Built by %s at %s" % (built_by or "unknown", built)]
        age = _age(built, now) if now is not None else None
        if age:
            lines += ["Snapshot age: " + age]
        lines += [""]
    lines += ["## Stuck chains", ""]
    lines += ["- #%d holds %d" % (n, k) for k, n in stuck] or ["- none"]
    lines += ["", "## Active chains", ""]
    act = [(c, [n for n in m if n in openn]) for c, m in sorted(comps.items()) if len(m) >= 2]
    act = [(c, o, comps[c]) for c, o in act if o]
    lines += ["- #%d: %d open of %d: %s" % (c, len(o), len(m), _nums(o)) for c, o, m in act] or ["- none"]
    cyc = _sccs(openn, out)
    lines += ["", "## Cycles", ""]
    lines += ["- " + _nums(c) for c in cyc] or ["- none"]
    month = newest[:7]
    done = sorted({(ch, n) for t, ch, n in completed if t[:7] == month})
    lines += ["", "## Completed this month", ""]
    lines += ["- #%d%s" % (ch, "" if n is None else " (%d goals)" % n) for ch, n in done] or ["- none"]
    lines += ["", "## Counts", "",
              "open %d, resolved %d, edges %d, chains %d, stuck %d, cycles %d, completed %d"
              % (len(openn), len(allnodes - openn), len(present), sum(1 for m in comps.values() if len(m) >= 2),
                 len(stuck), len(cyc), len(done))]
    return "\n".join(lines) + "\n"
