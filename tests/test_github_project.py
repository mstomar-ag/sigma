"""GitHubSource Projects-v2 board integration. Like test_sources.py, these are hermetic: a
tiny in-memory simulator of the `gh project` surface stands in for the network, so we assert the
real board behavior (find-or-create, status mapping, no-duplicate-add, fail-open) without `gh`."""
import json, re, pathlib, importlib.util, tempfile, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gqlfake

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _arg(a, flag):
    return a[a.index(flag) + 1] if flag in a else None


def _rest_field(a, key):
    """The value of a `gh api -f key=value` field, or None if `key` was never passed. #1829:
    REST fields combine the key and value into ONE token, unlike a `--flag value` pair `_arg`
    already handles."""
    prefix = key + "="
    return next((v[len(prefix):] for v in a if isinstance(v, str) and v.startswith(prefix)), None)


def _limit(a):
    """`gh`'s `--limit` is a MAXIMUM, not a page size: it returns fewer when the source is
    exhausted, and truncates at the limit when it is not (verified live against project #6 —
    `--limit 200` -> 200 items on a 262-item board, `--limit 300` -> 262). The simulator has to
    honour it or #692's truncation bug is structurally unreproducible here. Default 30 matches
    `gh`'s own default for a call that passes no limit at all."""
    raw = _arg(a, "--limit")
    return int(raw) if raw is not None else 30


_FAKE_ME = "me-login"   # #1437: who GitHub resolves `@me` to, in this simulator


def _apply_query(items, query):
    """`gh project item-list --query` filters SERVER-SIDE using Projects filter syntax — verified
    live: `--limit 5 --query "-status:Done"` returned five NON-Done items from a board whose first
    ~85 items are all Done, which a client-side filter could not do. The simulator has to model that
    or a status-driven queue read is untestable here. Supports only the subset the kit emits:
    `is:open`, `status:<Name>` (quoted when it contains a space) and `assignee:<login>`.

    #1437: `assignee:` is modelled because the board query now carries the ownership scope
    server-side, mirroring the label path's `--assignee` flag. Verified live: the term is accepted
    and really filters (a login owning nothing returned 0 items on a 63-item board). `@me` resolves
    to `_FAKE_ME`, exactly as GitHub resolves it for the authenticated user."""
    if not query:
        return items
    out = items
    for term in re.findall(r'[-\w:]+:"[^"]*"|\S+', query):
        if term == "is:open":
            out = [i for i in out if (i.get("content") or {}).get("state", "OPEN") == "OPEN"]
        elif term.startswith("status:"):
            want = term.split(":", 1)[1].strip('"')
            out = [i for i in out if i.get("status") == want]
        elif term.startswith("assignee:"):
            want = term.split(":", 1)[1].strip('"').lstrip("@")
            want = _FAKE_ME if want == "me" else want
            out = [i for i in out
                   if want.casefold() in {str(a).casefold() for a in (i.get("assignees") or [])}]
        else:
            raise AssertionError("simulator does not model the query term %r" % term)
    return out


def _pissue(number, *labels):
    """An open goal issue as `gh issue list --json number,labels` returns it."""
    return {"number": number, "labels": [{"name": "sdlc:goal"}] + [{"name": l} for l in labels]}


def _is_issues_list_call(a):
    """#1829: true for either the old `gh issue list` shape or the new `gh api
    repos/{owner}/{repo}/issues` REST shape `_fetch_pending`/`list_needs_label` now construct.

    DELIBERATELY a boolean, not a verb-normalizer returning the string "list": this file's own
    fakes ALSO see `gh project list` calls, whose second token is independently the literal
    string "list" -- a generic `a[1] if len(a) > 1 else a[0]` fallback would equate the two,
    exactly the collision a first version of this helper had (caught by
    test_blocking_priority_override_board_mode_uncarded_fallback_still_finds_a_separate_non_blocking_goal
    misrouting `project list`'s response through the issues branch). Checking BOTH tokens for
    each shape, with no generic fallback, is what test_sources.py's own two-token exact match on
    the old shape already did -- this restores that same precision for the new shape too."""
    return (len(a) > 1 and a[0] == "issue" and a[1] == "list") or (
        len(a) > 1 and a[0] == "api" and str(a[1]).startswith("repos/") and str(a[1]).endswith("/issues"))


def _recording_runner(by_subcommand=None):
    """Fake `gh` runner keyed by the gh verb, mirroring test_sources.py's own."""
    calls = []
    by_subcommand = by_subcommand or {}
    def run(args):
        calls.append(list(args))
        verb = "list" if _is_issues_list_call(args) else (args[1] if len(args) > 1 else args[0])
        return by_subcommand.get(verb, "")
    run.calls = calls
    return run


# A built-in Status single-select field, as GitHub auto-creates on a new project.
DEFAULT_FIELDS = [
    {"id": "F_title", "name": "Title", "type": "ProjectV2Field"},
    {"id": "F_status", "name": "Status", "type": "ProjectV2SingleSelectField",
     "options": [{"id": "o_todo", "name": "Todo"}, {"id": "o_ip", "name": "In Progress"},
                 {"id": "o_done", "name": "Done"}]},
]


# An ADOPTED board's built-in Status field, already configured with our columns (the model the kit now
# drives — GitHub's native Status field, not a separate one).
STATUS_FILLED = {"id": "F_status", "name": "Status", "type": "ProjectV2SingleSelectField",
                 "options": [{"id": "s_backlog", "name": "Backlog"}, {"id": "s_in_progress", "name": "In Progress"},
                             {"id": "s_qc", "name": "QC"}, {"id": "s_done", "name": "Done"},
                             {"id": "s_blocked", "name": "Blocked"}]}


# --- #1391 step 2: lifecycle transitions issue ONE graphql label swap ----------------------------
# `project_world` already answers `api graphql` for the Status field option-set; label swaps arrive
# on the same verb and must be answered too, or every transition in these board tests silently
# fails open and writes no labels at all.

_PW_LABEL_IDS = {}
_PW_LAST_ISSUE = {"n": "0"}


def _pw_label_swap(a, labels_by_issue, calls=None, repo=None):
    """Answer sources._swap_labels' three shapes; apply the mutation to `labels_by_issue`.

    Also appends one synthetic `issue edit <n> --repo R --add-label/--remove-label X` record per
    label changed, so assertions written before #1391 step 2 -- which asked "did this transition
    write that label", not "over which transport" -- keep working unchanged.

    Returns a response string, or None when `a` is not part of a label swap."""
    if len(a) >= 2 and a[0] == "repo" and a[1] == "view":
        return json.dumps({"owner": {"login": "acme"}, "name": "widget"})
    if not (len(a) >= 2 and a[0] == "api" and a[1] == "graphql"):
        return None
    doc = _arg(a, "-f") or ""
    if doc.startswith("query="):
        doc = doc[len("query="):]          # gh takes `-f query=<document>`; strip the field name
    if "updateProjectV2Field" in doc:
        return None                      # the board's own Status write -- not ours
    if "label(name:" in doc:
        # #1393 review bug_001: ids are fetched BY NAME now, one aliased field per label
        fields = {}
        for alias, lbl in re.findall(r'(a\d+): label\(name: "([^"]*)"\)', doc):
            # reuse the fixture's own id table -- the mutation branch below decodes ids back to
            # names through it, so minting a different id here would make swaps undecodable
            _PW_LABEL_IDS.setdefault(lbl, "L_%d" % (abs(hash(lbl)) % 10**8))
            fields[alias] = {"id": _PW_LABEL_IDS[lbl], "name": lbl}
        return json.dumps({"data": {"repository": fields}})
    if "labels(first" in doc:
        for n in ("sdlc:goal", "sdlc:in-progress", "sdlc:parked", "sdlc:blocked",
                  "sdlc:needs-confirmation", "sdlc:blocking", "sdlc:dependency"):
            _PW_LABEL_IDS.setdefault(n, "L_%d" % (abs(hash(n)) % 10**8))
        return json.dumps({"data": {"repository": {"labels": {"nodes": [
            {"id": v, "name": k} for k, v in _PW_LABEL_IDS.items()]}}}})
    if "issue(number" in doc:
        m = re.search(r"issue\(number: (\d+)\)", doc)
        if m:
            _PW_LAST_ISSUE["n"] = m.group(1)
        return json.dumps({"data": {"repository": {"issue": {"id": "I_node"}}}})
    if doc.startswith("mutation"):
        by_id = {v: k for k, v in _PW_LABEL_IDS.items()}
        cur = labels_by_issue.setdefault(_PW_LAST_ISSUE["n"], set())
        for alias, add in (("a: addLabelsToLabelable", True),
                           ("r: removeLabelsFromLabelable", False)):
            if alias not in doc:
                continue
            seg = doc[doc.index(alias):]
            seg = seg[:seg.index("}")] if "}" in seg else seg
            for lid in re.findall(r'"(L_\d+)"', seg):
                name = by_id.get(lid)
                if not name:
                    continue
                cur.add(name) if add else cur.discard(name)
                if calls is not None:
                    calls.append(["issue", "edit", _PW_LAST_ISSUE["n"],
                                  *(["--repo", repo] if repo else []),
                                  "--add-label" if add else "--remove-label", name])
        return json.dumps({"data": {"a": {"clientMutationId": None}}})
    return "{}"


def _synced(gh):
    """#1305: single-issue verbs no longer back-fill the board; the watcher's `backfill_board` does."""
    gh.backfill_board(cap=10 ** 6)
    return gh


def project_world(projects=None, fields=None, items=None, issues=None):
    """In-memory `gh` simulator: records calls, mutates an in-memory board, returns gh-shaped JSON."""
    state = {"projects": [dict(p) for p in (projects or [])],
             "fields": [dict(f) for f in (fields if fields is not None else DEFAULT_FIELDS)],
             "items": [dict(i) for i in (items or [])],
             "issues": issues or []}
    calls = []

    state.setdefault("issue_labels", {})

    def run(a):
        if a[:2] == ["api", "rate_limit"]:      # #1305: REST budget read the watcher back-fill gates on
            return json.dumps({"resources": {"graphql": {"remaining": 5000, "limit": 5000}}})
        if a[:2] == ["api", "user"]:            # #1934: the login read is an explicit GET; `@me` is this simulator's _FAKE_ME
            calls.append(list(a))
            return _FAKE_ME + "\n"
        swap = _pw_label_swap(a, state["issue_labels"], calls=calls,
                              repo=next((c[c.index("--repo") + 1] for c in calls
                                         if "--repo" in c), None))
        if swap is not None:
            calls.append(list(a))
            return swap
        if gqlfake.is_issue_write(a):       # #895 slice 3a: REST issue writes, re-recorded legacy-shaped
            num = state.get("next_issue", 500)
            if gqlfake.rest_write_target(a)[1] == "create":
                state["next_issue"] = num + 1
            return gqlfake.rest_write(a, calls=calls, repo_args=("--repo", "acme/widget"), create_number=num)
        calls.append(list(a))
        v0, v1 = a[0], (a[1] if len(a) > 1 else "")
        if v0 == "issue" and v1 == "list":
            return json.dumps(state["issues"][:_limit(a)])
        # #1829: `_fetch_pending`/`list_needs_label` now read through `gh api
        # repos/{owner}/{repo}/issues` (REST) instead of `gh issue list` (graphql-search-billed)
        # -- genuinely paginate (page/per_page), unlike the old shape's flat `_limit` slice, since
        # the real code now issues more than one call when a fixture exceeds one page.
        if v0 == "api" and str(v1).startswith("repos/") and str(v1).endswith("/issues"):
            page = int(_rest_field(a, "page") or "1")
            per_page = int(_rest_field(a, "per_page") or "30")
            start = (page - 1) * per_page
            return json.dumps(state["issues"][start:start + per_page])
        if v0 == "issue" and v1 == "create":
            num = state.get("next_issue", 500)
            state["next_issue"] = num + 1
            return "https://github.com/acme/widget/issues/%d" % num      # gh prints the new issue URL
        if v0 in ("issue", "label"):
            return ""
        if v0 == "api" and v1 == "graphql":          # the GraphQL option-set for the built-in Status field
            q = _arg(a, "-f") or ""
            if "updateProjectV2Field" in q:
                m = re.search(r'fieldId: "([^"]+)"', q)
                names = re.findall(r'name: "([^"]+)"', q)
                for f in state["fields"]:
                    if m and f.get("id") == m.group(1):
                        f["options"] = [{"id": "s_" + n.lower().replace(" ", "_"), "name": n} for n in names]
                return json.dumps({"data": {"updateProjectV2Field": {"projectV2Field": {"id": m.group(1) if m else None}}}})
            return "{}"
        if v0 == "project":
            if v1 == "list":
                return json.dumps({"projects": state["projects"]})
            if v1 == "create":
                p = {"number": 99, "id": "PVT_new", "title": _arg(a, "--title"), "url": "u"}
                state["projects"].append(p); return json.dumps(p)
            if v1 == "link":
                return ""
            if v1 == "field-list":
                return json.dumps({"fields": state["fields"]})
            if v1 == "field-create":
                opts = [o.strip() for o in _arg(a, "--single-select-options").split(",")]
                f = {"id": "F_sdlc", "name": _arg(a, "--name"), "type": "ProjectV2SingleSelectField",
                     "options": [{"id": "s_" + o.lower().replace(" ", "_"), "name": o} for o in opts]}
                state["fields"].append(f); return json.dumps(f)
            if v1 == "field-delete":
                fid = _arg(a, "--id")
                state["fields"] = [f for f in state["fields"] if f["id"] != fid]; return ""
            if v1 == "item-list":
                return json.dumps({"items": _apply_query(state["items"], _arg(a, "--query"))[:_limit(a)]})
            if v1 == "item-add":
                num = int(_arg(a, "--url").rstrip("/").split("/")[-1])
                it = {"id": "PVTI_%d" % num, "content": {"type": "Issue", "number": num, "url": _arg(a, "--url")}}
                state["items"].append(it); return json.dumps(it)
            if v1 == "item-edit":
                return ""
        return ""

    run.calls = calls
    run.state = state
    return run


def _edits(run):
    """Parsed item-edit calls: {item, field, option, project}."""
    return [{"item": _arg(c, "--id"), "field": _arg(c, "--field-id"),
             "option": _arg(c, "--single-select-option-id"), "project": _arg(c, "--project-id")}
            for c in run.calls if c[:2] == ["project", "item-edit"]]


def _verbs(run):
    return [" ".join(c) for c in run.calls]


def _cfg(project=None, repo="acme/chatgpt-clone-demo", **gh):
    g = {"repo": repo, **gh}
    if project is not None:
        g["project"] = project
    return {"discovery": {"source": "github", "github": g}}


# --- default / opt-in ---

def test_no_project_block_defaults_disabled():
    """Backward-compat: a github config with no `project` block makes ZERO project calls."""
    src = _mod("sources")
    run = project_world()
    gh = src.GitHubSource(_cfg(), run=run)
    gh.mark_in_progress("5"); gh.complete("5"); gh.park("9", "r")
    assert not any(c and c[0] == "project" for c in run.calls)


def test_project_enabled_false_makes_no_project_calls():
    src = _mod("sources")
    run = project_world()
    gh = src.GitHubSource(_cfg(project={"enabled": False}), run=run)
    gh.mark_in_progress("5"); gh.mark_qc("5")
    assert not any(c and c[0] == "project" for c in run.calls)


# --- create + status mapping ---

def test_first_transition_creates_board_field_and_syncs_backlog():
    src = _mod("sources")
    issues = [{"number": 5, "labels": [{"name": "sdlc:goal"}]},
              {"number": 7, "labels": [{"name": "sdlc:goal"}]}]
    run = project_world(projects=[], issues=issues)
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    _synced(gh).mark_in_progress("5")
    v = _verbs(run)
    assert any(c.startswith("project create") for c in v)                       # no board existed -> create
    assert any("api graphql" in c and "updateProjectV2Field" in c and "QC" in c and "Blocked" in c
               for c in v)                                                      # built-in Status options set via GraphQL
    # No separate STATUS field: the kit drives GitHub's built-in one. A `field-create` for the
    # P0-P4 Priority column (#719) is expected here and is a different field entirely.
    assert not any("field-create" in c and "Status" in c for c in v)
    assert not any("field-delete" in c for c in v)                             # built-in Status kept + driven
    # Backlog synced: both open goal issues are board items; the picked one is In Progress, the other
    # READY — not Backlog. Changed deliberately by #693 and only on the new-board path: a board this
    # kit creates now carries a `Ready` lane, and _sync_backlog only ever queries `--label sdlc:goal`,
    # so everything it cards is already queued. An ADOPTED board has no `Ready` option and still gets
    # `s_backlog` here — asserted separately in test_sync_still_uses_backlog_on_a_board_with_no_ready_option.
    e = _edits(run)
    assert {"item": "PVTI_5", "option": "s_in_progress"}.items() <= [x for x in e if x["item"] == "PVTI_5"][-1].items()
    assert any(x["item"] == "PVTI_7" and x["option"] == "s_ready" for x in e)
    assert all(x["field"] == "F_status" and x["project"] == "PVT_new" for x in e)  # the BUILT-IN Status field


