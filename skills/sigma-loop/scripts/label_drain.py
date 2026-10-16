#!/usr/bin/env python3
"""A read-only view of everything a temporary run label would drain (#1576).

    label_drain.py plan   <sdlc_dir> <label> [--json]
    label_drain.py status <sdlc_dir> <label> [--json]

Draining a label is more than filtering. Members that carry the label but are not goals are invisible
to the picker; a member held by an open blocker outside the label stalls the run silently; a parked
member is a question nobody has seen. This module shows the whole set, with its transitive open
blockers, before anything is written. IT NEVER WRITES: not to GitHub, the mirror, the ledger or any
issue, and it never deletes or removes a label (a person does that). The guarantee is structural, not
a habit: every call goes through `_ReadOnly`, which raises on anything that is not a GET, a GraphQL
query, or a read-only `gh` view/list.

REUSE, NOT COPIES.
  - the label rules are `sources.scope_to_label` (slice #1575), imported and called as is;
  - members come from `sources.fetch_issues_rest`; single issues from `gh_api.view_issue`;
  - edges come from `blocker_scan.extract_refs`, the explicit vocabulary the pick gate uses;
  - cycles come from `slices._cycles`, as `feature_frontier.py` does;
  - a parked member's kind and question come from `unpark.brief`.

FAIL CLOSED, like `feature_frontier.py` and for the same reason: this is advisory output, and a pull
request, another repository's reference, an unreadable ref, or a ref the walk was stopped before
reading keeps its dependent OUT of the ready set (listed as unresolved). Reading one as satisfied
would print "ready" for a member whose real prerequisite may still be open.

BOUNDS. At most `MAX_MEMBERS` (200) members (more is refused with the count, never truncated), at
most `MAX_PULLED` (50) single-issue GETs, depth `MAX_DEPTH` (8). Hitting either walk bound stops the
walk and the output says which bound and where. Calls are counted and printed (`calls` in --json).

The "one list + at most 50 GETs" bound covers the label lookup, the member list and the blocker walk.
A parked member's brief (`unpark.brief`) adds its own reads (comments, uncached blocker lookups; its
API takes no shared cache, so they are not deduplicated), so at most `MAX_PARKED_BRIEFS` (20) parked
members are briefed; the rest are reported `parked` with kind `not-read`, no question text, and a
`parked_not_briefed` warning. Those reads are counted in `calls` like any other, so the total is
bounded by the above plus 20 briefs, not by the 50-GET walk bound alone.

`closure()` takes a seed set of issue numbers, not a label, so a later dependency-chain feature can
call it with one end goal as the seed.
"""
import collections
import importlib.util
import json
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


USAGE = ("usage: label_drain.py plan|status <sdlc_dir> <label> [--json]\n"
         "  read-only: lists the open issues carrying <label>, their open blockers, and a verdict\n"
         "  for each. Never writes and never removes the label.")

MAX_MEMBERS = 200
MAX_PULLED = 50
MAX_DEPTH = 8
MAX_PARKED_BRIEFS = 20

_PAID_RE = re.compile(r"(?im)^\s*(?:[-*]\s+)?(?:\*\*)?paid[ -]step(?:\*\*)?\s*:")
_WRITE_VERBS = {"POST", "PUT", "PATCH", "DELETE"}
_READ_VERBS = {"issue view", "issue list", "repo view"}   # read-only `gh` verbs, as words


