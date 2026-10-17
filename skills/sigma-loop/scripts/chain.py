#!/usr/bin/env python3
"""chain.py -- the plan for one end goal: every open prerequisite it waits on, in order (#1584).

    chain.py plan <sdlc_dir> <N> [--json]

READ-ONLY toward GitHub. Every call goes through `_GetOnly`, which raises on anything that is not an
explicit `gh api ... --method GET`: no `gh issue ...` verb, no GraphQL, no body flag. The one write is
the local record `<sdlc_dir>/state/chains/c<N>.json` (issue numbers, verdict words and labels only; never
a title, a body, a comment or a question), written only for a plan that was produced, never for a refusal.

CLOSURE. From the stored dependency map in one local read (`dag_chain`), bounded by `chain.max_members`
(default 100). When the map is off, unusable, stale, or does not know the goal, from the label drain's
seed closure (`label_drain.closure`, one end goal as the seed), bounded by min(50, `chain.max_members`)
and one REST read per unknown reference. The output says which source and its age or the reason for the
fallback. A closure over the bound is refused with its count, never truncated; a cycle is refused with
every member named.

VERDICTS. Live: the label drain's `_Judge.verdict` (armed, unarmed, parked, upper-tier,
assigned-elsewhere, held-by-gate). From the map only the life is known, so only armed, unarmed, parked
and unknown can be given (`verdict_basis: map-life`). A parked member's kind is read through the label
drain's brief (several GETs, at most 20 members).

EDGES. `explicit` for an edge from an issue body, `ledger` for any other recorded source (the map's
comment, timeline and other edges). Weak "needs/after" mentions are counted from bodies and never
become an edge; from the map they are not knowable (`weak_mentions` is null).

PRIORITY. Inversions (a blocker less urgent than the goal that waits on it) and `higher_priority_ahead`
(open goals outside the chain more urgent than the least urgent member) are reported, never written.

Exit 0 planned, 2 refused or bad usage, 1 the record could not be written.
"""
import collections
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


label_drain = _load("label_drain")
dag_chain = _load("dag_chain")
dag_store = dag_chain.dag_store
dag_core = dag_chain.dag_core

SCHEMA = "chain-plan/1"
USAGE = ("usage: chain.py plan <sdlc_dir> <N> [--json]\n"
         "  Plan the chain of open prerequisites behind goal N, deepest first. Read-only toward GitHub;\n"
         "  writes only .sdlc/state/chains/c<N>.json (numbers and labels, no titles or bodies).")
DEFAULT_MAX_MEMBERS = 100
LIVE_MAX_MEMBERS = 50
_UNPRIORITISED = dag_core.discovery.UNPRIORITISED
_VERDICT_OF_LIFE = {"goal": "armed", "in-progress": "armed", "blocked": "armed", "blocking": "armed",
                    "parked": "parked", "needs-label": "unarmed", "needs-unit": "unarmed",
                    "none": "unarmed"}
_TAG_RE = re.compile(r"^(\S*#\d+) \((.*)\)$")


class ChainRefused(Exception):
    """A plan that is not produced. `members` names the cycle members when the cause is a cycle."""

    def __init__(self, message, members=()):
        super().__init__(message)
        self.members = sorted(members)


class _GetOnly(label_drain._ReadOnly):
    """The label drain's counting wrapper, narrowed: only an explicit `gh api ... --method GET`."""

    @staticmethod
    def _check(args):
        if args[:1] != ["api"] or args[1:2] == ["graphql"]:
            raise RuntimeError("chain is read-only: refused `gh %s`" % " ".join(args[:2]))
        methods = label_drain._ReadOnly._methods(args)
        if not methods or any(m != "GET" for m in methods):
            raise RuntimeError("chain is read-only: refused a `gh api` call that is not an explicit GET: %s"
                               % " ".join(args[:4]))


def _max_members(config):
    v = ((config or {}).get("chain") or {}).get("max_members") if isinstance((config or {}).get("chain"), dict) \
        else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else DEFAULT_MAX_MEMBERS


def _rank(prio):
    return dag_core.discovery.priority_rank(prio)


def _depths(members, waits):
    """Longest path (in edges) below each member; `waits` maps member -> its blockers among members."""
    memo = {}

    def d(n):
        if n not in memo:
            memo[n] = 0 if not waits.get(n) else 1 + max(d(b) for b in waits[n])
        return memo[n]
    return {n: d(n) for n in members}


def _cycles_of(edges_by_blocked):
    slices = _load("slices")
    graph = [{"id": str(n), "needs": [str(x) for x in bs], "status": "pending"}
             for n, bs in sorted(edges_by_blocked.items())]
    return [[int(x) for x in c] for c in slices._cycles(graph)]


