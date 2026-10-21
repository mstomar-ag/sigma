"""The cheap change check for the stored dependency map (#1617): one request before a pick.

`check(sdlc, config, ...)` asks GitHub for the issues and pull requests changed since the map's cursor
(`issues?state=all&since=<cursor>&sort=updated&direction=asc&per_page=100`), sending the stored ETag, and
applies what changed to the stored map in place. It is called once at the start of a pick, and only when
`discovery.dag.mode` is not `off` and a stored map exists; otherwise it does nothing at all (no request,
no file, no output). It never changes how a pick chooses; it only keeps the map current between the
hourly full reads (`dag_refresh`).

The request is `gh_api.conditional_get` (`gh api -i`; exit 1 with a 304 status line means not modified).
- 304: nothing changed, no quota is used, `checked_at` advances.
- 200: each changed item is applied: state, life, priority and unit for a labelled open issue (the same
  corpus rule and the same helpers as `dag_collect`), body edges re-extracted from the issue text in the
  response, a pull request's merged flag from the pulls endpoint, a closed or no-longer-labelled issue
  becoming an outside node (or dropped when nothing references it), a new issue added. Paged until a
  short page. The cursor becomes the newest `updated_at` seen and the ETag the one just returned. The
  result equals what a full read of the same final state would build (read stamps aside).
- More than 500 changed items (`CHANGED_MAX`): nothing is applied; a forced full refresh is requested
  instead, after the lock is released.
- Any failure (transport, status, parse, save): the map is left byte-identical, ONE stderr line is
  printed and the pick goes on with today's path. Nothing is raised to the caller.

The first check on a map that has no cursor yet uses its last good stamp minus `OVERLAP_SECONDS`. `since`
is inclusive, so the newest item comes back once more on the next 200; applying it again is idempotent,
and the request after that is a 304. Outside references new to the map are read under `dag_collect`'s
`OUTSIDE_MAX`; past it they are stored `unknown` (never resolving), as a full read does. A changed pull
request past that cap is stored `unknown` too; a failed pulls read fails the whole check.

Known gaps, by design (the hourly full read closes them): a priority changed only on the Project board
field does not change the issue, so this check misses it; a deleted or transferred issue is invisible
to a `since` query. A priority a full read takes from labels is the same here.

One writer: the map is published only through `dag_store.save`, under the refresh lock (a busy lock
skips the check). Cost: one request when nothing changed; otherwise ceil(changed/100) pages plus one
pulls read per changed pull request and at most `OUTSIDE_MAX` reads, bounded by `CHANGED_MAX`.
"""
import importlib.util
import pathlib
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


dag_collect = _load("dag_collect")
dag_store = dag_collect.dag_store
dag_core = dag_collect.dag_core
gh_api = dag_collect.gh_api
blocker_scan = dag_collect.blocker_scan

CHANGED_MAX = 500               # more changed items than this asks for a full refresh instead
PAGE = 100
OVERLAP_SECONDS = 120           # first cursor: the map's stamp minus this
_STATUSES = ("off", "no-map", "busy", "not-modified", "applied", "refresh-requested", "error")


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class _TooMany(Exception):
    """More than `CHANGED_MAX` items changed."""


def _fetch(get, endpoint, cursor, etag):
    """-> `(first_response, items)`; pages until a short page. Raises `_TooMany` past the limit."""
    fields = {"state": "all", "since": cursor, "sort": "updated", "direction": "asc", "per_page": str(PAGE)}
    first = gh_api.conditional_get(endpoint, etag=etag, fields=fields, run=get)
    if first["not_modified"]:
        return first, []
    items = list(_as_list(first["body"]))
    page, last = 1, len(items)
    while last >= PAGE:
        if len(items) > CHANGED_MAX:
            break
        page += 1
        more = gh_api.conditional_get(endpoint, fields=dict(fields, page=str(page)), run=get)
        got = list(_as_list(more["body"]))
        items += got
        last = len(got)
    if len(items) > CHANGED_MAX:
        raise _TooMany()
    return first, items


def _as_list(body):
    if not isinstance(body, list):
        raise ValueError("gh returned a non-list issues payload")
    return body


def _pr_state(pr):
    raw = "MERGED" if pr.get("merged") else str(pr.get("state") or "").upper()
    if blocker_scan.closed_state(raw, ""):
        return "merged"
    if raw == "CLOSED":
        return "closed-unmerged"
    if raw == "OPEN":
        return "open"
    return "unknown"


