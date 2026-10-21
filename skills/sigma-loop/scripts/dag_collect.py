"""The full read for the stored dependency map (#1611): lists, outside nodes, the refresh.

`collect(...)` reads GitHub through an injectable `run` and returns a report holding a map in the
`dag_store` schema (nodes, edges, header) and the call counts by class. `refresh(...)` runs it under
the refresh lock and publishes through `dag_store.save`. `dag.py rebuild` is the command line.

What it reads. Open issues for five label sets (goal, blocked, blocking, parked, follow-up), each its
own list, through `gh_api.list_issues` (so `sources.fetch_issues_rest`): no assignee argument is ever
sent, because the map is about structure, not about one owner; each listed issue's own assignees
are kept as a node field (no extra call). An OPEN outside issue keeps its assignees, priority and life too, from the
payload its one read already holds (zero extra calls); a closed or merged outside node has none of the three, which
reads as unknown. 2,000 issues per list at most; a list
that reaches that cap marks the map `partial`, which `dag_store.usable` refuses. Per issue: life from
`blockers.classify` plus the closed set of label states, priority from `discovery.priority_rank` with
the repository's aliases and prefix, edges from `dag_core.edges_from` over title and body, and the
outstanding ledger hand-offs when the ledger is on. A number named by an edge but absent from the
lists is an outside node: read one by one, newest first, at most 25 per periodic refresh and at most
`REBUILD_OUTSIDE_MAX` (500) per `dag.py rebuild`, which takes `--outside-max N` up to the hard cap
`OUTSIDE_HARD_MAX` (2,000; a larger N is clamped to it and the report says so); a resolved one stays
cached for seven days in the previous map, and the rest are reported `unread` (state `unknown`).
Pull requests are read through the pulls endpoint: merged resolves, closed-unmerged does not, open is
open, unreadable is unknown (`blocker_scan.closed_state` decides).

What it never does. A failed list, a non-list payload or an empty result never replaces a good map:
the previous file stays byte-identical and the report carries the error class as `last_error`. With
`discovery.dag.mode` off it reads nothing and writes nothing.

Cost and scale: calls = sum of ceil(n/100) pages over the five lists + at most `outside_max` outside
issue reads (+1 pulls read for each pull request): 25 on the periodic refresh, 500 on a rebuild, 2,000
at the hard cap. Outside numbers cost one read each: at 10x (250) a rebuild makes 250 reads; at 100x
(2,500) it makes 500 and leaves 2,000 `unread` (2,000 reads, 500 unread, with `--outside-max 2000`);
the periodic refresh stays at 25 at any size. The list pages dominate nothing there: about 20 per list at the cap.
Not measured against a real repository here; that run is owner-gated.
"""
import importlib.util
import pathlib
import re
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


dag_store = _load("dag_store")
dag_core = _load("dag_core")
gh_api = _load("gh_api")
blockers = _load("blockers")
blocker_scan = dag_core.blocker_scan
discovery = dag_core.discovery

LIST_CAP = 2000                 # per label set; reaching it marks the map partial
OUTSIDE_MAX = 25                # individual outside reads per periodic refresh
REBUILD_OUTSIDE_MAX = 500       # ... per `dag.py rebuild` unless --outside-max says otherwise
OUTSIDE_HARD_MAX = 2000         # no run reads more than this many outside numbers
CLOSED_TTL = 7 * 86400          # a resolved outside node is not read again for this long
FOLLOWUP_LABEL = "sdlc:followup"
FEATURE_PREFIX = "feature:"
_CLASSES = ("list", "read", "pull")
_RESOLVED = ("closed", "merged")
_LIST_RE = re.compile(r"/issues$")
_READ_RE = re.compile(r"/issues/\d+$")
_PULL_RE = re.compile(r"/pulls/\d+$")


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _gh(config):
    gh = ((config or {}).get("discovery") or {}).get("github")
    return gh if isinstance(gh, dict) else {}


def label_sets(config):
    """The five lists: `(name, label)` pairs, labels as the repository spells them."""
    gh = _gh(config)
    return (("goal", gh.get("goal_label", "sdlc:goal")),
            ("blocked", gh.get("goal_blocked_label", "sdlc:blocked")),
            ("blocking", gh.get("blocking_label", "sdlc:blocking")),
            ("parked", gh.get("parked_label", "sdlc:parked")),
            ("follow-up", FOLLOWUP_LABEL))


def _counting(run):
    calls = {c: 0 for c in _CLASSES}

    def wrapped(args):
        ep = str(args[1]) if len(args) > 1 else ""
        if _LIST_RE.search(ep):
            calls["list"] += 1
        elif _READ_RE.search(ep):
            calls["read"] += 1
        elif _PULL_RE.search(ep):
            calls["pull"] += 1
        return run(args)
    return wrapped, calls