def _refuse_cycles(cycles):
    if cycles:
        names = sorted({n for c in cycles for n in c})
        raise ChainRefused("dependency cycle among %s; break it first (drop one blocker line)"
                           % ", ".join("#%d" % n for n in names), names)


def _refuse_bound(count, bound, where, at_least=False):
    if count > bound:
        raise ChainRefused("chain has %s%d members, over the bound of %d (%s); refusing rather than "
                           "truncating. Raise chain.max_members or plan a nearer goal."
                           % ("at least " if at_least else "", count, bound, where))


def _brief_kinds(rows, sdlc, config, source):
    ld, scrub = label_drain, _load("scrub").scrub
    briefed, skipped, unpark = 0, [], None
    for r in rows:
        if r["verdict"] != "parked":
            continue
        tmp = {}
        briefed, unpark = ld._brief_row(tmp, sdlc, config, r["number"], source, scrub, briefed, skipped, unpark)
        r["kind"] = tmp.get("kind", "unreadable")
    return skipped


def _finish(out, rows, edges, ext, done, units, ranks, outside_ranks, weak):
    """Fields shared by both sources: order, depth, inversions, cross-unit, ahead-of."""
    nums = [r["number"] for r in rows]
    waits = collections.defaultdict(list)
    for e in edges:
        waits[e["to"]].append(e["from"])
    depth = _depths(nums, waits)
    for r in rows:
        r["depth"] = depth[r["number"]]
        r["waits_on"] = sorted(waits.get(r["number"], []))
    rows.sort(key=lambda r: (r["depth"], ranks[r["number"]], r["number"]))
    inversions = [{"blocker": e["from"], "dependent": e["to"], "blocker_prio": _label(ranks[e["from"]]),
                   "dependent_prio": _label(ranks[e["to"]])}
                  for e in edges if ranks[e["from"]] > ranks[e["to"]] and ranks[e["to"]] < _UNPRIORITISED]
    worst = max(ranks.values()) if ranks else _UNPRIORITISED
    out.update({
        "members": rows, "done": sorted(done), "externals": ext,
        "edges": sorted(edges, key=lambda e: (e["from"], e["to"])),
        "cross_unit": [{"from": e["from"], "to": e["to"], "units": [units[e["from"]], units[e["to"]]]}
                       for e in sorted(edges, key=lambda e: (e["from"], e["to"]))
                       if units.get(e["from"]) and units.get(e["to"])
                       and units[e["from"]].lower() != units[e["to"]].lower()],
        "priority_inversions": sorted(inversions, key=lambda i: (i["blocker"], i["dependent"])),
        "higher_priority_ahead": None if outside_ranks is None else sum(1 for r in outside_ranks if r < worst),
        "weak_mentions": weak,
    })


def _label(rank):
    return "P%d" % rank if rank < _UNPRIORITISED else None


def _external(items):
    by = {}
    for ref, kind, needed_by in items:
        e = by.setdefault((ref, kind), {"ref": ref, "kind": kind, "needed_by": set()})
        e["needed_by"].add(needed_by)
    return [dict(e, needed_by=sorted(e["needed_by"])) for _, e in sorted(by.items(), key=lambda kv: (kv[0][0], kv[0][1]))]


# --- the map ----------------------------------------------------------------------------------------

def _plan_map(sdlc, goal, config, ro, source, now, m, bound):
    g = dag_chain._graph(m)
    body, _closure = dag_chain._walk(g, m, goal, True, set())
    _refuse_cycles(body["cycles"])
    if g.resolved(goal):
        raise ChainRefused("#%d is already closed; there is no chain to plan" % goal)
    unit = {n["number"]: n.get("unit") for n in m["nodes"]}
    listed = {x["number"] for x in body["members"]}
    done, ext, keep = [], [], []
    for x in body["members"]:
        n = x["number"]
        if g.resolved(n) and n != goal:
            done.append(n)
        elif x["kind"] == "pr":
            for d in g.dependents[n]:
                if d in listed:
                    ext.append(("#%d" % n, "pull-request", d))
        else:
            keep.append(x)
    _refuse_bound(len(keep), bound, "from the map")
    members = {x["number"] for x in keep}
    esrc = {(e["blocker"], e["blocked"]): e.get("source") for e in m["edges"]}
    edges = [{"from": a, "to": b, "tag": "explicit" if esrc.get((a, b)) == "body" else "ledger"}
             for a, b, _s in g.edges if a in members and b in members]
    ranks = {n: g.rank(n) for n in members}
    rows = []
    for x in keep:
        life = dag_chain._LIFE_OUT.get(g.nodes[x["number"]].life, "unmanaged")
        raw = g.nodes[x["number"]].life
        verdict = "unknown" if (g.nodes[x["number"]].state == "unknown" or raw == "unknown") \
            else _VERDICT_OF_LIFE.get(raw, "unarmed")
        rows.append({"number": x["number"], "verdict": verdict, "life": life,
                     "prio": g.nodes[x["number"]].prio})
    outside = [g.rank(n) for n, nd in g.nodes.items()
               if n not in members and nd.kind == "issue" and nd.state == "open" and nd.life == "goal"]
    out = {"verdict_basis": "map-life", "map_built_at": int(dag_store.parse_ts(m["header"].get("built_at")) or 0) or None}
    t = dag_store.stamp(m)
    out["map_age_seconds"] = max(0, int(now - t)) if t is not None else None
    _finish(out, rows, edges, _external(ext), done, {n: unit.get(n) for n in members}, ranks, outside, None)
    skipped = _brief_kinds(out["members"], sdlc, config, source)
    out["parked_not_briefed"] = sorted(skipped)
    out["complete"] = not skipped and all(r["verdict"] != "unknown" for r in rows)
    out["stops"] = []
    return out


