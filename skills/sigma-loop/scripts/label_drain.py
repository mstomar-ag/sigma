#!/usr/bin/env python3
"""A read-only view of everything a temporary run label would drain (#1576).

    label_drain.py plan   <sdlc_dir> <label> [--json]
    label_drain.py status <sdlc_dir> <label> [--json]
    label_drain.py arm    <sdlc_dir> <label> [--apply]
    label_drain.py answer <sdlc_dir> <label> --answers <file.json> [--dry-run]

Draining a label is more than filtering. Members that carry the label but are not goals are invisible
to the picker; a member held by an open blocker outside the label stalls the run silently; a parked
member is a question nobody has seen. This module shows the whole set, with its transitive open
blockers, before anything is written. `plan` and `status` NEVER WRITE (`arm`, #1577, and `answer`,
#1580, are the verbs that can; `arm` only with --apply, `answer` unless --dry-run, see `answer()`): not to GitHub, the mirror, the ledger or any
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
import hashlib
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


_LIGHT = {}


def _light(name):
    """A small sibling module, loaded once (`_load` re-executes the module each call, which is costly for
    the helpers below that run per question)."""
    if name not in _LIGHT:
        _LIGHT[name] = _load(name)
    return _LIGHT[name]


USAGE = ("usage: label_drain.py plan|status <sdlc_dir> <label> [--json]\n"
         "       label_drain.py arm <sdlc_dir> <label> [--apply]\n"
         "       label_drain.py answer <sdlc_dir> <label> --answers <file.json> [--dry-run]\n"
         "  plan/status are read-only: they list the open issues carrying <label>, their open blockers,\n"
         "  and a verdict for each. arm is a dry run unless --apply: it arms what plan calls unarmed.\n"
         "  answer applies a person's answers to the questions plan printed ({\"<question id>\": \"<option>\"}):\n"
         "  effect first, then one decision record; --dry-run prints every write and makes none.\n"
         "  Nothing here ever removes the label; the one body edit is the spend-approval marker line.")

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


def _brief_row(row, sdlc_dir, config, n, source, scrub, briefed, not_briefed, unpark):
    """Read one parked issue's kind and question into `row`, within the MAX_PARKED_BRIEFS bound."""
    if briefed >= MAX_PARKED_BRIEFS:
        row["kind"], row["question"] = "not-read", ""
        not_briefed.append(n)
        return briefed, unpark
    unpark = unpark or _load("unpark")
    try:
        b = unpark.brief(sdlc_dir, config, n, source=source)
        row["kind"] = b.get("reason_class") if b.get("kind_declared") else "undeclared"
        row["question"] = scrub(str(b.get("park_reason") or ""))
    except Exception:  # noqa: BLE001 - say so rather than guess
        row["kind"], row["question"] = "unreadable", ""
    return briefed + 1, unpark


def plan(sdlc_dir, label, config=None, run=None, hide_answered=True):
    """-> the full read-only plan dict. A question with a recorded answer is dropped and `ready` follows
    (#1732); `answer` passes hide_answered=False so it can still validate a repeated answer. Raises `sources.LabelScopeError` or `ValueError` (too many
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
            row["labels"] = sorted(names)
            if verdict == "parked":
                briefed, unpark = _brief_row(row, sdlc_dir, config, n, source, scrub, briefed, not_briefed, unpark)
            outside.append(row)
            continue
        row["labels"] = sorted(names)
        if verdict == "parked":
            briefed, unpark = _brief_row(row, sdlc_dir, config, n, source, scrub, briefed, not_briefed, unpark)
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
    paid = {i["number"]: i.get("body") or "" for i in member_issues}
    qs = _annotate(sdlc_dir, config, label, questions(label, rows, outside, paid, config))
    if hide_answered and qs:
        done = _recorded(sdlc_dir, config, label)
        qs = [q for q in qs if not done.get(q["id"])]
    unread = [r["number"] for r in rows + outside if r.get("kind") in ("not-read", "unreadable")]
    return {"label": label, "repo": repo, "members": rows, "outside": outside,
            "questions": qs, "ready": not qs and not unread,
            "unresolved": [{"ref": t, "dependents": d} for t, d in sorted(unresolved_all.items())],
            "warnings": warnings, "stops": clo["stops"], "counts": dict(sorted(counts.items())),
            "pickable": sum(1 for r in rows if r["ready"]), "pulled": clo["pulled"],
            "drained": not rows, "calls": ro.summary()}


# --- label kinds and scope (#1581) ---------------------------------------------------------------

def label_kind(label, config=None):
    """The kind a run label belongs to: `label_drain.kinds` (exact name -> kind) when it names this label
    with a non-empty string, else the name up to its first `:` or `/`, else the whole name. A label with
    no usable name reads `other`. A wrong-typed map or value is ignored."""
    name = str(label or "").strip()
    try:
        kinds = (config or {}).get("label_drain", {}).get("kinds")
    except AttributeError:
        kinds = None
    if isinstance(kinds, dict):
        v = kinds.get(name)
        if isinstance(v, str) and v.strip():
            return v.strip()
    head = re.split(r"[:/]", name, maxsplit=1)[0].strip()
    return head or name or "other"


def _public(config):
    return _light("decision_store")._visibility(config) == "public"


def _hash(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:12]


def _scope(label, config):
    """The record scope of one run label; under public visibility a short hash, never the name."""
    return "label:" + (_hash(label) if _public(config) else label)


def _kind_area(label, config):
    """The area a label-level question is recorded and learned under: `label-kind:<kind>` (hashed when
    public); the old `repo` while the records part is closed, so closed gates change nothing."""
    if not _light("decision_store").enabled(config):
        return "repo"
    kind = label_kind(label, config)
    return "label-kind:" + (_hash(kind) if _public(config) else kind)


def _rec_qid(qid, label, config):
    """The question id as it is stored: the label-level id carries the label name, so public hashes it."""
    if _public(config) and qid == "label_set:" + label:
        return "label_set:" + _hash(label)
    return qid


def _history(sdlc_dir, config, label, q):
    """#1581: one line saying what was answered before on this label (or, failing that, on labels of its
    kind); '' when nothing is on record. Only with the records part open, and only for the label-set
    question: a member-level question never shows another member's last answer."""
    if q["kind"] != "label_set":
        return ""
    try:
        scope, recs = _scope(label, config), _light("decision_store").list_by(sdlc_dir, config)
        mine = [r for r in recs if r.get("scope") == scope and r.get("qkind") == q["kind"]]
        if mine:
            last = mine[-1]
            return "last time on this label: %s" % (last.get("answer") or last.get("choice") or "(none recorded)")
        if q["kind"] == "label_set":
            counts = collections.Counter(r.get("answer") or r.get("choice") for r in recs
                                         if r.get("qkind") == "label_set" and r.get("area") == q["area"]
                                         and (r.get("answer") or r.get("choice")))
            if counts:
                return "on labels of this kind: " + ", ".join("%s %d" % kv for kv in sorted(counts.items()))
    except Exception:                                   # noqa: BLE001 - a history line never breaks a plan
        pass
    return ""