def test_backlog_sync_pages_from_the_oldest_end_like_next_pending():
    # _sync_backlog seeds the board from the SAME goal-labelled backlog next_pending picks from, so
    # it has to page from the same end. A bare --limit 200 is created-DESC: over the cap it would
    # seed the board with the newest goals and never card the ones actually being worked.
    # #1833: migrated onto the REST fetch (`sort=created&direction=asc` fields, not a `--search`
    # qualifier) `_fetch_pending`/`list_needs_label` already use -- see `_is_issues_list_call`/
    # `_rest_field` above for why this file's fakes already normalize either shape.
    src = _mod("sources")
    run = project_world(projects=[], issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    _synced(gh).mark_in_progress("5")
    listings = [c for c in run.calls if _is_issues_list_call(c)
                and _rest_field(c, "state") == "open"]
    assert listings, "no open-goal listing was issued at all"
    for c in listings:
        assert _rest_field(c, "sort") == "created"
        assert _rest_field(c, "direction") == "asc"


def test_backlog_sync_never_uses_the_graphql_search_field():
    """#1833: `_sync_backlog` was the one `--label`-filtered `gh issue list` call #1829's own
    research explicitly enumerated as graphql-search-billed by the same mechanism as the pick
    path, but left out of scope for being off it. It must never construct that shape again."""
    src = _mod("sources")
    run = project_world(projects=[], issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    _synced(gh).mark_in_progress("5")
    assert not any(len(c) > 1 and c[0] == "issue" and c[1] == "list" for c in run.calls)
    assert any(c and c[0] == "api" and str(c[1]).endswith("/issues") for c in run.calls)
    assert not any("--search" in c for c in run.calls if _is_issues_list_call(c))


def test_complete_sets_done():
    src = _mod("sources")
    run = project_world(projects=[], issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    gh.mark_in_progress("5"); gh.complete("5")
    assert any(x["item"] == "PVTI_5" and x["option"] == "s_done" for x in _edits(run))
    assert any("issue close 5" in c for c in _verbs(run))                        # issue still closed


def test_complete_strips_membership_not_just_the_overlay():
    """#1445: `complete()` removed `sdlc:in-progress` and LEFT `sdlc:goal` behind, so every issue the
    loop finished stayed labelled as the loop's work forever -- 38 on `os` in ~25h across three
    operators, the same drift that forced stripping ~310 closed issues by hand. A Done issue carries
    no `sdlc:*` label."""
    src = _mod("sources")
    run = project_world(projects=[], issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    gh.mark_in_progress("5"); gh.complete("5")
    # `--remove-label`, NOT a bare substring: `sdlc:goal` already appears in the joined verb string
    # after `mark_in_progress` alone (the graphql label lookup names it), so `"sdlc:goal" in v`
    # passes with the OLD complete() too -- it tested nothing. Caught in review.
    assert any("--remove-label sdlc:goal" in c for c in _verbs(run))
    assert any("--remove-label sdlc:in-progress" in c for c in _verbs(run))
    assert any("issue close 5" in c for c in _verbs(run))        # and the close still happens


def test_complete_never_drops_membership_when_the_close_itself_fails():
    """The safety property the whole change rests on. `complete()` removes MEMBERSHIP, so if it
    could run the swap while the issue is still OPEN it would produce an orphan -- invisible to
    every `--label sdlc:goal` query, which is the one failure the membership model exists to
    prevent. The close raising must make the swap unreachable."""
    src = _mod("sources")
    run = project_world(projects=[], issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    inner = run

    def failing(cwd_or_args, *rest):
        args = rest[0] if rest else cwd_or_args
        line = " ".join(str(a) for a in args)
        if "issues/5 --method PATCH" in line and "state=closed" in line:      # #895 slice 3a: REST close
            raise RuntimeError("gh: could not close (502)")
        return inner(cwd_or_args, *rest) if rest else inner(cwd_or_args)
    for attr in ("calls", "labels", "state"):
        if hasattr(inner, attr):
            setattr(failing, attr, getattr(inner, attr))
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=failing)
    gh.mark_in_progress("5")
    before = len(_verbs(failing))
    try:
        gh.complete("5")
    except Exception:
        pass                                    # the raise is the point; run_loop parks on it
    after = [c for c in _verbs(failing)[before:]]
    assert not any("--remove-label sdlc:goal" in c for c in after)   # membership NEVER dropped


def test_mark_qc_sets_qc():
    src = _mod("sources")
    run = project_world(projects=[], issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    gh.mark_in_progress("5"); gh.mark_qc("5")
    assert any(x["item"] == "PVTI_5" and x["option"] == "s_qc" for x in _edits(run))  # Review -> QC column


def test_park_sets_parked_and_keeps_issue_transitions():
    # A board this kit CREATES now provisions a `Parked` option, so a park lands there rather than
    # sharing the `Blocked` column with machine blocks. The issue-side transitions are unchanged --
    # that half of this test is the original assertion and must stay byte-identical.
    src = _mod("sources")
    run = project_world(projects=[], issues=[{"number": 9, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    gh.park("9", "hit a deploy gate")
    assert any(x["item"] == "PVTI_9" and x["option"] == "s_parked" for x in _edits(run))
    v = _verbs(run)
    assert any("issue comment 9" in c and "hit a deploy gate" in c for c in v)   # existing park behavior intact
    assert any("issue edit 9" in c and "--remove-label sdlc:goal" in c for c in v)


STATUS_WITH_PARKED = {"id": "F_status", "name": "Status", "type": "ProjectV2SingleSelectField",
                      "options": [{"id": "s_backlog", "name": "Backlog"},
                                  {"id": "s_in_progress", "name": "In Progress"},
                                  {"id": "s_qc", "name": "QC"}, {"id": "s_done", "name": "Done"},
                                  {"id": "s_blocked", "name": "Blocked"},
                                  {"id": "s_parked", "name": "Parked"}]}


def test_park_and_block_land_in_DIFFERENT_columns_when_the_board_has_both():
    """A park and a machine block are different states; on a board carrying both options the
    card must say which one it is. Before this, both wrote `Blocked` and the board contradicted
    the labels -- exactly the distinction the label model exists to draw."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "chatgpt-clone-demo — SDLC"}],
                        fields=[STATUS_WITH_PARKED],
                        issues=[{"number": 9, "labels": [{"name": "sdlc:goal"}]},
                                {"number": 10, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    gh.park("9", "hit a deploy gate")
    gh.mark_in_progress("10"); gh.mark_blocked("10")
    edits = _edits(run)
    assert any(x["item"] == "PVTI_9" and x["option"] == "s_parked" for x in edits)    # park -> Parked
    assert not any(x["item"] == "PVTI_9" and x["option"] == "s_blocked" for x in edits)
    assert any(x["item"] == "PVTI_10" and x["option"] == "s_blocked" for x in edits)  # block -> Blocked
    assert not any(x["item"] == "PVTI_10" and x["option"] == "s_parked" for x in edits)


def test_park_falls_back_to_blocked_on_a_board_with_no_parked_option():
    """BACKWARD COMPAT: every adopter whose board predates the `Parked` option. `_offboard` tries
    `Parked` first, `_set_board_status` returns False WITHOUT spending a gh call when the option is
    absent, and the park lands in exactly the historical column."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "chatgpt-clone-demo — SDLC"}],
                        fields=[STATUS_FILLED],            # no `Parked` option on this board
                        issues=[{"number": 9, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    gh.park("9", "hit a deploy gate")
    assert any(x["item"] == "PVTI_9" and x["option"] == "s_blocked" for x in _edits(run))
    assert not any("project field-create" in c for c in _verbs(run))   # never rewrites an adopted board


# --- reuse + idempotency ---

def test_reuse_existing_project_no_create_no_default_delete():
    src = _mod("sources")
    existing = [{"number": 4, "id": "PVT_x", "title": "chatgpt-clone-demo — SDLC"}]
    fields = [STATUS_FILLED]
    run = project_world(projects=existing, fields=fields, issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    gh.mark_in_progress("5")
    v = _verbs(run)
    assert not any(c.startswith("project create") for c in v)        # reused, not recreated
    assert not any("project field-create" in c for c in v)           # Status field already configured
    # #1391 step 2: label swaps also travel on `api graphql` now, so this must name the BOARD
    # mutation specifically -- the point of the assertion is that an adopted board's Status options
    # are used as-is, never rewritten.
    assert not any("updateProjectV2Field" in c for c in v)           # adopted board's options used as-is
    assert not any("project field-delete" in c for c in v)           # never touch a reused board's fields
    assert any(x["item"] == "PVTI_5" and x["option"] == "s_in_progress" for x in _edits(run))


def test_no_duplicate_item_add_when_already_on_board():
    src = _mod("sources")
    items = [{"id": "PVTI_5", "content": {"type": "Issue", "number": 5, "url": "x/5"}}]
    run = project_world(projects=[], items=items, issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    gh.mark_in_progress("5")
    adds5 = [c for c in run.calls if c[:2] == ["project", "item-add"] and c[-1].endswith("/5")]
    assert adds5 == []                                              # already an item -> not re-added
    assert any(x["item"] == "PVTI_5" and x["option"] == "s_in_progress" for x in _edits(run))


# --- fail-open ---

def test_project_failures_do_not_break_issue_transitions():
    """If the project layer throws (e.g. no `project` token scope), the loop's issue-level
    transitions must still happen and nothing propagates."""
    src = _mod("sources")

    def run(a):
        if a and a[0] == "project":
            raise RuntimeError("missing `project` scope")
        if a[:2] == ["issue", "list"]:
            return "[]"
        return ""
    run.calls = []
    real = run
    _swap_labels_state = {}
    def recording(a):
        swap = _pw_label_swap(a, _swap_labels_state, calls=recording.calls)   # #1391 step 2
        if swap is not None:
            recording.calls.append(list(a)); return swap
        if gqlfake.is_issue_write(a):                                       # #895 slice 3a
            return gqlfake.rest_write(a, calls=recording.calls, repo_args=("--repo", "acme/widget"))
        recording.calls.append(list(a)); return real(a)
    recording.calls = []

    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=recording)
    gh.mark_in_progress("5")     # must not raise
    gh.complete("5")             # must not raise
    v = [" ".join(c) for c in recording.calls]
    assert any("issue edit 5" in c and "--add-label sdlc:in-progress" in c for c in v)
    assert any("issue close 5" in c for c in v)


# --- transient-error retry: a `gh project` blip must not silently drop a card-status update ---

def test_transient_project_error_retried_until_card_set():
    """The bug: an intermittent `gh project` error ("unknown owner type") silently dropped a
    card-status update, so the board fell out of sync with the issues. With retry/backoff the
    item-edit is retried and the card eventually lands."""
    src = _mod("sources")
    base = project_world(projects=[], issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    attempts = {"item_edit": 0}
    def flaky(a):
        if a[:2] == ["project", "item-edit"]:
            attempts["item_edit"] += 1
            if attempts["item_edit"] <= 2:                 # first 2 tries blip...
                raise RuntimeError("gh project item-edit failed: unknown owner type")
        return base(a)                                     # ...3rd try (and everything else) succeeds
    flaky.calls = base.calls
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=flaky)
    gh._RETRY_BASE = 0                                     # hermetic: no real backoff sleeps
    gh.mark_in_progress("5")
    assert any(x["item"] == "PVTI_5" and x["option"] == "s_in_progress" for x in _edits(base))  # card landed
    assert attempts["item_edit"] == 3                      # 2 transient failures were retried, then success


def test_permanent_project_error_not_retried_stays_fail_open():
    """A non-transient project error (e.g. missing `project` scope) must fail fast — one attempt,
    no backoff burned on a hopeless call — and still fall open without breaking issue transitions."""
    src = _mod("sources")
    base = project_world(projects=[], issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    attempts = {"item_edit": 0}
    def flaky(a):
        if a[:2] == ["project", "item-edit"]:
            attempts["item_edit"] += 1
            raise RuntimeError("gh project item-edit failed: missing `project` scope")
        return base(a)
    flaky.calls = base.calls
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=flaky)
    gh._RETRY_BASE = 0
    gh.mark_in_progress("5")                               # must not raise (fail-open)
    assert attempts["item_edit"] == 1                      # permanent error -> exactly one attempt, no retry


# --- #1733: a board WRITE failure that is not the one recognised "missing scope" shape used to
# vanish completely -- exit 0, correct labels, zero stderr, board silently stale. `_note_scope`'s
# match is narrow on purpose (it names one specific, actionable remedy); everything else fell
# through with no trace at all. `_set_board_status`/`_apply_custom_fields` now route through
# `_note_board_write_failed`, which still gives the scope case its own specific message unchanged,
# and reports anything else generically, once per distinct failure text.

def test_a_non_scope_board_write_failure_is_now_reported_not_silent(capsys):
    """The bug as filed (#1733): `park()` completes, labels are correct, and the board write threw
    something that is NOT the missing-scope shape (a malformed response, an unexpected error) --
    that must no longer be total silence."""
    src = _mod("sources")
    base = project_world(projects=[], issues=[{"number": 9, "labels": [{"name": "sdlc:goal"}]}])
    def boom(a):
        if a[:2] == ["project", "item-edit"]:
            raise RuntimeError("unexpected response shape from item-edit")
        return base(a)
    boom.calls = base.calls
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=boom)
    gh._RETRY_BASE = 0
    gh.park("9", "hit a deploy gate")                      # must not raise (fail-open, unchanged)
    err = capsys.readouterr().err
    assert "unexpected response shape from item-edit" in err
    assert "gh auth refresh -s project" not in err          # not the scope message -- wrong remedy for this


def test_the_scope_specific_message_still_fires_unchanged(capsys):
    """Regression guard: the one case `_note_scope` already handled keeps its own specific,
    actionable message -- #1733's fix must not replace it with the generic fallback."""
    src = _mod("sources")
    base = project_world(projects=[], issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    def boom(a):
        if a[:2] == ["project", "item-edit"]:
            raise RuntimeError("missing `project` scope")
        return base(a)
    boom.calls = base.calls
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=boom)
    gh._RETRY_BASE = 0
    gh.mark_in_progress("5")
    err = capsys.readouterr().err
    assert "gh auth refresh -s project" in err
    assert "unexpected response shape" not in err


def test_generic_board_failure_warns_once_per_distinct_message_not_per_call(capsys):
    """Two DIFFERENT failures in one process both get reported (keyed by message, not a bare
    once-ever flag) -- but the SAME failure repeating does not spam. Board resolution succeeds
    (so `_ensure_board`'s own attempt-once cache never gates this), and the two item-edit calls
    mirror `_offboard`'s real parked-then-blocked fallback sequence."""
    src = _mod("sources")
    # Needs an option for BOTH `Parked` and `Blocked` -- on a board missing `Parked`,
    # `_set_board_status(parked)` returns False WITHOUT ever calling item-edit (see
    # test_park_falls_back_to_blocked_on_a_board_with_no_parked_option), so the fallback's own
    # item-edit would be the only call made and there would be no way to get two DISTINCT
    # item-edit failures out of one `_offboard` sequence.
    base = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "chatgpt-clone-demo — SDLC"}],
                         fields=[STATUS_WITH_PARKED], issues=[{"number": 9, "labels": [{"name": "sdlc:goal"}]}])
    calls = {"n": 0}
    def flaky(a):
        if a[:2] == ["project", "item-edit"]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("board write failed: reason A")
            raise RuntimeError("board write failed: reason B")
        return base(a)
    flaky.calls = base.calls
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=flaky)
    gh._RETRY_BASE = 0
    gh.park("9", "hit a deploy gate")                       # tries parked (A), falls back to blocked (B)
    err = capsys.readouterr().err
    assert err.count("reason A") == 1
    assert err.count("reason B") == 1
    gh.park("9", "again")                                   # a THIRD call, same board write failure as A
    err2 = capsys.readouterr().err
    assert "reason A" not in err2                            # already reported once -> no repeat spam


def test_a_disabled_board_produces_no_generic_board_failure_noise(capsys):
    """The fix must not turn the deliberately-silent board-disabled path noisy."""
    src = _mod("sources")
    run = project_world()
    gh = src.GitHubSource(_cfg(project={"enabled": False}), run=run)
    gh.mark_in_progress("5")
    gh.park("5", "reason")
    err = capsys.readouterr().err
    assert "board update failed" not in err


# --- backlog sync must not clobber in-flight cards; number match must be type-tolerant ---

def test_sync_does_not_reset_status_of_cards_already_on_board():
    """A card already on the board keeps its status — only brand-new cards get seeded to Todo.

    Card 7 carries an explicit `In Progress` here. It previously carried NO status key at all,
    which made the fixture say something narrower than the docstring and the section header
    ("must not clobber IN-FLIGHT cards") both claim: a blank column is not a status a human chose,
    so "keeps its status" could not have been about it. That accidental blank is now its own
    deliberate case — see test_sync_seeds_a_carded_issue_whose_status_is_blank, the 8-card
    regression on this repo's board #6 — so this test states the in-flight guard it was named for.
    """
    src = _mod("sources")
    existing = [{"number": 4, "id": "PVT_x", "title": "chatgpt-clone-demo — SDLC"}]
    items = [{"id": "PVTI_7", "status": "In Progress",
              "content": {"type": "Issue", "number": 7, "url": "x/7"}}]   # 7 already a card
    issues = [{"number": 5, "labels": [{"name": "sdlc:goal"}]}, {"number": 7, "labels": [{"name": "sdlc:goal"}]}]
    run = project_world(projects=existing, fields=[STATUS_FILLED], items=items, issues=issues)
    gh = src.GitHubSource(_cfg(project={"enabled": True}), run=run)
    gh.mark_in_progress("5")
    assert not any(e["item"] == "PVTI_7" and e["option"] == "s_backlog" for e in _edits(run))  # 7 untouched
    assert any(e["item"] == "PVTI_5" and e["option"] == "s_in_progress" for e in _edits(run))


def test_existing_board_matched_by_string_number():
    """A configured project `number` authored as a string must still match gh's integer number."""
    src = _mod("sources")
    existing = [{"number": 4, "id": "PVT_x", "title": "some other title"}]
    run = project_world(projects=existing, fields=[STATUS_FILLED],
                        issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(project={"enabled": True, "number": "4", "title": "won't match by title"}), run=run)
    gh.mark_in_progress("5")
    assert not any(c[:2] == ["project", "create"] for c in run.calls)   # reused via number, not recreated


# --- custom board fields on loop-created (hand-off) issues (issue #8: no silent-blank fields) ---

# An adopter's own custom single-select field, alongside the built-in Status — the shape that used to
# be invisible to sigma (it set labels + assignee + Status and nothing else).
PRIORITY_FIELD = {"id": "F_priority", "name": "Priority", "type": "ProjectV2SingleSelectField",
                  "options": [{"id": "p_crit", "name": "Critical"}, {"id": "p_high", "name": "High"},
                              {"id": "p_med", "name": "Medium"}, {"id": "p_low", "name": "Low"}]}


def _board(project=None, **kw):
    """A board-enabled config on a neutral repo, with an existing project #4."""
    p = {"enabled": True, "number": 4, "owner": "acme"}
    p.update(project or {})
    return _cfg(repo="acme/widget", project=p, **kw)


def test_custom_fields_stamped_on_a_loop_created_issue():
    """The fix: an issue the loop CREATES (a hand-off) gets the adopter's custom single-select field
    set, not just labels + assignee — so it isn't blank on Priority while every human-made card has it."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED, PRIORITY_FIELD], issues=[])
    gh = src.GitHubSource(_board(project={"custom_fields": {"Priority": "Medium"}}), run=run)
    num = gh.create_dependency("[engine] dep", "body", "eng-owner",
                               labels=["sdlc:dependency", "priority:P1"])
    assert num == "500"
    # the new issue's board item got Priority=Medium (the custom single-select field)
    assert any(x["item"] == "PVTI_500" and x["field"] == "F_priority" and x["option"] == "p_med"
               for x in _edits(run))
    # ...AND its Status is seeded to Backlog: carding it to set custom fields makes _sync_backlog skip
    # it as "already on the board", so it must not be left blank on Status.
    assert any(x["item"] == "PVTI_500" and x["field"] == "F_status" and x["option"] == "s_backlog"
               for x in _edits(run))
    # and the label path is unchanged — `priority:P1` is still a LABEL on the issue (a different thing)
    assert any(c[:2] == ["issue", "create"] and "priority:P1" in c for c in run.calls)


def test_no_custom_fields_configured_sets_only_labels_and_assignee():
    """Backward-compat: with no custom_fields mapping, create_dependency makes NO custom-field edit."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED, PRIORITY_FIELD], issues=[])
    gh = src.GitHubSource(_board(), run=run)
    assert gh.create_dependency("t", "b", "who", labels=["sdlc:dependency"]) == "500"
    assert not any(x["field"] == "F_priority" for x in _edits(run))     # Priority never touched


def test_custom_fields_skip_unknown_field_and_non_option_value():
    """A configured field the board doesn't have, or a value that isn't one of its options, is
    SKIPPED — never guessed, never a crash."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED, PRIORITY_FIELD], issues=[])
    gh = src.GitHubSource(_board(project={"custom_fields":
                          {"Priority": "Nope", "Nonexistent": "X"}}), run=run)
    assert gh.create_dependency("t", "b", "who") == "500"
    assert not any(x["field"] == "F_priority" for x in _edits(run))     # bad option -> that field skipped
    # the issue is still carded with Backlog status; ONLY the invalid custom fields are skipped
    p500 = [x for x in _edits(run) if x["item"] == "PVTI_500"]
    assert p500 and all(x["field"] == "F_status" and x["option"] == "s_backlog" for x in p500)


def test_custom_fields_ignored_when_project_disabled():
    """Board off => custom_fields is inert; a hand-off still opens the issue, makes zero project calls."""
    src = _mod("sources")
    run = project_world()
    gh = src.GitHubSource(_cfg(repo="acme/widget",
                          project={"enabled": False, "custom_fields": {"Priority": "Medium"}}), run=run)
    assert gh.create_dependency("t", "b", "who", labels=["sdlc:dependency"]) == "500"
    assert not any(c and c[0] == "project" for c in run.calls)


# --- #9: don't silently create a DUPLICATE board when the config is under-specified ---

def test_refuses_to_create_a_duplicate_when_owner_already_has_a_board(capsys):
    """enabled + no project.number + a board that doesn't match our auto-title => sigma must NOT
    create '<repo> — SDLC' as a second board; it warns loudly and leaves mirroring off (fail-open)."""
    src = _mod("sources")
    # owner 'acme' has a real board with a title that is NOT sigma's default 'widget — SDLC'
    existing = [{"number": 7, "id": "PVT_human", "title": "Acme Delivery Board"}]
    run = project_world(projects=existing, fields=[STATUS_FILLED],
                        issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(repo="acme/widget", project={"enabled": True}), run=run)   # number unset
    gh.mark_in_progress("5")
    v = _verbs(run)
    assert not any(c.startswith("project create") for c in v)     # NO duplicate board created
    assert not _edits(run)                                        # nothing carded — mirroring stayed off
    assert any("issue edit 5" in c and "sdlc:in-progress" in c for c in v)   # issue label still set
    err = capsys.readouterr().err
    assert "will NOT create a new board" in err and "project.number" in err


def test_refuse_warning_reaches_a_non_utf8_stderr(tmp_path):
    """The #9 warning interpolates the em-dash default board title; on a cp1252/C-locale stderr it must
    still be EMITTED, not swallowed by its own fail-open guard — else the loud warning is silent on
    exactly the platform the portability fix targets."""
    import subprocess, os, sys as _sys, textwrap
    prog = textwrap.dedent(r'''
        import importlib.util, pathlib, json
        S = pathlib.Path(%r)
        spec = importlib.util.spec_from_file_location("sources", S / "sources.py")
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        def run(a):
            if a[:2] == ["project", "list"]:
                return json.dumps({"projects": [{"number": 7, "id": "P", "title": "Human Board"}]})
            if a[:2] == ["issue", "list"]:
                return "[]"
            return ""
        cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget", "project": {"enabled": True}}}}
        m.GitHubSource(cfg, run=run).mark_in_progress("5")
    ''') % str(S)
    env = dict(os.environ, PYTHONIOENCODING="ascii", LC_ALL="C", LANG="C")
    p = subprocess.run([_sys.executable, "-c", prog], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert "will NOT create a new board" in p.stderr        # emitted, not swallowed by the fail-open guard


def test_still_creates_a_board_when_the_owner_has_none():
    """The fresh-setup path is preserved: owner with ZERO boards + no number => auto-create is safe."""
    src = _mod("sources")
    run = project_world(projects=[], issues=[{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    gh = src.GitHubSource(_cfg(repo="acme/widget", project={"enabled": True}), run=run)
    gh.mark_in_progress("5")
    assert any(c.startswith("project create") for c in _verbs(run))   # created (nothing to duplicate)
    assert any(x["item"] == "PVTI_5" and x["option"] == "s_in_progress" for x in _edits(run))


# --- missing `project` scope: warn LOUDLY once instead of silently no-op'ing board writes ---

def test_missing_project_scope_warns_once_and_keeps_issue_transitions(capsys):
    src = _mod("sources")
    def run(a):
        if a and a[0] == "project":
            raise RuntimeError("error: your token is missing the required scopes. missing: 'project'. "
                               "run: gh auth refresh -s project")
        if a[:2] == ["issue", "list"]:
            return "[]"
        return ""
    run.calls = []
    real = run
    _swap_state = {}
    def rec(a):
        swap = _pw_label_swap(a, _swap_state, calls=rec.calls)        # #1391 step 2
        if swap is not None:
            rec.calls.append(list(a)); return swap
        if gqlfake.is_issue_write(a):                                 # #895 slice 3a
            return gqlfake.rest_write(a, calls=rec.calls, repo_args=("--repo", "acme/widget"))
        rec.calls.append(list(a)); return real(a)
    rec.calls = []
    gh = src.GitHubSource(_cfg(repo="acme/widget", project={"enabled": True}), run=rec)
    gh._RETRY_BASE = 0
    gh.mark_in_progress("5")     # first board write fails on scope -> warns
    gh.complete("5")             # must NOT warn a second time
    err = capsys.readouterr().err
    assert "gh auth refresh -s project" in err
    assert err.count("board updates OFF") == 1                     # one-time, not per-call spam
    v = [" ".join(c) for c in rec.calls]
    assert any("issue edit 5" in c and "sdlc:in-progress" in c for c in v)   # issue work still happened
    assert any("issue close 5" in c for c in v)


def test_custom_field_write_failure_does_not_break_the_handoff():
    """Fail-open: if the board edit throws, the issue was still created and its number returned."""
    src = _mod("sources")
    base = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                         fields=[STATUS_FILLED, PRIORITY_FIELD], issues=[])
    def boom(a):
        if a[:2] == ["project", "item-edit"]:
            raise RuntimeError("missing `project` scope")
        return base(a)
    boom.calls = base.calls
    gh = src.GitHubSource(_board(project={"custom_fields": {"Priority": "Medium"}}), run=boom)
    gh._RETRY_BASE = 0
    assert gh.create_dependency("t", "b", "who") == "500"               # issue still created, no raise


# --- #692: the board item/issue reads must not silently truncate ---------------------------------
# Measured live against this repo's own board (project #6) on 2026-08-11:
#   gh project item-list 6 --format json --limit 200  ->  200 items
#   gh project item-list 6 --format json --limit 300  ->  262 items
# 62 cards were invisible to `_items`, so `_item_id` re-issued `project item-add` for every one of
# them on every board touch, and `_sync_backlog`'s `on_board` set was wrong for the same 62.

def _cards(n_from, n_to):
    return [{"id": "PVTI_%d" % n, "content": {"type": "Issue", "number": n}}
            for n in range(n_from, n_to + 1)]


def test_load_items_maps_every_card_on_a_board_larger_than_the_old_200_cap():
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED], items=_cards(1, 262), issues=[])
    gh = src.GitHubSource(_board(), run=run)
    gh._RETRY_BASE = 0
    gh.mark_in_progress("5")
    assert len(gh._items) == 262, "the id cache dropped the tail of the board"
    assert 262 in gh._items and 201 in gh._items      # the cards the old --limit 200 lost


def test_a_card_past_the_old_cap_is_not_re_added_to_the_board():
    """The user-visible symptom of the truncated cache: a card that IS on the board looks absent,
    so `_item_id` issues a redundant `project item-add` for it on every single board touch."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED], items=_cards(1, 262), issues=[])
    gh = src.GitHubSource(_board(), run=run)
    gh._RETRY_BASE = 0
    gh.mark_in_progress("250")                        # a card beyond the old 200-item window
    assert not any(c[:2] == ["project", "item-add"] for c in run.calls)


def test_sync_backlog_cards_goal_issues_past_the_old_200_cap():
    src = _mod("sources")
    issues = [{"number": n, "labels": [{"name": "sdlc:goal"}]} for n in range(1, 251)]
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED], items=[], issues=issues)
    gh = src.GitHubSource(_board(), run=run)
    gh._RETRY_BASE = 0
    _synced(gh).mark_in_progress("5")
    added = {int(_arg(c, "--url").rsplit("/", 1)[-1])
             for c in run.calls if c[:2] == ["project", "item-add"]}
    assert 250 in added and 201 in added, "goal issues past #200 were never carded"
    # Every open goal issue ends up carded. `exclude` only suppresses the STATUS write for the goal
    # being transitioned (#5) inside _sync_backlog — #5 is still carded, by _set_board_status's own
    # _item_id call on the way to setting it In Progress.
    assert added == set(range(1, 251))


def test_hitting_the_limit_warns_instead_of_truncating_silently(capsys):
    """The cap can be raised but never removed — `gh --limit 0` is NOT unlimited (it falls back to
    gh's default of 30, verified live). So the ceiling must announce itself rather than quietly
    dropping the tail the way 200 did."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED], items=_cards(1, 12), issues=[])
    gh = src.GitHubSource(_board(), run=run)
    gh._RETRY_BASE = 0
    gh._BOARD_ITEM_LIMIT = 12                          # simulate a board that fills the ceiling
    gh.mark_in_progress("5")
    err = capsys.readouterr().err
    assert "12" in err and "item" in err.lower()
    assert len(gh._items) == 12                        # still returns what it did get


def test_small_board_is_unchanged_by_the_limit_fix(capsys):
    """Backward compatibility: a board under the old cap maps identically, warns about nothing, and
    makes exactly ONE item-list call — the fix must not introduce a retry/escalation loop."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED], items=_cards(1, 12), issues=[])
    gh = src.GitHubSource(_board(), run=run)
    gh._RETRY_BASE = 0
    gh.mark_in_progress("5")
    assert gh._items == {n: "PVTI_%d" % n for n in range(1, 13)}
    assert len([c for c in run.calls if c[:2] == ["project", "item-list"]]) == 1
    assert "truncat" not in capsys.readouterr().err.lower()


# --- #693: board Status "Ready" is the queue source of truth --------------------------------------
# The label was the queue and the column a derived copy, which produced two defects: every issue
# _sync_backlog carded went to "Backlog" even though it only ever queries goal-labelled (i.e. already
# queued) issues, and a label added by hand did not move the card until the next loop run. Making
# Status authoritative removes the derivation, so there is nothing to reconcile and nothing to lag.

READY_FIELD = {"id": "F_status", "name": "Status", "type": "ProjectV2SingleSelectField",
               "options": [{"id": "s_backlog", "name": "Backlog"}, {"id": "s_ready", "name": "Ready"},
                           {"id": "s_in_progress", "name": "In Progress"}, {"id": "s_qc", "name": "QC"},
                           {"id": "s_done", "name": "Done"}, {"id": "s_blocked", "name": "Blocked"}]}


def _queue_world(items, issues, fields=None):
    """A board that can answer `item-list --query`, plus the issue list the label path reads."""
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[fields or READY_FIELD], items=items, issues=issues)
    return run


def _card(number, status, *labels):
    return {"id": "PVTI_%d" % number, "status": status,
            "labels": list(labels), "content": {"type": "Issue", "number": number}}


def _src(run, **project):
    p = {"enabled": True, "number": 4, "owner": "acme", **project}
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget", "project": p}}}
    gh = _mod("sources").GitHubSource(cfg, run=run)
    gh._RETRY_BASE = 0
    gh._BACKLOG_READ_RETRY_BASE = 0
    return gh


# --- #1437: the board path needs the SAME ownership guard the label path got in #1216 ----------

def _acard(number, status, *labels, assignees=None):
    """A Ready card carrying assignees in the shape `gh project item-list` really returns: BARE
    STRINGS, not {"login": ...} objects. Verified against a live board. `gh` OMITS the key
    entirely when a card has no assignees, which `assignees=None` reproduces."""
    c = _card(number, status, *labels)
    if assignees is not None:
        c["assignees"] = list(assignees)
    return c


def _asrc(run, assignee):
    p = {"enabled": True, "number": 4, "owner": "acme"}
    cfg = {"discovery": {"source": "github",
                         "github": {"repo": "acme/widget", "assignee": assignee, "project": p}}}
    gh = _mod("sources").GitHubSource(cfg, run=run)
    gh._RETRY_BASE = 0
    gh._BACKLOG_READ_RETRY_BASE = 0
    return gh


def test_board_does_not_serve_a_ready_card_assigned_to_someone_else():
    """The #1437 hole: `_board_queue` returns Ready cards directly and never reaches
    `_fetch_pending`, so #1216's check did not apply here at all. On a shared board that served
    every owner's Ready cards to whoever picked first."""
    run = _queue_world(items=[_acard(1, "Ready", "sdlc:goal", assignees=["someone-else"]),
                              _acard(2, "Ready", "sdlc:goal", assignees=["me-login"])],
                       issues=[_pissue(1), _pissue(2)])
    assert _asrc(run, "me-login").next_pending() == "2"


def test_board_client_check_holds_when_the_server_scope_is_dropped():
    """THE defence-in-depth test. The server-side `assignee:` term and the client-side check are
    two layers, and the whole point of #1216/#1437 is that the second holds when the first does
    not -- an older gh, a query term silently ignored, a config read that lost the value after the
    query was built. Modelled by stripping the assignee term out of the query on the way to the
    fake, so the read comes back UNFILTERED and only the client check can save it.

    Without this test the other board tests all pass with the client check deleted, because the
    server filter alone already excluded the wrong-owner card."""
    inner = _queue_world(items=[_acard(1, "Ready", "sdlc:goal", assignees=["someone-else"]),
                                _acard(2, "Ready", "sdlc:goal", assignees=[_FAKE_ME])],
                         issues=[_pissue(1), _pissue(2)])

    def run(args):
        args = list(args)
        for i, a in enumerate(args):                      # drop the scope the server would apply
            if isinstance(a, str) and a.startswith("is:open assignee:"):
                args[i] = "is:open"
        return inner(args)
    for attr in ("calls", "labels", "state"):             # carry whatever the fake exposes
        if hasattr(inner, attr):
            setattr(run, attr, getattr(inner, attr))
    assert _asrc(run, _FAKE_ME).next_pending() == "2"     # the client check alone excludes #1


def test_board_assignees_are_bare_strings_not_login_objects():
    """The shape trap: the issue API returns [{"login": x}], the project API returns ["x"].
    Handling only the object shape would match nothing and empty the Ready lane."""
    src = _mod("sources")
    assert src._logins(["Me-Login"]) == {"me-login"}                 # project shape
    assert src._logins([{"login": "Me-Login"}]) == {"me-login"}      # issue shape
    assert src._logins(None) == set() and src._logins([]) == set()


def test_board_card_with_no_assignees_is_not_served_when_an_assignee_is_configured():
    """`gh` omits the key entirely for an unassigned card. Consistent with the label path, where
    the server-side --assignee also excludes unassigned issues."""
    run = _queue_world(items=[_acard(1, "Ready", "sdlc:goal")],           # no assignees key at all
                       issues=[_pissue(1)])
    assert _asrc(run, "me-login").next_pending() is None


def test_board_at_me_is_resolved_by_the_server_without_a_local_lookup():
    """`@me` goes into the QUERY verbatim and GitHub resolves it, so the common case costs no
    `gh api user` call at all. Drives the real resolution path rather than pre-seeding the cache."""
    run = _queue_world(items=[_acard(1, "Ready", "sdlc:goal", assignees=["other"]),
                              _acard(2, "Ready", "sdlc:goal", assignees=[_FAKE_ME])],
                       issues=[_pissue(1), _pissue(2)])
    gh = _asrc(run, "@me")
    assert gh.next_pending() == "2"
    assert any("assignee:@me" in " ".join(c) for c in run.calls)   # scoped server-side


def test_board_with_no_configured_assignee_is_unchanged():
    """Existing installs must behave exactly as before this check existed."""
    run = _queue_world(items=[_acard(1, "Ready", "sdlc:goal", assignees=["anyone"])],
                       issues=[_pissue(1)])
    assert _src(run).next_pending() == "1"


def test_board_fails_open_when_the_login_cannot_be_resolved():
    """A transient auth failure must not empty the Ready lane."""
    run = _queue_world(items=[_acard(1, "Ready", "sdlc:goal", assignees=["anyone"])],
                       issues=[_pissue(1)])
    gh = _asrc(run, "@me")
    gh._assignee_login_cache = None            # resolution failed -> check skipped
    assert gh.next_pending() == "1"


def test_only_ready_cards_are_queued():
    run = _queue_world(items=[_card(1, "Backlog", "sdlc:goal"), _card(2, "Ready", "sdlc:goal"),
                              _card(3, "In Progress", "sdlc:goal")],
                       issues=[_pissue(1), _pissue(2), _pissue(3)])
    assert _src(run).next_pending() == "2"


def test_in_progress_qc_done_and_blocked_are_structurally_unpickable():
    """Better than the label era, where an in-flight issue still matched the label query and only the
    ledger claim stopped a double-pick."""
    for status in ("In Progress", "QC", "Done", "Blocked", "Backlog"):
        run = _queue_world(items=[_card(1, status)], issues=[_pissue(1)])
        assert _src(run).next_pending() is None, status


def test_board_order_not_issue_number_decides_among_ready_cards():
    """Card position is the sprint order — a human drags to prioritise and the loop obeys."""
    run = _queue_world(items=[_card(9, "Ready", "sdlc:goal"), _card(3, "Ready", "sdlc:goal")],
                       issues=[_pissue(3), _pissue(9)])
    assert _src(run).next_pending() == "9"


def test_status_wins_over_the_label_for_a_carded_issue():
    """The precedence rule: carded -> Status decides, whatever the label says."""
    run = _queue_world(items=[_card(1, "Backlog")], issues=[_pissue(1)])   # labelled but not Ready
    assert _src(run).next_pending() is None


def test_an_uncarded_labelled_issue_is_still_queued():
    """The forgiving direction: a goal filed and labelled but never carded must not vanish."""
    run = _queue_world(items=[], issues=[_pissue(7)])
    assert _src(run).next_pending() == "7"


def test_an_uncarded_in_progress_labelled_issue_is_not_queued():
    """#1198: the uncarded fallback (`_pending_by_label`) runs through the SAME `_fetch_pending`
    filter as the plain label queue -- an issue whose card write failed (or that was claimed
    before the board was ever wired up) but still carries `sdlc:in-progress` must not be offered
    as a fallback candidate either."""
    run = _queue_world(items=[], issues=[_pissue(7, "sdlc:in-progress")])
    assert _src(run).next_pending() is None


def test_ready_cards_come_before_uncarded_labelled_issues():
    run = _queue_world(items=[_card(9, "Ready", "sdlc:goal")], issues=[_pissue(3), _pissue(9)])
    assert _src(run).next_pending() == "9"


def test_skip_still_applies_to_board_ready_cards():
    run = _queue_world(items=[_card(3, "Ready", "sdlc:goal"), _card(9, "Ready", "sdlc:goal")],
                       issues=[_pissue(3), _pissue(9)])
    assert _src(run).next_pending(skip={"3"}) == "9"


def test_closed_issues_are_never_picked_even_if_their_card_says_ready():
    run = _queue_world(items=[_card(1, "Ready")], issues=[])   # issue list = the OPEN goal issues
    run.state["items"][0]["content"]["state"] = "CLOSED"
    assert _src(run).next_pending() is None


# --- backward compatibility: the Ready option's absence IS the gate ------------------------------

def test_a_board_without_a_ready_option_falls_back_to_the_label_queue():
    """Every existing adopter. Without this gate their queue would read as permanently empty."""
    run = _queue_world(items=[_card(1, "Backlog"), _card(2, "Backlog")],
                       issues=[_pissue(1), _pissue(2)], fields=STATUS_FILLED)
    assert _src(run).next_pending() == "1"          # lowest number, exactly as before


def test_a_board_without_a_ready_option_issues_no_board_query_at_all():
    run = _queue_world(items=[_card(1, "Backlog")], issues=[_pissue(1)], fields=STATUS_FILLED)
    _src(run).next_pending()
    assert not any("--query" in c for c in run.calls)


def test_queue_source_label_forces_the_old_rule_even_with_a_ready_lane():
    run = _queue_world(items=[_card(1, "Backlog"), _card(2, "Ready")],
                       issues=[_pissue(1), _pissue(2)])
    assert _src(run, queue_source="label").next_pending() == "1"


def test_a_board_disabled_repo_is_untouched():
    src = _mod("sources")
    run = _recording_runner({"list": json.dumps([_pissue(3), _pissue(9)])})
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "acme/widget"}}}, run=run)
    assert gh.next_pending() == "3"
    assert not any(c and c[0] == "project" for c in run.calls)