def _apply(m, items, config, run, now):
    """The new map for `items` applied to `m` (a copy; `m` is not touched)."""
    gh = dag_collect._gh(config)
    repo = gh.get("repo") or m["header"].get("repo") or ""
    labels = dict(dag_collect.label_sets(config))
    corpus_labels = set(labels.values())
    in_progress = gh.get("in_progress_label", "sdlc:in-progress")
    prefix = gh.get("priority_label_prefix", "priority:")
    aliases = dag_collect._priority_aliases(config)
    nodes = {n["number"]: dict(n) for n in m["nodes"]}
    edges = [dict(e) for e in m["edges"]]
    latest = {}
    for it in items:                                    # ascending by updated_at: the last one wins
        if isinstance(it, dict) and isinstance(it.get("number"), int):
            latest[it["number"]] = it
    reads = [0]
    stamp = _iso(now)

    def outside_from(it):
        node = {"number": it["number"], "kind": "issue", "state": "unknown", "life": "none", "prio": None,
                "in_corpus": False, "read_at": stamp,
                "updated_at": it.get("updated_at") if isinstance(it.get("updated_at"), str) else None}
        if it.get("pull_request") is None:
            node["state"] = "closed" if it.get("state") == "closed" else "open"
            return node
        node["kind"] = "pr"
        if reads[0] >= dag_collect.OUTSIDE_MAX:         # over the cap: unread, never resolving
            return node
        reads[0] += 1
        node["state"] = _pr_state(gh_api.view_pr(run, it["number"], repo or None))   # a failed read fails the check
        return node

    corpus = {}
    for num, it in sorted(latest.items()):
        names = dag_collect._names(it)
        if it.get("state") == "open" and names & corpus_labels:
            node = {"number": num, "kind": "issue", "state": "open",
                    "life": dag_collect._life(names, labels, in_progress),
                    "prio": dag_collect._prio(names, prefix, aliases), "in_corpus": True,
                    "updated_at": it.get("updated_at") if isinstance(it.get("updated_at"), str) else None}
            unit = dag_collect._unit(names)
            if unit:
                node["unit"] = unit
            if isinstance(it.get("assignees"), list):   # as `dag_collect.collect` stores them (#1982)
                node["assignees"] = [a["login"] for a in it["assignees"]
                                     if isinstance(a, dict) and isinstance(a.get("login"), str)]
            nodes[num] = node
            corpus[num] = it
        elif num in nodes:
            nodes[num] = outside_from(it)
    changed = set(latest)
    edges = [e for e in edges if not (e["source"] == "body" and e["blocked"] in changed)]
    have = {(e["blocker"], e["blocked"], e["source"]) for e in edges}
    for num, it in sorted(corpus.items()):
        text = "%s\n%s" % (it.get("title") or "", it.get("body") or "")
        for a, b, _p in dag_core.edges_from(text, num):
            if (a, b, "body") not in have:
                have.add((a, b, "body"))
                edges.append({"blocker": a, "blocked": b, "source": "body"})
    for num in sorted({n for e in edges for n in (e["blocker"], e["blocked"])} - set(nodes), reverse=True):
        if num in latest:
            nodes[num] = outside_from(latest[num])
        elif reads[0] < dag_collect.OUTSIDE_MAX:
            reads[0] += 1
            nodes[num], _ok = dag_collect._outside(run, repo, num, now)
        else:
            nodes[num] = {"number": num, "kind": "issue", "state": "unknown", "life": "none",
                          "prio": None, "in_corpus": False}
    referenced = {n for e in edges for n in (e["blocker"], e["blocked"])}
    nodes = {n: v for n, v in nodes.items() if v.get("in_corpus") or n in referenced}
    out = {"header": dict(m["header"]), "nodes": [nodes[n] for n in sorted(nodes)],
           "edges": sorted(edges, key=lambda e: (e["blocker"], e["blocked"], e["source"]))}
    return out


def _once(sdlc, config, m, get, run, now):
    """Under the lock. -> `(status, detail)`; saves on 304 and 200."""
    header = m["header"]
    repo = dag_collect._gh(config).get("repo") or header.get("repo") or ""
    endpoint = gh_api._endpoint(repo, "issues")
    cursor = header.get("cursor")
    if not cursor:
        t = dag_store.stamp(m)
        if t is None:
            raise ValueError("map has no stamp to start from")
        cursor = _iso(t - OVERLAP_SECONDS)
    try:
        first, items = _fetch(get, endpoint, cursor, header.get("etag"))
    except _TooMany:
        return "refresh-requested", "changed items over %d" % CHANGED_MAX
    new = {"header": dict(header), "nodes": m["nodes"], "edges": m["edges"]}
    if not first["not_modified"]:
        new = _apply(m, items, config, run, now)
        stamps = [it["updated_at"] for it in items if isinstance(it, dict)
                  and isinstance(it.get("updated_at"), str) and dag_store._ts(it["updated_at"])]
        if stamps:
            cursor = max([cursor] + stamps)
    new["header"].update({"cursor": cursor, "checked_at": _iso(now)})
    if first.get("etag"):
        new["header"]["etag"] = first["etag"]
    ok, why = dag_store.save(sdlc, new, config)
    if not ok:
        raise RuntimeError("SaveFailed: " + why)
    return ("not-modified" if first["not_modified"] else "applied"), "%d changed" % len(items)


def check(sdlc, config=None, get=None, run=None, now=None, refresh=None):
    """One change check. Never raises. -> `{status, detail}`; status is one of `off`, `no-map`, `busy`,
    `not-modified`, `applied`, `refresh-requested` or `error`. `get` is the conditional runner
    (`(returncode, stdout, stderr)`), `run` the stdout-only runner; `refresh(sdlc, config, run=, force=)`
    is the full refresh a too-large change asks for. All three default to the real ones."""
    now = time.time() if now is None else now
    try:
        if config is None:
            config = dag_store.state.load_config(sdlc)
    except Exception:                                   # noqa: BLE001
        config = {}
    try:
        if dag_store.mode(config) == "off":
            return {"status": "off", "detail": ""}
        m, why = dag_store.load(sdlc, config)
        if m is None:
            return {"status": "no-map", "detail": why}
        status = detail = None
        with dag_store.refresh_lock(sdlc, config) as held:
            if held == "busy":
                return {"status": "busy", "detail": ""}
            status, detail = _once(sdlc, config, m, get, run or gh_api._default_run, now)
        if status == "refresh-requested":
            if refresh is None:
                refresh = _load("dag_refresh").refresh_if_due
            refresh(sdlc, config, run=run, force=True)
        return {"status": status, "detail": detail}
    except Exception as exc:                            # noqa: BLE001 - a change check never fails a pick
        kind = getattr(exc, "kind", None)
        print("dag_check: change check failed (%s%s) — using the existing path"
              % (type(exc).__name__, "/" + kind if kind else ""), file=sys.stderr)
        return {"status": "error", "detail": type(exc).__name__}
