#!/usr/bin/env python3
"""chain.py -- the plan for one end goal: every open prerequisite it waits on, in order (#1584).

    chain.py plan <sdlc_dir> <N> [--json]
    chain.py sheet <sdlc_dir> <chain>
    chain.py answer <sdlc_dir> <chain> --answers <file> | --defaults
    chain.py approve <sdlc_dir> <chain>
    chain.py arm <sdlc_dir> <chain> [--dry-run]
    chain.py run <sdlc_dir> <chain> [--session-pid P]
    chain.py stop <sdlc_dir> <chain>
    chain.py resume <sdlc_dir> <chain> [--cap N]

`plan` and `sheet` are READ-ONLY toward GitHub. Every call they make goes through `_GetOnly`, which raises
on anything that is not an explicit `gh api ... --method GET`: no `gh issue ...` verb, no GraphQL, no body
flag. `answer` is the one verb that reaches GitHub: it hands a parked member's bank-option answer to
`unpark.resolve` (the Q&A block, one comment, and a label change only if the answer starts the member
again); it never posts free text, and the write-surface scanner does not see this path, so this paragraph
and the sheet's own list of writes are its record. `arm` is the verb that creates the label `chain:<root>`,
adds it to the members and arms them: a new GitHub write path, reached only after approval (see ARM). The local write is the record `<sdlc_dir>/state/chains/c<N>.json` (issue numbers, verdict words and labels only; never
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

APPROVE (#1587). A local state change only: sets the record's state to `approved` when no blocking question
is open and the plan is complete, and refuses (naming the open ids) otherwise. No `gh` call.

ARM (#1587). Refuses, before a single `gh` call of any kind, unless the record's state is `approved`, the
plan is complete and no question is open: before approval no GitHub write exists in any verb. After it,
`arm` (a) creates the label `chain:<root>` only when a read says it is missing (never removes or deletes it,
never repaints it), (b) adds it to every open member, (c) for each unarmed member the answers allow
(`label_set:c<N>` says `leave them`: none; `arm only the ones that can start`: those waiting on no member)
posts ONE fixed comment (issue numbers only, no title or free text) and arms it through the label drain's
own machinery (`label_drain._prepare`: keep an existing priority, otherwise the drain's default; assignee
only when nobody is assigned, an issue assigned to someone else is left alone and reported; the body is never
rewritten; a spend approval is never defaulted). An upper-tier, parked or gate-held member is labelled, never
armed. It reads every member's CURRENT state, so a second `arm` computes zero actions. The comment goes before
the arming writes of its member, whose goal-label write is the last, and is skipped when the fixed text is already there, so a crash at any write
followed by a re-run completes with no duplicate label and no duplicate comment. `--dry-run` hands the source
a read-only runner (a write is impossible, not skipped) and prints the same lines the sheet listed.

RUN, STOP, RESUME (#1588). A foreground driver, no daemon, no loop and no model call: one `run` is ONE cycle
and is called again (by a person or a supervising loop) until it reports done. It refuses (exit 2, nothing
written, no process) unless the record is approved and `armed_at` is set (`approve` and a finished `arm` stamp
them), or the run was stopped. A cycle: the existing auto-unpark sweep; the spend cap
(`chain_spend.admit`, before any claim; a refusal records `capped` and claims nothing); a read of the label's
members to report what was added and what closed since the last cycle (reported, never acted on; a closed
member is confirmed by reading the issue itself, so a lost label never reads as done); then ONE pick, the
loop's own `loop.py next-batch --label chain:<root> --session-pid P`, the only process this verb starts, so
nothing outside the label is ever claimed and the claim stays lease based. A non-empty pick is recorded
(numbers only) with a heartbeat, exit 0. An empty pick is `done` only when every member is confirmed closed;
otherwise it is STUCK, exit 3, with ONE reason: decision, held-gate, claimed, external, rate-limit,
degraded-read. `stop` and `resume [--cap N]` change only the record's `run` block (local, no `gh` call); a
stop does not release goals already claimed. The record is a local file with no lock: run one driver per
chain. Not measured: a run against a live repository, and the cost of one cycle's reads beyond the fake's
call counts.

GROWTH (#1589). Each `run` cycle re-reads the end goal's open closure (read-only; a failed read is no growth)
and looks for new open prerequisites of the members. One that needs no decision joins through
`label_drain.pull` (the label, the fixed comment, arming as `arm` does), one printed line, up to
`chain.max_added` (default 5) and `chain.max_goals`; the root never changes and a decomposed end goal's slices
join under it. One that needs a decision, is over a bound, or arrives while `--no-grow` or `chain.freeze: true`
holds is NOT pulled: it is recorded in `run.paused` with its dependents, the pick skips those dependents (the
other branches go on), and a delta sheet with ONLY the new questions is printed and kept in `run.delta`
(`answer` accepts them; `grow:<n>`: leave it out, or add it, which also passes a bound). Each cycle that prints a
delta sheet counts in `run.pauses`; past `chain.max_reasks` (default 3) the run ends stuck, reason decision.
Anything filed from a member that is not a prerequisite never joins and is listed as "filed, not in the chain".

Exit 0 planned/sheet printed/answers recorded/armed/cycle picked or done, 2 refused or bad usage, 1 the record
could not be written or a member's answer or a write failed, 3 the cycle ended capped or stuck.
"""
import collections
import importlib.util
import json
import os
import pathlib
import re
import subprocess
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
ARM_USAGE = ("usage: chain.py approve <sdlc_dir> <chain>\n"
             "       chain.py arm <sdlc_dir> <chain> [--dry-run]\n"
             "  `approve` marks a fully answered sheet approved (local only). `arm` refuses unless the chain is\n"
             "  approved; then it creates the label chain:<root> if missing, adds it to every member and arms\n"
             "  the unarmed ones with one comment each. --dry-run writes nothing and prints the same lines.")