def _strict(run):
    """A list endpoint answering anything but a JSON list (an error object) raises, not 'no issues'."""
    import json

    def wrapped(args):
        raw = run(args)
        if _LIST_RE.search(str(args[1])) and not isinstance(json.loads(raw or "[]"), list):
            raise ValueError("gh returned a non-list issues payload")
        return raw
    return wrapped


def _names(item):
    out = set()
    for lab in item.get("labels") or []:
        n = lab.get("name") if isinstance(lab, dict) else lab
        if isinstance(n, str):
            out.add(n)
    return out


def _prio(names, prefix, aliases):
    best = discovery.UNPRIORITISED
    for n in names:
        if prefix and n.startswith(prefix) or n.lower() in (aliases or {}):
            best = min(best, discovery.priority_rank(n, aliases))
    return discovery.PRIORITIES[best] if best < discovery.UNPRIORITISED else None


def _life(names, labels, in_progress):
    v = blockers.classify({"labels": names, "assignees": [], "closed": False},
                          labels["goal"], "sdlc:proposed", labels["parked"], FOLLOWUP_LABEL, "")
    if v == blockers.CHAINED:
        return "parked"
    if labels["blocked"] in names:
        return "blocked"
    if labels["blocking"] in names:
        return "blocking"
    if in_progress in names:
        return "in-progress"
    if v == blockers.PICKABLE:
        return "goal"
    if v == blockers.ROUTED or FOLLOWUP_LABEL in names:
        return "needs-label"
    return "none"


def _unit(names):
    for n in sorted(names):
        if n.startswith(FEATURE_PREFIX):
            return n[len(FEATURE_PREFIX):]
    return None


def _assignees(item):
    return [a["login"] for a in item["assignees"] if isinstance(a, dict) and isinstance(a.get("login"), str)]


def keep_open_fields(node, item, ctx):
    """An OPEN outside issue keeps its assignees, priority and life from the payload in hand (#1994); a closed
    one is left as it is. Shared with `dag_check`, so an incremental check and a rebuild give the same node."""
    if node["state"] != "open":
        return
    labels, in_progress, prefix, aliases = ctx
    names = _names(item)
    node["life"], node["prio"] = _life(names, labels, in_progress), _prio(names, prefix, aliases)
    if isinstance(item.get("assignees"), list):
        node["assignees"] = _assignees(item)


def _outside(run, repo, number, now, ctx=None):
    """`(node, ok)` for one number outside the lists. Unreadable is `unknown`, never a guess. `ctx` is the
    `(labels, in_progress, prefix, aliases)` the listed nodes use: an OPEN issue then keeps its assignees,
    priority and life from the payload already read (#1994); a closed one gains nothing."""
    node = {"number": number, "kind": "issue", "state": "unknown", "life": "none", "prio": None,
            "in_corpus": False, "read_at": _iso(now)}
    try:
        item = gh_api.view_issue(run, number, repo, comments="none")
    except Exception:                                   # noqa: BLE001 - unreadable is unknown
        return node, False
    node["updated_at"] = item.get("updated_at") if isinstance(item.get("updated_at"), str) else None
    if "pull_request" not in item:
        node["state"] = "closed" if item.get("state") == "closed" else "open"
        if ctx is not None:
            keep_open_fields(node, item, ctx)
        return node, True
    node["kind"] = "pr"
    try:
        pr = gh_api.view_pr(run, number, repo)
        raw = "MERGED" if pr.get("merged") else str(pr.get("state") or "").upper()
        if blocker_scan.closed_state(raw, ""):
            node["state"] = "merged"
        elif raw == "CLOSED":
            node["state"] = "closed-unmerged"
        elif raw == "OPEN":
            node["state"] = "open"
    except Exception:                                   # noqa: BLE001
        pass
    return node, node["state"] != "unknown"


def _cached(previous, now):
    """Resolved nodes still inside their own TTL; the stamp is the node's `read_at`, never the
    header's, so a carried-forward node cannot outlive the week by riding fresh refreshes."""
    out = {}
    for n in (previous or {}).get("nodes", []):
        if n.get("state") not in _RESOLVED:
            continue
        t = dag_store.parse_ts(n.get("read_at"))
        if t is not None and 0 <= now - t < CLOSED_TTL:
            out[n["number"]] = n
    return out


def _handoff_edges(sdlc, config):
    ledger = _load("ledger")
    if not ledger.enabled(config):
        return []
    out = []
    for h in ledger.outstanding(ledger.read_all(sdlc)):
        a, b = str(h.get("issue") or ""), str(h.get("goal") or "")
        if a.isdigit() and b.isdigit() and a != b:
            out.append((int(a), int(b), "other"))
    return out