class _ReadOnly:
    """Wrap a `gh` runner: count every call and refuse anything that could write."""

    def __init__(self, run):
        self._run = run
        self.counts = collections.Counter()

    def __call__(self, args):
        args = [str(a) for a in args]
        self._check(args)
        self.counts["total"] += 1
        self.counts[self._kind(args)] += 1
        return self._run(args)

    @staticmethod
    def _methods(args):
        """Every HTTP method the args name: `--method M`, `--method=M`, `-X M`, `-XM`, `-X=M`."""
        out = []
        for i, a in enumerate(args):
            if a in ("--method", "-X"):
                out.append(args[i + 1] if i + 1 < len(args) else "")
            elif a.startswith("--method="):
                out.append(a[len("--method="):])
            elif a.startswith("-X"):
                out.append(a[2:].lstrip("="))
        return [m.upper() for m in out]

    @staticmethod
    def _check(args):
        if args[:1] == ["api"]:
            methods = _ReadOnly._methods(args)
            if any(m != "GET" for m in methods):
                raise RuntimeError("label_drain is read-only: refused %s" % " ".join(args[:4]))
            doc = next((a for a in args if a.startswith("query=") or "=query=" in a), "")
            if "mutation" in doc.lower():
                raise RuntimeError("label_drain is read-only: refused a GraphQL mutation")
            graphql = args[1:2] == ["graphql"]
            has_body = any(a in ("-f", "-F", "--field", "--raw-field", "--input")
                           or a.startswith(("--field=", "--raw-field=", "--input=")) for a in args)
            # `gh api` turns any field/input flag into a POST unless the method is explicitly GET.
            # GraphQL is always a POST and is a read only because it is a query.
            if has_body and not graphql and "GET" not in methods:
                raise RuntimeError("label_drain is read-only: refused a `gh api` call with a body "
                                   "and no explicit GET: %s" % " ".join(args[:4]))
            return
        if " ".join(args[:2]) in _READ_VERBS:
            return
        raise RuntimeError("label_drain is read-only: refused `gh %s`" % " ".join(args[:2]))

    @staticmethod
    def _kind(args):
        if args[:2] == ["api", "graphql"]:
            return "graphql"
        if args[:1] == ["api"] and re.search(r"/issues$", args[1]):
            return "list"
        if args[:1] == ["api"] and re.search(r"/issues/\d+$", args[1]):
            return "get"
        return "other"

    def summary(self):
        return {k: self.counts.get(k, 0) for k in ("total", "list", "get", "graphql", "other")}


def _names(issue):
    return {(l.get("name") if isinstance(l, dict) else str(l)) or ""
            for l in (issue.get("labels") or [])}


def _cross_repo_tags(body, blocker_scan):
    """`owner/repo#N` tags for explicit blocker phrases pointing at another repository, which
    `extract_refs` deliberately skips (they must never collapse to a local #N)."""
    text = blocker_scan.strip_unpark_qa(body or "")
    out = []
    for m in blocker_scan._EXPLICIT_BLOCK_RE.finditer(text):
        if blocker_scan.is_cross_repo(text, m):
            i = m.start(2) - 1
            j = i
            while j > 0 and (text[j - 1].isalnum() or text[j - 1] in "_/-."):
                j -= 1
            out.append("%s#%s (another repository)" % (text[j:i], m.group(2)))
    return out