def _recommend(sdlc_dir, config, q):
    """#1581: the approved rule for the arming question's kind, as `{rule_id, option}`, else None. Only the
    arming question is learnable; a spend question is never recommended. Never applied by itself."""
    try:
        rb = _light("rulebook")
        if q["kind"] != "label_set" or not rb.enabled(config) or not _light("decision_store").enabled(config):
            return None
        pre = rb.prefill(sdlc_dir, "label_set", q["area"], q["options"], config)
        return {"rule_id": pre["rule"], "option": pre["recommend"]} if pre else None
    except Exception:                                   # noqa: BLE001
        return None


def _annotate(sdlc_dir, config, label, qs):
    """Add `history` and `recommendation` to the questions. Closed gates add nothing (same dicts)."""
    on = _light("decision_store").enabled(config)
    for q in qs:
        if on:
            h = _history(sdlc_dir, config, label, q)
            if h:
                q["history"] = h
        rec = _recommend(sdlc_dir, config, q)
        if rec:
            q["recommendation"] = rec
            q["options"] = [rec["option"]] + [o for o in q["options"] if o != rec["option"]]
    return qs


def kind_summary(sdlc_dir, config):
    """-> [{"kind", "answers", "level", "covered"}] one per label-level area with records, for the doctor.
    [] when the records part is closed. Never raises."""
    try:
        dr = _light("decision_record")
        if not dr.enabled(config):
            return []
        recs = [r for r in _light("decision_store").list_by(sdlc_dir, config, qkind="label_set")
                if str(r.get("area") or "").startswith("label-kind:")]
        au, rb = _light("autonomy"), _light("rulebook")
        out = []
        for area in sorted({r["area"] for r in recs}):
            rules = rb.load(sdlc_dir, config) if rb.enabled(config) else []
            out.append({"kind": area[len("label-kind:"):],
                        "answers": sum(1 for r in recs if r["area"] == area and dr.is_park_answer(r)),
                        "level": au.level(sdlc_dir, "label_set", area, config),
                        "covered": rb.match("label_set", area, None, rules, config) is not None})
        return out
    except Exception:                                   # noqa: BLE001
        return []


# --- up-front questions (#1578) ------------------------------------------------------------------

#: Printed with the spend question. Pinned by a test: it is the whole of what spend approval promises.
SPEND_NOTE = ("Approval is single-use for each approved item, and Sigma does not meter external spend, "
              "so no dollar cap is enforced.")


def _dependents(rows, outside):
    """-> {n: sorted member numbers that wait on n, directly or through other issues}."""
    waits = {r["number"]: r["waiting_on"] for r in rows + outside}
    members = {r["number"] for r in rows}
    out = {}
    for n in waits:
        seen, todo = set(), [n]
        while todo:
            cur = todo.pop()
            for m, ws in waits.items():
                if cur in ws and m not in seen:
                    seen.add(m)
                    todo.append(m)
        out[n] = sorted(seen & members)
    return out


