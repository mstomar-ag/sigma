"""Shared fake for `sources._swap_labels`' GraphQL transport (#1391 step 2, extended #1392).

Lifecycle transitions do not issue `gh issue edit --add-label/--remove-label`; they issue ONE
`gh api graphql` document with two aliased root mutations. Several test modules drive a REAL
`GitHubSource` through an injectable runner, so each of them needs to answer those three request
shapes. This is that answer, in ONE place -- it models GITHUB'S BEHAVIOUR, not the transport, so a
swap is translated into the equivalent add/remove records and every pre-existing assertion about
WHICH labels a transition writes stays valid.

Imported as a plain sibling module (`import gqlfake`) -- pytest puts each test file's own directory
on `sys.path`, the same way these tests already reach `skills/` by explicit path.
"""
import json, re

_GQL_LABEL_IDS = {}          # name -> synthetic node id, stable within a run
#: Label names this fake should answer as ABSENT from the repo — a test opts a name in to exercise
#: the unknown-label path. Empty by default: every name resolves.
_UNKNOWN_LABELS = set()

_GQL_LAST_ISSUE = {"n": "0"}  # the mutation carries only a node id, so remember the
                              # number from the `issue(number:)` lookup that precedes it


def swap(args, labels=None, calls=None, repo_args=("--repo", "o/r")):
    """Handle sources._swap_labels' three GraphQL shapes. Returns a canned response string, or None
    if `args` is not a graphql call (so the caller falls through to its own handling).

    `labels`: a mutable set the mutation is applied to (models the real label set).
    `calls`: if given, one synthetic `issue edit ... --add-label/--remove-label` entry is appended
    per label changed, so call-log assertions written before this change keep working."""
    if len(args) >= 2 and args[0] == "repo" and args[1] == "view":
        # `_owner_name` falls back to this when discovery.github.repo is unset (the shipped default,
        # and what most of these fixtures use).
        return json.dumps({"owner": {"login": "o"}, "name": "r"})
    m = re.fullmatch(r"repos/[^/]+/[^/]+/labels/([^/]+)", str(args[1])) if len(args) >= 2 and args[0] == "api" else None
    if m:
        # #1939: `label_exists` is one REST GET now. Same model as the GraphQL lookup: every name
        # resolves unless a test opted it into _UNKNOWN_LABELS, which answers a 404 like gh does.
        from urllib.parse import unquote
        name = unquote(m.group(1))
        if name in _UNKNOWN_LABELS:
            exc = RuntimeError("gh: Not Found (HTTP 404)")
            exc.hint = "gh: Not Found (HTTP 404)"
            raise exc
        return json.dumps({"name": name})
    if not (len(args) >= 2 and args[0] == "api" and args[1] == "graphql"):
        return None
    doc = next((a[len("query="):] for a in args if str(a).startswith("query=")), "")
    if "label(name:" in doc:
        # #1393 review bug_001: `_label_node_ids` fetches BY NAME now (one aliased field per label)
        # instead of an unpaginated `labels(first: 100)` page -- on a repo with >100 labels the
        # sdlc:* ones could fall outside that window, and every lifecycle transition then silently
        # stopped writing. This fake answers the same shape: a null field for a label the repo does
        # not have, which is exactly the "unknown label" signal `_swap_labels` raises on.
        fields = {}
        for alias, name in re.findall(r'(a\d+): label\(name: "((?:[^"\\]|\\.)*)"\)', doc):
            name = name.replace('\\"', '"').replace("\\\\", "\\")
            if name in _UNKNOWN_LABELS:
                fields[alias] = None
                continue
            _GQL_LABEL_IDS.setdefault(name, "L_%d" % (abs(hash(name)) % 10**8))
            fields[alias] = {"id": _GQL_LABEL_IDS[name], "name": name}
        return json.dumps({"data": {"repository": fields}})
    if "labels(first" in doc:
        names = sorted(labels) if labels is not None else []
        for n in ("sdlc:goal", "sdlc:in-progress", "sdlc:parked", "sdlc:blocked",
                  "sdlc:needs-confirmation", "sdlc:blocking", "sdlc:dependency"):
            if n not in names:
                names.append(n)
        for n in names:
            _GQL_LABEL_IDS.setdefault(n, "L_%d" % (abs(hash(n)) % 10**8))
        return json.dumps({"data": {"repository": {"labels": {"nodes": [
            {"id": _GQL_LABEL_IDS[n], "name": n} for n in names]}}}})
    if "issue(number" in doc:
        m = re.search(r"issue\(number: (\d+)\)", doc)
        if m:
            _GQL_LAST_ISSUE["n"] = m.group(1)
        return json.dumps({"data": {"repository": {"issue": {"id": "I_node"}}}})
    if doc.startswith("mutation"):
        num = _GQL_LAST_ISSUE["n"]
        by_id = {v: k for k, v in _GQL_LABEL_IDS.items()}
        for alias, verb in (("a: addLabelsToLabelable", "--add-label"),
                            ("r: removeLabelsFromLabelable", "--remove-label")):
            if alias not in doc:
                continue
            seg = doc[doc.index(alias):]
            ids = re.findall(r'"(L_\d+)"', seg[:seg.index("}")] if "}" in seg else seg)
            for lid in ids:
                name = by_id.get(lid)
                if name is None:
                    continue
                if verb == "--add-label":
                    if labels is not None:
                        labels.add(name)
                else:
                    if labels is not None:
                        labels.discard(name)
                if calls is not None:
                    calls.append(["issue", "edit", num or "0", *repo_args, verb, name])
        return json.dumps({"data": {"a": {"clientMutationId": None}}})
    return "{}"