def closure(seed_numbers, get, preloaded=None, max_depth=MAX_DEPTH, max_pulled=MAX_PULLED,
            blocker_scan=None):
    """Walk the open blockers of `seed_numbers` breadth-first.

    `get(n)` -> one REST issue dict (raises when unreadable); `preloaded` maps number -> issue for
    seeds already in hand. Seeds are depth 0, their blockers depth 1, and so on to `max_depth`.
    -> {"nodes": {n: {"issue", "depth", "outside"}}, "edges": {n: [open blocker n]},
        "closed": {n: [closed blocker n]}, "unresolved": {n: [tag]}, "stops": [message],
        "pulled": int}
    A blocker that is a pull request, in another repository, unreadable, or past a bound is
    UNRESOLVED for its dependent. A closed blocker is satisfied and dropped."""
    blocker_scan = blocker_scan or _load("blocker_scan")
    preloaded = preloaded or {}
    seeds = sorted(int(n) for n in seed_numbers)
    nodes, edges, closed, unresolved, stops = {}, {}, {}, {}, []
    dead = {}                      # ref -> tag, for refs that are PR/unreadable (memoised)
    closed_refs = set()
    pulled = 0
    queue = collections.deque()
    for n in seeds:
        issue = preloaded.get(n)
        if issue is None:
            try:
                issue = get(n)
            except Exception:      # noqa: BLE001 - an unreadable seed is reported, not fatal
                unresolved.setdefault(n, []).append("#%d (seed unreadable)" % n)
                continue
        nodes[n] = {"issue": issue, "depth": 0, "outside": False}
        queue.append(n)
    while queue:
        n = queue.popleft()
        issue, depth = nodes[n]["issue"], nodes[n]["depth"]
        body = issue.get("body") or ""
        for tag in _cross_repo_tags(body, blocker_scan):
            unresolved.setdefault(n, []).append(tag)
        for item in blocker_scan.extract_refs(body, self_ref=n):
            r = int(item["ref"])
            if r in nodes:
                edges.setdefault(n, []).append(r)
                continue
            if r in closed_refs:
                closed.setdefault(n, []).append(r)
                continue
            if r in dead:
                unresolved.setdefault(n, []).append(dead[r])
                continue
            if depth + 1 > max_depth:
                stops.append("depth limit %d reached: #%d (blocker of #%d, depth %d) was not read"
                             % (max_depth, r, n, depth + 1))
                unresolved.setdefault(n, []).append("#%d (not read: depth limit)" % r)
                continue
            if pulled >= max_pulled:
                stops.append("pulled-issue limit %d reached before reading #%d (blocker of #%d)"
                             % (max_pulled, r, n))
                unresolved.setdefault(n, []).append("#%d (not read: pulled-issue limit)" % r)
                continue
            pulled += 1
            try:
                rec = get(r)
                if not isinstance(rec, dict):
                    raise ValueError("malformed")
            except Exception:      # noqa: BLE001 - fail closed: unresolved
                dead[r] = "#%d (unreadable)" % r
                unresolved.setdefault(n, []).append(dead[r])
                continue
            if "pull_request" in rec:
                dead[r] = "#%d (pull request)" % r
                unresolved.setdefault(n, []).append(dead[r])
                continue
            state = str(rec.get("state") or "").lower()
            if state == "closed":
                closed_refs.add(r)
                closed.setdefault(n, []).append(r)
                continue
            if state != "open":
                dead[r] = "#%d (unreadable)" % r
                unresolved.setdefault(n, []).append(dead[r])
                continue
            nodes[r] = {"issue": rec, "depth": depth + 1, "outside": True}
            edges.setdefault(n, []).append(r)
            queue.append(r)
    return {"nodes": nodes, "edges": edges, "closed": closed, "unresolved": unresolved,
            "stops": list(dict.fromkeys(stops)), "pulled": pulled}