def _spend_text(body, hard_stop, config):
    """The first declared paid step the hard-stop net recognises as spend, else None."""
    for line in (body or "").splitlines():
        if not _PAID_RE.search(line):
            continue
        res = hard_stop.classify(line.split(":", 1)[1].strip(), {}, config)
        if res.cls and hard_stop.is_spend_like(res.cls):
            return res
    return None


def questions(label, rows, outside, paid_bodies, config):
    """-> the ordered list of questions a person answers once, before the drain starts. Pure: no reads.

    Each is {"id": "<kind>:<issue or label>", "kind", "area", "ask", "options", "unblocks": [n]}.
    Every text comes from the question bank (`unpark.QUESTIONS`), none is improvised. Order: parked
    members, members no gate can classify, pulled blockers, the arming route (once), spend. A question
    that cannot change what the drain does is not asked."""
    unpark, hard_stop = _load("unpark"), _load("hard_stop")
    area_of = unpark.decision_record.area_of
    deps = _dependents(rows, outside)
    out = []

    def add(qid, kind, labels, slot_text, options, unblocks, **extra):
        out.append({"id": qid, "kind": kind, "area": area_of(labels), "ask": slot_text,
                    "options": list(options), "unblocks": sorted(set(unblocks)), **extra})

    def own_question(r):
        kind, n = r.get("kind"), r["number"]
        if kind == "undeclared":
            guess = unpark.loop._suggest_kind(r.get("question") or "")
            opts = [guess] + [k for k in unpark.qkind.QKINDS if k not in (guess, "label_set")]
            add("kind:%d" % n, "kind", r.get("labels"), "Which kind of question is this?", opts,
                [n] + deps[n])
        elif kind in unpark.QUESTIONS:
            refs = [str(x) for x in r["waiting_on"]]
            q = unpark._questions_for(kind, r.get("question") or "", refs, {})[0]
            add("%s:%d" % (kind, n), kind, r.get("labels"), q["ask"], q["options"], [n] + deps[n])

    for r in sorted(rows, key=lambda x: x["number"]):
        if r["verdict"] == "parked":
            own_question(r)
    for r in sorted(rows, key=lambda x: x["number"]):
        if r["verdict"] == "held-by-gate":
            slot = unpark.QUESTIONS["scope_hold"][0]
            add("scope_hold:%d" % r["number"], "scope_hold", r.get("labels"), slot["ask"],
                slot["options"], [r["number"]] + deps[r["number"]])
    slots = {s["id"]: s for s in unpark.QUESTIONS["label_set"]}
    for r in sorted(outside, key=lambda x: x["number"]):
        own, n = r.get("own_verdict"), r["number"]
        if own == "parked":
            own_question(r)
        elif own == "assigned-elsewhere":
            add("dependency:%d" % n, "dependency", r.get("labels"), slots["taken"]["ask"],
                slots["taken"]["options"], deps[n])
        elif own == "upper-tier":
            add("dependency:%d" % n, "dependency", r.get("labels"), slots["upper"]["ask"],
                slots["upper"]["options"], deps[n])
    unarmed = sorted(r["number"] for r in rows if r["verdict"] == "unarmed")
    if unarmed:
        slot = unpark.route_slot("label_set")
        add("label_set:%s" % label, "label_set", [], slot["ask"], slot["options"], unarmed)
        out[-1]["area"] = _kind_area(label, config)
    for r in sorted(rows, key=lambda x: x["number"]):
        if r["verdict"] in ("upper-tier", "closed"):
            continue
        if _spend_text(paid_bodies.get(r["number"]), hard_stop, config) is None:
            continue
        if any(q["id"] == "irreversible:%d" % r["number"] for q in out):
            continue                      # the member's own parked question is already this one
        slot = unpark.route_slot("irreversible")
        add("irreversible:%d" % r["number"], "irreversible", r.get("labels"), slot["ask"],
            slot["options"], [r["number"]] + deps[r["number"]], note=SPEND_NOTE)
    return out


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
    out.append("questions: %d open, ready: %s" % (len(p["questions"]), "true" if p["ready"] else "false"))
    for i, q in enumerate(p["questions"], 1):
        out.append("  %d. [%s] %s" % (i, q["id"], q["ask"]))
        if q["options"]:
            out.append("     options: " + " / ".join(q["options"]))
        if q.get("recommendation"):
            out.append("     recommended: %s (rule %s, a suggestion; nothing is applied until you answer)" % (
                q["recommendation"]["option"], q["recommendation"]["rule_id"]))
        if q.get("history"):
            out.append("     history: " + q["history"])
        out.append("     unblocks: %s; recorded under area %s" % (
            ", ".join("#%d" % x for x in q["unblocks"]) or "nothing yet", q["area"]))
        if q.get("note"):
            out.append("     " + q["note"])
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


# --- arm (#1577) ---------------------------------------------------------------------------------