def labels_in(args):
    """The label NAMES a `_swap_labels` mutation is about, decoded from its node ids — or `[]` when
    `args` is not a mutation call.

    Exists so a test can express "make THIS label's write fail" in the vocabulary it cares about
    (`sdlc:blocking`) instead of in transport details. Before the swap, a fixture could simply match
    `--add-label sdlc:blocking` in the argv; the mutation carries opaque node ids instead, and a
    test forced to match those would be asserting on the fake rather than on behaviour.
    """
    if not (len(args) >= 2 and args[0] == "api" and args[1] == "graphql"):
        return []
    doc = next((a[len("query="):] for a in args if str(a).startswith("query=")), "")
    if not doc.startswith("mutation"):
        return []
    by_id = {v: k for k, v in _GQL_LABEL_IDS.items()}
    return [by_id[i] for i in re.findall(r'"(L_\d+)"', doc) if i in by_id]


# ---------------------------------------------------------------- #895: REST-first issue reads
# `gh_api.read_issue` (sources.py's issue reads since #895) asks REST first:
# `api repos/<o>/<r>/issues/<n> --method GET`, then `.../comments` pages. A fake written for the old
# `gh issue view --json <fields>` argv answers it here from the SAME gh-shape dict it already builds,
# converted to REST shape (state lower, author -> user, comments -> count + pages). The fake's own
# `issue view` branch stays: it is now the one-call fallback path.

ALL_FIELDS = ("state", "stateReason", "author", "closedAt", "body", "labels", "assignees", "comments")
_REST_ISSUE_RE = re.compile(r"repos/[^/]+/[^/]+/issues/(\d+)(/comments)?")


def rest_issue_target(args):
    """-> (number str, is_comments_page) for gh_api.read_issue's REST GET argv, else None."""
    args = [str(a) for a in args]
    if len(args) < 4 or args[0] != "api" or args[2:4] != ["--method", "GET"]:
        return None
    m = _REST_ISSUE_RE.fullmatch(args[1])
    return (m.group(1), bool(m.group(2))) if m else None


def is_issue_read(args):
    """One issue READ on either path: `issue view` (fallback) or the REST issue GET (not a page)."""
    t = rest_issue_target(args)
    return (len(args) >= 2 and list(args[:2]) == ["issue", "view"]) or (t is not None and not t[1])


def _rest_user(author):
    login = (author or {}).get("login") if isinstance(author, dict) else None
    if login is None:
        return None
    if login.startswith("app/"):
        return {"login": login[4:] + "[bot]", "type": "Bot"}
    return {"login": login, "type": "User"}


def _rest_comment(c, i):
    out = {"id": None, "node_id": c.get("id"), "user": _rest_user(c.get("author")), "body": c.get("body"),
           "created_at": c.get("createdAt")}
    if "authorAssociation" in c:
        out["author_association"] = c["authorAssociation"]
    return out


def rest_issue(args, view):
    """Answer a REST issue GET / comments page from `view(number, fields)` -> the gh-shape dict (or
    JSON string) the fake returns for `issue view`. None when `args` is not such a read. A gh dict
    with no `body` key answers `body: null` (an empty body), never an absent key."""
    t = rest_issue_target(args)
    if t is None:
        return None
    full = view(t[0], list(ALL_FIELDS))
    full = json.loads(full) if isinstance(full, str) else (full or {})
    comments = [c if isinstance(c, dict) else {} for c in (full.get("comments") or [])]
    if t[1]:
        page = int(next(a for a in args if str(a).startswith("page="))[5:])
        return json.dumps([_rest_comment(c, i) for i, c in enumerate(comments)][(page - 1) * 100:page * 100])
    state = str(full.get("state") or "OPEN").upper()
    out = {
        "number": int(t[0]), "title": full.get("title"),
        "state": "closed" if state == "MERGED" else state.lower(),
        "state_reason": (full.get("stateReason") or "").lower() or None,
        "user": _rest_user(full.get("author")), "body": full.get("body"),
        "labels": full.get("labels") or [], "assignees": full.get("assignees") or [],
        "closed_at": full.get("closedAt"), "comments": len(comments)}
    if state == "MERGED":      # REST says a merged PR is `closed` + pull_request.merged_at (#895 2b)
        out["pull_request"] = {"merged_at": "2026-10-09T00:00:00Z"}
    return json.dumps(out)


