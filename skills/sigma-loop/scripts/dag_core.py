"""The pure dependency-graph core (#1609): a graph of goals and pull requests as an object.

Pure and deterministic: standard library only, no I/O, no network, no `gh`, nothing written. Every
walk is iterative, so a chain's depth is not bounded by the interpreter's recursion limit (the
one older shared walk, `discovery.blocker_required_ranks`, is recursive). The same input gives
byte-identical output: every result is sorted, and no result depends on the order nodes or edges
were supplied in, or on which node a walk happens to start at.

Input: `Node(number, kind, state, life, prio)` records and `(blocker, blocked, source)` edges.
`kind` is `issue` or `pr`; `state` is `open`, `closed`, `merged`, `closed-unmerged` or `unknown`.
`life` is carried for callers and not read here. `prio` is any value `discovery.priority_rank`
accepts. A node named only by an edge is created with state `unknown`.

Resolved means an issue that is `closed` or a pull request that is `merged`. A closed-unmerged pull
request (or a pull request reported just `closed`) never landed, so it stays unresolved. `unknown`
is reported by `unknown()` and never resolves. Everything below looks at UNRESOLVED nodes only: a
resolved blocker no longer holds anything, and a walk stops at it.

`edges_from(text, self_ref)` is the text extractor: it reuses `blocker_scan.extract_refs`, the same
explicit vocabulary that writes priority (unpark Q&A stripped, self and other-repository references
skipped), and returns edges for `self_ref`. A self edge in `build` is ignored.
"""
import collections
import heapq
import importlib.util
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


blocker_scan = _load("blocker_scan")   # the explicit blocker vocabulary, shared with the mirror
discovery = _load("discovery")         # priority_rank: the one rank vocabulary

Node = collections.namedtuple("Node", "number kind state life prio")


class Graph:
    """Built by `build`. Read-only by convention; `ops` counts edge visits in `depth` (a cost probe
    for the ceiling test, not a result)."""

    def __init__(self, nodes, blockers, dependents, edges):
        self.nodes = nodes              # number -> Node
        self.blockers = blockers        # number -> sorted list of direct blockers
        self.dependents = dependents    # number -> sorted list of direct dependents
        self.edges = edges              # sorted list of (blocker, blocked, source)
        self.ops = 0
        self._scc = None
        self._depth = {}

    def resolved(self, number):
        node = self.nodes.get(number)
        return node is not None and (
            (node.kind == "issue" and node.state == "closed")
            or (node.kind == "pr" and node.state == "merged"))

    def rank(self, number):
        return discovery.priority_rank(self.nodes[number].prio)


def edges_from(text, self_ref):
    """Edges `(blocker, self_ref, phrase)` for every explicit blocker reference in `text`."""
    out = []
    for rec in blocker_scan.extract_refs(text, self_ref=self_ref):
        if rec["ref"].isdigit():
            out.append((int(rec["ref"]), int(self_ref), rec["phrase"]))
    return out


def build(nodes, edges):
    """A `Graph` from node records and `(blocker, blocked, source)` edges. A duplicate edge is kept
    once (first source in sorted order); a self edge is dropped; an unseen endpoint becomes an
    `unknown` issue node. A node supplied twice keeps its last record."""
    by_num = {}
    for n in nodes:
        by_num[n.number] = n
    seen = {}
    for blocker, blocked, source in edges:
        if blocker == blocked:
            continue
        for num in (blocker, blocked):
            if num not in by_num:
                by_num[num] = Node(num, "issue", "unknown", "", None)
        key = (blocker, blocked)
        if key not in seen or str(source) < str(seen[key]):
            seen[key] = source
    blockers = {n: [] for n in by_num}
    dependents = {n: [] for n in by_num}
    for (blocker, blocked) in sorted(seen):
        blockers[blocked].append(blocker)
        dependents[blocker].append(blocked)
    return Graph({n: by_num[n] for n in sorted(by_num)}, blockers, dependents,
                 [(a, b, seen[(a, b)]) for (a, b) in sorted(seen)])


def _open(g):
    return [n for n in g.nodes if not g.resolved(n)]


def unknown(g):
    """Every node whose state is `unknown`, sorted. Never resolved, so it holds what it blocks."""
    return [n for n, node in g.nodes.items() if node.state == "unknown"]


def closure(g, goal):
    """The unresolved transitive blockers of `goal`, sorted; `goal` itself excluded. A walk stops at
    a resolved node (it is not listed and nothing behind it is reached through it)."""
    if goal not in g.nodes:
        return []
    seen, stack = {goal}, [goal]
    while stack:
        cur = stack.pop()
        for b in g.blockers[cur]:
            if b not in seen and not g.resolved(b):
                seen.add(b)
                stack.append(b)
    seen.discard(goal)
    return sorted(seen)


def open_dependents(g, number):
    """Unresolved nodes that transitively wait on `number`, sorted. The walk goes through
    unresolved nodes only, so a resolved node in the middle cuts it."""
    if number not in g.nodes:
        return []
    seen, stack = {number}, [number]
    while stack:
        cur = stack.pop()
        for d in g.dependents[cur]:
            if d not in seen and not g.resolved(d):
                seen.add(d)
                stack.append(d)
    seen.discard(number)
    return sorted(seen)