ARM_COMMENT = "Sigma is arming this issue as part of the chain for #%d."
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

def _resolve(sdlc_dir, goal, config, run, now):
    """The open closure of `goal` as a plan dict, and the read-only runner that served it. Writes nothing."""
    sources, state = _load("sources"), _load("state")
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
    return out, ro


def plan(sdlc_dir, goal, config=None, run=None, now=None):
    """The plan dict for `goal`, and the local record written. Raises `ChainRefused`."""
    state = _load("state")
    now = time.time() if now is None else now
    config = config if config is not None else state.load_config(sdlc_dir)
    out, ro = _resolve(sdlc_dir, goal, config, run, now)
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
    qs += _delta_questions(rec, config)
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


def chain_label(goal):
    return "chain:%d" % goal


def _arm_scope(rec):
    """-> the member numbers `arm` may arm, from the record: the unarmed ones the label question allows."""
    ans = (rec.get("answers") or {}).get("label_set:c%d" % rec["goal"]) or ""
    if ans == "leave them":
        return set()
    out = {r["number"] for r in rec["members"] if r["verdict"] == "unarmed"}
    if ans.startswith("arm only"):
        out = {r["number"] for r in rec["members"] if r["number"] in out and not r.get("waits_on")}
    return out


def _label_line(n, goal):
    return "#%d: add label %s" % (n, chain_label(goal))


def _arm_line(n):
    return "#%d: arm it and add one comment" % n


def _writes(rec, qs, config=None):
    unpark = _load("unpark")
    goal = rec["goal"]
    rows = {r["number"]: r for r in rec["members"]}
    out = ["record each answer in .sdlc/state/chains/c%d.json (numbers and labels only)" % goal]
    for q in qs:
        tail = q["id"].split(":", 1)[1]
        if not q["hidden"] and q["kind"] in unpark.QUESTIONS and tail.isdigit() \
                and (rows.get(int(tail)) or {}).get("verdict") == "parked":
            out.append("#%s: the Q&A block and one comment through the unpark resolve path; its labels change "
                       "only if the answer starts it again" % tail)
    out.append("answers to an undeclared-kind or spend question are recorded here only: declaring the kind "
               "and writing the spend marker belong to a later slice")
    out.append("after approval, `chain.py arm` creates the label %s if it is missing" % chain_label(goal))
    out += [_label_line(r["number"], goal) for r in rec["members"]]
    out.append("arming, per member below: add sdlc:goal, priority %s only if it has none, assign you only if "
               "nobody is assigned, never edit the body; one comment with issue numbers only"
               % label_drain.arm_settings(config)["default_priority"])
    scope = _arm_scope(rec)
    out += [_arm_line(r["number"]) for r in rec["members"] if r["number"] in scope]
    out.append("nothing else: an upper-tier, parked or assigned-elsewhere member is labelled, never armed; "
               "starting the members belongs to a later slice")
    return out


def _decision_lines(n, ident, q, ans, ids=None):
    """The sheet's lines for one decision, shared by the full sheet and the growth delta."""
    opts = " / ".join(o + (" (recommended)" if o == q["recommended"] else "") for o in q["options"])
    done = "  [answered: %s]" % ans[ident] if ident in ans else ""
    L = ["  %d. [%s] %s%s" % (n, ident, q["ask"], done)]
    if ids:
        L.append("     applies to %d questions: %s" % (len(ids), ", ".join(ids)))
    L.append("     options: %s" % (opts or "none, answer with a positive whole number"))
    if q.get("note"):
        L.append("     note: %s" % q["note"])
    return L


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
        L.extend(_decision_lines(n, ident, q, ans, ids))
    for gid, ids in groups.items():
        first = next(q for q in shown if q["id"] == ids[0])
        put(gid, dict(first, ask="%d questions of kind %s share these options" % (len(ids), first["kind"]),
                      note=None), ids)
    for q in shown:
        if q["id"] not in grouped_ids:
            put(q["id"], q)
    L += ["", "Writes that follow approval:"] + ["  - " + w for w in _writes(rec, qs, config)]
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
    grown = {q["id"] for q in _delta_questions(rec, config)}      # growth questions never un-approve a running chain
    base_open = [i for i in still if i not in grown]
    rec["state"] = "approved" if not base_open and rec.get("complete") else "awaiting-answers"
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


# --- approve and arm (#1587) -------------------------------------------------------------------------------