def collect(sdlc, config, run=None, now=None, previous=None, cap=LIST_CAP, outside_max=None):
    """The report: `{map, calls, unread, last_error, partial, clamped}`; `map` is None when nothing may be
    published (a failed list, a goal list empty beside non-empty others). `outside_max` (default
    `OUTSIDE_MAX`) is clamped to `OUTSIDE_HARD_MAX`; `clamped` holds the cap when it bound."""
    limit = OUTSIDE_MAX if outside_max is None else max(0, int(outside_max))
    clamped = OUTSIDE_HARD_MAX if limit > OUTSIDE_HARD_MAX else None
    limit = min(limit, OUTSIDE_HARD_MAX)
    now = time.time() if now is None else now
    counted, calls = _counting(_strict(run or gh_api._default_run))
    repo = _gh(config).get("repo") or ""
    labels = dict(label_sets(config))
    in_progress = _gh(config).get("in_progress_label", "sdlc:in-progress")
    prefix = _gh(config).get("priority_label_prefix", "priority:")
    aliases = _priority_aliases(config)
    rep = {"map": None, "calls": calls, "unread": [], "last_error": None, "partial": False,
           "clamped": clamped}
    items, partial, sizes = {}, False, {}
    try:
        for _name, label in label_sets(config):
            got = gh_api.list_issues(counted, repo, [label], cap)
            sizes[_name] = len(got)
            partial = partial or len(got) >= cap
            for it in got:
                if isinstance(it, dict) and isinstance(it.get("number"), int):
                    items.setdefault(it["number"], it)
    except Exception as exc:                            # noqa: BLE001 - recorded as a class name
        rep["last_error"] = type(exc).__name__
        return rep
    # Every list read fine and all came back empty: an empty backlog is a valid state (#1933), so it
    # falls through to a 0-node map. A list that returned entries none of which parsed is not empty.
    if any(sizes.values()) and (not sizes.get("goal") or not items):   # PROVISIONAL (owner to ratify): goals never empty alone
        rep["last_error"] = "EmptyList"
        return rep
    nodes, edges = {}, []
    for num, it in sorted(items.items()):
        names = _names(it)
        node = {"number": num, "kind": "issue", "state": "open",
                "life": _life(names, labels, in_progress), "prio": _prio(names, prefix, aliases),
                "in_corpus": True, "updated_at": it.get("updated_at") if isinstance(it.get("updated_at"), str) else None}
        unit = _unit(names)
        if unit:
            node["unit"] = unit
        if isinstance(it.get("assignees"), list):       # listed with the issue: no extra call
            node["assignees"] = _assignees(it)
        nodes[num] = node
        edges += [(a, b, "body") for a, b, _p in
                  dag_core.edges_from("%s\n%s" % (it.get("title") or "", it.get("body") or ""), num)]
    try:
        edges += _handoff_edges(sdlc, config)
    except Exception:                                   # noqa: BLE001 - ledger trouble costs hand-off edges only
        pass
    known = _cached(previous, now)
    reads = 0
    for num in sorted({n for e in edges for n in e[:2]} - set(nodes), reverse=True):
        if num in known:
            nodes[num] = known[num]
        elif reads < limit:
            reads += 1
            nodes[num], _ok = _outside(counted, repo, num, now, (labels, in_progress, prefix, aliases))
        else:
            rep["unread"].append(num)
            nodes[num] = {"number": num, "kind": "issue", "state": "unknown", "life": "none",
                          "prio": None, "in_corpus": False}
    stamp = _iso(now)
    rep["partial"] = partial
    rep["map"] = {"header": {"repo": repo or None, "built_at": stamp, "refreshed_at": stamp,
                             "last_ok_at": stamp, "last_error": None, "partial": partial,
                             "cursor": None, "etag": None, "checked_at": None},
                  "nodes": [nodes[n] for n in sorted(nodes)],
                  "edges": [{"blocker": a, "blocked": b, "source": s} for a, b, s in sorted(set(edges))]}
    return rep


def _priority_aliases(config):
    a = ((config or {}).get("discovery") or {}).get("priority_aliases")
    return {str(k).strip().lower(): v for k, v in a.items()} if isinstance(a, dict) else {}


def refresh(sdlc, config=None, run=None, now=None, cap=LIST_CAP, outside_max=None):
    """Collect and publish under the refresh lock. `status`: `off`, `busy`, `kept` (nothing
    published, the old file untouched) or `saved`."""
    if config is None:
        try:
            config = dag_store.state.load_config(sdlc)
        except Exception:                               # noqa: BLE001
            config = {}
    if dag_store.mode(config) == "off":
        return {"status": "off", "calls": {c: 0 for c in _CLASSES}, "unread": [], "last_error": None,
                "partial": False}
    with dag_store.refresh_lock(sdlc, config) as held:
        if held == "busy":
            return {"status": "busy", "calls": {c: 0 for c in _CLASSES}, "unread": [],
                    "last_error": None, "partial": False}
        previous, _why = dag_store.load(sdlc, config)
        rep = collect(sdlc, config, run, now, previous, cap, outside_max)
        if rep["map"] is None:
            rep["status"] = "kept"
            return rep
        ok, _r = dag_store.save(sdlc, rep["map"], config)
        rep["status"] = "saved" if ok else "kept"
        if not ok:
            rep["last_error"] = "SaveFailed"
        return rep