class _Judge:
    """Per-run verdict context: the source's label names, config, and lazily resolved login."""

    def __init__(self, sdlc_dir, config, source):
        self.sdlc_dir, self.config, self.source = sdlc_dir, config, source
        self.features = _load("features")
        self.gate_hold = _load("gate_hold")
        self.promote = _load("promote")
        self._login = False

    def login(self):
        if self._login is False:
            try:
                self._login = self.source._assignee_login()
            except Exception:      # noqa: BLE001 - fails open, like the picker
                self._login = None
        return self._login

    def verdict(self, issue):
        """-> (verdict, detail) for one open issue. Order: the first that applies wins."""
        names = _names(issue)
        if str(issue.get("state") or "").lower() == "closed":
            return "closed", ""
        tier = self.promote._upper_tier(names)
        if tier:
            return "upper-tier", "labelled %s; the picker never takes it" % tier
        if self.source.parked_label in names:
            return "parked", "carries %s" % self.source.parked_label
        if self.source.goal_label not in names:
            return "unarmed", "no %s label" % self.source.goal_label
        logins = [(a.get("login") if isinstance(a, dict) else str(a)) for a in (issue.get("assignees") or [])]
        if getattr(self.source, "assignee", None):
            me = self.login()
            if me and not logins:
                return "assigned-elsewhere", "unassigned; the picker only takes issues assigned to %s" % me
            if me and me.casefold() not in {x.casefold() for x in logins}:
                return "assigned-elsewhere", "assigned to %s, not %s" % (", ".join(logins), me)
        held = self._gate(issue, names)
        if held:
            return "held-by-gate", held
        return "armed", ""

    def _gate(self, issue, names):
        f = self.features
        try:
            v = f.read(issue)
        except f.AmbiguousUnit:
            return "declares two different units; a person must edit it"
        if v.state == f.CONFLICT:
            return "body names unit %s but the label names %s" % (v.body, v.label)
        if v.state == f.BODY_ONLY:
            return "body declares unit %s but the feature label is missing" % v.unit
        if v.state == f.NONE and getattr(self.source, "no_dangling_goal_enabled", False):
            return "declares no unit and the no-dangling-goal rule is on"
        author = (issue.get("user") or {}).get("login")
        try:
            hold = self.gate_hold.feature_hold(self.sdlc_dir, self.config, issue.get("number"),
                                               names, author)
        except Exception:          # noqa: BLE001 - read-only predicate, fails open
            hold = None
        return hold or ""


def _done_when_missing(body, acceptance):
    try:
        return acceptance.criteria(body or "") is None
    except ValueError:
        return False               # a malformed section is present, just not valid


def _unit(issue, features):
    try:
        return features.read(issue).unit
    except features.AmbiguousUnit:
        return None