def _components_scc(g, start=None):
    """Iterative Tarjan over the unresolved subgraph (edges blocked -> blocker). Returns
    `(comp_of, comps)`: `comps` in emission order, a component's blockers always emitted before it.
    Computed once per graph and cached (unless `start` names the node to walk from first, which
    exists so a test can prove the answer does not depend on it). Entry independent: the SCC partition is a property of the
    graph, and members are sorted before use."""
    if start is None and g._scc is not None:
        return g._scc
    index, low, on_stack, stack = {}, {}, set(), []
    comp_of, comps, counter = {}, [], 0
    succ_of = {}
    roots = _open(g)
    if start is not None and start in g.nodes and not g.resolved(start):
        roots = [start] + roots
    for root in roots:
        if root in index:
            continue
        index[root] = low[root] = counter; counter += 1
        stack.append(root); on_stack.add(root)
        work = [(root, 0)]
        while work:
            node, i = work.pop()
            succ = succ_of.get(node)
            if succ is None:                                # once per node: a pop is not a rebuild
                succ = succ_of[node] = [b for b in g.blockers[node] if not g.resolved(b)]
            if i < len(succ):
                work.append((node, i + 1))
                nxt = succ[i]
                if nxt not in index:
                    index[nxt] = low[nxt] = counter; counter += 1
                    stack.append(nxt); on_stack.add(nxt)
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            if low[node] == index[node]:
                members = []
                while True:
                    m = stack.pop(); on_stack.discard(m)
                    comp_of[m] = len(comps)
                    members.append(m)
                    if m == node:
                        break
                comps.append(sorted(members))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    if start is None:
        g._scc = (comp_of, comps)
    return comp_of, comps


def cycles(g, start=None):
    """Every distinct blocking cycle once, as a sorted tuple of members, the list sorted. A cycle is
    a strongly connected group of two or more unresolved nodes; a self edge never reaches here.
    `start` only picks which node the walk enters first; the answer is the same for every start."""
    _, comps = _components_scc(g, start)
    return sorted(tuple(c) for c in comps if len(c) > 1)


def _kahn_levels(g):
    """Levels of unresolved nodes by Kahn's algorithm; members of a cycle and anything waiting on
    one never reach zero blockers and are left out."""
    left = {n: sum(1 for b in g.blockers[n] if not g.resolved(b)) for n in _open(g)}
    level = sorted(n for n, k in left.items() if k == 0)
    out = []
    while level:
        out.append(sorted(level, key=lambda n: (g.rank(n), n)))
        nxt = []
        for n in level:
            for d in g.dependents[n]:
                if d in left:
                    left[d] -= 1
                    if left[d] == 0:
                        nxt.append(d)
        level = sorted(nxt)
    return out


def waves(g):
    """Level 0 is every unresolved node with no unresolved blocker; a node sits one level above its
    deepest blocker. Each level sorted by priority rank then number. Cycle members, and what waits
    on one, are omitted."""
    return _kahn_levels(g)


def leaves(g):
    """Unresolved nodes with no unresolved blocker (ready to work), sorted."""
    return sorted(n for n in _open(g) if all(g.resolved(b) for b in g.blockers[n]))


def order(g):
    """A blocker-first order of unresolved nodes (Kahn). Among nodes ready together, the more urgent
    priority rank first, then the lower number. Cycle members, and what waits on one, are omitted."""
    left = {n: sum(1 for b in g.blockers[n] if not g.resolved(b)) for n in _open(g)}
    heap = [(g.rank(n), n) for n, k in left.items() if k == 0]
    heapq.heapify(heap)
    out = []
    while heap:
        _, n = heapq.heappop(heap)
        out.append(n)
        for d in g.dependents[n]:
            if d in left:
                left[d] -= 1
                if left[d] == 0:
                    heapq.heappush(heap, (g.rank(d), d))
    return out


def depth(g, number):
    """The longest chain of unresolved blockers below `number` (0 for a leaf or a resolved node).
    A cycle counts as one step (its members share a depth). Memoised per strongly connected group,
    so across any number of calls each blocker edge is visited once."""
    if number not in g.nodes or g.resolved(number):
        return 0
    comp_of, comps = _components_scc(g)
    memo = g._depth

    def below(c):
        out = set()
        for m in comps[c]:
            for b in g.blockers[m]:
                if not g.resolved(b) and comp_of[b] != c:
                    out.add(comp_of[b])
        return sorted(out)

    start = comp_of[number]
    stack = [(start, below(start), 0)]
    while stack:
        c, kids, i = stack.pop()
        if i < len(kids):
            stack.append((c, kids, i + 1))
            k = kids[i]
            g.ops += 1
            if k not in memo:
                stack.append((k, below(k), 0))
            continue
        memo[c] = 1 + max((memo[k] for k in kids), default=-1)
    return memo[start]


def components(g):
    """Weakly connected components over every node (resolved ones included), each a sorted tuple,
    the list sorted."""
    parent = {n: n for n in g.nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for blocker, blocked, _ in g.edges:
        a, b = find(blocker), find(blocked)
        if a != b:
            parent[max(a, b)] = min(a, b)
    groups = {}
    for n in g.nodes:
        groups.setdefault(find(n), []).append(n)
    return sorted(tuple(sorted(v)) for v in groups.values())


def summary(g):
    """Every result in one JSON-ready dict, in a fixed key order: what the determinism test
    compares byte for byte."""
    return {
        "cycles": [list(c) for c in cycles(g)],
        "order": order(g),
        "waves": waves(g),
        "leaves": leaves(g),
        "unknown": unknown(g),
        "components": [list(c) for c in components(g)],
        "depth": {str(n): depth(g, n) for n in g.nodes},
        "closure": {str(n): closure(g, n) for n in g.nodes},
        "dependents": {str(n): open_dependents(g, n) for n in g.nodes},
    }