def test_a_failing_board_read_degrades_to_the_label_queue_rather_than_stalling():
    """Fail-open: the board is an optimisation of WHERE the queue lives, never a single point of
    failure for whether the loop can run at all."""
    base = _queue_world(items=[_card(2, "Ready")], issues=[_pissue(1), _pissue(2)])
    def boom(a):
        if a[:2] == ["project", "item-list"]:
            raise RuntimeError("missing `project` scope")
        return base(a)
    boom.calls = base.calls
    boom.state = base.state
    assert _src(boom).next_pending() == "1"          # label path, lowest number


# --- sync writes Ready, not Backlog --------------------------------------------------------------

def test_sync_cards_goal_issues_as_ready_because_they_are_by_definition_queued():
    """_sync_backlog only ever queries --label sdlc:goal, so everything it cards is already queued.
    Filing those under a column named Backlog is what made the board unreadable."""
    run = _queue_world(items=[], issues=[_pissue(5), _pissue(7)])
    gh = _src(run)
    _synced(gh).mark_in_progress("5")
    e = _edits(run)
    assert any(x["item"] == "PVTI_7" and x["option"] == "s_ready" for x in e)
    assert any(x["item"] == "PVTI_5" and x["option"] == "s_in_progress" for x in e)


def test_sync_still_uses_backlog_on_a_board_with_no_ready_option():
    run = _queue_world(items=[], issues=[_pissue(5), _pissue(7)], fields=STATUS_FILLED)
    gh = _src(run)
    _synced(gh).mark_in_progress("5")
    assert any(x["item"] == "PVTI_7" and x["option"] == "s_backlog" for x in _edits(run))