def plan(sdlc_dir, label, config=None, run=None):
    """-> the full read-only plan dict. Raises `sources.LabelScopeError` or `ValueError` (too many
    members) for the refusals the CLI turns into exit 2."""
    sources = _load("sources")
    state_mod = _load("state")
    config = config if config is not None else state_mod.load_config(sdlc_dir)
    ro = _ReadOnly(run or sources._run_gh)
    source = sources.GitHubSource(config, run=ro)   # no sdlc_dir: gh_api cannot write breaker state
    sources.scope_to_label(source, label)             # slice #1575's rules, not a copy
    repo = source.repo
    listed = sources.fetch_issues_rest(ro, repo, [label], MAX_MEMBERS + 1)
    if len(listed) > MAX_MEMBERS:
        raise ValueError("label %r has more than %d open members (%d read before stopping); "
                         "refusing rather than truncating. Narrow the label first."
                         % (label, MAX_MEMBERS, len(listed)))
    members = {int(i["number"]): i for i in listed}
    gh_api, blocker_scan = _load("gh_api"), _load("blocker_scan")
    features, acceptance, slices = _load("features"), _load("acceptance"), _load("slices")
    scrub = _load("scrub").scrub

    def get(n):
        return gh_api.view_issue(ro, n, repo or None, comments="none")

    clo = closure(members, get, preloaded=members, blocker_scan=blocker_scan)
    judge = _Judge(sdlc_dir, config, source)
    unpark = None
    briefed, not_briefed = 0, []
    rows, outside = [], []
    for n in sorted(clo["nodes"]):
        node = clo["nodes"][n]
        issue = node["issue"]
        verdict, detail = judge.verdict(issue)
        names = _names(issue)
        waiting = sorted(clo["edges"].get(n, []))
        unres = list(dict.fromkeys(clo["unresolved"].get(n, [])))
        row = {"number": n, "title": scrub(str(issue.get("title") or "")), "verdict": verdict,
               "detail": detail, "waiting_on": waiting, "unresolved": unres,
               "dropped_closed": sorted(set(clo["closed"].get(n, [])))}
        if node["outside"]:
            row["depth"] = node["depth"]
            row["verdict"], row["own_verdict"] = "outside", verdict
            row["display"] = "outside/%s" % verdict
            outside.append(row)
            continue
        if verdict == "parked":
            if briefed >= MAX_PARKED_BRIEFS:
                row["kind"], row["question"] = "not-read", ""
                not_briefed.append(n)
            else:
                briefed += 1
                unpark = unpark or _load("unpark")
                try:
                    b = unpark.brief(sdlc_dir, config, n, source=source)
                    row["kind"] = b.get("reason_class") if b.get("kind_declared") else "undeclared"
                    row["question"] = scrub(str(b.get("park_reason") or ""))
                except Exception:  # noqa: BLE001 - say so rather than guess
                    row["kind"], row["question"] = "unreadable", ""
        row["display"] = verdict
        in_progress = source.in_progress_label in names
        row["in_progress"] = in_progress
        row["reason"] = _reason(row, in_progress, source.goal_blocked_label in names)
        row["ready"] = row["reason"] is None
        rows.append(row)
    for r in rows + outside:
        r["ready"] = r.get("ready", False)

    # --- warnings
    unit_of = {n: _unit(clo["nodes"][n]["issue"], features) for n in clo["nodes"]}
    cross = []
    for a, bs in sorted(clo["edges"].items()):
        for b in sorted(bs):
            ua, ub = unit_of.get(a), unit_of.get(b)
            if ua and ub and ua.lower() != ub.lower():
                cross.append({"from": a, "to": b, "message":
                              "#%d is based on its unit's branch (%s) and will not contain #%d's "
                              "change until #%d's unit (%s) lands" % (a, ua, b, b, ub)})
    graph = [{"id": str(n), "needs": [str(x) for x in clo["edges"].get(n, [])], "status": "pending"}
             for n in sorted(clo["nodes"])]
    cycles = [[int(x) for x in c] for c in slices._cycles(graph)]
    member_issues = [clo["nodes"][n]["issue"] for n in sorted(members) if n in clo["nodes"]]
    warnings = {
        "cross_unit": cross,
        "cycles": cycles,
        "no_done_when": sorted(i["number"] for i in member_issues
                               if _done_when_missing(i.get("body"), acceptance)),
        "paid_step": sorted(i["number"] for i in member_issues if _PAID_RE.search(i.get("body") or "")),
        "parked_not_briefed": (["%d parked member(s) not briefed: only the first %d parked members get "
                                "their kind and question read (each read costs several GETs): %s"
                                % (len(not_briefed), MAX_PARKED_BRIEFS,
                                   ", ".join("#%d" % x for x in not_briefed))] if not_briefed else []),
    }
    unresolved_all = collections.defaultdict(list)
    for r in rows + outside:
        for tag in r["unresolved"]:
            unresolved_all[tag].append(r["number"])
    counts = collections.Counter(r["display"] for r in rows)
    return {"label": label, "repo": repo, "members": rows, "outside": outside,
            "unresolved": [{"ref": t, "dependents": d} for t, d in sorted(unresolved_all.items())],
            "warnings": warnings, "stops": clo["stops"], "counts": dict(sorted(counts.items())),
            "pickable": sum(1 for r in rows if r["ready"]), "pulled": clo["pulled"],
            "drained": not rows, "calls": ro.summary()}


def _reason(row, in_progress, blocked_overlay):
    """The one reason an open member is not pickable now, or None when it is."""
    v = row["verdict"]
    if v == "upper-tier":
        return row["detail"]
    if v == "parked":
        q = row.get("question") or ""
        return "parked (%s)%s" % (row.get("kind") or "undeclared", ": " + q[:140] if q else "")
    if v in ("unarmed", "assigned-elsewhere", "held-by-gate"):
        return "%s: %s" % (v, row["detail"])
    if in_progress:
        return "in progress (claimed by a run)"
    if blocked_overlay:
        return "carries the blocked label"
    if row["waiting_on"]:
        return "waiting on open " + ", ".join("#%d" % x for x in row["waiting_on"])
    if row["unresolved"]:
        return "unresolved blocker " + ", ".join(row["unresolved"])
    return None