def _require_approved(rec, config):
    """Raise `ChainRefused` unless the chain is approved. Reads only the record: no `gh` call. Three clauses,
    each alone enough to refuse: the state, the plan being complete, and no question left open."""
    if rec.get("state") != "approved":
        raise ChainRefused("chain #%d is not approved (state %s); answer the sheet, then approve; nothing was "
                           "written" % (rec["goal"], rec.get("state", "planned")))
    if not rec.get("complete"):
        raise ChainRefused("chain #%d: the plan is INCOMPLETE; plan it again; nothing was written" % rec["goal"])
    _qs, openq = open_questions(rec, config)
    if openq:
        raise ChainRefused("chain #%d: %d question(s) still open (%s); nothing was written"
                           % (rec["goal"], len(openq), ", ".join(q["id"] for q in openq)))


def _iso(now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _save(sdlc_dir, goal, rec):
    """The one write of the run, stop, resume and arm stamps: the local chain record, atomically."""
    _load("state").atomic_write_text(_record_path(sdlc_dir, goal), json.dumps(rec, indent=2, sort_keys=True) + "\n")


def approve(sdlc_dir, goal, config=None, now=None):
    """Mark a fully answered, complete chain approved and stamp `approved_at`. Local only.
    -> {"goal", "state"}."""
    state = _load("state")
    config = config if config is not None else state.load_config(sdlc_dir)
    path = _record_path(sdlc_dir, goal)
    rec = _read_record(path)
    _qs, openq = open_questions(rec, config)
    if openq or not rec.get("complete"):
        raise ChainRefused("chain #%d cannot be approved: %s" % (
            goal, "%d question(s) open (%s)" % (len(openq), ", ".join(q["id"] for q in openq)) if openq
            else "the plan is INCOMPLETE; plan it again"))
    if rec.get("state") != "approved" or not rec.get("approved_at"):
        rec["state"] = "approved"
        rec.setdefault("approved_at", _iso(time.time() if now is None else now))
        state.atomic_write_text(path, json.dumps(rec, indent=2, sort_keys=True) + "\n")
    return {"goal": goal, "state": "approved"}


def _goal_label_last(got, goal_label):
    """The arming writes with the one that adds the goal label moved last. That label is what a re-run reads
    as `armed`, so a crash before it leaves the member unarmed (the rest is recomputed and finished) rather
    than armed with its priority or assignee missing and nothing left to notice it."""
    def adds_goal(a):
        return goal_label in (a.get("add") or ()) or (a["action"] == "add-label" and a["detail"] == goal_label)
    return [a for a in got if not adds_goal(a)] + [a for a in got if adds_goal(a)]


def _act(kind, n, detail):
    return {"action": kind, "issue": str(n), "detail": detail, "result": None, "error": None}


def arm(sdlc_dir, goal, dry_run=False, config=None, run=None, now=None):
    """Label and arm an approved chain. -> {"label", "apply", "actions", "refused", "notes"}.
    Raises `ChainRefused` before any `gh` call unless approved. See the module docstring (ARM)."""
    state, sources, triage = _load("state"), _load("sources"), _load("triage")
    gh_api = _load("gh_api")
    config = config if config is not None else state.load_config(sdlc_dir)
    rec = _read_record(_record_path(sdlc_dir, goal))
    _require_approved(rec, config)
    ld = label_drain
    base = run or sources._run_gh
    rd = ld._ReadOnly(base) if dry_run else base
    source = sources.GitHubSource(config, run=rd)
    cfg, label, repo = ld.arm_settings(config), chain_label(goal), None
    repo = source.repo or None
    try:
        exists = bool(gh_api.label_exists(rd, label, repo))
    except Exception as exc:        # noqa: BLE001 - fail closed: no write on a read that failed
        raise ChainRefused("could not check whether the label %s exists (%s); nothing was written" % (label, exc))
    judge, scope = ld._Judge(sdlc_dir, config, source), _arm_scope(rec)
    refused, notes, per_member = [], [], []
    for r in rec["members"]:
        n = r["number"]
        try:
            issue = gh_api.view_issue(rd, n, repo, comments="all")
        except Exception:           # noqa: BLE001 - say so rather than guess
            refused.append({"issue": n, "verdict": "unreadable", "reason": "could not read its current state"})
            continue
        names = ld._names(issue)
        verdict, detail = judge.verdict(issue)
        if verdict == "closed":
            notes.append("#%d is closed; left alone" % n)
            continue
        acts = []
        if label not in names:
            acts.append(_act("add-label", n, label))
        if verdict == "unarmed" and n in scope:
            got_refused = len(refused)
            prepared = ld._prepare(source, triage, gh_api, label, cfg, [(n, None)], refused, notes)
            if n in prepared and len(refused) == got_refused:
                text = ARM_COMMENT % goal
                if not any(isinstance(c, dict) and str(c.get("body") or "").strip() == text
                           for c in issue.get("comments") or []):
                    acts.append(_act("comment", n, text))
                acts += _goal_label_last(prepared[n][1], source.goal_label)
        elif verdict in ld._REFUSED:
            refused.append({"issue": n, "verdict": verdict, "reason": detail or verdict})
        per_member.append(acts)
    actions = []
    if not exists:
        make = _act("create-label", "-", label)
        if dry_run:
            make["result"] = "would"
        else:
            try:
                gh_api.create_label(base, label, repo)
                make["result"] = "done"
            except Exception as exc:    # noqa: BLE001 - nothing else is attempted without the label
                make["result"], make["error"] = "failed", str(exc)
        actions.append(make)
        if make["result"] == "failed":
            return {"label": label, "apply": not dry_run, "actions": actions,
                    "refused": sorted(refused, key=lambda x: x["issue"]), "notes": notes}
    for acts in per_member:
        for a in acts:                  # one member: label, comment, then arming; stop at its first failure
            done = triage.apply_actions(source, [a], not dry_run)[0]
            actions.append(done)
            if done["result"] == "failed":
                break
    if not dry_run and not any(a["result"] == "failed" for a in actions):
        rec["armed_at"] = rec.get("armed_at") or _iso(time.time() if now is None else now)
        _save(sdlc_dir, goal, rec)          # #1588: `run` starts only from a chain that finished arming
    return {"label": label, "apply": not dry_run, "actions": actions,
            "refused": sorted(refused, key=lambda x: x["issue"]), "notes": notes}


def render_arm(res):
    goal = int(res["label"].split(":", 1)[1])
    groups = collections.OrderedDict()
    for a in res["actions"]:
        if a["action"] == "create-label":
            key, line = ("create",), "create label %s" % res["label"]
        elif a["action"] == "add-label" and a["detail"] == res["label"]:
            key, line = (a["issue"], "label"), _label_line(int(a["issue"]), goal)
        else:
            key, line = (a["issue"], "arm"), _arm_line(int(a["issue"]))
        g = groups.setdefault(key, {"line": line, "results": [], "error": None})
        g["results"].append(a["result"])
        g["error"] = g["error"] or a.get("error")
    out = ["arm %s (%s): %d write(s), %d refused" % (res["label"], "APPLIED" if res["apply"] else "DRY-RUN",
                                                       len(res["actions"]), len(res["refused"]))]
    for g in groups.values():
        r = "failed" if "failed" in g["results"] else g["results"][-1]
        out.append("  [%s] %s%s" % (r, g["line"], " -- %s" % g["error"] if r == "failed" and g["error"] else ""))
    for r in res["refused"]:
        out.append("  refused #%d (%s): %s" % (r["issue"], r["verdict"], r["reason"]))
    out += ["  note: " + n for n in res["notes"]]
    if not res["actions"]:
        out.append("  no actions needed")
    if not res["apply"]:
        out.append("dry run: nothing was written; run without --dry-run to execute")
    return "\n".join(out) + "\n"


# --- run, stop, resume (#1588) -----------------------------------------------------------------------------

RUN_USAGE = ("usage: chain.py run <sdlc_dir> <chain> [--session-pid P] [--no-grow]\n"
             "       chain.py stop <sdlc_dir> <chain>\n"
             "       chain.py resume <sdlc_dir> <chain> [--cap N]\n"
             "  `run` is ONE cycle of a foreground driver (no daemon): it claims the next goals of the approved,\n"
             "  armed chain through the loop's own label-scoped pick, or says why it cannot (exit 3: capped or\n"
             "  stuck). Call it again. `stop` and `resume` change only the local record. A new prerequisite of the\n"
             "  chain joins by itself up to chain.max_added; --no-grow (or chain.freeze) makes every addition ask.")
STUCK_REASONS = ("decision", "held-gate", "claimed", "external", "rate-limit", "degraded-read", "capped")
_DEFAULT_PICK_TIMEOUT = 900
_NO_PICK = ("DONE", "BUDGET", "HANDOFF", "FULL", "HOLD")


def pick_argv(sdlc_dir, label, session_pid=None, skip=()):
    """The default pick: the loop's own scoped batch pick, nothing else. Built, never run, by this function."""
    argv = [sys.executable, str(_HERE / "loop.py"), "next-batch", str(sdlc_dir), "--label", label,
            "--session-pid", str(session_pid if session_pid else os.getpid())]
    if skip:
        argv += ["--skip", ",".join(sorted(str(x) for x in skip))]
    return argv


def _default_pick(sdlc_dir, label, session_pid, skip):
    """-> the goal numbers the pick claimed. The one process `run` starts. Raises on a failed pick."""
    got = subprocess.run(pick_argv(sdlc_dir, label, session_pid, skip), capture_output=True, text=True,
                         timeout=_DEFAULT_PICK_TIMEOUT)
    if got.returncode != 0:
        raise RuntimeError("the pick exited %d: %s" % (got.returncode, (got.stderr or "").strip()[-200:]))
    return [int(m.group(0)) for line in got.stdout.splitlines()
            if (m := re.match(r"\d+", line.strip())) and line.strip() not in _NO_PICK]


def _default_sweep(sdlc_dir, config, run):
    """The loop's own fail-open auto-unpark sweep, by its existing entry. -> the goal refs it lifted."""
    try:
        if _load("sources")._auto_unpark(config) == "off":
            return frozenset()
        return frozenset(((_load("auto_unpark").sweep_unpark(sdlc_dir, config, apply=True, run=run)) or {})
                         .get("unparked") or ())
    except Exception:           # noqa: BLE001 - a background sweep never stops a run
        return frozenset()


def _numbers(items):
    out = set()
    for x in items or ():
        m = re.fullmatch(r"#?(\d+)", str(x).strip())
        if m:
            out.add(int(m.group(1)))
    return out


def _confirm_closed(gh_run, config, numbers):
    """-> the subset of `numbers` whose own issue reads closed (read-only, one GET each)."""
    sources, gh_api = _load("sources"), _load("gh_api")
    ro = _GetOnly(gh_run or sources._run_gh)
    repo = sources.GitHubSource(config, run=ro).repo or None
    closed = set()
    for n in sorted(numbers):
        try:
            if str(gh_api.view_issue(ro, n, repo, comments="none").get("state") or "").lower() == "closed":
                closed.add(n)
        except Exception:       # noqa: BLE001 - unread is not closed
            pass
    return closed


def _why_failed(exc):
    return "rate-limit" if re.search(r"rate.?limit|\b429\b|secondary", str(exc), re.I) else "degraded-read"


# --- growth of a running chain (#1589) ------------------------------------------------------------------------

GROW_OPTIONS = ["leave it out", "add it"]       # the first is the recommended one: nothing is added unasked
DEFAULT_MAX_REASKS = 3
_DELTA_KEYS = ("number", "verdict", "waits_on", "labels", "kind", "spend")
_GROW_WHY = {"bound": "the chain has already taken in its limit of additions",
             "freeze": "growth is frozen for this run",
             "refused": "it could not be added automatically"}


def _own_questions(goal, rows, config):
    """-> the decisions a new prerequisite carries by itself (its kind, its spend), asked of no one else."""
    nums = {str(r["number"]) for r in rows}
    return [q for q in questions_of({"goal": goal, "members": [dict(r) for r in rows]}, config)
            if q["kind"] != "cap" and q["id"].split(":", 1)[1] in nums]


def _delta_questions(rec, config=None):
    """-> the questions of the held-back prerequisites in the run block: their own decisions, or one
    `grow:<n>` question where nothing but a limit or a refusal held them. Pure: reads only the record."""
    d = (rec.get("run") or {}).get("delta") or {}
    rows, why = d.get("rows") or [], d.get("why") or {}
    out = []
    for r in sorted(rows, key=lambda x: x["number"]):
        n = r["number"]
        own = _own_questions(rec["goal"], [r], config) if why.get(str(n)) == "decision" else []
        deps = (((rec.get("run") or {}).get("paused") or {}).get(str(n))) or []
        out += own or [{"id": "grow:%d" % n, "kind": "grow", "options": list(GROW_OPTIONS),
                        "ask": "#%d is a new prerequisite of the chain, and %s. Leave it out, or add it?"
                               % (n, _GROW_WHY.get(why.get(str(n)), "it needs a decision")),
                        "unblocks": sorted([n] + list(deps))}]
    return out


def render_delta(rec, newly, config=None):
    """The delta sheet: ONLY the questions of the prerequisites newly held back this cycle."""
    qs = [q for q in _delta_questions(rec, config) if int(q["id"].split(":", 1)[1]) in set(newly)]
    ans = rec.get("answers") or {}
    for q in qs:
        q["recommended"] = q["options"][0] if q["options"] else None
    L = ["Chain growth for #%d: %d new decision(s), nothing else changed" % (rec["goal"], len(qs)),
         "Held out of the chain for now: %s" % _ids(sorted(newly)), "", "Decisions:"]
    for i, q in enumerate(qs, 1):
        L += _decision_lines(i, q["id"], q, ans)
    L += ["", "Answer with `chain.py answer`, then `chain.py resume`; members that do not wait on these go on."]
    return "\n".join(L) + "\n"


def _dependents_of(edges, start):
    """-> every goal that waits, directly or through others, on `start`."""
    by = collections.defaultdict(set)
    for e in edges:
        by[e["from"]].add(e["to"])
    seen, todo = set(), [start]
    while todo:
        for nxt in by.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return sorted(seen)


def _int_cfg(block, key, default):
    v = block.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else default


def _default_closure(sdlc_dir, goal, config, run, now):
    return _resolve(sdlc_dir, goal, config, run, now)[0]


def _grow(sdlc_dir, goal, rec, blk, config, cfg, now, run, closure, pull, filed, no_grow, res):
    """Take in the new open prerequisites of the chain, within bounds. Edits `blk` and `res`; returns the
    set of goal numbers the pick must skip. A failed read is no growth. See the module docstring (GROWTH)."""
    chain_spend = _load("chain_spend")
    recorded = [r["number"] for r in rec["members"]]
    joined = list(blk.get("joined") or [])
    try:
        cl = (closure or _default_closure)(sdlc_dir, goal, config, run, now)
    except Exception:           # noqa: BLE001 - growth is an extra read; it never stops a cycle
        cl = None
    try:
        res["filed"] = sorted(_numbers((filed or (lambda *_a: ()))(sdlc_dir, goal, cl)))
    except Exception:           # noqa: BLE001
        res["filed"] = []
    if res["filed"]:
        blk["filed"] = sorted(set(blk.get("filed") or ()) | set(res["filed"]))
    old_paused = dict(blk.get("paused") or {})
    if cl is None:
        return {int(d) for ds in old_paused.values() for d in ds}
    block = cfg.get("chain") if isinstance(cfg.get("chain"), dict) else {}
    freeze = bool(no_grow) or block.get("freeze") is True
    max_added = _int_cfg(block, "max_added", chain_spend.DEFAULT_MAX_ADDED)
    max_goals = block.get("max_goals")
    max_goals = max_goals if isinstance(max_goals, (int, float)) and not isinstance(max_goals, bool) \
        else len(recorded) + max_added
    max_reasks = _int_cfg(block, "max_reasks", DEFAULT_MAX_REASKS)
    answers = rec.get("answers") or {}
    known = set(recorded) | set(blk.get("seen") or ()) | set(joined) | {goal}
    held, why, rows = {}, {}, {}
    for r in cl.get("members") or ():
        n = r["number"]
        if n in known:
            continue
        mine = {k: r.get(k) for k in _DELTA_KEYS if k in r}
        rows[n] = mine
        said = answers.get("grow:%d" % n)
        if said == "leave it out":
            held[n], why[n] = _dependents_of(cl.get("edges") or [], n), "declined"
            continue
        own = [q for q in _own_questions(goal, [mine], config) if not q["hidden"] and q["id"] not in answers]
        override = said == "add it"
        if own:
            reason = "decision"
        elif freeze and not override:
            reason = "freeze"
        elif (len(joined) >= max_added or len(recorded) + len(joined) >= max_goals) and not override:
            reason = "bound"
        else:
            got = pull(sdlc_dir, chain_label(goal), n, goal, config=config, run=run) if pull \
                else label_drain.pull(sdlc_dir, chain_label(goal), n, goal, config=config, run=run)
            result = (got or {}).get("result")
            if result in ("pulled", "already"):
                joined.append(n)
                res["joined"].append(n)
                continue
            if result in ("closed", "unreadable"):          # nothing to hold back; an unreadable one is retried
                continue
            reason = "refused"
        held[n], why[n] = _dependents_of(cl.get("edges") or [], n), reason
    newly = sorted(n for n in held if why[n] != "declined" and str(n) not in old_paused)
    for key, val in (("joined", joined), ("paused", {str(n): d for n, d in sorted(held.items())})):
        if val:
            blk[key] = val
        else:
            blk.pop(key, None)
    open_rows = {n: rows[n] for n in held if why[n] != "declined"}
    if open_rows:
        blk["delta"] = {"rows": [open_rows[n] for n in sorted(open_rows)],
                        "why": {str(n): why[n] for n in sorted(open_rows)}}
    else:
        blk.pop("delta", None)
    res["paused"] = sorted(held)
    if newly:
        blk["pauses"] = int(blk.get("pauses") or 0) + 1
        if blk["pauses"] > max_reasks:
            res["over_asked"] = "chain.max_reasks (%d) reached: #%d needs an answer" % (max_reasks, newly[0])
        res["delta"] = render_delta(dict(rec, run=blk), newly, config)
    return {d for ds in held.values() for d in ds}


def _stuck(p, failure, unconfirmed):
    """-> (reason, detail), ONE reason from STUCK_REASONS for an empty pick with members still open. Numbers
    only in the detail. The order is the order a person would act in: a failed read first (nothing else can
    be trusted), then what needs a person, then what only waits."""
    if failure is not None:
        return _why_failed(failure), "a read or the pick failed"
    rows, outside = p["members"], p.get("outside") or []
    if any(r.get("kind") in ("not-read", "unreadable") for r in rows + outside):
        return "degraded-read", "a parked member could not be read"
    parked = sorted(r["number"] for r in rows if r["verdict"] == "parked")
    if parked:
        return "decision", "#%d is parked and needs an answer" % parked[0]
    held = sorted(r["number"] for r in rows if r["verdict"] == "held-by-gate")
    if held:
        return "held-gate", "#%d is held by a gate" % held[0]
    claimed = sorted(r["number"] for r in rows if r.get("in_progress") or r["verdict"] == "assigned-elsewhere")
    if claimed:
        return "claimed", "#%d is in progress or assigned to someone else" % claimed[0]
    ext = sorted({n for r in outside for n in [r["number"]]} | {r["number"] for r in rows if r.get("unresolved")})
    if ext:
        return "external", "#%d waits on an open prerequisite outside the chain" % ext[0]
    needs = sorted(r["number"] for r in rows if r["verdict"] in ("unarmed", "upper-tier"))
    if needs:
        return "decision", "#%d is not armed and needs a person" % needs[0]
    if unconfirmed:
        return "degraded-read", "#%d could not be confirmed closed" % sorted(unconfirmed)[0]
    return "claimed", "open members remain but none could be claimed this cycle (skipped or leased elsewhere)"


def run(sdlc_dir, goal, session_pid=None, pick=None, now=None, config=None, run=None, sweep=None,
        no_grow=False, closure=None, pull=None, filed=None):
    """ONE cycle of the chain run, re-callable. -> {"goal", "state", "exit", "reason", "detail", "picked",
    "added", "closed", "cycle"}. Raises `ChainRefused`, with nothing written and nothing started, unless the
    record is approved, armed and not stopped. `pick(label, skip)` -> goal numbers and `sweep(sdlc_dir,
    config, run)` -> refs are injectable; the defaults are the loop's own. `closure`, `pull` and `filed` (see
    GROWTH in the module docstring) are injectable too; `no_grow` makes every addition ask."""
    state, sources = _load("state"), _load("sources")
    now = time.time() if now is None else now
    config = config if config is not None else state.load_config(sdlc_dir)
    rec = _read_record(_record_path(sdlc_dir, goal))
    if rec.get("state") != "approved" or not rec.get("armed_at"):
        raise ChainRefused("chain #%d is not %s; nothing was started"
                           % (goal, "armed (run `chain.py arm`)" if rec.get("state") == "approved"
                              else "approved (state %s)" % rec.get("state", "planned")))
    blk = dict(rec.get("run") or {})
    if blk.get("state") == "stopped":
        raise ChainRefused("chain #%d is stopped; `chain.py resume` first; nothing was started" % goal)
    res = {"goal": goal, "state": blk.get("state"), "exit": 0, "reason": None, "detail": "", "picked": [],
           "added": [], "closed": [], "cycle": int(blk.get("cycle") or 0),
           "joined": [], "paused": [], "delta": "", "filed": []}
    if blk.get("state") == "done":
        return res
    label = chain_label(goal)
    recorded = [r["number"] for r in rec["members"]]
    try:
        skip = frozenset(str(x) for x in ((sweep or _default_sweep)(sdlc_dir, config, run) or ()))
    except Exception:           # noqa: BLE001 - fail open, like the loop's own sweep
        skip = frozenset()
    cfg = json.loads(json.dumps(config))
    if blk.get("cap"):
        cfg.setdefault("chain", {})
        cfg["chain"] = dict(cfg["chain"] if isinstance(cfg["chain"], dict) else {}, max_goals=blk["cap"])
    blk["cycle"] = res["cycle"] = res["cycle"] + 1
    ok, why = _load("chain_spend").admit(sdlc_dir, cfg, {"members": recorded, "approved_at": rec.get("approved_at")},
                                         now=now)
    if not ok:                  # before the claim: a refused cycle picks nothing
        blk.update(state="capped", reason=why, stuck=None)
        rec["run"] = blk
        _save(sdlc_dir, goal, rec)
        res.update(state="capped", exit=3, reason="capped", detail=why)
        return res
    held_back = _grow(sdlc_dir, goal, rec, blk, config, cfg, now, run, closure, pull, filed, no_grow, res)
    if res.pop("over_asked", None) is not None:
        detail = "chain.max_reasks reached: %s still need an answer" % _ids(sorted(int(k) for k in blk["paused"]))
        blk.update(state="stuck", reason=detail, stuck="decision")
        rec["run"] = blk
        _save(sdlc_dir, goal, rec)
        res.update(state="stuck", exit=3, reason="decision", detail=detail)
        return res
    skip = skip | frozenset(str(d) for d in held_back)
    known = set(recorded) | set(blk.get("seen") or ()) | set(blk.get("joined") or ())
    already = set(blk.get("closed") or ())
    failure, p, open_now, confirmed = None, None, set(), set()
    try:
        p = label_drain.plan(sdlc_dir, label, config=config, run=run)
        open_now = {r["number"] for r in p["members"]}
        res["added"] = sorted(open_now - known)
        confirmed = _confirm_closed(run, config, known - open_now - already)
    except Exception as exc:    # noqa: BLE001 - reported as the stuck reason only if the pick finds nothing
        failure = exc
    closed_new = sorted(confirmed)
    res["closed"] = closed_new
    blk["closed"] = sorted(already | confirmed)
    blk["seen"] = sorted(known | open_now)
    if closed_new:
        blk["heartbeat"] = _iso(now)
    try:
        got = _numbers((pick or (lambda lab, sk: _default_pick(sdlc_dir, lab, session_pid, sk)))(label, skip))
    except Exception as exc:    # noqa: BLE001 - a failed pick is a stuck cycle, not a crash
        got, failure = set(), failure or exc
    if got:
        blk.update(state="running", heartbeat=_iso(now), reason=None, stuck=None,
                   picked=sorted(set(blk.get("picked") or ()) | got))
        res.update(state="running", picked=sorted(got))
    else:
        unconfirmed = known - open_now - set(blk["closed"])
        if failure is None and not open_now and not unconfirmed and not blk.get("paused"):
            blk.update(state="done", reason=None, stuck=None)
            res.update(state="done", detail="all %d members closed in %d cycle(s); %d claimed by this run"
                       % (len(known), blk["cycle"], len(blk.get("picked") or ())))
            if blk.get("joined"):
                res["detail"] += "; joined: " + _ids(blk["joined"])
            if blk.get("filed"):
                res["detail"] += "; filed, not in the chain: " + _ids(blk["filed"])
        else:
            if failure is None and blk.get("paused"):
                reason, detail = "decision", "%s held out of the chain and waiting for an answer" % _ids(
                    sorted(int(k) for k in blk["paused"]))
            else:
                reason, detail = _stuck(p, failure, unconfirmed) if (p is not None or failure is not None) \
                    else ("degraded-read", "no read")
            blk.update(state="stuck", reason=detail, stuck=reason)
            res.update(state="stuck", exit=3, reason=reason, detail=detail)
    rec["run"] = blk
    _save(sdlc_dir, goal, rec)
    return res


def stop(sdlc_dir, goal):
    """Mark the run stopped. Local only; goals already claimed stay claimed. -> {"goal", "state"}."""
    rec = _read_record(_record_path(sdlc_dir, goal))
    rec["run"] = dict(rec.get("run") or {}, state="stopped")
    _save(sdlc_dir, goal, rec)
    return {"goal": goal, "state": "stopped"}


def resume(sdlc_dir, goal, cap=None):
    """Clear a stopped, capped or stuck run back to `approved-running`, optionally with a new goal cap kept in
    the record. Local only. Raises `ChainRefused` for a run that is not held, or a cap that is not a positive
    whole number. -> {"goal", "state", "cap"}."""
    rec = _read_record(_record_path(sdlc_dir, goal))
    blk = dict(rec.get("run") or {})
    if blk.get("state") not in ("stopped", "capped", "stuck"):
        raise ChainRefused("chain #%d run is %s; there is nothing to resume" % (goal, blk.get("state") or "not started"))
    if cap is not None and not (isinstance(cap, int) and not isinstance(cap, bool) and cap > 0):
        raise ChainRefused("--cap must be a positive whole number")
    if cap is not None:
        blk["cap"] = cap
    blk.update(state="approved-running", reason=None, stuck=None)
    answered = rec.get("answers") or {}
    for key in [k for k in (blk.get("paused") or {})           # clear what the person answered
                if answered.get("grow:%s" % k, "leave it out") != "leave it out"]:
        blk["paused"].pop(key)
    kept = set(blk.get("paused") or ())
    if not kept:
        blk.pop("paused", None)
    if blk.get("delta"):
        blk["delta"]["rows"] = [r for r in blk["delta"]["rows"] if str(r["number"]) in kept]
        blk["delta"]["why"] = {k: v for k, v in blk["delta"]["why"].items() if k in kept}
        if not blk["delta"]["rows"]:
            blk.pop("delta")
    rec["run"] = blk
    _save(sdlc_dir, goal, rec)
    return {"goal": goal, "state": "approved-running", "cap": blk.get("cap")}


def render_run(res):
    head = "chain #%d: %s (cycle %d)" % (res["goal"], res["state"], res["cycle"])
    out = [head]
    if res["picked"]:
        out.append("claimed: " + _ids(res["picked"]))
    if res["added"]:
        out.append("added to the label since the last cycle (not acted on): " + _ids(res["added"]))
    if res["closed"]:
        out.append("closed since the last cycle: " + _ids(res["closed"]))
    out += ["joined #%d (a new prerequisite of #%d)" % (n, res["goal"]) for n in res.get("joined") or ()]
    if res.get("filed"):
        out.append("filed, not in the chain: " + _ids(res["filed"]))
    if res.get("delta"):
        out.append(res["delta"].rstrip("\n"))
    if res["reason"]:
        out.append("%s: %s" % (res["reason"], res["detail"]))
    elif res["detail"]:
        out.append(res["detail"])
    return "\n".join(out) + "\n"


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


def main(argv, run=None, now=None, pick=None):
    args = list(argv[1:])
    if args in (["-h"], ["--help"]):
        print(USAGE)
        print(SHEET_USAGE)
        print(ARM_USAGE)
        print(RUN_USAGE)
        return 0
    if args[:1] in (["run"], ["stop"], ["resume"]):
        return _main_run(args, run, now, pick)
    if args[:1] in (["sheet"], ["answer"]):
        return _main_sheet(args, run, now)
    if args[:1] in (["approve"], ["arm"]):
        return _main_arm(args, run)
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


def _main_run(args, run, now, pick):
    verb, rest = args[0], args[1:]
    opts = {"run": "--session-pid", "resume": "--cap"}.get(verb)
    no_grow = verb == "run" and "--no-grow" in rest
    if no_grow:
        rest = [a for a in rest if a != "--no-grow"]
    val = None
    if opts and opts in rest:
        i = rest.index(opts)
        val, rest = (rest[i + 1] if i + 1 < len(rest) else None), rest[:i] + rest[i + 2:]
        if val is None or not val.isascii() or not val.isdigit() or int(val) < 1:
            print(RUN_USAGE, file=sys.stderr)
            return 2
    if len(rest) != 2:
        print(RUN_USAGE, file=sys.stderr)
        return 2
    try:
        goal = _chain_arg(rest[1])
        if verb == "stop":
            print("chain #%d: %s" % (goal, stop(rest[0], goal)["state"]))
            return 0
        if verb == "resume":
            r = resume(rest[0], goal, cap=int(val) if val else None)
            print("chain #%d: %s%s" % (goal, r["state"], " (cap %d)" % r["cap"] if r["cap"] else ""))
            return 0
        res = globals()["run"](rest[0], goal, session_pid=val, pick=pick, now=now, run=run,
                           **({"no_grow": True} if no_grow else {}))
    except ChainRefused as exc:
        print("chain.py: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("chain.py: could not write the record: %s" % exc, file=sys.stderr)
        return 1
    print(render_run(res), end="")
    return res["exit"]


def _main_arm(args, run):
    verb, rest = args[0], args[1:]
    dry = verb == "arm" and "--dry-run" in rest
    rest = [a for a in rest if not (verb == "arm" and a == "--dry-run")]
    if len(rest) != 2:
        print(ARM_USAGE, file=sys.stderr)
        return 2
    try:
        goal = _chain_arg(rest[1])
        if verb == "approve":
            print("chain #%d: %s" % (goal, approve(rest[0], goal)["state"]))
            return 0
        res = arm(rest[0], goal, dry_run=dry, run=run)
    except ChainRefused as exc:
        print("chain.py: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("chain.py: could not write the record: %s" % exc, file=sys.stderr)
        return 1
    print(render_arm(res), end="")
    return 1 if any(a["result"] == "failed" for a in res["actions"]) else 0


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