# --- a carded issue with NO Status at all ---------------------------------------------------------
# The third seedable case, and the only one that never self-healed. Measured on this repo's own
# board #6: 8 open cards blank on Status, five of them goal-labelled (#739-743) and carrying a
# correctly mirrored Priority — the same sync pass filled one column and skipped the other, because
# _mirror_priority handles its blank case explicitly and the status seed did not.

def test_sync_seeds_a_carded_issue_whose_status_is_blank():
    """Blank is neither `was_new` (the card exists) nor `Backlog` (the stale-lane case), so before
    the fix neither clause fired and the card stayed blank forever — unreachable by _board_queue,
    which matches on `status == ready_name`, and uncounted by _warn_unseeded, which only counts
    cards sitting in Backlog. Nothing in the system even reported it."""
    run = _queue_world(items=[_card(7, None)], issues=[_pissue(5), _pissue(7)])
    _synced(_src(run)).mark_in_progress("5")
    assert any(x["item"] == "PVTI_7" and x["option"] == "s_ready" for x in _edits(run))


def test_sync_leaves_a_card_that_already_has_a_lane_alone():
    """The other half of the same guard, and the reason the fix is narrow: seeding blank cannot lose
    a human's decision, but re-seeding a real lane would. In Progress / Blocked stay untouched."""
    run = _queue_world(items=[_card(7, "In Progress"), _card(8, "Blocked")],
                       issues=[_pissue(5), _pissue(7), _pissue(8)])
    _src(run).mark_in_progress("5")
    assert not any(x["item"] in ("PVTI_7", "PVTI_8") for x in _edits(run))


# --- #694: archive the card on completion --------------------------------------------------------
# Board hygiene, NOT performance: `item-list --query` filters server-side, so the queue read already
# scales with open items rather than board size (#693). 177 of this board's 268 cards are Done, which
# is noise for anyone looking at it and slows every unfiltered read and the UI.

def _archives(run):
    return [c for c in run.calls if c[:3] == ["api", "graphql", "-f"]
            and "archiveProjectV2Item" in " ".join(c)]


def test_completion_archives_the_card_when_enabled():
    run = _queue_world(items=[_card(5, "In Progress")], issues=[])
    gh = _src(run, archive_done=True)
    gh.complete("5")
    assert len(_archives(run)) == 1
    assert "PVTI_5" in " ".join(_archives(run)[0])


def test_archive_happens_after_the_done_status_is_set():
    """Order matters: archive first and the Done write could land on an archived item."""
    run = _queue_world(items=[_card(5, "In Progress")], issues=[])
    _src(run, archive_done=True).complete("5")
    seq = [" ".join(c) for c in run.calls]
    done_at = next(i for i, c in enumerate(seq) if "item-edit" in c and "s_done" in c)
    arch_at = next(i for i, c in enumerate(seq) if "archiveProjectV2Item" in c)
    assert done_at < arch_at


def test_a_failing_archive_never_fails_a_goal_that_genuinely_completed():
    base = _queue_world(items=[_card(5, "In Progress")], issues=[])
    def boom(a):
        if "archiveProjectV2Item" in " ".join(a):
            raise RuntimeError("missing `project` scope")
        return base(a)
    boom.calls = base.calls; boom.state = base.state
    _src(boom, archive_done=True).complete("5")            # must not raise
    assert any("issue close 5" in " ".join(c) for c in base.calls)


# --- backward compatibility ---

def test_absent_archive_done_key_archives_nothing():
    """An existing adopter's config has no `archive_done`, so their complete() is unchanged."""
    run = _queue_world(items=[_card(5, "In Progress")], issues=[])
    _src(run).complete("5")
    assert _archives(run) == []


def test_archive_done_false_archives_nothing():
    run = _queue_world(items=[_card(5, "In Progress")], issues=[])
    _src(run, archive_done=False).complete("5")
    assert _archives(run) == []


def test_archive_is_never_inferred_from_the_board_merely_being_enabled():
    """Archiving is user-visible and not bulk-reversible, so it must be an explicit opt-in and never
    ride along on another setting being on."""
    run = _queue_world(items=[_card(5, "In Progress")], issues=[])
    _src(run, queue_source="status").complete("5")
    assert _archives(run) == []


# --- #707: an un-seeded migration must not look like a drained backlog ---------------------------

def test_a_ready_lane_with_no_cards_but_goal_labelled_backlog_cards_warns(capsys):
    """The deadlock signature: Ready exists (so Status is authoritative) but nothing is in it, while
    goal-labelled cards sit in Backlog. That is an unfinished migration, not an empty queue, and the
    loop must say so rather than reporting DONE in silence."""
    run = _queue_world(items=[_card(1, "Backlog", "sdlc:goal"), _card(2, "Backlog", "sdlc:goal")],
                       issues=[])
    assert _src(run).next_pending() is None
    err = capsys.readouterr().err
    assert "Ready" in err and "board_migrate" in err


def test_a_genuinely_drained_board_warns_about_nothing(capsys):
    run = _queue_world(items=[_card(1, "Done", "sdlc:goal")], issues=[])
    assert _src(run).next_pending() is None
    assert "board_migrate" not in capsys.readouterr().err


def test_backlog_cards_without_the_goal_label_are_not_mistaken_for_an_unseeded_migration(capsys):
    """A Backlog card nobody labelled is just un-triaged work — warning about it would cry wolf."""
    run = _queue_world(items=[_card(1, "Backlog")], issues=[])
    assert _src(run).next_pending() is None
    assert "board_migrate" not in capsys.readouterr().err


def test_goal_cards_stranded_in_a_humans_own_lanes_warn_once_per_run(capsys):
    """#235 (review of PR #279, block #2): a board that GAINED a `Ready` lane while its goal cards
    sat in a human's own lanes (`Todo`, `Needs design`) or in no lane at all reads as an empty queue
    -- `_board_queue` picks only Ready, and the uncarded fallback only issues with NO card. Counting
    only Backlog cards left that silent (the loop reported DONE). Every open, eligible goal card
    outside Ready and outside the loop's own post-pick lanes is stranded; one warning per run."""
    run = _queue_world(items=[_card(5, "Todo", "sdlc:goal"), _card(6, "Needs design", "sdlc:goal"),
                              _card(8, None, "sdlc:goal"), _card(7, "Done", "sdlc:goal"),
                              _card(9, "Todo", "sdlc:goal", "sdlc:parked"), _card(10, "Todo")],
                       issues=[])
    src = _src(run)
    assert src.next_pending() is None
    err = capsys.readouterr().err
    assert "3 sdlc:goal card(s)" in err and "board_migrate" in err, err
    assert "'Todo'" in err and "'Needs design'" in err and "no Status" in err
    assert "--owner acme --project 4" in err
    assert src.next_pending() is None
    assert "board_migrate" not in capsys.readouterr().err             # once per run, not per pick


def test_goal_cards_in_the_loops_own_in_flight_lanes_are_not_stranded(capsys):
    """In Progress / QC / Blocked / Parked / Done are where the loop itself puts picked work: a goal
    card there is not a missed migration."""
    run = _queue_world(items=[_card(n, s, "sdlc:goal") for n, s in
                              enumerate(("In Progress", "QC", "Blocked", "Parked", "Done"), 1)],
                       issues=[])
    assert _src(run).next_pending() is None
    assert "board_migrate" not in capsys.readouterr().err


# --- #719: a real Priority field on the board, mirrored from the label ---------------------------
# priority:P* is a LABEL. The loop reads it for ordering (#698) but the board never knew about it,
# so there was no column to sort or group by and the only way to see it was to switch on `Labels`,
# which renders every label a card carries. The field is a DISPLAY MIRROR: the label stays the
# source of truth, nothing reads the field, and it must never be what a human edits.

def _pcard(number, status="Ready", priority=None):
    c = {"id": "PVTI_%d" % number, "status": status, "content": {"number": number}}
    if priority:
        c["priority"] = priority
    return c


# NOTE: distinct from the PRIORITY_FIELD fixture further up, which models an ADOPTER'S OWN
# Critical/High/Medium/Low field for the `custom_fields` feature. This one is the P0-P4 field
# sigma itself provisions (#719). Different concepts, so different names — the first version of
# this shadowed the other and broke a passing test.
SDLC_PRIORITY_FIELD = {"id": "F_sdlc_priority", "name": "Priority",
                       "type": "ProjectV2SingleSelectField",
                       "options": [{"id": "p_%s" % p.lower(), "name": p}
                                   for p in ("P0", "P1", "P2", "P3", "P4")]}


def _prio_edits(run):
    return [{"item": _arg(c, "--id"), "option": _arg(c, "--single-select-option-id")}
            for c in run.calls if c[:2] == ["project", "item-edit"]
            and _arg(c, "--field-id") == "F_sdlc_priority"]


def test_a_board_sigma_creates_gets_a_priority_field():
    src = _mod("sources")
    run = project_world(projects=[], issues=[])          # no board -> the kit creates one
    src.GitHubSource(_cfg(project={"enabled": True}), run=run).mark_in_progress("5")
    created = [c for c in run.calls if c[:2] == ["project", "field-create"]
               and _arg(c, "--name") == "Priority"]
    assert len(created) == 1
    assert _arg(created[0], "--single-select-options") == "P0,P1,P2,P3,P4"


def test_an_adopted_board_gets_no_priority_field_from_the_loop():
    """Same gate as the Status options: only a board the kit created. Adding a field to somebody
    else's board belongs in the opt-in migration, not in a loop tick."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED], issues=[])
    src.GitHubSource(_board(), run=run).mark_in_progress("5")
    assert not any(c[:2] == ["project", "field-create"] and _arg(c, "--name") == "Priority"
                   for c in run.calls)


def test_priority_field_disabled_creates_nothing_and_writes_nothing():
    src = _mod("sources")
    run = project_world(projects=[], issues=[_pissue(7, "priority:P1")])
    src.GitHubSource(_cfg(project={"enabled": True, "priority_field": False}),
                     run=run).mark_in_progress("5")
    assert not any(c[:2] == ["project", "field-create"] and _arg(c, "--name") == "Priority"
                   for c in run.calls)
    assert _prio_edits(run) == []


def test_sync_mirrors_the_label_onto_the_field():
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED, SDLC_PRIORITY_FIELD],
                        items=[_pcard(7, status="Ready")],
                        issues=[_pissue(7, "priority:P2")])
    _synced(src.GitHubSource(_board(), run=run)).mark_in_progress("5")
    assert {"item": "PVTI_7", "option": "p_p2"} in _prio_edits(run)


def test_sync_writes_nothing_when_the_field_already_agrees():
    """Diff-only, so steady state costs no API calls: item-list returns the value as a flattened
    key (verified live on project #6: {'status': 'Backlog', 'priority': 'P3'})."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED, SDLC_PRIORITY_FIELD],
                        items=[_pcard(7, status="Ready", priority="P2")],
                        issues=[_pissue(7, "priority:P2")])
    src.GitHubSource(_board(), run=run).mark_in_progress("5")
    assert _prio_edits(run) == []


def test_an_issue_with_no_priority_label_is_left_alone_not_blanked():
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED, SDLC_PRIORITY_FIELD],
                        items=[_pcard(7, status="Ready")], issues=[_pissue(7)])
    src.GitHubSource(_board(), run=run).mark_in_progress("5")
    assert _prio_edits(run) == []


def test_the_most_urgent_label_is_mirrored_when_an_issue_carries_several():
    """#381 on this repo carries both priority:P0 and priority:P1. The column and the queue must
    never disagree about which one wins, so both go through the same ranking."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED, SDLC_PRIORITY_FIELD],
                        items=[_pcard(7, status="Ready")],
                        issues=[_pissue(7, "priority:P1", "priority:P0")])
    _synced(src.GitHubSource(_board(), run=run)).mark_in_progress("5")
    assert {"item": "PVTI_7", "option": "p_p0"} in _prio_edits(run)


def test_a_board_without_a_priority_field_writes_no_priority():
    """Backward compatibility: the field lookup returning nothing IS the gate — no config needed."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED], items=[_pcard(7, status="Ready")],
                        issues=[_pissue(7, "priority:P2")])
    src.GitHubSource(_board(), run=run).mark_in_progress("5")
    assert _prio_edits(run) == []


# --- #1206: a card stranded at Done while its issue is open must never receive a Priority field
# write. GitHub has no "Item reopened" workflow (only "Item closed" exists), so once an issue is
# reopened its card just sits at Done for the rest of its life -- and _sync_backlog's per-issue
# loop used to call the priority mirror unconditionally for every open goal-labelled issue,
# regardless of the card's Status. Guarded at _write_priority_field itself (the one place the
# Priority field is ever written), so both the sync's own _mirror_priority path AND
# _promote_blockers' direct call are covered by the same check.

def test_sync_skips_the_priority_write_on_a_card_stranded_at_done(capsys):
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED, SDLC_PRIORITY_FIELD],
                        items=[_pcard(7, status="Done")],
                        issues=[_pissue(7, "priority:P2")])
    _synced(src.GitHubSource(_board(), run=run)).mark_in_progress("5")
    assert _prio_edits(run) == []
    err = capsys.readouterr().err
    assert "#7" in err and "Done" in err


