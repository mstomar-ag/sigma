#!/usr/bin/env python3
"""chain.py -- the plan for one end goal: every open prerequisite it waits on, in order (#1584).

    chain.py plan <sdlc_dir> <N> [--json]
    chain.py sheet <sdlc_dir> <chain>
    chain.py answer <sdlc_dir> <chain> --answers <file> | --defaults

`plan` and `sheet` are READ-ONLY toward GitHub. Every call they make goes through `_GetOnly`, which raises
on anything that is not an explicit `gh api ... --method GET`: no `gh issue ...` verb, no GraphQL, no body
flag. `answer` is the one verb that reaches GitHub: it hands a parked member's bank-option answer to
`unpark.resolve` (the Q&A block, one comment, and a label change only if the answer starts the member
again); it never posts free text, and the write-surface scanner does not see this path, so this paragraph
and the sheet's own list of writes are its record. The local write is the record `<sdlc_dir>/state/chains/c<N>.json` (issue numbers, verdict words and labels only; never
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

SHEET (#1585). Every decision the chain needs, in front of the person together, before anything is
changed. It reads ONLY the local record `plan` wrote (no `gh` call at all, so no GitHub write is possible)
and prints requested content, not a status block. The questions are the label drain's own question list
over the member set (`label_drain.questions`) plus three chain-only ones: a cross-unit edge, an end goal
that cannot be armed, and the cap (it has no default; its estimate arrives in a later slice). A question
the record already answers (a parked member whose own question has a recorded answer, or an answer in
this record) is not asked. More than 25 questions are grouped by kind.

ANSWER. Records answers in the chain record, nothing else locally. A parked member's answer is handed to
`unpark.resolve`, the verb `/sigma-unpark` uses, so the issue gets the identical recorded Q&A block. Every
answer is a bank option (or, for the cap, a positive integer), never free text, so nothing a person typed
is posted on a public repository. `--defaults` accepts the recommended (first) option of every question
except one that has none (the cap, a spend question). The state is `approved` only when no blocking
question is open and the plan is complete. `--partial` is refused for now.

Exit 0 planned/sheet printed/answers recorded, 2 refused or bad usage, 1 the record could not be written
or a member's answer failed.
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
         "  writes only .sdlc/state/chains/c<N>.json (numbers and labels, no titles or bodies).\n"
         "       chain.py plan <sdlc_dir> --longest [--by freeing] [--json]\n"
         "  Offer the top three end goals to work on first, from the stored map only. Writes nothing.")
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


def _record_path(sdlc_dir, goal):
    return pathlib.Path(sdlc_dir) / "state" / "chains" / ("c%d.json" % goal)


def _read_record(path):
    try:
        rec = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        raise ChainRefused("no readable chain record at %s; run `chain.py plan` first" % path)
    if not isinstance(rec, dict) or rec.get("schema") != SCHEMA or not isinstance(rec.get("goal"), int) \
            or not isinstance(rec.get("members"), list):
        raise ChainRefused("%s is not a chain plan record; run `chain.py plan` again" % path)
    return rec


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
    """Each parked member's kind, and `answered` (#1585): its own route question already has a recorded
    answer in the issue's Q&A block. Same bound as the label drain's brief (`MAX_PARKED_BRIEFS`)."""
    ld = label_drain
    briefed, skipped, unpark = 0, [], _load("unpark")
    for r in rows:
        if r["verdict"] != "parked":
            continue
        if briefed >= ld.MAX_PARKED_BRIEFS:
            r["kind"] = "not-read"
            skipped.append(r["number"])
            continue
        briefed += 1
        try:
            b = unpark.brief(sdlc, config, r["number"], source=source)
            r["kind"] = b.get("reason_class") if b.get("kind_declared") else "undeclared"
            if b.get("kind_declared"):
                slot = unpark.route_slot(r["kind"]) or unpark.CLOSING_QUESTION
                r["answered"] = slot["id"] in (b.get("prior_answers") or {})
        except Exception:  # noqa: BLE001 - say so rather than guess
            r["kind"] = "unreadable"
    return skipped


def _finish(out, rows, edges, ext, done, units, ranks, outside_ranks, weak):
    """Fields shared by both sources: order, depth, inversions, cross-unit, ahead-of."""
    nums = [r["number"] for r in rows]
    waits = collections.defaultdict(list)
    for e in edges:
        waits[e["to"]].append(e["from"])
    depth = _depths(nums, waits)
    for r in rows:
        r["unit"] = units.get(r["number"])
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
    dag_collect, hard_stop = _load("dag_collect"), _load("hard_stop")
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
        rows.append({"number": n, "verdict": verdict, "labels": sorted(names), "prio": prio,
                     "spend": ld._spend_text(issue.get("body"), hard_stop, config) is not None})
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
    out.update(schema=SCHEMA, goal=goal, calls=ro.summary(), planned_at=int(now), state="planned")
    path = _record_path(sdlc_dir, goal)
    try:
        out["answers"] = dict(_read_record(path).get("answers") or {})     # a re-plan keeps what was answered
    except ChainRefused:
        out["answers"] = {}
    os.makedirs(path.parent, exist_ok=True)
    state.atomic_write_text(path, json.dumps(out, indent=2, sort_keys=True) + "\n")
    out["record"] = str(path)
    return out


# --- the decision sheet and its answers (#1585) ------------------------------------------------------------

GROUP_OVER = 25
#: kinds with no recommended answer: the cap has no estimate yet, and a spend question is never defaulted.
NO_DEFAULT = ("cap", "irreversible")
CROSS_UNIT_OPTIONS = ["keep going", "stop here"]
UPPER_TIER_OPTIONS = ["plan its slices instead", "leave it"]
SHEET_USAGE = ("usage: chain.py sheet <sdlc_dir> <chain>\n"
               "       chain.py answer <sdlc_dir> <chain> --answers <file> | --defaults\n"
               "  <chain> is the end goal number (or c<N>). `sheet` prints every decision the chain needs and\n"
               "  changes nothing. `answer` records answers in .sdlc/state/chains/c<N>.json; a parked member's\n"
               "  answer goes through the unpark resolve path. --partial is not available yet.")


def _chain_arg(text):
    m = re.fullmatch(r"c?(\d+)", str(text or ""))
    if not m:
        raise ChainRefused("the chain is the end goal number, such as 12 or c12")
    return int(m.group(1))


def _row_for_questions(r):
    return {"number": r["number"], "verdict": r["verdict"], "waiting_on": list(r.get("waits_on") or []),
            "labels": r.get("labels") or [], "kind": r.get("kind"), "question": ""}


def questions_of(rec, config=None):
    """-> the ordered decisions a chain record needs, each
    {id, kind, ask, options, unblocks, recommended, hidden}. `hidden` marks one the data already answers
    (it is not asked and does not block). Pure: reads only the record."""
    ld, unpark = label_drain, _load("unpark")
    goal, rows = rec["goal"], rec["members"]
    label = "c%d" % goal
    qs = ld.questions(label, [_row_for_questions(r) for r in rows], [], {}, config or {})
    have = {q["id"] for q in qs}
    by = {r["number"]: r for r in rows}
    deps = ld._dependents([_row_for_questions(r) for r in rows], [])
    for r in sorted(rows, key=lambda x: x["number"]):                # a spend question, as the drain asks it
        qid = "irreversible:%d" % r["number"]
        if r.get("spend") and r["verdict"] not in ("upper-tier", "closed") and qid not in have:
            slot = unpark.route_slot("irreversible")
            qs.append({"id": qid, "kind": "irreversible", "area": None, "ask": slot["ask"],
                       "options": list(slot["options"]), "unblocks": sorted([r["number"]] + deps[r["number"]]),
                       "note": ld.SPEND_NOTE})
    for x in rec.get("cross_unit") or []:
        qs.append({"id": "cross_unit:%d-%d" % (x["from"], x["to"]), "kind": "cross_unit",
                   "ask": "#%d (unit %s) waits on #%d (unit %s): its change reaches #%d only when that unit "
                          "lands. Keep going or stop here?" % (x["from"], x["units"][0], x["to"], x["units"][1],
                                                                x["from"]),
                   "options": list(CROSS_UNIT_OPTIONS), "unblocks": [x["from"], x["to"]]})
    end = by.get(goal) or {}
    if end.get("verdict") == "upper-tier":
        qs.append({"id": "upper_tier:%d" % goal, "kind": "upper_tier",
                   "ask": "#%d is an upper-tier piece of work and cannot be armed. Plan its slices instead, or "
                          "leave it?" % goal, "options": list(UPPER_TIER_OPTIONS), "unblocks": [goal]})
    qs.append({"id": "cap:%s" % label, "kind": "cap",
               "ask": "What cap applies to this chain? (a positive whole number; no estimate exists yet, so "
                      "there is no default)", "options": [], "unblocks": sorted(by)})
    for q in qs:
        q["recommended"] = q["options"][0] if q["options"] and q["kind"] not in NO_DEFAULT else None
        row = by.get(int(q["id"].split(":", 1)[1])) if q["kind"] in unpark.QUESTIONS \
            and q["id"].split(":", 1)[1].isdigit() else None
        q["hidden"] = bool(row and row.get("answered") and row.get("verdict") == "parked")
    return qs


def _groups(qs):
    """-> {group id: [question ids]} for kinds with 2+ open questions sharing one option set."""
    by_key, out = collections.OrderedDict(), {}
    for q in qs:
        if q["options"] and not q["hidden"]:
            by_key.setdefault((q["kind"], tuple(q["options"])), []).append(q["id"])
    seen = collections.Counter()
    for (kind, _o), ids in by_key.items():
        if len(ids) < 2:
            continue
        seen[kind] += 1
        out["group:%s" % kind if seen[kind] == 1 else "group:%s#%d" % (kind, seen[kind])] = ids
    return out


def open_questions(rec, config=None):
    """-> (all questions, the ones still open), an open one being not hidden and not answered in the record."""
    qs = questions_of(rec, config)
    ans = rec.get("answers") or {}
    return qs, [q for q in qs if not q["hidden"] and q["id"] not in ans]


def _grouped(qs):
    return len([q for q in qs if not q["hidden"]]) > GROUP_OVER


def _writes(rec, qs):
    unpark = _load("unpark")
    rows = {r["number"]: r for r in rec["members"]}
    out = ["record each answer in .sdlc/state/chains/c%d.json (numbers and labels only)" % rec["goal"]]
    for q in qs:
        tail = q["id"].split(":", 1)[1]
        if not q["hidden"] and q["kind"] in unpark.QUESTIONS and tail.isdigit() \
                and (rows.get(int(tail)) or {}).get("verdict") == "parked":
            out.append("#%s: the Q&A block and one comment through the unpark resolve path; its labels change "
                       "only if the answer starts it again" % tail)
    out.append("answers to an undeclared-kind or spend question are recorded here only: declaring the kind "
               "and writing the spend marker belong to a later slice")
    out.append("nothing else: arming and starting members belong to a later slice")
    return out


def render_sheet(rec, now, config=None):
    qs, openq = open_questions(rec, config)
    shown = [q for q in qs if not q["hidden"]]
    ans = rec.get("answers") or {}
    members = rec["members"]
    units = sorted({r["unit"] for r in members if r.get("unit")}, key=str.lower)
    src = ("map, age %s" % _age(rec["map_age_seconds"])) if rec.get("source") == "map" \
        else "live (map: %s)" % rec.get("source_reason")
    age = _age(now - rec["planned_at"]) if isinstance(rec.get("planned_at"), int) else "unknown"
    depth = max([r["depth"] for r in members] or [0])
    ahead = rec.get("higher_priority_ahead")
    L = ["Chain sheet for #%d" % rec["goal"],
         "End goal: #%d (%s)" % (rec["goal"], next((r["verdict"] for r in members if r["number"] == rec["goal"]),
                                                    "not in the plan")),
         "Open members: %d, depth %d, units: %s" % (len(members), depth, ", ".join(units) or "none"),
         "Source: %s; planned %s ago; plan %s" % (src, age, "complete" if rec.get("complete") else "INCOMPLETE"),
         "State: %s" % rec.get("state", "planned"), "", "Order, deepest first:"]
    for i, r in enumerate(members, 1):
        extra = " (kind %s)" % r["kind"] if r.get("kind") else ""
        w = "; waits on %s" % _ids(r["waits_on"]) if r.get("waits_on") else ""
        L.append("  %d. #%d %s%s%s" % (i, r["number"], r["verdict"], extra, w))
    L.append("Done prerequisites: %s" % _ids(rec.get("done") or []))
    L.append("Runs ahead of: %s higher-priority open goal(s)" % ("an unknown number of" if ahead is None else ahead))
    for x in rec.get("cross_unit") or []:
        L.append("Cross-unit: #%d (%s) waits on #%d (%s)" % (x["from"], x["units"][0], x["to"], x["units"][1]))
    for e in rec.get("externals") or []:
        L.append("External: %s (%s), needed by %s" % (e["ref"], e["kind"], _ids(e["needed_by"])))
    L.append("")
    groups = _groups(qs) if _grouped(qs) else {}
    grouped_ids = {i for ids in groups.values() for i in ids}
    L.append("Decisions (%d open of %d%s):" % (len(openq), len(shown),
                                                 ", grouped by kind" if groups else ""))
    n = 0

    def put(ident, q, ids=None):
        nonlocal n
        n += 1
        opts = " / ".join(o + (" (recommended)" if o == q["recommended"] else "") for o in q["options"])
        done = "  [answered: %s]" % ans[ident] if ident in ans else ""
        L.append("  %d. [%s] %s%s" % (n, ident, q["ask"], done))
        if ids:
            L.append("     applies to %d questions: %s" % (len(ids), ", ".join(ids)))
        L.append("     options: %s" % (opts or "none, answer with a positive whole number"))
        if q.get("note"):
            L.append("     note: %s" % q["note"])
    for gid, ids in groups.items():
        first = next(q for q in shown if q["id"] == ids[0])
        put(gid, dict(first, ask="%d questions of kind %s share these options" % (len(ids), first["kind"]),
                      note=None), ids)
    for q in shown:
        if q["id"] not in grouped_ids:
            put(q["id"], q)
    L += ["", "Writes that follow approval:"] + ["  - " + w for w in _writes(rec, qs)]
    L += ["", "Nothing has been changed yet."]
    return "\n".join(L) + "\n"


def sheet(sdlc_dir, goal, now=None, config=None):
    now = time.time() if now is None else now
    rec = _read_record(_record_path(sdlc_dir, goal))
    return render_sheet(rec, now, config if config is not None else _load("state").load_config(sdlc_dir))


def _check_answer(q, raw):
    """-> (choice, error)."""
    if q["kind"] == "cap":
        text = str(raw).strip()
        if re.fullmatch(r"[1-9]\d{0,8}", text):
            return text, None
        return None, "%s: the cap is a positive whole number" % q["id"]
    if not isinstance(raw, str) or not raw.strip():
        return None, "%s: the answer must be non-empty text" % q["id"]
    hit = label_drain._bank_match(q["options"], raw)
    if hit is None:
        return None, "%s: %r is not one of: %s" % (q["id"], raw, " / ".join(q["options"]))
    return hit, None


def answer(sdlc_dir, goal, answers=None, defaults=False, config=None, run=None):
    """Record answers in the chain record. -> {"results": [...], "errors": [...], "open": [ids], "state"}.
    The whole set is judged before any write; a failed member write records nothing for that question."""
    sources, unpark = _load("sources"), _load("unpark")
    state = _load("state")
    config = config if config is not None else state.load_config(sdlc_dir)
    path = _record_path(sdlc_dir, goal)
    rec = _read_record(path)
    qs, openq = open_questions(rec, config)
    byid = {q["id"]: q for q in qs if not q["hidden"]}
    groups = _groups(qs) if _grouped(qs) else {}
    have = dict(rec.get("answers") or {})
    chosen, errors = {}, []
    if defaults:
        for q in openq:
            if q["recommended"] is not None:
                chosen[q["id"]] = q["recommended"]
    else:
        if not isinstance(answers, dict) or not answers:
            errors.append("the answers file must be a JSON object mapping question ids to answers")
        for ident, raw in (answers or {}).items():
            targets = groups.get(ident) or ([ident] if ident in byid else None)
            if not targets:
                errors.append("unknown question id %r (run `sheet` to list the ids)" % (ident,))
                continue
            for t in targets:
                choice, err = _check_answer(byid[t], raw)
                if err:
                    errors.append(err)
                else:
                    chosen[t] = choice
    out = {"goal": goal, "results": [], "errors": errors, "open": [], "state": rec.get("state", "planned")}
    if errors:
        return out
    rows = {r["number"]: r for r in rec["members"]}
    source = None
    scope = "chain:c%d" % goal
    for qid in sorted(chosen, key=lambda i: [q["id"] for q in qs].index(i)):
        q, choice = byid[qid], chosen[qid]
        res = {"id": qid, "choice": choice, "outcome": "recorded", "lines": []}
        out["results"].append(res)
        if have.get(qid) == choice:
            res["outcome"] = "already-recorded"
            continue
        target = qid.split(":", 1)[1]
        row = rows.get(int(target)) if target.isdigit() else None
        gesture = unpark.option_gestures(q["kind"]).get(choice) if q["kind"] in unpark.QUESTIONS else None
        if row is not None and row["verdict"] == "parked" and gesture in ("unpark", "keep-parked"):
            if qid in have:
                res["outcome"], res["lines"] = "refused", ["already resolved on GitHub as %r; use /sigma-unpark" % have[qid]]
                continue
            source = source or sources.GitHubSource(config, run=run or sources._run_gh)
            slot = unpark.route_slot(q["kind"]) or unpark.CLOSING_QUESTION
            r = unpark.resolve(sdlc_dir, config, int(target), {slot["id"]: choice}, gesture, source=source,
                               apply=True, extra={"scope": scope, "qid": qid})
            res["lines"] = ["[%s] %s" % (r["outcome"], r["detail"])]
            if r["outcome"] == "failed":
                res["outcome"] = "failed"
                continue
            res["outcome"] = "resolved"
        have[qid] = choice
    rec["answers"] = have
    still = [q["id"] for q in qs if not q["hidden"] and q["id"] not in have]
    rec["state"] = "approved" if not still and rec.get("complete") else "awaiting-answers"
    state.atomic_write_text(path, json.dumps(rec, indent=2, sort_keys=True) + "\n")
    out["open"], out["state"] = still, rec["state"]
    return out


def render_answer(res):
    L = ["answer for #%d: %d answer(s)" % (res["goal"], len(res["results"]))]
    for r in res["results"]:
        L.append("  [%s] %s -> %s" % (r["outcome"], r["id"], r["choice"]))
        L += ["      " + l for l in r["lines"]]
    L.append("state: %s; %d question(s) still open" % (res["state"], len(res["open"])))
    L += ["  - " + i for i in res["open"]]
    return "\n".join(L) + "\n"


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


def render_longest(p):
    lines = ["end goals to work on first (by %s), from the stored map:" % p["by"]]
    for e in p["ends"]:
        lines.append("  #%d prio %s, depth %d, %d members" % (
            e["goal"], "P%d" % e["prio"] if e["prio"] <= 4 else "none", e["depth"], e["members"]))
    if not p["ends"]:
        lines.append("  none")
    for x in p["excluded"]:
        lines.append("excluded: #%d (in or behind a cycle: %s)" % (x["goal"], _ids(x["cycle"])))
    return "\n".join(lines) + "\n"


def _main_longest(args, as_json, now):
    rest = [a for a in args[2:] if a != "--longest"]
    by = "length"
    if rest[:1] == ["--by"] and rest[1:] == ["freeing"]:
        by, rest = "freeing", []
    if len(args) < 3 or args[1].startswith("--") or args.count("--longest") != 1 or rest:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        p = dag_chain.longest(args[1], by=by, now=now)
    except dag_chain.ChainError as exc:
        print("chain.py: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(p, indent=2, sort_keys=True) if as_json else render_longest(p), end="\n" if as_json else "")
    return 0


def main(argv, run=None, now=None):
    args = list(argv[1:])
    if args in (["-h"], ["--help"]):
        print(USAGE)
        print(SHEET_USAGE)
        return 0
    if args[:1] in (["sheet"], ["answer"]):
        return _main_sheet(args, run, now)
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    if args[:1] == ["plan"] and "--longest" in args:
        return _main_longest(args, as_json, now)
    if len(args) != 3 or args[0] != "plan" or not (args[2].isascii() and args[2].isdigit()):
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


def _main_sheet(args, run, now):
    verb, rest = args[0], args[1:]
    if "--partial" in rest:
        print("chain.py: --partial is not available yet; answer every question or none", file=sys.stderr)
        return 2
    try:
        if verb == "sheet":
            if len(rest) != 2:
                print(SHEET_USAGE, file=sys.stderr)
                return 2
            print(sheet(rest[0], _chain_arg(rest[1]), now=now), end="")
            return 0
        flags = rest[2:]
        if len(rest) < 3 or not (flags == ["--defaults"] or (len(flags) == 2 and flags[0] == "--answers")):
            print(SHEET_USAGE, file=sys.stderr)
            return 2
        goal = _chain_arg(rest[1])
        answers = None
        if flags[0] == "--answers":
            try:
                answers = json.loads(pathlib.Path(flags[1]).read_text())
            except (OSError, ValueError) as exc:
                print("chain.py: could not read the answers file: %s" % exc, file=sys.stderr)
                return 2
        res = answer(rest[0], goal, answers, defaults=flags[0] == "--defaults", run=run)
    except ChainRefused as exc:
        print("chain.py: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("chain.py: could not write the record: %s" % exc, file=sys.stderr)
        return 1
    if res["errors"]:
        print("chain.py: %s\nnothing was written" % "\n".join(res["errors"]), file=sys.stderr)
        return 2
    print(render_answer(res), end="")
    return 1 if any(r["outcome"] == "failed" for r in res["results"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