# ---------------------------------------------------------------- #895 slice 2c: REST-first issue LISTS
# `gh_api.list_issues_gh` asks REST first: `api repos/<o>/<r>/issues --method GET -f labels=.. -f
# state=.. -f sort=.. -f direction=.. -f per_page=.. -f page=..`. A fake written for the old
# `gh issue list` argv answers it here from REST-shaped items. A mis-keyed fake would otherwise pass
# vacuously through the sites' fail-open arms ("" or a raise both read as failed/empty), so every site
# test ALSO asserts the REST list call was actually recorded (`rest_list_calls(run)`), which goes red
# when the fake stops matching the argv (control C9).

_REST_LIST_RE = re.compile(r"repos/[^/]+/[^/]+/issues")


def rest_list_params(args):
    """-> dict of the `-f k=v` fields for a REST issues-LIST GET argv, else None."""
    args = [str(a) for a in args]
    if len(args) < 4 or args[0] != "api" or args[2:4] != ["--method", "GET"] or not _REST_LIST_RE.fullmatch(args[1]):
        return None
    params = {}
    for i, a in enumerate(args):
        if a == "-f" and i + 1 < len(args) and "=" in args[i + 1]:
            k, v = args[i + 1].split("=", 1)
            params[k] = v
    return params


def _item_labels(it):
    return {(l.get("name") if isinstance(l, dict) else l) for l in (it.get("labels") or [])}


def rest_list(args, items):
    """Answer a REST issues-list GET from `items` (REST-shaped dicts, NEWEST FIRST). Honours
    `labels=` (AND), `state=`, `direction=` (asc reverses), `per_page=`/`page=`. `items` may be a
    callable `(params) -> list` for a test that wants to decide itself. Returns a JSON string, or
    None when `args` is not such a read (the caller then falls through to its own handling)."""
    params = rest_list_params(args)
    if params is None:
        return None
    rows = items(params) if callable(items) else list(items)
    if not callable(items):
        want = {l for l in params.get("labels", "").split(",") if l}
        state = params.get("state", "open")
        rows = [r for r in rows if want <= _item_labels(r)
                and (state == "all" or str(r.get("state", "open")).lower() == state)]
        if params.get("direction") == "asc":
            rows = rows[::-1]
    per = int(params.get("per_page", 30))
    page = int(params.get("page", 1))
    return json.dumps(rows[(page - 1) * per:page * per])


def rest_item(number, labels=(), state="open", body="", title=None, **kw):
    """One REST-shaped issue for `rest_list` (labels as dicts with `name`, state lowercase)."""
    d = {"number": number, "title": title if title is not None else "T%d" % number, "state": state,
         "labels": [{"name": l} for l in labels], "assignees": [], "body": body, "closed_at": None,
         "user": {"login": "alice", "type": "User"}}
    d.update(kw)
    return d


# ---------------------------------------------------------------- #895 slice 3a: REST-first issue WRITES
# `gh_api.comment_issue/create_issue/add_labels/remove_label/close_issue/edit_issue/add_assignees` (and
# `label_exists`, `GET user`) ask REST first. A fake written for the old `gh issue comment|edit|create|
# close` argv answers it here, in the `swap` style: it RE-RECORDS one legacy-shaped call per change into
# `calls` (so assertions about WHICH write happened stay stable) and returns REST-shaped JSON. A fake
# that stops matching the REST argv falls through (returns None), so a site whose REST path is broken
# goes red instead of passing vacuously (control C23 breaks one site's path and the suite must notice).

_REST_WRITE_RE = re.compile(r"repos/[^/]+/[^/]+/(?:issues(?:/(\d+)(?:/(comments|labels|assignees)(?:/(.+))?)?)?"
                            r"|labels/(.+))")
_CREATED = {"n": 100}


def _fields(args):
    out = {}
    args = [str(a) for a in args]
    for i, a in enumerate(args):
        if a == "-f" and i + 1 < len(args) and "=" in args[i + 1]:
            k, v = args[i + 1].split("=", 1)
            out.setdefault(k, []).append(v)
    return out