def test_a_ready_card_next_to_a_stranded_one_still_gets_its_priority_written():
    """The guard is per-card, not a whole-sync abort: a Done-stranded card's neighbour in the same
    sync pass is untouched by it."""
    src = _mod("sources")
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED, SDLC_PRIORITY_FIELD],
                        items=[_pcard(7, status="Done"), _pcard(11, status="Ready")],
                        issues=[_pissue(7, "priority:P2"), _pissue(11, "priority:P1")])
    _synced(src.GitHubSource(_board(), run=run)).mark_in_progress("5")
    assert {"item": "PVTI_11", "option": "p_p1"} in _prio_edits(run)
    assert not any(e["item"] == "PVTI_7" for e in _prio_edits(run))


# --- #719b: the Priority FIELD is the source of truth, the label is the fallback -----------------
# Symmetric with #693's Status rule. Measured on project #6 before this: Ready cards sit in board
# order and priority was consulted nowhere, so two P0s (#254, #318) queued 10th and 11th behind
# eight P1s. Ordering is now (priority, board position): tiers first, drag order within a tier.

def _ready(number, priority=None, labels=("sdlc:goal",)):
    c = {"id": "PVTI_%d" % number, "status": "Ready", "labels": list(labels),
         "content": {"number": number}}
    if priority:
        c["priority"] = priority
    return c


def _pworld(cards, issues=()):
    return project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                         fields=[STATUS_FILLED_READY, SDLC_PRIORITY_FIELD],
                         items=cards, issues=list(issues))


STATUS_FILLED_READY = dict(STATUS_FILLED,
                           options=STATUS_FILLED["options"] + [{"id": "s_ready", "name": "Ready"}])


def test_priority_field_outranks_board_position():
    run = _pworld([_ready(9, "P2"), _ready(3, "P0")])
    assert _src(run).next_pending() == "3"


def test_board_position_still_breaks_ties_inside_a_priority_tier():
    """#693's drag-to-reorder survives — it just ranks WITHIN a tier now."""
    run = _pworld([_ready(9, "P1"), _ready(3, "P1")])
    assert _src(run).next_pending() == "9"          # 9 is first on the board


def test_card_rank_resolves_configured_aliases():
    """Second-round independent-review finding on #854: `_card_rank` is the BOARD-QUEUE pick path
    (`_board_queue()` is checked FIRST by `next_pending`, before `_pick_key`'s label-queue path ever
    runs) -- it never threaded `self.priority_aliases` through its own `discovery.priority_rank`
    call, so on any Ready-lane board (the sigma-init DEFAULT for new repos, queue_source="status")
    an alias-labelled card's REAL priority was silently ignored by the actual picking decision, even
    though `_pick_key`/`_mirror_priority` had already been made alias-aware. The single most
    consequential of the paths this feature touches, since it is live work-picking, not a display
    or plan artifact."""
    run = _pworld([_ready(9, "P2"), _ready(3, "Critical")])
    assert _src_aliases(run, {"critical": "P0"}).next_pending() == "3"


def test_card_rank_unset_aliases_leaves_english_priorities_unranked():
    run = _pworld([_ready(9, "P2"), _ready(3, "Critical")])
    assert _src(run).next_pending() == "9"          # "Critical" unrecognised -> sorts last


def test_a_blank_priority_field_falls_back_to_the_issue_label():
    """Forgiving direction: a card nobody has set a Priority on is ranked by its label rather than
    being treated as unprioritised, so no information is lost."""
    run = _pworld([_ready(9, "P2"), _ready(3, None, ("sdlc:goal", "priority:P0"))])
    assert _src(run).next_pending() == "3"


def test_the_field_wins_over_a_disagreeing_label():
    run = _pworld([_ready(9, "P0", ("sdlc:goal", "priority:P4")),
                   _ready(3, "P3", ("sdlc:goal", "priority:P0"))])
    assert _src(run).next_pending() == "9"


def test_an_unprioritised_ready_card_sorts_after_every_prioritised_one():
    run = _pworld([_ready(9, None, ("sdlc:goal",)), _ready(3, "P4")])
    assert _src(run).next_pending() == "3"


def test_a_board_with_no_priority_field_keeps_pure_board_order():
    """Backward compatibility: no Priority column means the lookup finds nothing and ordering is
    exactly what #693 shipped."""
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED_READY], items=[_ready(9), _ready(3)], issues=[])
    assert _src(run).next_pending() == "9"


# --- #1352: the board-authoritative half of "blocking work sorts first" -- distinct code path
# from `_pick_key`'s label-queue ranking (`_board_queue` runs FIRST, before the label queue is
# ever reached), so this needs its own coverage per the design doc's own adversarial review
# finding (§10.3): a fix to only one path would silently fail on whichever one a given repo's
# `queue_source` config actually routes through.

def _src_override(run, override=True, **project):
    """#1394: `override` is a parameter now, because the DEFAULT is on -- the interesting fixture is
    the one that turns it OFF."""
    p = {"enabled": True, "number": 4, "owner": "acme", **project}
    cfg = {"discovery": {"source": "github", "blocking_priority_override": override,
                        "github": {"repo": "acme/widget", "project": p}}}
    gh = _mod("sources").GitHubSource(cfg, run=run)
    gh._RETRY_BASE = 0
    gh._BACKLOG_READ_RETRY_BASE = 0
    return gh


def test_blocking_priority_override_sorts_a_low_priority_blocking_card_first():
    """A structural sort, not a priority nudge: a P3 card carrying sdlc:blocking must outrank a
    plain P0 card when the override is on."""
    run = _pworld([_ready(9, "P0"), _ready(3, "P0", ("sdlc:goal", "sdlc:blocking"))])
    assert _src_override(run).next_pending() == "3"      # #1409: same tier; a P3 no longer passes


def test_blocking_priority_override_can_be_turned_off_on_the_board_path_too():
    """#1394 inverted the default, so this test inverted with it: it now proves the OPT-OUT works on
    the board path, which is what an adopter who wants pure priority ordering needs. With the
    override explicitly off, a blocking card gets no special treatment and P0 wins -- byte-identical
    to the behaviour before the feature existed."""
    run = _pworld([_ready(9, "P0"), _ready(3, "P3", ("sdlc:goal", "sdlc:blocking"))])
    assert _src_override(run, override=False).next_pending() == "9"


def test_a_blocking_card_sorts_first_by_default_on_the_board_path():
    """The counterpart, and the default: the board path honours the override with no config at all,
    exactly as the label path does. A fix to only one of the two would silently fail on whichever
    path a repo happens to route through -- the finding this feature's own plan review raised."""
    run = _pworld([_ready(9, "P0"), _ready(3, "P0", ("sdlc:goal", "sdlc:blocking"))])
    assert _src(run).next_pending() == "3"


def test_blocking_priority_override_ties_among_blocking_cards_resolve_by_priority_then_position():
    """The override only decides the FIRST split; among multiple simultaneously-blocking cards,
    ordering is still (priority, board position) exactly as `_card_rank` already gives it."""
    run = _pworld([_ready(9, "P2", ("sdlc:goal", "sdlc:blocking")),
                  _ready(3, "P0", ("sdlc:goal", "sdlc:blocking"))])
    assert _src_override(run).next_pending() == "3"


def test_blocking_priority_override_does_not_promote_a_non_ready_blocking_card():
    """Structurally un-pickable columns stay that way regardless of the override -- this only ever
    reorders WITHIN the Ready lane, never reaches into In Progress/QC/Done/Blocked."""
    run = _queue_world(items=[_card(1, "In Progress", "sdlc:blocking", "sdlc:goal"),
                              _card(2, "Ready", "sdlc:goal")],
                       issues=[_pissue(1), _pissue(2)])
    assert _src_override(run).next_pending() == "2"


def test_blocking_priority_override_on_but_no_card_is_blocking_preserves_normal_order():
    run = _pworld([_ready(9, "P2"), _ready(3, "P0")])
    assert _src_override(run).next_pending() == "3"          # override on, nothing blocking -> pure priority order


def _src_override_aliases(run, aliases, **project):
    p = {"enabled": True, "number": 4, "owner": "acme", **project}
    cfg = {"discovery": {"source": "github", "blocking_priority_override": True,
                        "priority_aliases": aliases,
                        "github": {"repo": "acme/widget", "project": p}}}
    gh = _mod("sources").GitHubSource(cfg, run=run)
    gh._RETRY_BASE = 0
    gh._BACKLOG_READ_RETRY_BASE = 0
    return gh


def test_blocking_priority_override_composes_with_priority_aliases():
    """Two independently-legal config keys used together: the override still promotes a blocking
    card over an alias-labelled non-blocking one, proving the two features don't fight (the same
    class of interaction #813/#815/#854/#861 have each caught before in this exact area)."""
    run = _pworld([_ready(9, "Critical"), _ready(3, "Critical", ("sdlc:goal", "sdlc:blocking"))])
    assert _src_override_aliases(run, {"critical": "P0"}).next_pending() == "3"


def test_blocking_priority_override_board_mode_uncarded_fallback_still_finds_a_separate_non_blocking_goal():
    """#1352 review finding (BLOCKING, fixed): if the ONLY currently-blocking issue is carded but
    stuck in Backlog (not yet promoted to Ready -- a normal, transient state, since promotion only
    happens on a WRITE path this READ never takes), the uncarded fallback must still find a
    separate, genuinely pickable, uncarded, non-blocking goal -- never silently report "nothing
    pending" just because the blocking pool happened to be entirely carded already."""
    src = _mod("sources")
    stuck_blocking_issue = {"number": 50, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:blocking"}]}
    uncarded_goal = {"number": 99, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P0"}]}
    board_run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                              fields=[STATUS_FILLED_READY],
                              items=[_card(50, "Backlog")],   # carded, but NOT Ready
                              issues=[])

    def run(a):
        if _is_issues_list_call(a):
            labels = _rest_field(a, "labels")
            labels = labels.split(",") if labels else [a[i + 1] for i, x in enumerate(a) if x == "--label"]
            if "sdlc:blocking" in labels:
                return json.dumps([stuck_blocking_issue])
            return json.dumps([stuck_blocking_issue, uncarded_goal])
        return board_run(a)

    p = {"enabled": True, "number": 4, "owner": "acme"}
    cfg = {"discovery": {"source": "github", "blocking_priority_override": True,
                        "github": {"repo": "acme/widget", "project": p}}}
    gh = _mod("sources").GitHubSource(cfg, run=run)
    gh._RETRY_BASE = 0
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh.next_pending() == "99"


# --- #2264: feature_rank on the board-authoritative path, D-9/BR-9 of `.sdlc/design/2253.md` -----
# `_board_queue` is a completely separate sort key from `_pick_key` -- (is_blocking, _card_rank,
# pos, n) -- and the design doc's own text (quoting the code's docstring) records that fixing #2262
# on the label queue alone "would silently fail on whichever path a given repo's config routes
# through". This gives it the SAME term at the SAME depth: (is_blocking, _card_rank, feature_rank,
# pos, n). Zero extra calls -- the board read already returns each card's labels as bare strings --
# but D-9 discloses a real fidelity gap from the label-queue path on purpose: this path has no
# issue BODY, only the LABEL, and the label is attached AT PICK (`feature_labels.attach_at_pick`),
# so a genuine unit member that has never been picked resolves UNPRIORITISED here until its own
# first pick attaches the label. That is a disclosed, self-healing degradation -- deliberately NOT
# asserted as "both paths agree" (the design's own text: a passing test of that claim cannot be
# written, since the two paths' fidelity genuinely differs).

def _registry_with(d, **priorities):
    """Write one unit per `name=priority` kwarg into `d`'s `.sdlc/features/` registry (#2261's
    `priority` field). Mirrors `test_sources.py`'s own helper of the same name -- this file is
    hermetic and does not import across test modules."""
    fr = _mod("feature_registry")
    for name, priority in priorities.items():
        fr.write_unit(fr.registry_dir(d), name, {"priority": priority})


def _src_feature(run, sdlc_dir=None, enabled=True, **project):
    p = {"enabled": True, "number": 4, "owner": "acme", **project}
    cfg = {"discovery": {"source": "github", "feature_priority": {"enabled": enabled},
                        "github": {"repo": "acme/widget", "project": p}}}
    gh = _mod("sources").GitHubSource(cfg, run=run, sdlc_dir=sdlc_dir)
    gh._RETRY_BASE = 0
    gh._BACKLOG_READ_RETRY_BASE = 0
    return gh


def test_github_board_feature_priority_off_when_explicitly_disabled_is_byte_identical_to_before_2264():
    """THE CONTROL TEST (#2264, explicitly named in the issue; #2284 flipped the default to on, so
    this now tests the EXPLICIT escape hatch rather than "unset" -- see the sibling test below for
    that). Two Ready cards at the SAME `_card_rank` tier, from different units whose registry
    entries carry DIFFERENT priorities that WOULD reorder them if `feature_rank` engaged -- with
    `discovery.feature_priority.enabled` explicitly `false`, the board pick is exactly the
    pre-#2264 key, (is_blocking, _card_rank, pos, n): board position alone decides among the tied
    tier, unaffected by either unit's registered priority."""
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0", beta="P2")
        run = _pworld([_ready(9, "P1", ("sdlc:goal", "feature:beta")),
                      _ready(3, "P1", ("sdlc:goal", "feature:alpha"))])
        gh = _src_feature(run, sdlc_dir=d, enabled=False)
        assert gh.next_pending() == "9"          # board position wins: alpha's P0 has NO effect when off


def test_github_board_feature_priority_engages_by_default_when_unset():
    """#2284: THE NEW CONTROL TEST -- the default flipped to on. Same disambiguating pair, but the
    config carries NO `feature_priority` key at all (not even an explicit `true`) -- the
    higher-priority unit's card must still win the tie on the board-authoritative path too,
    proving the default itself engages the term here, not just on the label-queue side."""
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0", beta="P2")
        run = _pworld([_ready(9, "P1", ("sdlc:goal", "feature:beta")),
                      _ready(3, "P1", ("sdlc:goal", "feature:alpha"))])
        cfg = {"discovery": {"source": "github",
                             "github": {"repo": "acme/widget",
                                       "project": {"enabled": True, "number": 4, "owner": "acme"}}}}
        gh = _mod("sources").GitHubSource(cfg, run=run, sdlc_dir=d)
        gh._RETRY_BASE = 0
        gh._BACKLOG_READ_RETRY_BASE = 0
        assert gh.next_pending() == "3"          # alpha (P0) beats beta (P2) with NO config at all


def test_github_board_feature_rank_breaks_a_same_tier_tie_by_unit_priority():
    """Config ON: two Ready cards at the SAME `_card_rank` tier, from units with different
    registered priorities -- the higher-priority unit's card sorts first even though board position
    alone would have put it second (mirrors #2262's own label-queue tie-break test, exercised
    through `_board_queue` instead of `_pick_key`)."""
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0", beta="P2")
        run = _pworld([_ready(9, "P1", ("sdlc:goal", "feature:beta")),
                      _ready(3, "P1", ("sdlc:goal", "feature:alpha"))])
        gh = _src_feature(run, sdlc_dir=d, enabled=True)
        assert gh.next_pending() == "3"          # alpha (P0) beats beta (P2) despite worse position


def test_github_board_feature_rank_no_label_at_all_sorts_as_unranked_not_a_crash():
    """D-9's disclosed degradation, asserted directly. A genuine unit member that has never been
    picked carries NO `feature:*` label at all yet (`attach_at_pick` only writes it after a pick) --
    it must resolve UNPRIORITISED, sorting after a labelled member's real unit priority, without
    raising and without being mistaken for a member of any other unit."""
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0")
        run = _pworld([_ready(9, "P1", ("sdlc:goal", "feature:alpha")),
                      _ready(3, "P1", ("sdlc:goal",))])                 # no feature: label at all
        gh = _src_feature(run, sdlc_dir=d, enabled=True)
        assert gh.next_pending() == "9"          # alpha's real priority beats the UNPRIORITISED sentinel


def test_github_board_feature_rank_tuple_arity_matches_the_build():
    """#2264's own explicit flag: `_board_queue`'s sort tuple grows a 5th element, and its unpack
    has to grow alongside the build. Calling `_board_queue()` directly (not just through
    `next_pending`) exercises the exact `ready.sort()` / `[n for ..., n in ready]` line pair; a
    leftover 4-element unpack against a 5-element build raises `ValueError: too many values to
    unpack` immediately, so completing without raising -- with the correct order -- is itself the
    assertion, not merely an implicit side effect of the tie-break test above."""
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0", beta="P2")
        run = _pworld([_ready(9, "P1", ("sdlc:goal", "feature:beta")),
                      _ready(3, "P1", ("sdlc:goal", "feature:alpha"))])
        gh = _src_feature(run, sdlc_dir=d, enabled=True)
        ready, carded = gh._board_queue()
        assert ready == [3, 9]            # `_board_queue` itself returns ints; `next_pending` str()s them
        assert carded == {3, 9}


def test_github_board_feature_rank_never_overrides_the_cards_own_priority_tier():
    """D-3, restated on the board path at the SAME depth `_pick_key` places it: a genuine
    tie-break, never a replacement for the card's own priority tier. A P0 card in an unprioritised
    unit still beats a P1 card in the top-priority unit, because `feature_rank` sits AFTER
    `_card_rank` in the tuple, not before it."""
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0")
        run = _pworld([_ready(9, "P1", ("sdlc:goal", "feature:alpha")),   # top unit, but tier P1
                      _ready(3, "P0", ("sdlc:goal",))])                   # no unit, but tier P0
        gh = _src_feature(run, sdlc_dir=d, enabled=True)
        assert gh.next_pending() == "3"          # the P0 card wins regardless of unit priority


def test_github_board_feature_rank_degrades_gracefully_with_no_sdlc_dir():
    """Mirrors `test_github_feature_rank_degrades_gracefully_with_no_sdlc_dir` (#2262) for the
    board path: `GitHubSource(config, run=...)` with no `sdlc_dir` at all must resolve
    `feature_rank` to UNPRIORITISED rather than raising, even with the config on."""
    run = _pworld([_ready(9, "P1", ("sdlc:goal", "feature:alpha")),
                  _ready(3, "P1", ("sdlc:goal", "feature:beta"))])
    gh = _src_feature(run, sdlc_dir=None, enabled=True)
    assert gh.next_pending() == "9"          # no registry to read -> board position alone decides


def test_sync_brings_a_drifted_LABEL_into_line_with_the_field():
    """The mirror flips direction once the field decides: a human setting P0 on the board must not
    leave the label saying P2, or issue search and local mode disagree with the queue."""
    run = _pworld([_ready(7, "P0", ("sdlc:goal", "priority:P2"))],
                  issues=[_pissue(7, "priority:P2")])
    _synced(_src(run)).mark_in_progress("5")
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    # #895 slice 3a: the add and the remove are now two sequential REST calls (add FIRST), not one edit
    assert any("--add-label priority:P0" in e for e in edits) and any("--remove-label priority:P2" in e for e in edits)
    assert [i for i, e in enumerate(edits) if "--add-label priority:P0" in e][0] < \
        [i for i, e in enumerate(edits) if "--remove-label priority:P2" in e][0]


def test_sync_warns_loudly_when_the_field_overwrites_a_disagreeing_label(capsys):
    """#814: this correction is silent today — a board whose Priority FIELD is stale relative to a
    more-recently-updated LABEL (the normal shape right after a period the board wasn't the live
    interface, e.g. #652/#653/#654/#771/#812 on this repo's own board: label P1, field still P2)
    gets its label silently rewritten DOWN to match the stale field, with no signal an operator
    watching stderr — this file's own established convention (`_note_scope`, `_warn_unseeded`,
    `_warn_truncated`) — could ever notice. This does not change the field-wins precedence (a
    legitimate, documented steady-state choice); it only makes the correction visible."""
    run = _pworld([_ready(7, "P0", ("sdlc:goal", "priority:P2"))],
                  issues=[_pissue(7, "priority:P2")])
    _synced(_src(run)).mark_in_progress("5")
    err = capsys.readouterr().err
    assert "7" in err and "P0" in err and "P2" in err


def test_a_label_only_priority_is_honoured_and_not_reverted_when_the_board_is_unreachable():
    """#814: the read/pick path's own fallback is already correct — `_board_queue()` catches a
    `project` call's exception, warns via `_note_scope`, and returns None; `next_pending()` then
    uses `_first_by_label()` unconditionally, which never calls `_card_rank` or reads any board
    item at all. This guards that invariant explicitly: a label-only P0, with the board fully
    unreachable, must be picked (not silently treated as unprioritised) and nothing about the
    label may be rewritten as a side effect of merely picking (picking is read-only)."""
    def run(a):
        if a and a[0] == "project":
            raise RuntimeError("error: your token is missing the required scopes. missing: "
                               "'project'. run: gh auth refresh -s project")
        if _is_issues_list_call(a):
            return json.dumps([_pissue(3, "priority:P0")])
        return ""
    calls = []
    real = run
    def rec(a):
        calls.append(list(a)); return real(a)
    src = _mod("sources").GitHubSource(_cfg(repo="acme/widget", project={"enabled": True}), run=rec)
    src._RETRY_BASE = 0
    src._BACKLOG_READ_RETRY_BASE = 0
    assert src.next_pending() == "3"
    assert not any(c[:2] == ["issue", "edit"] for c in calls)   # a mere pick must mutate nothing


# --- priority_aliases: an adopter's own vocabulary (Critical/High/..., not just P0-P4) -----------

def _cfg_aliases(aliases, project=None, **gh):
    p = {"enabled": True, "number": 4, "owner": "acme", **(project or {})}
    return {"discovery": {"source": "github", "priority_aliases": aliases,
                          "github": {"repo": "acme/widget", "project": p, **gh}}}


def _src_aliases(run, aliases, **project):
    gh = _mod("sources").GitHubSource(_cfg_aliases(aliases, project=project), run=run)
    gh._RETRY_BASE = 0
    gh._BACKLOG_READ_RETRY_BASE = 0
    return gh


def test_an_unrecognised_field_value_no_longer_overwrites_a_real_label():
    """The root-cause bug, independent of whether aliases are configured at all: before this fix,
    ANY non-blank field value was treated as authoritative, so a field carrying leftover text from
    before sigma managed it (a stray "Critical", a typo, anything not P0-P4) would silently
    replace a genuinely correct priority:P0 label with an unparseable priority:Critical one on the
    very next sync -- sinking a real P0 to UNPRIORITISED. No aliases configured here on purpose:
    this must hold even with the feature entirely unused."""
    run = _pworld([_ready(7, "Critical", ("sdlc:goal", "priority:P0"))],
                  issues=[_pissue(7, "priority:P0")])
    _src(run).mark_in_progress("5")
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    assert not any("priority:" in e for e in edits)      # label must survive untouched


def test_field_and_label_agreeing_via_an_alias_writes_nothing():
    run = _pworld([_ready(7, "Critical", ("sdlc:goal", "priority:P0"))],
                  issues=[_pissue(7, "priority:P0")])
    _src_aliases(run, {"critical": "P0"}).mark_in_progress("5")
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    assert not any("priority:" in e for e in edits)


def test_field_wins_via_an_alias_writes_the_canonical_label_not_the_alias_spelling():
    """When the field really does disagree (once resolved through the alias), the correction must
    still write a clean priority:P<n> label -- never propagate the alias spelling into a label,
    which would defeat the "one vocabulary" point of the feature."""
    run = _pworld([_ready(7, "Critical", ("sdlc:goal", "priority:P2"))],
                  issues=[_pissue(7, "priority:P2")])
    _synced(_src_aliases(run, {"critical": "P0"})).mark_in_progress("5")
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    # #895 slice 3a: the add and the remove are now two sequential REST calls (add FIRST), not one edit
    assert any("--add-label priority:P0" in e for e in edits) and any("--remove-label priority:P2" in e for e in edits)
    assert [i for i, e in enumerate(edits) if "--add-label priority:P0" in e][0] < \
        [i for i, e in enumerate(edits) if "--remove-label priority:P2" in e][0]
    assert not any("Critical" in e for e in edits)


def test_sync_fills_an_alias_only_field_from_the_label_via_reverse_lookup():
    """An adopter who wants to keep Critical/High/Medium/Low as the field's own vocabulary, never
    adding P0-P4 at all, still gets a working label->field mirror: no literal "P0" option exists,
    but "Critical" is a configured alias for P0, so that option is used instead."""
    run = project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                        fields=[STATUS_FILLED_READY, PRIORITY_FIELD],
                        items=[{"id": "PVTI_7", "status": "Ready", "labels": ["sdlc:goal"],
                                "content": {"number": 7}}],
                        issues=[_pissue(7, "priority:P0")])
    aliases = {"critical": "P0", "high": "P1", "medium": "P2", "low": "P3"}
    _synced(_src_aliases(run, aliases)).mark_in_progress("5")
    edits = [{"item": _arg(c, "--id"), "option": _arg(c, "--single-select-option-id")}
            for c in run.calls if c[:2] == ["project", "item-edit"]
            and _arg(c, "--field-id") == "F_priority"]
    assert {"item": "PVTI_7", "option": "p_crit"} in edits