# --- the live fallback --------------------------------------------------------------------------------

def _weak_count(issue, goal_n, blocker_scan):
    text = blocker_scan.strip_unpark_qa(issue.get("body") or "")
    explicit = {i["ref"] for i in blocker_scan.extract_refs(text, self_ref=issue["number"])}
    weak = set()
    for mt in blocker_scan._BLOCK_RE.finditer(text):
        if blocker_scan.is_cross_repo(text, mt) or mt.group(1).lower() not in blocker_scan.TRIGGERS[3:]:
            continue
        ref = mt.group(2)
        if ref not in explicit and ref != str(issue["number"]):
            weak.add(ref)
    return len(weak)


def _plan_live(sdlc, goal, config, ro, source, bound):
    ld = label_drain
    gh_api, blocker_scan, features = _load("gh_api"), _load("blocker_scan"), _load("features")
    dag_collect = _load("dag_collect")
    repo = source.repo

    def get(n):
        return gh_api.view_issue(ro, n, repo or None, comments="none")

    clo = ld.closure([goal], get, max_depth=bound, max_pulled=bound, blocker_scan=blocker_scan)
    if goal not in clo["nodes"]:
        raise ChainRefused("#%d could not be read" % goal)
    seed = clo["nodes"][goal]["issue"]
    if "pull_request" in seed:
        raise ChainRefused("#%d is a pull request, not a goal" % goal)
    if str(seed.get("state") or "").lower() == "closed":
        raise ChainRefused("#%d is already closed; there is no chain to plan" % goal)
    _refuse_cycles(_cycles_of(clo["edges"]))
    _refuse_bound(len(clo["nodes"]), bound, "live read",
                  at_least=any("pulled-issue limit" in s for s in clo["stops"]))
    judge = ld._Judge(sdlc, config, source)
    prefix = dag_collect._gh(config).get("priority_label_prefix", "priority:")
    aliases = dag_collect._priority_aliases(config)
    rows, ranks, units, weak = [], {}, {}, 0
    for n in sorted(clo["nodes"]):
        issue = clo["nodes"][n]["issue"]
        names = ld._names(issue)
        verdict, _detail = judge.verdict(issue)
        prio = dag_collect._prio(names, prefix, aliases)
        ranks[n] = _rank(prio)
        units[n] = ld._unit(issue, judge.features)
        weak += _weak_count(issue, goal, blocker_scan)
        rows.append({"number": n, "verdict": verdict, "labels": sorted(names), "prio": prio})
    edges = [{"from": b, "to": n, "tag": "explicit"} for n, bs in clo["edges"].items() for b in bs]
    ext = []
    for n, tags in clo["unresolved"].items():
        for tag in tags:
            mt = _TAG_RE.match(tag)
            if not mt:
                continue
            ref, why = mt.groups()
            if why == "another repository":
                same_owner = repo and "/" in ref and ref.split("/")[0].lower() == repo.split("/")[0].lower()
                kind = "other-repository" if same_owner or "/" not in ref else "third-party"
            elif why == "pull request":
                kind = "pull-request"
            elif why.startswith("not read"):
                kind = "not-read"
            else:
                kind = "unreadable"
            ext.append((ref, kind, n))
    done = {r for rs in clo["closed"].values() for r in rs}
    try:
        listed = ld.sources.fetch_issues_rest(ro, repo, [source.goal_label], 200) if hasattr(ld, "sources") \
            else _load("sources").fetch_issues_rest(ro, repo, [source.goal_label], 200)
        outside = []
        for it in listed:
            nm = ld._names(it)
            if it["number"] in clo["nodes"] or source.parked_label in nm or source.goal_blocked_label in nm:
                continue
            outside.append(_rank(dag_collect._prio(nm, prefix, aliases)))
    except Exception:       # noqa: BLE001 - the comparison is advisory; say so rather than guess
        outside = None
    out = {"verdict_basis": "labels", "map_age_seconds": None, "stops": list(clo["stops"])}
    _finish(out, rows, edges, _external(ext), done, units, ranks, outside, weak)
    skipped = _brief_kinds(out["members"], sdlc, config, source)
    out["parked_not_briefed"] = sorted(skipped)
    out["complete"] = not clo["stops"] and not skipped and all(
        e["kind"] not in ("not-read", "unreadable") for e in out["externals"])
    return out