def render_plan(p):
    out = ["label %s (%s): %d open member(s), %d pickable now" % (
        p["label"], p["repo"] or "this repo", len(p["members"]), p["pickable"])]
    for r in p["members"]:
        extra = ""
        if r["verdict"] == "parked":
            extra = " [%s] %s" % (r.get("kind"), r.get("question"))
        elif r["detail"]:
            extra = " -- " + r["detail"]
        wait = " (waiting on %s)" % ", ".join("#%d" % x for x in r["waiting_on"]) if r["waiting_on"] else ""
        out.append("  #%d %s%s%s: %s" % (r["number"], r["display"], extra, wait, r["title"]))
    for r in p["outside"]:
        out.append("  #%d %s (blocker, depth %d): %s" % (r["number"], r["display"], r["depth"], r["title"]))
    for u in p["unresolved"]:
        out.append("  unresolved: %s, blocks %s" % (u["ref"], ", ".join("#%d" % d for d in u["dependents"])))
    w = p["warnings"]
    for c in w["cross_unit"]:
        out.append("  warning: " + c["message"])
    for c in w["cycles"]:
        out.append("  warning: cycle " + " -> ".join("#%d" % x for x in c))
    if w["no_done_when"]:
        out.append("  warning: no Done when: " + ", ".join("#%d" % x for x in w["no_done_when"]))
    if w["paid_step"]:
        out.append("  warning: declares a paid step: " + ", ".join("#%d" % x for x in w["paid_step"]))
    for m in w["parked_not_briefed"]:
        out.append("  warning: " + m)
    for s in p["stops"]:
        out.append("  STOPPED: " + s)
    out.append("calls: %s" % json.dumps(p["calls"], sort_keys=True))
    return "\n".join(out) + "\n"


def render_status(p):
    if p["drained"]:
        return ("label %s: drained, the label can be deleted by a person (Sigma never deletes a "
                "label).\ncalls: %s\n" % (p["label"], json.dumps(p["calls"], sort_keys=True)))
    out = ["label %s: %d open member(s), %d pickable now" % (p["label"], len(p["members"]), p["pickable"]),
           "  verdicts: " + ", ".join("%s %d" % (k, v) for k, v in p["counts"].items())]
    for r in p["members"]:
        if r["reason"]:
            out.append("  #%d not pickable: %s" % (r["number"], r["reason"]))
    for s in p["stops"]:
        out.append("  STOPPED: " + s)
    out.append("calls: %s" % json.dumps(p["calls"], sort_keys=True))
    return "\n".join(out) + "\n"


def main(argv, run=None):
    args = list(argv[1:])
    if args in ([], ["-h"], ["--help"]):
        print(USAGE)
        return 0 if args else 2
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    if len(args) != 3 or args[0] not in ("plan", "status"):
        print(USAGE, file=sys.stderr)
        return 2
    verb, sdlc_dir, label = args
    sources = _load("sources")
    try:
        p = plan(sdlc_dir, label, run=run)
    except (sources.LabelScopeError, ValueError) as exc:
        print("label_drain.py: %s" % exc, file=sys.stderr)
        return 2
    if as_json:
        view = p if verb == "plan" else {k: p[k] for k in ("label", "counts", "pickable", "drained", "stops", "calls")}
        if verb == "status":
            view["not_pickable"] = [{"number": r["number"], "reason": r["reason"]}
                                    for r in p["members"] if r["reason"]]
            view["message"] = ("drained, the label can be deleted by a person" if p["drained"] else "")
        print(json.dumps(view, indent=2, sort_keys=True))
    else:
        print((render_plan if verb == "plan" else render_status)(p), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