DEFAULT_PRIORITY = "P2"
_REFUSED = ("upper-tier", "parked", "assigned-elsewhere", "held-by-gate")


def arm_settings(config):
    """-> {"default_priority": str, "arm_pulled_blockers": bool} from the optional `label_drain` block.
    A wrong-typed value reads as its default, like every other block."""
    blk = (config or {}).get("label_drain")
    blk = blk if isinstance(blk, dict) else {}
    pr = blk.get("default_priority")
    if not isinstance(pr, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", pr.strip()):
        pr = DEFAULT_PRIORITY
    pulled = blk.get("arm_pulled_blockers")
    return {"default_priority": pr.strip(), "arm_pulled_blockers": pulled if isinstance(pulled, bool) else True}


def _prepare(source, triage, gh_api, label, cfg, todo, refused, notes):
    """Compute, per (issue, pulled_for) in `todo`, the pre-actions and arm actions -> {issue: (pre, got)}.
    Reads only. Appends to `refused` / `notes`. Shared by `arm` and `pull`."""
    me = source._assignee_login()
    prefix = source.priority_prefix
    label_ok = {}
    pre_by_issue = {}
    for n, pulled_for in sorted(todo):
        state = triage._fetch_issue_state(source, n)
        if state is None:
            refused.append({"issue": n, "verdict": "unreadable", "reason": "could not read its current state"})
            continue
        mine = next((a for a in state["assignees"] if me and (a or "").casefold() == me), None)
        if me and state["assignees"] and mine is None:
            refused.append({"issue": n, "verdict": "assigned-elsewhere", "reason":
                            "assigned to %s, not %s" % (", ".join(sorted(map(str, state["assignees"]))), me)})
            continue
        item = {"issue": n}
        if not any(l.startswith(prefix) for l in state["labels"]):
            name = prefix + cfg["default_priority"]
            if name not in label_ok:
                try:
                    label_ok[name] = bool(gh_api.label_exists(source._run, name, source.repo or None))
                except Exception:      # noqa: BLE001 - fail closed: do not mint a label
                    label_ok[name] = False
                if not label_ok[name]:
                    notes.append("label %s does not exist or could not be checked; arm never creates a "
                                 "label, so no priority is set (create it, then re-run)" % name)
            if label_ok[name]:
                item["priority"] = cfg["default_priority"]
        got = triage.compute_actions({"picked": [item]}, {str(n): state}, mine or me, source.goal_label,
                                     source.parked_label, blocked_label=source.goal_blocked_label,
                                     proposed_label=source.proposed_label)
        for a in got:                  # compute_actions spells the prefix `priority:` unconditionally
            if a["action"] == "add-label" and a["detail"].startswith("priority:") and prefix != "priority:":
                a["detail"] = prefix + a["detail"][len("priority:"):]
        pre = []
        if pulled_for is not None:
            pre = [{"action": "add-label", "issue": str(n), "detail": label, "result": None, "error": None},
                   {"action": "comment", "issue": str(n), "result": None, "error": None,
                    "detail": "Pulled into %s as a prerequisite of #%d." % (label, pulled_for)}]
        pre_by_issue[n] = (pre, got)
    return pre_by_issue


def _apply(source, triage, pre_by_issue, apply):
    """Run (or, when `apply` is false, only describe) what `_prepare` computed -> the action list."""
    actions = []
    for n in sorted(pre_by_issue):
        pre, got = pre_by_issue[n]
        done_pre = []
        for a in pre:                  # label first, then its comment; stop at the first failure so a
            r = triage.apply_actions(source, [a], apply)[0]   # retry never posts a second comment
            done_pre.append(r)
            if r["result"] == "failed":
                break
        actions += done_pre
        if not any(a["result"] == "failed" for a in done_pre):
            actions += triage.apply_actions(source, got, apply)
    return actions


def arm(sdlc_dir, label, apply=False, config=None, run=None, only=None):
    """Arm what `plan()` calls unarmed, reusing `triage.compute_actions` / `triage.apply_actions`.

    -> {"label", "apply", "actions": [triage action dicts], "refused": [{"issue", "verdict",
    "reason"}], "notes": [str]}. Dry-run (the default) hands the source a read-only runner, so a write
    call is impossible rather than merely skipped. Never edits a body, creates or removes a label, or
    deletes. `only` (a set of issue numbers, `answer` #1580) restricts the arming to those issues.
    Raises what `plan()` raises."""
    sources, triage = _load("sources"), _load("triage")
    gh_api = _load("gh_api")
    config = config if config is not None else _load("state").load_config(sdlc_dir)
    cfg = arm_settings(config)
    base = run or sources._run_gh
    p = plan(sdlc_dir, label, config=config, run=base)
    source = sources.GitHubSource(config, run=base if apply else _ReadOnly(base))
    dependents = collections.defaultdict(list)
    for r in p["members"] + p["outside"]:
        for b in r["waiting_on"]:
            dependents[b].append(r["number"])
    todo, refused, notes = [], [], []
    for r in p["members"] + p["outside"]:
        outside = r["verdict"] == "outside"
        v = r["own_verdict"] if outside else r["verdict"]
        if v in _REFUSED:
            refused.append({"issue": r["number"], "verdict": v, "reason": r["detail"] or v})
        elif v == "unarmed":
            if not outside:
                todo.append((r["number"], None))
            elif cfg["arm_pulled_blockers"]:
                todo.append((r["number"], min(dependents[r["number"]] or [0])))
            else:
                notes.append("#%d is an open blocker outside the label; arm_pulled_blockers is off, left alone"
                             % r["number"])
    if only is not None:
        todo = [t for t in todo if t[0] in only]
    pre_by_issue = _prepare(source, triage, gh_api, label, cfg, todo, refused, notes)
    actions = _apply(source, triage, pre_by_issue, apply)
    return {"label": label, "apply": bool(apply), "actions": actions,
            "refused": sorted(refused, key=lambda x: x["issue"]), "notes": notes}


def pull(sdlc_dir, label, ref, for_goal, config=None, run=None):
    """Pull ONE open prerequisite that sits outside the label into it, for a run already in flight (#1579).

    -> {"ref", "result": "pulled"|"already"|"closed"|"refused"|"unreadable", "reason": str,
    "actions": [triage action dicts]}. Reuses `arm`'s machinery (`_prepare`/`_apply`), so an unarmed
    blocker is armed exactly as `arm` arms it and an armed one only gets the label plus the fixed comment.
    Parked, assigned elsewhere, upper-tier, held by a gate, in another repository, or `arm_pulled_blockers`
    off: nothing is written and the reason is returned. Writes for real; never raises on a bad ref."""
    sources, triage, gh_api = _load("sources"), _load("triage"), _load("gh_api")
    config = config if config is not None else _load("state").load_config(sdlc_dir)
    cfg = arm_settings(config)
    base = run or sources._run_gh
    out = lambda res, why="", acts=(): {"ref": str(ref), "result": res, "reason": why, "actions": list(acts)}
    text = str(ref).strip().lstrip("#")
    if not text.isdigit():
        return out("refused", "in another repository (%s); Sigma stays out of it" % ref)
    n = int(text)
    source = sources.GitHubSource(config, run=base)
    try:
        issue = gh_api.view_issue(base, n, source.repo or None, comments="none")
        verdict, detail = _Judge(sdlc_dir, config, source).verdict(issue)
    except Exception as exc:       # noqa: BLE001 - a pull never breaks the pick
        return out("unreadable", "could not read #%d (%s)" % (n, exc))
    if verdict == "closed":
        return out("closed")
    if label in _names(issue):
        return out("already")
    if verdict in _REFUSED:
        return out("refused", "%s: %s" % (verdict, detail or verdict))
    if verdict == "unarmed" and not cfg["arm_pulled_blockers"]:
        return out("refused", "unarmed and arm_pulled_blockers is off")
    refused, notes = [], []
    if verdict == "unarmed":
        prepared = _prepare(source, triage, gh_api, label, cfg, [(n, int(for_goal))], refused, notes)
        if refused:
            return out("refused", "%s: %s" % (refused[0]["verdict"], refused[0]["reason"]))
        pre, got = prepared[n]
        prepared = {n: (pre, got)}
    else:
        pre = [{"action": "add-label", "issue": str(n), "detail": label, "result": None, "error": None},
               {"action": "comment", "issue": str(n), "result": None, "error": None,
                "detail": "Pulled into %s as a prerequisite of #%d." % (label, int(for_goal))}]
        prepared = {n: (pre, [])}
    acts = _apply(source, triage, prepared, True)
    failed = [a for a in acts if a["result"] == "failed"]
    if failed:
        return out("refused", "a write failed: %s" % (failed[0].get("error") or failed[0]["action"]), acts)
    return out("pulled", "", acts)


def render_arm(res):
    out = ["arm %s (%s): %d action(s), %d refused" % (
        res["label"], "APPLIED" if res["apply"] else "DRY-RUN", len(res["actions"]), len(res["refused"]))]
    verbs = _load("triage")._ACTION_VERBS
    for a in res["actions"]:
        line = "  [%s] #%s: %s %s" % (a["result"], a["issue"], verbs.get(a["action"], a["action"]), a["detail"])
        if a["result"] == "failed" and a.get("error"):
            line += " -- %s" % a["error"]
        out.append(line)
    for r in res["refused"]:
        out.append("  refused #%d (%s): %s" % (r["issue"], r["verdict"], r["reason"]))
    for n in res["notes"]:
        out.append("  note: " + n)
    if not res["actions"]:
        out.append("  no actions needed")
    if not res["apply"]:
        out.append("dry run: pass --apply to execute")
    return "\n".join(out) + "\n"


# --- answer (#1580) ------------------------------------------------------------------------------

def _token(label):
    """The spend marker token derived from a run label: letters, digits, dot, dash, underscore; starts
    with a letter or digit; at most 64."""
    tok = re.sub(r"[^A-Za-z0-9._-]", "-", str(label)).lstrip("._-")[:64]
    return tok or "approved"


def _bank_match(options, text):
    want = _load("unpark").normalise_answer(text)
    for o in options:
        if o.lower() == want:
            return o
    return None


def _lookup(qid, p, label, config=None):
    """-> the question `qid` names, or None. A question `plan` no longer asks (its effect already
    stands, or it was answered before) is still accepted when its id is well formed for this label and
    its options come from the bank, so a repeat or a crash recovery is not refused."""
    unpark = _load("unpark")
    for q in p["questions"]:
        if q["id"] == qid:
            return dict(q, target=qid.split(":", 1)[1])
    kind, _, target = str(qid).partition(":")
    rows = {r["number"]: r for r in p["members"] + p["outside"]}
    if kind == "label_set":
        if target != label:
            return None
        slot = unpark.route_slot("label_set")
        return {"id": qid, "kind": kind, "options": list(slot["options"]), "unblocks": [], "target": target,
                "area": _kind_area(label, config)}
    if not target.isdigit() or int(target) not in rows:
        return None
    r, n = rows[int(target)], int(target)
    area = unpark.decision_record.area_of(r.get("labels"))
    if kind == "kind":
        opts = [k for k in _load("qkind").QKINDS if k != "label_set"]
    elif kind == "dependency" and r["verdict"] == "outside" and r.get("own_verdict") in (
            "assigned-elsewhere", "upper-tier"):
        slot_id = "taken" if r["own_verdict"] == "assigned-elsewhere" else "upper"
        opts = list(next(s for s in unpark.QUESTIONS["label_set"] if s["id"] == slot_id)["options"])
    elif kind in unpark.QUESTIONS:
        opts = unpark.valid_options(kind)
    else:
        return None
    return {"id": qid, "kind": kind, "options": opts, "unblocks": [n], "target": target, "area": area}


def _validate_answers(answers, p, label, config=None):
    """-> (errors, {qid: (question, choice)}). The whole file is judged before any write."""
    errors, good = [], {}
    if not isinstance(answers, dict) or not answers:
        return ["the answers file must be a JSON object mapping question ids to answers"], good
    for qid, raw in answers.items():
        q = _lookup(qid, p, label, config)
        if q is None:
            errors.append("unknown question id %r (run `plan` to list the ids)" % (qid,))
        elif not isinstance(raw, str) or not raw.strip():
            errors.append("%s: the answer must be non-empty text" % qid)
        elif q["options"]:
            hit = _bank_match(q["options"], raw)
            if hit is None:
                errors.append("%s: %r is not one of: %s" % (qid, raw, " / ".join(q["options"])))
            else:
                good[qid] = (q, hit)
        else:
            good[qid] = (q, " ".join(raw.split()))      # no option list: the bank allows free text
    return errors, good


def _recorded(sdlc_dir, config, label):
    """-> {question id: {choice}} already recorded under this label's scope (empty when records are off)."""
    out = {}
    try:
        for r in _load("decision_store").list_by(sdlc_dir, config):
            if r.get("scope") == _scope(label, config) and r.get("qid"):
                qid = "label_set:" + label if r["qid"] == _rec_qid("label_set:" + label, label, config) else r["qid"]
                out.setdefault(qid, set()).update(x for x in (r.get("choice"), r.get("answer")) if x)
    except Exception:                                   # noqa: BLE001 - an unreadable store only loses dedupe
        pass
    return out


def _spend_effect(n, q_ids, p, source, config, token, dry_run):
    """The marker write for one `go ahead`. -> (ok, [lines]). Only an approver-authored paid member
    gets the marker; every other case is named and left as it is."""
    sa = _load("spend_approval")
    if n not in set(p["warnings"]["paid_step"]):
        return True, ["#%d: no declared paid step, no marker written" % n]
    approvers, enabled = sa._approvers(config)
    try:
        issue = source.fetch_issue_for_approval(n)
    except Exception as exc:                            # noqa: BLE001 - unreadable == not approved
        return False, ["#%d: could not read the issue: %s" % (n, _load("sources")._fx(exc))]
    author = (issue.get("author") or "").strip().lower()
    trusted = _load("sources").TRUSTED_ASSOCIATIONS
    if not (enabled and approvers and author in approvers and issue.get("author_association") in trusted):
        return True, ["#%d: not marked, its author is not a trusted spend approver "
                      "(spend_approval.approvers); a person must write the marker" % n]
    status, found, _line = sa.parse_marker(issue["body"])
    if status == "ok" and found == token:
        return True, ["#%d: marker already present" % n]
    if status != "none":
        return True, ["#%d: not marked, its first line already holds a different spend marker" % n]
    if dry_run:
        return True, ["would write marker %s%s on #%d" % (sa.MARKER, token, n)]
    new_body = "%s%s\n\n%s" % (sa.MARKER, token, issue["body"])
    try:
        source._issue_edit_body(n, new_body)
    except Exception as exc:                            # noqa: BLE001
        return False, ["#%d: the marker write failed: %s" % (n, _load("sources")._fx(exc))]
    return True, ["wrote marker %s%s on #%d" % (sa.MARKER, token, n)]


def answer(sdlc_dir, label, answers, dry_run=False, config=None, run=None):
    """Apply a person's answers to the questions `plan` asked, one at a time.

    ORDER inside one answer (the rulebook's rule): the effect first, then the decision record and its
    one comment; an effect that fails writes no record. The effects are the existing verbs, never
    copies: `arm` for the arming route, `unpark.resolve` / `unpark.declare` for a parked member, the
    `spend_approval` marker line for `go ahead`. Nothing is defaulted: a question left out stays open.
    -> {"label", "dry_run", "errors": [str], "results": [{"id", "choice", "outcome", "lines"}],
        "open": [question], "ready": bool}. Raises what `plan()` raises."""
    sources, unpark = _load("sources"), _load("unpark")
    dr = unpark.decision_record
    config = config if config is not None else _load("state").load_config(sdlc_dir)
    base = run or sources._run_gh
    p = plan(sdlc_dir, label, config=config, run=base, hide_answered=False)
    errors, good = _validate_answers(answers, p, label, config)
    out = {"label": label, "dry_run": bool(dry_run), "errors": errors, "results": [], "open": [], "ready": False}
    if errors:
        return out                                      # refused whole, nothing written
    source = sources.GitHubSource(config, run=base)
    rows = {r["number"]: r for r in p["members"] + p["outside"]}
    order = [q["id"] for q in p["questions"]]
    done_before = _recorded(sdlc_dir, config, label)
    answered = set()
    scope = _scope(label, config)

    def record(q, kind, choice, gesture="ack"):
        """The one record + comment, through the shared writer. -> list of lines."""
        ids = q.get("unblocks") or []
        goal = int(q["target"]) if str(q["target"]).isdigit() else (min(ids) if ids else None)
        if kind == "label_set":                         # #1725: the lowest open label member carries it, never an outside blocker (a recovered question also has no members)
            nums = [r["number"] for r in p["members"] if str(r.get("number", "")).isdigit()]
            mine = [n for n in ids if n in nums]
            goal = min(mine) if mine else (min(nums) if nums else None)
        if not dr.enabled(config):
            return []
        if dry_run:
            return ["would record the decision (%s: %s) and post one comment on #%s" % (q["id"], choice, goal)]
        if kind == "label_set":                         # #1581: a counted answer (an `ack` never counts toward a level)
            gesture = "unpark" if str(choice).startswith("arm") else "keep-parked"
        rec = dr.build(gesture, goal, kind, choice, "answer to %s" % q["id"], dr.how_for(gesture),
                       area=q.get("area"), answer=choice)
        rec["scope"], rec["qid"] = scope, _rec_qid(q["id"], label, config)
        shown = q.get("recommendation")
        if kind == "label_set" and shown:               # #1581: the person saw a rule's suggestion
            rec["rule_id"], rec["shown"] = str(shown["rule_id"]), True
            rec["accepted"] = dr.key_of(rec) == shown["option"]
        res = dr.write_all(sdlc_dir, rec, config, source,
                           text="Run label %s: answered %r." % ("(name withheld)" if _public(config) else label,
                                                                choice),
                           poster=None if goal is not None else (lambda number, body: None))   # #1751: no open member, no comment target
        return ["recorded decision %s" % (res["id"] or "(not stored)")] + (
            [res["error"]] if res["error"] else []) + (
            ["no open member left: recorded without a comment"] if goal is None else [])

    for qid in sorted(good, key=lambda x: (order.index(x) if x in order else len(order), x)):
        q, choice = good[qid]
        kind, n = q["kind"], (int(q["target"]) if q["target"].isdigit() else None)
        row = rows.get(n) if n is not None else None
        res = {"id": qid, "choice": choice, "outcome": "applied", "lines": []}
        out["results"].append(res)
        if choice in done_before.get(qid, ()):
            res["outcome"], res["lines"] = "already-recorded", ["already answered, nothing written"]
            answered.add(qid)
            continue
        ok, lines, recorded_by_effect = True, [], False
        if kind == "label_set":
            if choice == "leave them":
                lines = ["left the set unarmed"]
            else:
                only = None
                if choice.startswith("arm only"):
                    only = {r["number"] for r in p["members"]
                            if r["verdict"] == "unarmed" and not r["waiting_on"] and not r["unresolved"]}
                armed = arm(sdlc_dir, label, apply=not dry_run, config=config, run=base, only=only)
                lines = [l.strip() for l in render_arm(armed).splitlines()[1:]]
                ok = not any(a["result"] == "failed" for a in armed["actions"])
        elif kind == "irreversible":
            if choice == "go ahead":
                ok, lines = _spend_effect(n, q.get("unblocks"), p, source, config, _token(label), dry_run)
            else:
                lines = ["no marker written"]
        elif kind == "kind":
            if row is not None and row.get("kind") == "undeclared":
                if dry_run:
                    lines = ["would declare kind %s on #%d" % (choice, n)]
                else:
                    ok, detail = unpark.declare(sdlc_dir, config, n, choice, source=source)
                    lines = [detail]
            else:
                lines = ["#%s already has a declared kind, nothing to declare" % n]
        elif kind in unpark.QUESTIONS and row is not None and row["verdict"] == "parked" \
                and unpark.option_gestures(kind).get(choice) in ("unpark", "keep-parked"):
            slot = unpark.route_slot(kind) or unpark.CLOSING_QUESTION
            r = unpark.resolve(sdlc_dir, config, n, {slot["id"]: choice}, unpark.option_gestures(kind)[choice],
                               source=source, apply=not dry_run, extra={"scope": scope, "qid": qid})
            ok, recorded_by_effect = r["outcome"] != "failed", True
            lines = ["[%s] %s" % (r["outcome"], r["detail"])]
        else:
            lines = ["answer recorded only: Sigma does not reassign, close or re-register for you"
                     if kind in ("dependency", "scope_hold") else "answer recorded; the issue is left as it is"]
        if not ok:
            res["outcome"] = "failed"
            res["lines"] = lines + ["no decision record written"]
            continue
        if not recorded_by_effect:
            lines += record(q, kind if kind != "kind" else choice, choice)
        res["lines"] = lines
        answered.add(qid)
    after = p if dry_run else plan(sdlc_dir, label, config=config, run=base, hide_answered=False)
    skip = answered | {i for i, cs in _recorded(sdlc_dir, config, label).items() if i in order and cs}
    out["open"] = [q for q in after["questions"] if q["id"] not in skip]
    unread = [r for r in after["members"] + after["outside"] if r.get("kind") in ("not-read", "unreadable")]
    out["ready"] = not out["open"] and not unread
    return out


def render_answer(res):
    out = ["answer %s (%s): %d answer(s)" % (res["label"], "DRY-RUN" if res["dry_run"] else "APPLIED",
                                           len(res["results"]))]
    for r in res["results"]:
        out.append("  [%s] %s -> %s" % (r["outcome"], r["id"], r["choice"]))
        out += ["      " + l for l in r["lines"]]
    out.append("questions: %d still open, ready: %s" % (len(res["open"]), "true" if res["ready"] else "false"))
    for q in res["open"]:
        out.append("  - [%s] %s" % (q["id"], q["ask"]))
        if q["options"]:
            out.append("     options: " + " / ".join(q["options"]))
    if res["dry_run"]:
        out.append("dry run: nothing was written")
    return "\n".join(out) + "\n"


def _main_answer(args, run):
    rest = args[1:]
    dry = "--dry-run" in rest
    rest = [a for a in rest if a != "--dry-run"]
    path = None
    if "--answers" in rest:
        i = rest.index("--answers")
        path = rest[i + 1] if i + 1 < len(rest) else None
        rest = rest[:i] + rest[i + 2:]
    if len(rest) != 2 or not path:
        print(USAGE, file=sys.stderr)
        return 2
    sdlc_dir, label = rest
    try:
        payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception as exc:                            # noqa: BLE001
        print("label_drain.py answer: could not read --answers %r: %s" % (path, exc), file=sys.stderr)
        return 2
    sources = _load("sources")
    try:
        res = answer(sdlc_dir, label, payload, dry_run=dry, run=run)
    except (sources.LabelScopeError, ValueError) as exc:
        print("label_drain.py: %s" % exc, file=sys.stderr)
        return 2
    if res["errors"]:
        for e in res["errors"]:
            print("label_drain.py answer: " + e, file=sys.stderr)
        print("nothing was written", file=sys.stderr)
        return 2
    print(render_answer(res), end="")
    return 1 if any(r["outcome"] == "failed" for r in res["results"]) else 0


def main(argv, run=None):
    args = list(argv[1:])
    if args in ([], ["-h"], ["--help"]):
        print(USAGE)
        return 0 if args else 2
    if args[:1] == ["answer"]:
        return _main_answer(args, run)
    as_json = "--json" in args
    do_apply = "--apply" in args
    args = [a for a in args if a not in ("--json", "--apply")]
    if len(args) != 3 or args[0] not in ("plan", "status", "arm") or (do_apply and args[0] != "arm") \
            or (as_json and args[0] == "arm"):
        print(USAGE, file=sys.stderr)
        return 2
    verb, sdlc_dir, label = args
    sources = _load("sources")
    if verb == "arm":
        try:
            res = arm(sdlc_dir, label, apply=do_apply, run=run)
        except (sources.LabelScopeError, ValueError) as exc:
            print("label_drain.py: %s" % exc, file=sys.stderr)
            return 2
        print(render_arm(res), end="")
        return 1 if any(a["result"] == "failed" for a in res["actions"]) else 0
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
