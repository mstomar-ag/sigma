"""The stored dependency map as the input of the blocker-priority repair (#1626).

`repair_input(sdlc, config, cache=None, now=None)` answers `None` (the caller takes today's path) or a dict:

* `corpus`: {ref: prio name or None} for every open map node that is in the corpus and whose life is a goal
  one (`goal`, `blocked`, `blocking`, `in-progress`); the map keeps a derived life, not the label set, so
  this is the nearest membership it can state (PROVISIONAL, owner to ratify, #1859: a parked or unrouted
  issue is not in it, even when it carries the goal label, because the label model says parked stands alone;
  a parked issue is also never a blocker the repair raises or strips: `parked` lists those refs).
* `edges`: [(blocker ref, blocked ref)] in sorted order, strings, taken from the edges written in an issue
  body and nothing else. Today's extractor reads the title and body only, so a `timeline`, `comment` or
  `other` edge is not an input (an `other` edge is a hand-off; the caller still reads the ledger itself,
  as today, which costs no network call).
* `states`: {ref: "open" | "closed" | "unknown"} for every ref an edge names. A pull request reads as
  `closed` (never written to), as `GitHubSource._blocker_not_open` does today.
* `prio`: {ref: prio name or None} for every ref an edge names.
* `blocker_refs`: every ref that blocks something.
* `parked`: every ref an edge names whose map life is `parked`.

`None` for every unusable clause: the mode is not written exactly `on` (the pick hold's gate), the map is
missing, unreadable, partial, stale or has no change check inside `dag_store.CHANGE_CHECK_SECONDS`
(all `dag_pick._entry`'s clauses), or anything raises. Pure reader: it opens one local file, makes no
`gh` call and calls no writer; a structural test holds that line.

Scale: one file read and one linear pass over nodes and edges (a map is bounded by `dag_store`'s own caps).
"""
import importlib.util
import pathlib
import time

_HERE = pathlib.Path(__file__).resolve().parent
_LIVES = ("goal", "blocked", "blocking", "in-progress")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


dag_pick = _load("dag_pick")


def gate_on(config):
    """True only when `discovery.dag.mode` is written exactly `on` (the pick hold's rule)."""
    block = (((config or {}).get("discovery") or {}).get("dag"))
    return isinstance(block, dict) and block.get("mode") == "on"


def repair_input(sdlc, config, cache=None, now=None):
    """The map-backed input, or `None` (take today's path). Never raises."""
    try:
        if not sdlc or not gate_on(config):
            return None
        now = time.time() if now is None else now
        m, _graph, _why = dag_pick._entry(sdlc, config, {} if cache is None else cache, now)
        if m is None:
            return None
        nodes = {n["number"]: n for n in m["nodes"]}
        corpus = {str(num): n.get("prio") for num, n in sorted(nodes.items())
                  if n.get("in_corpus") and n.get("kind", "issue") == "issue" and n.get("state") == "open"
                  and n.get("life") in _LIVES}
        edges = sorted({(str(e["blocker"]), str(e["blocked"])) for e in m["edges"]
                        if e.get("source", "other") == "body" and e["blocker"] != e["blocked"]},
                       key=lambda e: (int(e[0]), int(e[1])))
        states, prio = {}, {}
        for ref in {r for e in edges for r in e}:
            n = nodes.get(int(ref))
            prio[ref] = None if n is None else n.get("prio")
            if n is None or n.get("state") == "unknown":
                states[ref] = "unknown"
            elif n.get("kind") == "pr" or n.get("state") != "open":
                states[ref] = "closed"
            else:
                states[ref] = "open"
        return {"corpus": corpus, "edges": edges, "states": states, "prio": prio,
                "blocker_refs": {b for b, _ in edges},
                "parked": {r for r in prio if (nodes.get(int(r)) or {}).get("life") == "parked"}}
    except Exception:                           # noqa: BLE001 - the old path is always safe
        return None