def test_sync_agreeing_field_and_label_warns_nothing(capsys):
    """The steady-state (no correction needed) case must stay silent — a warning on every sync,
    agreeing or not, would train operators to ignore it."""
    run = _pworld([_ready(7, "P2", ("sdlc:goal", "priority:P2"))],
                  issues=[_pissue(7, "priority:P2")])
    _src(run).mark_in_progress("5")
    assert capsys.readouterr().err == ""


def test_sync_fills_an_empty_field_from_the_label():
    run = _pworld([_ready(7, None, ("sdlc:goal", "priority:P2"))], issues=[_pissue(7, "priority:P2")])
    _synced(_src(run)).mark_in_progress("5")
    assert {"item": "PVTI_7", "option": "p_p2"} in _prio_edits(run)


def test_sync_touches_neither_side_when_they_already_agree():
    run = _pworld([_ready(7, "P2", ("sdlc:goal", "priority:P2"))], issues=[_pissue(7, "priority:P2")])
    _src(run).mark_in_progress("5")
    assert _prio_edits(run) == []
    assert not any("priority:" in " ".join(c) for c in run.calls if c[:2] == ["issue", "edit"])


# --- #719c: an issue the loop CREATES gets both sides set, at creation ---------------------------
# handoff.py writes `priority:<P>` as a label. Before this, the Priority column was filled only by
# the next _sync_backlog pass — and that pass only walks OPEN, GOAL-LABELLED issues, so a hand-off
# filed with goal_label=False kept a blank column forever while its label said P1.

def _handoff_world(fields):
    return project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                         fields=fields, items=[], issues=[])


def test_a_loop_created_issue_gets_its_priority_field_set_at_creation():
    src = _mod("sources")
    run = _handoff_world([STATUS_FILLED_READY, SDLC_PRIORITY_FIELD])
    gh = src.GitHubSource(_board(), run=run); gh._RETRY_BASE = 0
    num = gh.create_dependency("t", "b", "who", labels=("priority:P1", "area:loop"))
    assert num == "500"
    assert {"item": "PVTI_500", "option": "p_p1"} in _prio_edits(run)


def test_a_non_goal_labelled_handoff_still_gets_its_priority_field():
    """The case sync can never reach: _sync_backlog only walks goal-labelled issues."""
    src = _mod("sources")
    run = _handoff_world([STATUS_FILLED_READY, SDLC_PRIORITY_FIELD])
    gh = src.GitHubSource(_board(), run=run); gh._RETRY_BASE = 0
    gh.create_dependency("t", "b", "who", labels=("priority:P0",), goal_label=False)
    assert {"item": "PVTI_500", "option": "p_p0"} in _prio_edits(run)


def test_a_loop_created_issue_with_no_priority_label_stamps_nothing():
    src = _mod("sources")
    run = _handoff_world([STATUS_FILLED_READY, SDLC_PRIORITY_FIELD])
    gh = src.GitHubSource(_board(), run=run); gh._RETRY_BASE = 0
    gh.create_dependency("t", "b", "who", labels=("area:loop",))
    assert _prio_edits(run) == []


def test_a_board_with_no_priority_column_cards_nothing_extra_on_creation():
    """Backward compatibility: an adopted board has no Priority field, so the stamp is skipped
    BEFORE the issue would be carded — no new board writes for an existing adopter."""
    src = _mod("sources")
    run = _handoff_world([STATUS_FILLED])
    gh = src.GitHubSource(_board(), run=run); gh._RETRY_BASE = 0
    gh.create_dependency("t", "b", "who", labels=("priority:P1",))
    assert _prio_edits(run) == []
    assert not any(c[:2] == ["project", "item-add"] for c in run.calls)


# --- #720: the option rewrite must PRESERVE option ids, or it disables the board's workflows -----
# Measured on a throwaway project: GitHub enables all six built-in workflows on a new board, and an
# ID-LESS updateProjectV2Field turns five of them OFF — including "Item closed", the one that moves
# a card to Done when its issue closes. Same root cause as the card-Status wipe: an id-less rewrite
# DELETES and recreates the options, orphaning every workflow that points at one. That is what
# stranded 92 cards on project #6 and left five epics with no Status.

GH_DEFAULT_OPTIONS = [{"id": "o_todo", "name": "Todo"},
                      {"id": "o_ip", "name": "In Progress"},
                      {"id": "o_done", "name": "Done"}]
SIX = ["Backlog", "Ready", "In Progress", "QC", "Done", "Blocked"]


def _opt_entries(q):
    """[(name, id-or-None)] in the order the mutation emits them.

    Scoped to the singleSelectOptions list on purpose: a naive `\\{[^}]*\\}` over the whole query
    also swallows the outer `{fieldId: "F", singleSelectOptions: [...` and then reads `fieldId` as
    an option id, which made every option look like it had one. A helper that lenient would have
    reported this fix as working before it was written."""
    body = re.search(r"singleSelectOptions: \[(.*)\]", q).group(1)
    return [(re.search(r'name: "([^"]+)"', e).group(1),
             (re.search(r'\bid: "([^"]+)"', e).group(1) if re.search(r'\bid: "', e) else None))
            for e in re.findall(r"\{[^}]*\}", body)]


def test_an_existing_option_matched_by_name_keeps_its_id():
    src = _mod("sources")
    q = src.GitHubSource._options_mutation("F", SIX, GH_DEFAULT_OPTIONS)
    got = dict(_opt_entries(q))
    assert got["In Progress"] == "o_ip"
    assert got["Done"] == "o_done"


def test_a_case_only_difference_from_an_existing_option_claims_it_not_duplicates_it():
    """#1492: GitHub's own default Status options are `Todo` / `In progress` / `Done` — lowercase
    `p` — so a caller asking for our `In Progress` used to miss the exact-name lookup entirely,
    fall through every rule, and land on rule 3: a genuinely new option, i.e. a real second lane
    reading as the same thing to a human. Casefold-matching must claim the existing option's id
    AND keep GitHub's own spelling — never rename it out from under whoever is looking at the
    board — so exactly one option comes back, not two."""
    src = _mod("sources")
    existing = [{"id": "o_ip", "name": "In progress"}]
    entries = _opt_entries(src.GitHubSource._options_mutation("F", ["In Progress"], existing))
    assert entries == [("In progress", "o_ip")]


def test_an_unmatched_existing_option_is_RENAMED_rather_than_deleted():
    """GitHub's default first lane is `Todo` and ours is `Backlog`. Dropping Todo would delete its
    id, which is exactly what kills the workflows — so its id is reused for Backlog instead."""
    src = _mod("sources")
    got = dict(_opt_entries(src.GitHubSource._options_mutation(
        "F", SIX, GH_DEFAULT_OPTIONS, rename={"Todo": "Backlog"})))
    assert got["Backlog"] == "o_todo"


def test_genuinely_new_columns_carry_no_id():
    src = _mod("sources")
    got = dict(_opt_entries(src.GitHubSource._options_mutation("F", SIX, GH_DEFAULT_OPTIONS)))
    assert got["Ready"] is None and got["QC"] is None and got["Blocked"] is None


def test_no_existing_option_id_is_ever_dropped():
    """The property that actually protects the workflows AND every card's Status."""
    src = _mod("sources")
    q = src.GitHubSource._options_mutation("F", SIX, GH_DEFAULT_OPTIONS, rename={"Todo": "Backlog"})
    emitted = {i for _, i in _opt_entries(q) if i}
    assert {o["id"] for o in GH_DEFAULT_OPTIONS} <= emitted


def test_the_desired_columns_lead_and_any_kept_lane_trails():
    """Our six come first, in order, so the board reads correctly left to right. An existing lane we
    did not claim is appended rather than deleted — here GitHub's `Todo`, because no rename was
    given. With the rename the fresh-board path passes, `Todo` becomes Backlog and nothing trails."""
    src = _mod("sources")
    kept = _opt_entries(src.GitHubSource._options_mutation("F", SIX, GH_DEFAULT_OPTIONS))
    assert [n for n, _ in kept][:len(SIX)] == SIX
    assert ("Todo", "o_todo") in kept

    renamed = _opt_entries(src.GitHubSource._options_mutation(
        "F", SIX, GH_DEFAULT_OPTIONS, rename={"Todo": "Backlog"}))
    assert [n for n, _ in renamed] == SIX


def test_an_extra_existing_option_we_do_not_want_is_still_kept():
    """Never delete a lane a human added — deleting it would orphan every card sitting in it."""
    src = _mod("sources")
    existing = GH_DEFAULT_OPTIONS + [{"id": "o_theirs", "name": "Needs design"}]
    entries = _opt_entries(src.GitHubSource._options_mutation("F", SIX, existing))
    assert ("Needs design", "o_theirs") in entries


def test_with_no_existing_options_it_behaves_exactly_as_before():
    """Backward compatibility: the parameter is optional and defaults to the historical output."""
    src = _mod("sources")
    q = src.GitHubSource._options_mutation("F", SIX)
    assert all(i is None for _, i in _opt_entries(q))
    assert [n for n, _ in _opt_entries(q)] == SIX


def test_a_new_board_rewrite_sends_the_default_option_ids_back():
    """End to end through _ensure_board: the mutation the kit actually emits on a fresh board must
    carry GitHub's own option ids, not a bare list of names."""
    src = _mod("sources")
    run = project_world(projects=[], issues=[])          # no board -> created, options rewritten
    src.GitHubSource(_cfg(project={"enabled": True}), run=run).mark_in_progress("5")
    q = next(" ".join(c) for c in run.calls
             if c[:2] == ["api", "graphql"] and "updateProjectV2Field" in " ".join(c))
    for oid in ("o_todo", "o_ip", "o_done"):
        assert 'id: "%s"' % oid in q, oid


# --- #900 s1: blocker priority promotion -----------------------------------------------------
# Today the loop only PARKS a blocked issue -- its blocker's own priority is never touched, even
# when the blocker is the actual critical path (a P2 blocker sitting behind a P0 can sit at P2
# forever). Opt-in via discovery.blocker_promotion.mode ("off" default | "smart" | "always"):
# _sync_backlog runs the whole mechanism once per open sdlc:goal issue, on every board touch --
# the same cadence _mirror_priority already runs at, over the same fetched `issues`.

def _gissue(number, *labels, title="", body=""):
    """An open goal issue as `gh issue list --json number,labels,title,body` returns it once
    blocker_promotion is active -- a superset of _pissue's number,labels-only shape."""
    d = _pissue(number, *labels)
    d["title"], d["body"] = title, body
    return d


def _promo_world(issues, cards=(), fields=None):
    return project_world(projects=[{"number": 4, "id": "PVT_x", "title": "widget — SDLC"}],
                         fields=fields if fields is not None else [STATUS_FILLED_READY],
                         items=list(cards), issues=issues)


def _promo_src(run, mode="smart", sdlc_dir=None, **project):
    """Mirrors _src_aliases's own shape: a new, separate helper rather than growing _src itself,
    so none of _src's ~15 existing callers can be affected by a parameter they never pass."""
    p = {"enabled": True, "number": 4, "owner": "acme", **project}
    cfg = {"discovery": {"source": "github", "blocker_promotion": {"mode": mode},
                         "github": {"repo": "acme/widget", "project": p}}}
    gh = _mod("sources").GitHubSource(cfg, run=run, sdlc_dir=sdlc_dir)
    gh._RETRY_BASE = 0
    gh._BACKLOG_READ_RETRY_BASE = 0
    gh._NOTE_RETRY_BASE = 0
    return gh