# --- the plan ---------------------------------------------------------------------------------------

def plan(sdlc_dir, goal, config=None, run=None, now=None):
    """The plan dict for `goal`, and the local record written. Raises `ChainRefused`."""
    sources, state = _load("sources"), _load("state")
    now = time.time() if now is None else now
    config = config if config is not None else state.load_config(sdlc_dir)
    max_members = _max_members(config)
    ro = _GetOnly(run or sources._run_gh)
    source = sources.GitHubSource(config, run=ro)
    why = None
    try:
        m, _md, _cfg = dag_chain._open_map(sdlc_dir, now)
        if goal not in {n["number"] for n in m["nodes"]}:
            m, why = None, "goal not in map"
    except dag_chain.ChainError as exc:
        m, why = None, ("off" if str(exc) == "dag: off" else re.sub(r"^dag: ", "", str(exc)))
    if m is not None:
        out = _plan_map(sdlc_dir, goal, config, ro, source, now, m, max_members)
        out.update(source="map", source_reason=None, bound=max_members)
    else:
        bound = min(LIVE_MAX_MEMBERS, max_members)
        out = _plan_live(sdlc_dir, goal, config, ro, source, bound)
        out.update(source="live", source_reason=why, bound=bound)
    out.update(schema=SCHEMA, goal=goal, calls=ro.summary())
    path = pathlib.Path(sdlc_dir) / "state" / "chains" / ("c%d.json" % goal)
    os.makedirs(path.parent, exist_ok=True)
    state.atomic_write_text(path, json.dumps(out, indent=2, sort_keys=True) + "\n")
    out["record"] = str(path)
    return out


def _age(seconds):
    s = max(0, int(seconds or 0))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return "%d%s" % (s // size, unit)
    return "%ds" % s


def _ids(nums):
    return ", ".join("#%d" % n for n in nums) or "none"


def render(p):
    src = ("map, age %s" % _age(p["map_age_seconds"])) if p["source"] == "map" \
        else "live (map: %s)" % p["source_reason"]
    c = p["calls"]
    lines = ["chain for #%d: %d members, %s" % (p["goal"], len(p["members"]),
                                                "complete" if p["complete"] else "INCOMPLETE"),
             "source: %s" % src,
             "calls: %d total (get %d, list %d), bound %d" % (c["total"], c["get"], c["list"], p["bound"]),
             "members, deepest first:"]
    for r in p["members"]:
        extra = " (kind %s)" % r["kind"] if r.get("kind") else ""
        lines.append("  #%d depth %d %s%s%s" % (r["number"], r["depth"], r["verdict"], extra,
                                               "; waits on %s" % _ids(r["waits_on"]) if r["waits_on"] else ""))
    lines.append("done (closed prerequisites): %s" % _ids(p["done"]))
    for e in p["externals"]:
        lines.append("external %s (%s), needed by %s" % (e["ref"], e["kind"], _ids(e["needed_by"])))
    for e in p["edges"]:
        lines.append("edge #%d -> #%d (%s)" % (e["from"], e["to"], e["tag"]))
    for x in p["cross_unit"]:
        lines.append("cross-unit: #%d (%s) waits on #%d (%s)" % (x["from"], x["units"][0], x["to"], x["units"][1]))
    for i in p["priority_inversions"]:
        lines.append("priority inversion: #%d (%s) blocks #%d (%s); reported, not changed"
                     % (i["blocker"], i["blocker_prio"] or "none", i["dependent"], i["dependent_prio"] or "none"))
    ahead = p["higher_priority_ahead"]
    lines.append("higher-priority open goals this chain runs ahead of: %s" % ("not read" if ahead is None else ahead))
    lines.append("weak needs/after mentions ignored: %s" % ("unknown from the map" if p["weak_mentions"] is None
                                                            else p["weak_mentions"]))
    for s in p["stops"]:
        lines.append("stop: %s" % s)
    lines.append("record: %s" % p["record"])
    return "\n".join(lines) + "\n"


def main(argv, run=None, now=None):
    args = list(argv[1:])
    if args in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    if len(args) != 3 or args[0] != "plan" or not args[2].isdigit():
        print(USAGE, file=sys.stderr)
        return 2
    try:
        p = plan(args[1], int(args[2]), run=run, now=now)
    except ChainRefused as exc:
        print("chain.py: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("chain.py: could not write the record: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(p, indent=2, sort_keys=True) if as_json else render(p), end="" if not as_json else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