def rest_write_target(args):
    """-> (method, kind, number|None, extra) for a REST issue-write argv, else None. `kind` is one of
    comment | create | patch | add_labels | remove_label | add_assignees | label_get | user."""
    args = [str(a) for a in args]
    if len(args) >= 4 and args[0] == "api" and args[1] == "user" and args[2:4] == ["--method", "GET"]:
        return ("GET", "user", None, None)
    if len(args) < 4 or args[0] != "api" or args[2] != "--method":
        return None
    method = args[3]
    m = _REST_WRITE_RE.fullmatch(args[1])
    if not m or method == "GET" and not m.group(4):
        return None
    from urllib.parse import unquote
    number, sub, tail, label = m.groups()
    if label:
        return ("GET", "label_get", None, unquote(label)) if method == "GET" else None
    if method == "POST" and number is None:
        return (method, "create", None, None)
    if method == "POST" and sub == "comments":
        return (method, "comment", number, None)
    if method == "POST" and sub == "labels" and not tail:
        return (method, "add_labels", number, None)
    if method == "POST" and sub == "assignees":
        return (method, "add_assignees", number, None)
    if method == "DELETE" and sub == "labels" and tail:
        return (method, "remove_label", number, unquote(tail))
    if method == "PATCH" and number and not sub:
        return (method, "patch", number, None)
    return None


def is_issue_write(args):
    return rest_write_target(args) is not None


def rest_write(args, calls=None, repo_args=("--repo", "o/r"), labels=None, me="octocat", missing_labels=(),
               create_number=None, assignable=None):
    """Answer a REST issue write. Re-records the legacy `issue ...` call(s) into `calls`, applies label
    add/remove to the `labels` set when given, and returns REST-shaped JSON. None when `args` is not a
    REST write. `missing_labels`: names for which `GET labels/<name>` answers 404. `assignable`: logins
    the fake accepts (default: all); others are silently dropped, as GitHub does."""
    t = rest_write_target(args)
    if t is None:
        return None
    method, kind, n, extra = t
    f = _fields(args)
    rec = calls.append if calls is not None else (lambda c: None)
    if kind == "user":
        return json.dumps({"login": me})
    if kind == "label_get":
        if extra in missing_labels:
            e = RuntimeError("gh: Not Found (HTTP 404)")
            e.hint = "gh: Not Found (HTTP 404)"
            raise e
        return json.dumps({"name": extra})
    if kind == "comment":
        rec(["issue", "comment", n, *repo_args, "--body", f["body"][0]])
        return json.dumps({"id": 1, "body": f["body"][0]})
    if kind == "create":
        num = int(create_number) if create_number is not None else _CREATED["n"]
        _CREATED["n"] += 1
        legacy = ["issue", "create", *repo_args, "--title", f["title"][0], "--body", f["body"][0]]
        for lab in f.get("labels[]", []):
            legacy += ["--label", lab]
        rec(legacy)
        return json.dumps({"number": num, "html_url": "https://github.com/o/r/issues/%d" % num})
    if kind == "add_labels":
        for lab in f.get("labels[]", []):
            if labels is not None:
                labels.add(lab)
            rec(["issue", "edit", n, *repo_args, "--add-label", lab])
        return json.dumps([{"name": l} for l in f.get("labels[]", [])])
    if kind == "remove_label":
        if labels is not None:
            labels.discard(extra)
        rec(["issue", "edit", n, *repo_args, "--remove-label", extra])
        return "[]"
    if kind == "add_assignees":
        got = [l for l in f.get("assignees[]", []) if assignable is None or l in assignable]
        for l in f.get("assignees[]", []):
            rec(["issue", "edit", n, *repo_args, "--add-assignee", l])
        return json.dumps({"number": int(n), "assignees": [{"login": l} for l in got]})
    if kind == "patch":
        if "state" in f:
            legacy = ["issue", "close", n, *repo_args]
            if "state_reason" in f:
                legacy += ["--reason", f["state_reason"][0].replace("_", " ")]
            rec(legacy)
        if "body" in f:
            rec(["issue", "edit", n, *repo_args, "--body", f["body"][0]])
        return json.dumps({"number": int(n)})
    return None


def legacy_of(args, repo_args=("--repo", "o/r")):
    """The legacy-shaped `issue ...` argv list(s) a REST write argv stands for (for fakes that gate on
    `--remove-label`, `--body`, ...); [] when `args` is not a REST issue write."""
    out = []
    if is_issue_write(args):
        try:
            rest_write(args, calls=out, repo_args=repo_args)
        except Exception:     # a lookup that raises (label_get 404) records nothing
            pass
    return out