def test_mode_unset_is_the_full_rule_and_explicit_off_is_byte_identical():
    """#1406: an unset mode is now the full rule ("always"): a P2 blocker behind a P0 is raised, with
    exactly ONE open-goal listing. An explicit "off" is what is byte-identical to before the feature
    existed: the same fixture makes zero promotion writes."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(5, "priority:P2")]
    run = _promo_world(issues)
    p = {"enabled": True, "number": 4, "owner": "acme"}
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget", "project": p}}}
    gh = _mod("sources").GitHubSource(cfg, run=run)
    gh._RETRY_BASE = 0
    _synced(gh).mark_in_progress("9")
    listings = [c for c in run.calls if _is_issues_list_call(c) and _rest_field(c, "state") == "open"]
    assert len(listings) == 1
    assert any(c[:2] == ["issue", "comment"] and c[2] == "5" for c in run.calls)
    off_run = _promo_world(issues)
    _synced(_promo_src(off_run, mode="off")).mark_in_progress("9")
    assert not any(c[:2] == ["issue", "comment"] for c in off_run.calls)
    assert not any(c[:2] == ["issue", "edit"] and "priority:" in " ".join(c) for c in off_run.calls)


def test_mode_off_explicitly_configured_makes_no_promotion_writes():
    issues = [_gissue(5, "priority:P2", body="Blocked by: #9"), _gissue(9, "priority:P0")]
    run = _promo_world(issues)
    gh = _promo_src(run, mode="off")
    gh.mark_in_progress("9")
    assert not any(c[:2] == ["issue", "comment"] for c in run.calls)
    assert not any(c[:2] == ["issue", "edit"] and "priority:" in " ".join(c) for c in run.calls)


def test_mode_smart_promotes_a_blocker_with_no_other_unblocked_work_at_the_dependents_tier():
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"),    # A: blocked by B(#5)
             _gissue(5, "priority:P2")]                             # B: blocks A, own rank P2
    run = _promo_world(issues)
    gh = _promo_src(run, mode="smart")
    _synced(gh).mark_in_progress("9")
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    assert any("issue edit 5" in e and "--add-label priority:P0" in e for e in edits)
    assert any("issue edit 5" in e and "--remove-label priority:P2" in e for e in edits)


def test_mode_smart_promotion_comment_matches_the_spec_wording_exactly():
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(5, "priority:P2")]
    run = _promo_world(issues)
    gh = _promo_src(run, mode="smart")
    _synced(gh).mark_in_progress("9")
    comments = [c for c in run.calls if c[:2] == ["issue", "comment"] and c[2] == "5"]
    assert comments, "no comment was posted on the promoted blocker"
    body = comments[0][comments[0].index("--body") + 1]
    assert body == "priority promoted P2 -> P0: blocks #9, which has no other unblocked P0 work right now"


def test_mode_always_promotion_comment_has_no_other_unblocked_work_clause():
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(11, "priority:P0"),
             _gissue(5, "priority:P2")]
    run = _promo_world(issues)
    gh = _promo_src(run, mode="always")
    _synced(gh).mark_in_progress("9")
    comments = [c for c in run.calls if c[:2] == ["issue", "comment"] and c[2] == "5"]
    assert comments
    body = comments[0][comments[0].index("--body") + 1]
    assert body == "priority promoted P2 -> P0: blocks #9"
    assert "no other unblocked" not in body


def test_promotion_from_no_priority_label_at_all_says_unprioritised():
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(5)]   # 5 carries no priority label
    run = _promo_world(issues)
    gh = _promo_src(run, mode="always")
    _synced(gh).mark_in_progress("9")
    comments = [c for c in run.calls if c[:2] == ["issue", "comment"] and c[2] == "5"]
    assert comments
    body = comments[0][comments[0].index("--body") + 1]
    assert body.startswith("priority promoted unprioritised -> P0:")


def test_mode_smart_does_not_promote_when_the_dependent_has_other_unblocked_work_at_its_tier():
    """The eligibility rule: a dependent that ALSO has other pickable work at its own tier gains
    nothing from the blocker being promoted, so smart mode withholds entirely (never merely caps)."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"),   # A: blocked by B
             _gissue(11, "priority:P0"),                          # another unblocked P0 -> withhold
             _gissue(5, "priority:P2")]                            # B: blocks A
    run = _promo_world(issues)
    gh = _promo_src(run, mode="smart")
    gh.mark_in_progress("9")
    assert not any(c[:2] == ["issue", "edit"] and "priority:" in " ".join(c) for c in run.calls)
    assert not any(c[:2] == ["issue", "comment"] and c[2] == "5" for c in run.calls)


def test_mode_always_promotes_even_when_the_dependent_has_other_unblocked_work():
    """"always" ignores has_other_unblocked_work entirely -- the discriminator between the two
    opt-in modes."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(11, "priority:P0"),
             _gissue(5, "priority:P2")]
    run = _promo_world(issues)
    gh = _promo_src(run, mode="always")
    _synced(gh).mark_in_progress("9")
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    assert any("issue edit 5" in e and "--add-label priority:P0" in e for e in edits)


def test_promotion_never_demotes_a_blocker_already_at_least_as_urgent():
    issues = [_gissue(9, "priority:P2", body="Blocked by: #5"), _gissue(5, "priority:P0")]
    run = _promo_world(issues)
    gh = _promo_src(run, mode="always")
    gh.mark_in_progress("9")
    assert not any(c[:2] == ["issue", "edit"] and "priority:" in " ".join(c) for c in run.calls)
    assert not any(c[:2] == ["issue", "comment"] for c in run.calls)


def test_transitive_chain_propagates_through_the_promoted_not_original_rank():
    """A(#30, P0) is blocked by B(#20, P3), itself blocked by C(#10, P4). Under "always", B
    promotes to P0 from A directly -- and C must ALSO promote to P0, via B's PROMOTED rank (not
    B's original P3), exactly as s0's docstring documents the composition: one call to
    blocker_promotion_rank per edge, feeding one level's returned rank in as the next level's
    own_rank."""
    issues = [_gissue(30, "priority:P0", body="Blocked by: #20"),
             _gissue(20, "priority:P3", body="Blocked by: #10"),
             _gissue(10, "priority:P4")]
    run = _promo_world(issues)
    gh = _promo_src(run, mode="always")
    _synced(gh).mark_in_progress("30")
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    assert any("issue edit 20" in e and "--add-label priority:P0" in e for e in edits)
    assert any("issue edit 10" in e and "--add-label priority:P0" in e for e in edits)


def test_a_blocking_cycle_does_not_hang_and_warns(capsys):
    """Hand-authored data must never infinite-loop: #5 blocked-by #9 and #9 blocked-by #5."""
    issues = [_gissue(5, "priority:P2", body="Blocked by: #9"),
             _gissue(9, "priority:P3", body="Blocked by: #5")]
    run = _promo_world(issues)
    gh = _promo_src(run, mode="always")
    _synced(gh).mark_in_progress("5")           # must return promptly, not hang or raise
    err = capsys.readouterr().err
    assert "cycle" in err.lower()


def test_a_marker_referencing_its_own_issue_number_is_ignored():
    issues = [_gissue(9, "priority:P0", body="Blocked by: #9")]
    run = _promo_world(issues)
    gh = _promo_src(run, mode="always")
    gh.mark_in_progress("9")             # must not raise or self-promote
    assert not any(c[:2] == ["issue", "edit"] and "priority:" in " ".join(c) for c in run.calls)


def test_a_marker_pointing_outside_the_open_goal_corpus_is_ignored():
    """#999 is not part of this open, sdlc:goal-labelled corpus at all (closed, not a goal, or
    simply doesn't exist) -- promoting it would need a `gh` call this mechanism is not budgeted
    for, so it is silently not a blocker candidate, exactly like _explicit_blockers()'s own
    open_refs precision rule."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #999")]
    run = _promo_world(issues)
    gh = _promo_src(run, mode="always")
    gh.mark_in_progress("9")             # must not raise
    assert not any(c[:2] == ["issue", "edit"] and "priority:" in " ".join(c) for c in run.calls)


def test_promotion_also_writes_the_board_priority_field_when_present():
    """Reuses the exact write primitive _mirror_priority itself writes the field through -- same
    option resolution (_option_for_rank fallback), same cache update."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(5, "priority:P2")]
    run = _promo_world(issues, cards=[_ready(5, "P2"), _ready(9, "P0")],
                       fields=[STATUS_FILLED_READY, SDLC_PRIORITY_FIELD])
    gh = _promo_src(run, mode="smart")
    _synced(gh).mark_in_progress("9")
    prio_edits = [{"item": _arg(c, "--id"), "option": _arg(c, "--single-select-option-id")}
                  for c in run.calls if c[:2] == ["project", "item-edit"]
                  and _arg(c, "--field-id") == "F_sdlc_priority"]
    assert {"item": "PVTI_5", "option": "p_p0"} in prio_edits


def test_promotion_skips_the_field_write_on_a_blocker_stranded_at_done(capsys):
    """#1206: `_promote_blockers` reaches the Priority field through the exact same
    `_write_priority_field` primitive `_mirror_priority` writes through (the assertion above) -- so
    the same Done-card guard has to cover this call site too, not just the sync's own. The label
    write + audit comment are a SEPARATE mechanism (`_write_priority_label`, tried independently in
    `_write_blocker_promotion`) and still land: only the board FIELD write is guarded."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(5, "priority:P2")]
    run = _promo_world(issues, cards=[_pcard(5, status="Done"), _ready(9, "P0")],
                       fields=[STATUS_FILLED_READY, SDLC_PRIORITY_FIELD])
    gh = _promo_src(run, mode="smart")
    _synced(gh).mark_in_progress("9")
    prio_edits = [{"item": _arg(c, "--id"), "option": _arg(c, "--single-select-option-id")}
                  for c in run.calls if c[:2] == ["project", "item-edit"]
                  and _arg(c, "--field-id") == "F_sdlc_priority"]
    assert prio_edits == []
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    assert any("issue edit 5" in e and "--add-label priority:P0" in e for e in edits)
    err = capsys.readouterr().err
    assert "#5" in err and "Done" in err


def test_ledger_handoff_channel_promotes_a_blocker_with_no_body_marker_at_all():
    """The SECOND, independent channel (#900): a recorded ledger hand-off names the blocker even
    with no "Blocked by" marker anywhere -- the channel that survives a park stripping sdlc:goal
    from the blocked issue, which the body-marker channel (scoped to _sync_backlog's own
    open+labelled corpus) cannot see once that has happened."""
    ledger = _mod("ledger")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"
        cfg_ledger = {"ledger": {"enabled": True, "actor": "test-bot"}}
        ledger.append(str(base), cfg_ledger, "handoff", "9", issue="5")
        issues = [_gissue(9, "priority:P0"), _gissue(5, "priority:P2")]
        run = _promo_world(issues)
        gh = _promo_src(run, mode="smart", sdlc_dir=str(base))
        _synced(gh).mark_in_progress("9")
        edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
        assert any("issue edit 5" in e and "--add-label priority:P0" in e for e in edits)


def test_without_sdlc_dir_the_ledger_channel_is_silently_unavailable_but_markers_still_work():
    """Every construction site that predates this slice (triage.py, most tests in this file)
    passes no sdlc_dir -- the mechanism must degrade to explicit-markers-only, never raise."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(5, "priority:P2")]
    run = _promo_world(issues)
    gh = _promo_src(run, mode="smart", sdlc_dir=None)
    _synced(gh).mark_in_progress("9")             # must not raise despite no sdlc_dir
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    assert any("issue edit 5" in e and "--add-label priority:P0" in e for e in edits)


def test_no_open_goal_issues_at_all_is_a_cheap_no_op():
    run = _promo_world([])
    gh = _promo_src(run, mode="always")
    gh.mark_in_progress("5")             # must not raise on an empty corpus
    assert not any(c[:2] == ["issue", "comment"] for c in run.calls)


def _promo_cfg(mode="smart", **project):
    p = {"enabled": True, "number": 4, "owner": "acme", **project}
    return {"discovery": {"source": "github", "blocker_promotion": {"mode": mode},
                          "github": {"repo": "acme/widget", "project": p}}}


def test_a_broken_ledger_read_degrades_to_the_explicit_marker_channel_alone(monkeypatch):
    """The ledger channel is a second, ADDITIVE source of edges -- if reading it blows up (a
    corrupt entry file, anything), the explicit "Blocked by #N" marker channel must still work,
    and the pass must not raise.

    `_mod("sources")` re-execs a brand-new module object on every call (no `sys.modules` caching,
    same as every other helper in this file) -- `_promo_src`'s OWN internal `_mod("sources")` call
    would build an entirely different module than one patched here, so this constructs GitHubSource
    directly off the SAME loaded reference the monkeypatch targets."""
    src_mod = _mod("sources")
    monkeypatch.setattr(src_mod.ledger, "read_all",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("corrupt ledger")))
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(5, "priority:P2")]
    run = _promo_world(issues)
    gh = src_mod.GitHubSource(_promo_cfg(), run=run, sdlc_dir="/nonexistent/.sdlc")
    gh._RETRY_BASE = 0
    _synced(gh).mark_in_progress("9")             # must not raise despite the ledger read blowing up
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    assert any("issue edit 5" in e and "--add-label priority:P0" in e for e in edits)


def test_a_broken_promotion_pass_does_not_break_the_rest_of_the_sync(monkeypatch):
    """Fully fail-open, matching every other board write in this class: a promotion-pass crash
    (here, simulated at the explicit-marker-scan's own module load) must not stop _sync_backlog's
    OWN status-seeding/mirroring work for the other issues in the same corpus. Same same-module-
    reference reasoning as the ledger test above."""
    src_mod = _mod("sources")
    monkeypatch.setattr(src_mod, "_get_backlog_check",
                        lambda: (_ for _ in ()).throw(RuntimeError("backlog_check exploded")))
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(5, "priority:P2")]
    run = _promo_world(issues)
    gh = src_mod.GitHubSource(_promo_cfg(), run=run)
    gh._RETRY_BASE = 0
    _synced(gh).mark_in_progress("9")             # must not raise
    # #5's own board status was still seeded by _sync_backlog's per-issue loop, unaffected
    assert any(e["item"] == "PVTI_5" and e["option"] == "s_ready" for e in _edits(run))


