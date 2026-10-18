"""The "biggest chain first" pick-order term from the stored dependency map (#1620).

`chain_ranks(sdlc, config, cache, now)` returns a `ChainRanks` (or `None`) that answers, for a goal number,
`(-open_dependents, -depth)`: how many open goals transitively wait on it, then the longest chain of open
dependents above it. The sources put that pair right after the unit rank in both pick keys, so inside one
priority tier and one unit the goal that unblocks the most work is picked first. Smaller tuples sort
first, hence the negations.

`None` means "leave the order exactly as it is today". It is returned when `discovery.dag.order` is `off`,
or when the map cannot be trusted by the same gate the pick-time hold uses (`dag_pick._entry`: usable map and
a change check within 600 s), or on any error. A goal absent from the map ranks `(0, 0)`, the same as
a goal nothing waits on.

Only edges written in an issue body or found on the timeline count; a `comment`-source edge never holds
a goal (`dag_pick`) and never boosts one either.

Cost: the map is loaded once per pick call (the cache shared with `dag_pick`) and the two graphs are
built once, linear in nodes plus edges. A goal's number is computed on first ask and kept: the dependents
walk is linear in the goal's own subgraph, `dag_core.depth` visits each edge once across all asks. The
label queue asks about at most 200 goals; the board queue about its Ready cards. At 10x and 100x the cost
grows with the candidates asked about, not with the map; the worst case (every candidate the root of one
huge chain) is candidates times that chain, which the 200-oldest label window bounds. Not measured against a
real board.
"""
import importlib.util
import pathlib
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dag_pick = _load("dag_pick")
dag_core = dag_pick.dag_chain.dag_core
dag_store = dag_pick.dag_store


class ChainRanks:
    """Lazy `number -> (-dependents, -depth)`; unknown goals are `(0, 0)`."""

    def __init__(self, graph):
        self._edges = [(e[0], e[1], e[2]) for e in graph.edges if e[2] != "comment"]
        self._nodes = list(graph.nodes.values())
        self._build()

    def _build(self):
        edges, nodes = self._edges, self._nodes
        self._fwd = dag_core.build(nodes, edges)
        # Reversed edges: the "blockers below" of the reversed graph are the dependents above.
        self._rev = dag_core.build(nodes, [(b, a, s) for (a, b, s) in edges])
        self._memo = {}

    def mark_in_flight(self, number):
        """#1621: a goal a batch just claimed no longer waits anyone out in this view: drop the edges whose
        blocker it is and forget the memo, so later slots rank on what is still unclaimed."""
        try:
            n = int(number)
        except (TypeError, ValueError):
            return
        kept = [e for e in self._edges if e[0] != n]
        if len(kept) != len(self._edges):
            self._edges = kept
            self._build()
        self._memo = {}

    def get(self, number):
        try:
            n = int(number)
        except (TypeError, ValueError):
            return (0, 0)
        hit = self._memo.get(n)
        if hit is None:
            deps = len(dag_core.open_dependents(self._fwd, n))
            hit = self._memo[n] = (-deps, -dag_core.depth(self._rev, n) if deps else 0)
        return hit


def chain_ranks(sdlc, config, cache, now=None):
    """A `ChainRanks`, or `None` when the order must stay as it was. Never raises."""
    try:
        if dag_store.order(config) == "off":
            return None
        now = time.time() if now is None else now
        m, graph, _why = dag_pick._entry(sdlc, config, cache, now)
        if m is None or graph is None:
            return None
        return ChainRanks(graph)
    except Exception:                       # noqa: BLE001 - the old order is always safe
        return None