def test_one_blockers_write_failure_does_not_stop_another_blockers_promotion():
    """`_write_blocker_promotion` is independently try/excepted per blocker -- a transient gh
    error editing ONE blocker's label must not stop a DIFFERENT blocker, in the same pass, from
    being promoted (and must not propagate out of `_sync_backlog` at all)."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5\nBlocked by: #7"),
             _gissue(5, "priority:P2"), _gissue(7, "priority:P3")]
    base = _promo_world(issues)

    def flaky(a):
        if gqlfake.is_issue_write(a) and "/issues/5/labels" in a[1]:       # #895 slice 3a: REST label write
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return base(a)
    flaky.calls = base.calls
    flaky.state = base.state

    gh = _promo_src(flaky, mode="always")
    _synced(gh).mark_in_progress("9")             # must not raise despite #5's edit blowing up
    edits = [" ".join(c) for c in base.calls if c[:2] == ["issue", "edit"]]
    assert any("issue edit 7" in e and "--add-label priority:P0" in e for e in edits)


# --- independent review, round 2: two BLOCKING findings on #900 ------------------------------

def test_a_comment_failure_after_a_successful_label_write_is_loud_and_distinct(capsys):
    """Finding 2: the label write, field write, and comment post used to share ONE try/except --
    a comment-only failure was swallowed into the same generic, scope-gated `_note_scope` bucket
    as any other failure, which for a non-scope error (like this one) prints NOTHING at all.
    Proven exactly as the reviewer did: mock the runner so ONLY the comment call for the promoted
    blocker fails. The label write must still land (that part can't be prevented -- two network
    calls are never truly atomic), but the gap must now be reported LOUDLY and distinctly, naming
    the issue, so "the audit trail broke for #5" can never be confused with an ordinary swallowed
    failure.

    #1657: `note()` now retries a transient failure and falls back to a REST-shaped `gh api
    repos/.../issues/5/comments` call before giving up -- so the comment call for #5 must fail
    BOTH shapes to model genuine exhaustion; failing only the old GraphQL shape now has the REST
    fallback silently succeed against this fake, which would prove nothing about the loud-failure
    path this test exists to pin."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(5, "priority:P2")]
    base = _promo_world(issues)

    def flaky(a):
        if a[:2] == ["issue", "comment"] and a[2] == "5":
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        if a[:1] == ["api"] and len(a) > 1 and a[1].endswith("/issues/5/comments"):
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return base(a)
    flaky.calls = base.calls
    flaky.state = base.state

    gh = _promo_src(flaky, mode="always")
    _synced(gh).mark_in_progress("9")             # must not raise despite #5's comment blowing up

    # The label write still landed -- that part is expected and can't be prevented.
    edits = [" ".join(c) for c in base.calls if c[:2] == ["issue", "edit"]]
    assert any("issue edit 5" in e and "--add-label priority:P0" in e for e in edits)
    # No comment for #5 actually posted (the mocked call raised before it could be recorded).
    assert not any(c[:2] == ["issue", "comment"] and c[2] == "5" for c in base.calls)
    # The failure is reported LOUDLY and distinctly -- not silently swallowed.
    err = capsys.readouterr().err
    assert "#5" in err
    assert "label" in err.lower() and "comment" in err.lower()


def test_a_label_write_failure_posts_no_misleading_comment_and_stays_on_the_quiet_path(capsys):
    """The mirror image of the test above, and the reason `_write_blocker_promotion`'s three gh
    calls are independent WITHOUT being fully uncoupled: when the LABEL write itself fails, the
    priority never actually changed, so posting "priority promoted X -> Y" anyway would be a
    false claim -- worse than the silence #900 already accepts for an ordinary failure. This
    stays on the pre-existing quiet path (`_note_scope`, silent for a non-scope error), matching
    `_write_blocker_promotion`'s own pre-split behavior for this specific case."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"), _gissue(5, "priority:P2")]
    base = _promo_world(issues)

    def flaky(a):
        if gqlfake.is_issue_write(a) and "/issues/5/labels" in a[1]:       # #895 slice 3a: REST label write
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return base(a)
    flaky.calls = base.calls
    flaky.state = base.state

    gh = _promo_src(flaky, mode="always")
    gh.mark_in_progress("9")             # must not raise despite #5's label edit blowing up

    assert not any(c[:2] == ["issue", "comment"] and c[2] == "5" for c in base.calls)
    err = capsys.readouterr().err
    assert "#5" not in err              # no loud, issue-specific warning for an ordinary failure


def test_promotion_ignores_a_same_tier_issue_whose_card_is_not_actually_in_the_ready_lane():
    """Finding 3, the reviewer's exact 3-issue scenario: #921(P0) is blocked by #922(P2); a third
    #923(P0) is open and unblocked, but its card already sits in "In Progress" -- not pickable by
    `_board_queue` (the real picker), however open and unprioritised-by-blocking it looks. Smart
    mode must promote #922 exactly as if #923 didn't exist: #923 can never actually be picked
    next, so it grants #921 nothing."""
    issues = [_gissue(921, "priority:P0", body="Blocked by: #922"),
             _gissue(922, "priority:P2"),
             _gissue(923, "priority:P0")]
    run = _promo_world(issues, cards=[_pcard(921, status="Ready"), _pcard(922, status="Ready"),
                                      _pcard(923, status="In Progress")])
    gh = _promo_src(run, mode="smart")
    _synced(gh).mark_in_progress("921")
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    assert any("issue edit 922" in e and "--add-label priority:P0" in e for e in edits)
    comments = [c for c in run.calls if c[:2] == ["issue", "comment"] and c[2] == "922"]
    assert comments, "no comment was posted on the promoted blocker"
    body = comments[0][comments[0].index("--body") + 1]
    assert "923" not in body            # #923 was never the cause -- it granted #921 nothing


def test_an_uncarded_same_tier_issue_still_counts_as_other_unblocked_work():
    """The forgiving carve-out this fix deliberately keeps: an issue with NO board card at all
    (never synced) is NOT excluded from pickability, because it is exactly what this SAME
    `_sync_backlog` pass's own per-issue loop is about to seed into Ready. Without this
    carve-out, a fresh board (nothing carded yet) would make EVERY same-tier sibling look
    unpickable and over-promote every blocker on its first-ever sync."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"),
             _gissue(11, "priority:P0"),                          # uncarded -- still "other work"
             _gissue(5, "priority:P2")]
    run = _promo_world(issues)                                    # no cards at all
    gh = _promo_src(run, mode="smart")
    gh.mark_in_progress("9")
    assert not any(c[:2] == ["issue", "edit"] and "priority:" in " ".join(c) for c in run.calls)


def test_pickability_falls_back_to_the_label_rule_when_there_is_no_ready_lane_to_check():
    """`_ready_lane()` returns None whenever the board can't serve as the queue -- here,
    `queue_source: "label"` explicitly, even though the Status field DOES have a Ready option.
    With no Status concept the picker actually uses, the sane fallback is the historical rule,
    unchanged: a same-tier sibling still counts as "other work" even while its OWN card sits in
    the board's In Progress column, because the picker that will actually serve the pick in this
    mode (`_first_by_label`) has no notion of board status at all."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"),
             _gissue(11, "priority:P0"), _gissue(5, "priority:P2")]
    run = _promo_world(issues, cards=[_pcard(11, status="In Progress")])
    gh = _promo_src(run, mode="smart", queue_source="label")
    gh.mark_in_progress("9")
    assert not any(c[:2] == ["issue", "edit"] and "priority:" in " ".join(c) for c in run.calls)


def test_a_three_node_blocking_cycle_names_every_member_not_just_the_reentry_point(capsys):
    """The DFS used to record only the single ref it re-entered ON, not every node genuinely
    forming the cycle -- for #5 -> #7 -> #9 -> #5 that named just one of the three. All three
    must be named now."""
    issues = [_gissue(5, "priority:P2", body="Blocked by: #9"),
             _gissue(9, "priority:P3", body="Blocked by: #7"),
             _gissue(7, "priority:P4", body="Blocked by: #5")]
    run = _promo_world(issues)
    gh = _promo_src(run, mode="always")
    _synced(gh).mark_in_progress("5")           # must return promptly, not hang or raise
    err = capsys.readouterr().err
    for n in ("5", "7", "9"):
        assert ("#%s" % n) in err, err


def test_smart_mode_eligibility_uses_a_dependents_promoted_rank_not_its_raw_one():
    """Round-2 review finding: sources.py's has_other_unblocked_work compared a dependent against
    its RAW priority, while triage.py's already-reasoned fix (#900) uses the dependent's EFFECTIVE
    (promoted) rank for the identical question -- a genuine, empirically-reproduced divergence
    between what the live picker does and what /sigma-triage plan shows, on the same graph.

    Chain: C(#3, P4) blocks B(#5, P3) blocks A(#9, P0). B legitimately promotes to P0 (nothing
    else competes at A's tier). C should ALSO promote to P0, via B's PROMOTED rank -- but a sibling
    E(#7, P3), matching B's RAW (pre-promotion) tier and genuinely Ready, must NOT be allowed to
    block that: E has nothing to do with B's true (post-promotion) urgency. Pre-fix, E's raw-tier
    match wrongly withheld C's promotion entirely; C must promote here."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"),
             _gissue(5, "priority:P3", body="Blocked by: #3"),
             _gissue(3, "priority:P4"),
             _gissue(7, "priority:P3")]                      # sibling at B's RAW tier only
    run = _promo_world(issues, cards=[_pcard(7, status="Ready")])
    gh = _promo_src(run, mode="smart")
    _synced(gh).mark_in_progress("9")
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    assert any("issue edit 5" in e and "--add-label priority:P0" in e for e in edits), \
        "B should still promote to P0 (blocks A directly, nothing else competes at P0)"
    assert any("issue edit 3" in e and "--add-label priority:P0" in e for e in edits), \
        "C should promote to P0 via B's PROMOTED rank -- E sitting at B's raw P3 must not block this"


def test_a_stale_backlog_card_about_to_be_seeded_ready_this_pass_counts_as_pickable():
    """Round-2 review finding: the pickability check's forgiving treatment of an UNCARDED sibling
    (deliberate, #900 round 1) did not extend to a CARDED-but-stale-Backlog sibling that this exact
    sync pass is about to promote to Ready (the #707 stale_backlog case, `_sync_backlog`'s own
    per-issue loop, which runs before `_promote_blockers`). Both are the identical situation --
    "this pass is about to make it Ready" -- so both must be treated the same way.

    A(#9, P0) blocked by B(#5, P2). F(#11, P0) -- same tier as A -- is already carded but sitting
    stale in Backlog; this exact sync will seed it to Ready. Under "smart" mode, B should NOT
    promote: A already has other real (soon-to-be-Ready) P0 work in F, so promoting B would not
    change what gets worked next."""
    issues = [_gissue(9, "priority:P0", body="Blocked by: #5"),
             _gissue(11, "priority:P0"), _gissue(5, "priority:P2")]
    run = _promo_world(issues, cards=[_pcard(11, status="Backlog")])
    gh = _promo_src(run, mode="smart")
    gh.mark_in_progress("9")
    edits = [" ".join(c) for c in run.calls if c[:2] == ["issue", "edit"]]
    assert not any("issue edit 5" in e and "priority:" in e for e in edits), \
        "B should NOT promote -- F is real other P0 work, about to be seeded Ready this same pass"


# --- #1391 step 3b: LABELS DECIDE ELIGIBILITY, THE BOARD DECIDES ORDER ---------------------------
# The board lane used to decide eligibility entirely on its own (status == ready_name and nothing
# else), which INVERTED the label model on the shipped default config: every "do not pick me" label
# was ignored, the one eligibility label was not required, and sdlc:needs-confirmation -- the whole
# human-promotion gate -- was decoration. Both live repos are on the label queue, so this default
# path had zero production mileage.


def _ready_lbl(number, *labels, priority=None):
    c = {"id": "PVTI_%d" % number, "status": "Ready", "labels": list(labels),
         "content": {"number": number}}
    if priority:
        c["priority"] = priority
    return c


def test_board_ready_card_without_the_goal_label_is_not_picked():
    """The commonest fresh-board shape: a human drags an unlabelled card into Ready."""
    run = _pworld([_ready_lbl(9)])
    assert _src(run).next_pending() is None


def test_board_ready_card_carrying_parked_is_not_picked():
    run = _pworld([_ready_lbl(9, "sdlc:goal", "sdlc:parked")])
    assert _src(run).next_pending() is None


def test_board_ready_card_carrying_blocked_is_not_picked():
    run = _pworld([_ready_lbl(9, "sdlc:goal", "sdlc:blocked")])
    assert _src(run).next_pending() is None


def test_board_ready_card_carrying_needs_confirmation_is_not_picked():
    """The human-promotion gate. On the board path this label previously did nothing at all."""
    run = _pworld([_ready_lbl(9, "sdlc:goal", "sdlc:needs-confirmation")])
    assert _src(run).next_pending() is None


def test_board_ready_card_carrying_in_progress_is_NOT_picked():
    """#1393, reversing this test's own earlier position. It used to assert the opposite, on the
    grounds that "occupancy is the claim's job, not a label's" and that excluding in-progress here
    would rebuild an un-unstickable claim.

    Two things defeat that. First, `_fetch_pending` has excluded `sdlc:in-progress` since #1198 for
    a concrete reason -- an issue the loop had just claimed was being re-offered as the very next
    pick, oldest-first ordering actively favouring it -- so keeping the board path different meant
    the SAME issue was pickable or not depending only on which `queue_source` the config used.
    Second, the un-unstickable-claim worry is already answered, and answered identically on both
    paths: `_auto_reclaim_stale_claims` REMOVES the label once the ledger lease ages out. It
    operates on labels, so it frees a board-queued goal exactly as it frees a label-queued one.

    A `Ready` card whose issue still carries `sdlc:in-progress` means either the board write failed
    (the claim is real, and re-picking it is a double-dispatch) or a human dragged it back (for
    which `loop.py release` is the sanctioned undo, and it clears the label). Neither wants a
    second worker."""
    run = _pworld([_ready_lbl(9, "sdlc:goal", "sdlc:in-progress")])
    assert _src(run).next_pending() is None


def test_board_eligible_card_is_still_picked_and_board_order_still_decides():
    """Eligibility is the only thing added -- #693's drag-to-reorder must survive untouched."""
    run = _pworld([_ready_lbl(9, "sdlc:goal"), _ready_lbl(3, "sdlc:goal")])
    assert _src(run).next_pending() == "9"          # board position, not issue number


def test_board_priority_still_outranks_position_among_eligible_cards():
    run = _pworld([_ready_lbl(9, "sdlc:goal", priority="P2"),
                   _ready_lbl(3, "sdlc:goal", priority="P0")])
    assert _src(run).next_pending() == "3"


def test_board_skipping_an_ineligible_ready_card_is_announced(capsys):
    """A silent no-pick reads exactly like a broken loop -- which is how the inverted behaviour
    survived this long. It must be loud."""
    run = _pworld([_ready_lbl(9, "sdlc:goal", "sdlc:parked")])
    _src(run).next_pending()
    err = capsys.readouterr().err
    assert "#9" in err and "sdlc:parked" in err and "Ready" in err


def test_board_ineligible_card_does_not_block_an_eligible_one_behind_it():
    run = _pworld([_ready_lbl(9, "sdlc:goal", "sdlc:parked"), _ready_lbl(3, "sdlc:goal")])
    assert _src(run).next_pending() == "3"


# --- #1391 step 4: _set_board_status reports honestly ---------------------------------------------
# It used to return None unconditionally, making success, board-disabled, board-unresolvable and a
# swallowed exception all indistinguishable. A reconciler cannot be built on that.


def test_set_board_status_returns_true_when_the_write_lands():
    run = _pworld([_ready(7, "P1")], issues=[_pissue(7)])
    assert _src(run)._set_board_status("7", "Done") is True


def test_set_board_status_returns_false_when_the_board_is_disabled():
    src = _mod("sources")
    gh = src.GitHubSource(_cfg(project={"enabled": False}), run=project_world())
    assert gh._set_board_status("7", "Done") is False


def test_set_board_status_returns_false_when_the_board_call_fails():
    def run(a):
        if a and a[0] == "project":
            raise RuntimeError("error: your token is missing the required scopes. missing: 'project'")
        return ""
    gh = _mod("sources").GitHubSource(_cfg(project={"enabled": True}), run=run)
    gh._RETRY_BASE = 0
    assert gh._set_board_status("7", "Done") is False


def test_set_board_status_still_never_raises():
    """Fail-open is unchanged -- only the return value is new."""
    def run(a):
        raise RuntimeError("boom")
    gh = _mod("sources").GitHubSource(_cfg(project={"enabled": True}), run=run)
    gh._RETRY_BASE = 0
    assert gh._set_board_status("7", "Done") is False          # must not raise


def test_both_queue_paths_now_agree_on_every_not_eligible_label():
    """#1393: the eligibility rule is stated ONCE (`not_eligible_labels`) because an audit found it
    hand-written in six places, five of which had drifted. This pins the two that decide picks."""
    run = _pworld([_ready_lbl(9, "sdlc:goal")])
    src = _src(run)
    for label in src.not_eligible_labels():
        assert src._card_is_eligible(1, {"sdlc:goal", label}) is False, label
    assert src._card_is_eligible(1, {"sdlc:goal"}) is True


# --- #895 slice 3a: `_write_priority_label` is REST, add FIRST then one DELETE per stale label ---------

def _prio_src(fail_remove=False):
    """A real GitHubSource whose REST writes hit a label SET (the shim applies add/remove to it)."""
    labels, raw = {"priority:P0", "priority:P2"}, []

    def run(a):
        if gqlfake.is_issue_write(a):
            raw.append(list(a))
            if fail_remove and a[3] == "DELETE":
                raise RuntimeError("gh: HTTP 502 Bad Gateway")
            return gqlfake.rest_write(a, labels=labels, repo_args=("--repo", "acme/widget"))
        return ""
    gh = _mod("sources").GitHubSource(
        {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}}, run=run)
    return gh, labels, raw


def _lbls(*names):
    return [{"name": n} for n in names]


def test_priority_label_adds_before_removes():
    gh, labels, raw = _prio_src()
    gh._write_priority_label(7, "P1", _lbls("priority:P0", "priority:P2", "sdlc:goal"))
    assert [(c[3], c[1].rsplit("/", 1)[-1]) for c in raw] == [
        ("POST", "labels"), ("DELETE", "priority%3AP0"), ("DELETE", "priority%3AP2")]
    assert "labels[]=priority:P1" in raw[0] and sum(1 for x in raw[0] if str(x).startswith("labels[]=")) == 1
    assert labels == {"priority:P1"}


def test_priority_label_add_is_exactly_the_canonical_label_never_the_alias():
    gh, _, raw = _prio_src()
    gh._write_priority_label(7, "P0", _lbls("priority:P2"))
    assert [x for x in raw[0] if str(x).startswith("labels[]=")] == ["labels[]=priority:P0"]


def test_a_failing_remove_raises_and_leaves_the_add_done_both_self_heal_directions():
    """Partial failure is visible (two priority labels) and the call RAISES (`_mirror_priority` catches it
    into `on_error`). DEMOTION (new P2 over stale P0): the stale higher label still wins `_priority_name`,
    so the field/label disagreement persists and the next pass retries. PROMOTION (new P0 over stale P2):
    the effective priority is already right, nothing detects the stale lower label (asserted, not hidden)."""
    import pytest
    for canon, stale, want_after in (("P2", "priority:P0", "P0"), ("P0", "priority:P2", "P0")):
        gh, labels, _ = _prio_src(fail_remove=True)
        labels.clear(); labels.add(stale)
        with pytest.raises(Exception):
            gh._write_priority_label(7, canon, _lbls(stale))
        assert labels == {stale, "priority:" + canon}                     # the add landed, the remove did not
        assert gh._priority_name(_lbls(*labels)) == want_after
    # demotion: effective priority P0 != the desired P2 -> disagreement persists (retry next pass)
    # promotion: effective priority P0 == the desired P0 -> undetected, stale lower label stays


# --- #1406: the full rule through the real `_sync_backlog` gesture ----------------------------

def _with_outside(run, outside):
    """Answer a single-issue REST read for issues outside the listing, `outside` maps number ->
    (state, [labels]) or an Exception to raise. Everything else goes to the world fake."""
    reads = []

    def wrapped(a):
        m = re.match(r"repos/[^/]+/[^/]+/issues/(\d+)$", str(a[1])) if a[:1] == ["api"] and len(a) > 1 else None
        if m and int(m.group(1)) in outside:
            reads.append(int(m.group(1)))
            run.calls.append(list(a))
            v = outside[int(m.group(1))]
            if isinstance(v, Exception):
                raise v
            return json.dumps({"number": int(m.group(1)), "state": v[0], "comments": 0,
                               "labels": [{"name": l} for l in v[1]]})
        return run(a)

    wrapped.calls, wrapped.state, wrapped.reads = run.calls, run.state, reads
    return wrapped


def test_promotion_default_raises_a_non_goal_member_blocker_through_sync_backlog(capsys):
    issues = [_gissue(9, "priority:P0", body="Blocked by: #77")]
    run = _with_outside(_promo_world(issues), {77: ("open", ["priority:P3"])})
    p = {"enabled": True, "number": 4, "owner": "acme"}
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget", "project": p}}}
    gh = _mod("sources").GitHubSource(cfg, run=run)
    gh._RETRY_BASE = 0
    _synced(gh).mark_in_progress("9")
    assert run.reads == [77]                    # one bounded read, not a listing
    assert any(c[:2] == ["issue", "comment"] and c[2] == "77" for c in run.calls)
    assert any("77" in " ".join(c) and "priority:P0" in " ".join(c) for c in run.calls
               if c[:2] == ["issue", "edit"])


def test_promotion_never_treats_a_confirmation_waiting_issue_as_a_blocker():
    issues = [_gissue(9, "priority:P0", body="Blocked by: #77"),
              _gissue(8, "priority:P3", "sdlc:needs-confirmation"),
              _gissue(7, "priority:P0", body="Blocked by: #8")]
    run = _with_outside(_promo_world(issues), {77: ("open", ["sdlc:needs-confirmation", "priority:P3"])})
    p = {"enabled": True, "number": 4, "owner": "acme"}
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget", "project": p}}}
    gh = _mod("sources").GitHubSource(cfg, run=run)
    gh._RETRY_BASE = 0
    _synced(gh).mark_in_progress("9")
    assert not any(c[:2] == ["issue", "comment"] for c in run.calls)
    assert not any(c[:2] == ["issue", "edit"] and "priority:" in " ".join(c) for c in run.calls)


def test_promotion_reports_an_unreadable_or_cross_repo_blocker_on_stderr(capsys):
    issues = [_gissue(9, "priority:P0", body="Blocked by: #77\nBlocked by: other/repo#5")]
    run = _with_outside(_promo_world(issues), {77: RuntimeError("boom")})
    p = {"enabled": True, "number": 4, "owner": "acme"}
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget", "project": p}}}
    gh = _mod("sources").GitHubSource(cfg, run=run)
    gh._RETRY_BASE = 0
    _synced(gh).mark_in_progress("9")
    err = capsys.readouterr().err
    assert "#77" in err and "other repository" in err
    assert not any(c[:2] == ["issue", "comment"] for c in run.calls)
