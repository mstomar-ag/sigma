"""#1129: the opt-in auto-unpark sweep (auto_unpark.py) -- re-examines `sdlc:parked` GitHub issues
whose recorded `blocked by #N` target has since closed, and re-adds `sdlc:goal`."""
import json, pathlib, importlib.util

import gqlfake

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


LIST_GET = "/issues --method GET"     # #895 2c: the REST issues-LIST argv (the single-issue read is /issues/<n>)


def rest_list_calls(run):
    """The REST list reads the runner saw (one per label per page)."""
    return [c for c in run.calls if gqlfake.rest_list_params(c) is not None]


def _config(**gh):
    return {"discovery": {"source": "github", "github": {"repo": "acme/widget", **gh}}}


def _local_config():
    return {"discovery": {"source": "local-goals"}}


def _blocked_issue(number, title="", body="", labels=("sdlc:blocked",)):
    return {"number": number, "title": title, "body": body,
           "labels": [{"name": n} for n in labels]}


def _parked_issue(number, title="", body="", labels=("sdlc:parked",)):
    """A HUMAN-parked issue. #1394: the sweep no longer resumes these at all, so this fixture now
    exists to prove the skip, not to drive an unpark."""
    return {"number": number, "title": title, "body": body,
           "labels": [{"name": n} for n in labels]}


def _sweep_runner(parked="[]", states=None, state_reasons=None, comments=None, fail_on=(), list_fail=None,
                  fallback_list="[]"):
    """Fake `gh` runner covering every call shape the sweep makes:
    - `issue list --label sdlc:parked ...`               -> returns `parked` (a JSON array string)
    - `issue view <n> ... --json state,stateReason`       -> `{"state": ..., "stateReason": ...}`
    - `issue view <n> ... --json comments`                -> comments.get(n, []), fetch_comments' own shape
    - anything else (label create, issue edit, issue comment) -> recorded, returns "" (success)
    `fail_on`: substrings of the joined args that make that ONE call raise instead.

    `state_reasons`: explicit per-ref `stateReason` override (`#1186` -- real GitHub always
    populates this for a genuinely CLOSED issue, "COMPLETED"/"NOT_PLANNED", but leaves it empty for
    a PR of any state including CLOSED-without-merge -- see `_ref_is_open`). Defaults to
    `"COMPLETED"` for any ref whose `states` entry is `"CLOSED"` (the common, plain-closed-issue
    case every pre-#1186 test already assumes) and `""` otherwise, so existing callers that never
    pass `state_reasons` at all keep their original, pre-#1186 behaviour unchanged; a test that
    specifically wants to model a closed-but-unmerged PR passes `state_reasons={n: ""}` explicitly."""
    calls = []
    states = states or {}
    state_reasons = state_reasons or {}
    comments = comments or {}
    labels = set()
    fallbacks = []
    list_fallbacks = []

    def run(args):
        joined = " ".join(str(a) for a in args)
        for needle in fail_on:
            if needle in joined:
                raise RuntimeError(f"simulated gh failure: {needle}")
        # #1392: the unpark is now ONE `swap-label` action applied via `sources._swap_labels`
        # (`gh api graphql`), not an `add-label`/`remove-label` pair of `gh issue edit` calls.
        # `gqlfake.swap` answers that transport and records the equivalent synthetic edit calls, so
        # this fixture's existing "which labels did the sweep write" assertions still read the same
        # way. `fail_on` is checked BEFORE it, so a test simulating a failed label write still can.
        gql = gqlfake.swap(args, labels=labels, calls=calls, repo_args=("--repo", "o/r"))
        if gql is not None:
            return gql
        if gqlfake.is_issue_write(args):                # #895: REST writes, legacy-recorded
            return gqlfake.rest_write(args, calls=calls, repo_args=("--repo", "o/r"))
        calls.append(list(args))
        params = gqlfake.rest_list_params(args)
        if params is not None:                  # #895 2c: the list read is REST first
            if list_fail is not None:
                raise list_fail
            # #1351: the `sdlc:blocking` query (_fetch_blocking_issues, "what's currently labeled
            # blocking") is a DIFFERENT concept from `parked` -- returning the same canned `parked`
            # payload for it would make every parked issue look currently blocking.
            return "[]" if params.get("labels") == "sdlc:blocking" else parked
        if len(args) >= 2 and args[0] == "issue" and args[1] == "list":
            list_fallbacks.append(list(args))   # #895 2c: the ONE `gh issue list` fallback
            return fallback_list
        rest = gqlfake.rest_issue(args, lambda n, f: {      # #895: issue reads are REST first
            "state": states.get(n, "OPEN"),
            "stateReason": state_reasons.get(n, "COMPLETED" if states.get(n) == "CLOSED" else ""),
            "comments": [
                {"id": f"c{i}", "author": {"login": "x"}, "body": b,
                 "createdAt": "2026-01-01T00:00:00Z", "authorAssociation": "OWNER"}
                for i, b in enumerate(comments.get(n, []))]})
        if rest is not None:
            return rest
        if len(args) >= 3 and args[0] == "issue" and args[1] == "view":
            fallbacks.append(list(args))     # #895: the ONE fallback; a REST-first test asserts this empty
            n = args[2]
            json_field = args[args.index("--json") + 1] if "--json" in args else ""
            if json_field.startswith("state"):
                state = states.get(n, "OPEN")
                default_reason = "COMPLETED" if state == "CLOSED" else ""
                return json.dumps({"state": state,
                                   "stateReason": state_reasons.get(n, default_reason)})
            if json_field == "comments":
                bodies = comments.get(n, [])
                return json.dumps({"comments": [
                    {"id": f"c{i}", "author": {"login": "x"}, "body": b,
                     "createdAt": "2026-01-01T00:00:00Z", "authorAssociation": "OWNER"}
                    for i, b in enumerate(bodies)]})
            return "{}"
        return ""
    run.calls = calls
    run.fallbacks = fallbacks
    run.list_fallbacks = list_fallbacks
    return run


# --------------------------------------------------------------------------- _fetch_parked_issues


def test_fetch_parked_issues_parses_the_list_call():
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(
        parked=json.dumps([_blocked_issue(42, title="t", body="b")])))
    issues = au._fetch_parked_issues(source)
    assert issues == [_blocked_issue(42, title="t", body="b")]


def test_fetch_parked_issues_queries_by_the_parked_label_and_open_state():
    au = _mod("auto_unpark")
    run = _sweep_runner()
    source = au.sources.GitHubSource(_config(), run=run)
    au._fetch_parked_issues(source)
    call = rest_list_calls(run)[0]
    p = gqlfake.rest_list_params(call)
    assert p["labels"] == "sdlc:parked" and p["state"] == "open"
    assert p["sort"] == "created" and p["direction"] == "desc"      # newest first, like `gh issue list`
    assert run.list_fallbacks == []


def test_fetch_parked_issues_fails_open_on_gh_error():
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(fail_on=[LIST_GET]))
    assert au._fetch_parked_issues(source) == []


def test_fetch_parked_issues_fails_open_on_malformed_json():
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(parked="not json"))
    assert au._fetch_parked_issues(source) == []


def test_fetch_parked_issues_fails_open_on_non_list_payload():
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(parked=json.dumps({"oops": 1})))
    assert au._fetch_parked_issues(source) == []


# --- #1351 review finding (BLOCKING): `_fetch_parked_issues` alone cannot tell "genuinely nothing
# parked" apart from "the fetch failed" -- `_fetch_parked_issues_status` adds that signal so
# `sweep_unpark` can skip the (destructive-if-wrong) `sdlc:blocking` removal computation whenever
# either query didn't actually succeed, rather than treating a wrongly-empty result as truth.


def test_fetch_parked_issues_status_reports_complete_when_both_queries_succeed():
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(
        parked=json.dumps([_blocked_issue(42)])))
    issues, complete = au._fetch_parked_issues_status(source)
    assert [i["number"] for i in issues] == [42]
    assert complete is True


def test_fetch_parked_issues_status_reports_incomplete_when_both_queries_fail():
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(fail_on=[LIST_GET]))
    issues, complete = au._fetch_parked_issues_status(source)
    assert issues == []
    assert complete is False


def test_fetch_parked_issues_status_is_incomplete_even_if_only_one_of_two_queries_fails():
    au = _mod("auto_unpark")
    run = _label_aware_sweep_runner(
        by_label={"sdlc:blocked": json.dumps([_blocked_issue(42, labels=("sdlc:blocked",))])},
        fail_on=["labels=sdlc:parked"])
    source = au.sources.GitHubSource(_config(), run=run)
    issues, complete = au._fetch_parked_issues_status(source)
    # the OTHER query's real results are still returned (compute_unpark_actions' own partial-data
    # tolerance, unchanged) -- only `complete` reports the fetch wasn't fully trustworthy this pass
    assert [i["number"] for i in issues] == [42]
    assert complete is False


def test_fetch_parked_issues_status_is_incomplete_on_a_non_list_payload():
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(parked=json.dumps({"oops": 1})))
    issues, complete = au._fetch_parked_issues_status(source)
    assert issues == []
    assert complete is False


def test_fetch_parked_issues_is_the_issues_only_view_of_status():
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(
        parked=json.dumps([_blocked_issue(42)])))
    assert au._fetch_parked_issues(source) == au._fetch_parked_issues_status(source)[0]


def _label_aware_sweep_runner(by_label=None, states=None, state_reasons=None, comments=None,
                              fail_on=(), fail_labels=(), list_fail=None, fallback_list="[]"):
    """Like `_sweep_runner` above but distinguishes an `issue list --label X` call by the actual
    value of `X` -- needed to prove #1358's fix genuinely queries BOTH `sdlc:parked` and
    `sdlc:blocked` as two independently-filtered calls. `_sweep_runner` itself returns the same
    `parked=` payload for ANY `issue list` call regardless of which label was requested, which
    cannot tell a real per-label GitHub server-side filter apart from one that was never applied
    at all -- exactly the gap this helper exists to make provable."""
    calls = []
    by_label = by_label or {}
    states = states or {}
    state_reasons = state_reasons or {}
    comments = comments or {}

    labels = set()
    fallbacks = []
    list_fallbacks = []

    def run(args):
        joined = " ".join(str(a) for a in args)
        for needle in fail_on:
            if needle in joined:
                raise RuntimeError(f"simulated gh failure: {needle}")
        # #1392: the unpark is now ONE `swap-label` action applied via `sources._swap_labels`
        # (`gh api graphql`), not an `add-label`/`remove-label` pair of `gh issue edit` calls.
        # `gqlfake.swap` answers that transport and records the equivalent synthetic edit calls, so
        # this fixture's existing "which labels did the sweep write" assertions still read the same
        # way. `fail_on` is checked BEFORE it, so a test simulating a failed label write still can.
        # #1393: a label write is a graphql swap carrying opaque node ids, so "make THIS label's
        # write fail" is expressed by NAME via `gqlfake.labels_in` rather than by matching argv.
        if any(l in fail_labels for l in gqlfake.labels_in(args)):
            raise RuntimeError("simulated gh failure: label write %s" % sorted(fail_labels))
        gql = gqlfake.swap(args, labels=labels, calls=calls, repo_args=("--repo", "o/r"))
        if gql is not None:
            return gql
        if gqlfake.is_issue_write(args):                # #895: REST writes, legacy-recorded
            return gqlfake.rest_write(args, calls=calls, repo_args=("--repo", "o/r"))
        calls.append(list(args))
        params = gqlfake.rest_list_params(args)
        if params is not None:                  # #895 2c: the list read is REST first
            if list_fail is not None:
                raise list_fail
            return by_label.get(params.get("labels"), "[]")
        if len(args) >= 2 and args[0] == "issue" and args[1] == "list":
            list_fallbacks.append(list(args))   # #895 2c: the ONE `gh issue list` fallback
            return fallback_list
        rest = gqlfake.rest_issue(args, lambda n, f: {      # #895: issue reads are REST first
            "state": states.get(n, "OPEN"),
            "stateReason": state_reasons.get(n, "COMPLETED" if states.get(n) == "CLOSED" else ""),
            "comments": [
                {"id": f"c{i}", "author": {"login": "x"}, "body": b, "createdAt": "2026-01-01T00:00:00Z"}
                for i, b in enumerate(comments.get(n, []))]})
        if rest is not None:
            return rest
        if len(args) >= 3 and args[0] == "issue" and args[1] == "view":
            fallbacks.append(list(args))     # #895: the ONE fallback; a REST-first test asserts this empty
            n = args[2]
            json_field = args[args.index("--json") + 1] if "--json" in args else ""
            if json_field.startswith("state"):
                state = states.get(n, "OPEN")
                default_reason = "COMPLETED" if state == "CLOSED" else ""
                return json.dumps({"state": state,
                                   "stateReason": state_reasons.get(n, default_reason)})
            if json_field == "comments":
                bodies = comments.get(n, [])
                return json.dumps({"comments": [
                    {"id": f"c{i}", "author": {"login": "x"}, "body": b,
                     "createdAt": "2026-01-01T00:00:00Z"} for i, b in enumerate(bodies)]})
            return "{}"
        return ""
    run.calls = calls
    run.fallbacks = fallbacks
    run.list_fallbacks = list_fallbacks
    return run


# --- #1358 (against #1350's own gap): `mark_blocked`'s `sdlc:blocked` state (#1350) had no
# discovery/resume path anywhere in this sweep -- a goal blocked by a genuine dependency could
# never auto-resume even after its blocker closed, unlike the pre-#1350 equivalent (a plain
# `sdlc:parked` issue). The sweep must query and act on BOTH state labels, not just `sdlc:parked`.


def test_fetch_parked_issues_also_queries_by_goal_blocked_label():
    au = _mod("auto_unpark")
    blocked_issue = _blocked_issue(42, title="blocked goal", body="b", labels=("sdlc:blocked",))
    run = _label_aware_sweep_runner(
        by_label={"sdlc:parked": "[]", "sdlc:blocked": json.dumps([blocked_issue])})
    source = au.sources.GitHubSource(_config(), run=run)
    issues = au._fetch_parked_issues(source)
    assert [i["number"] for i in issues] == [42]
    queried = {gqlfake.rest_list_params(c)["labels"] for c in rest_list_calls(run)}
    assert queried == {"sdlc:parked", "sdlc:blocked"}


def test_fetch_parked_issues_dedupes_an_issue_returned_by_both_label_queries():
    # defensive: an issue cannot really carry both state labels at once (mark_blocked/_offboard's
    # own collision-avoidance guarantees that), but a hand-fed fixture (or a future label-model
    # change) returning the same issue for both queries must never double-count it.
    au = _mod("auto_unpark")
    dup = _blocked_issue(42, title="t", labels=("sdlc:parked",))
    run = _label_aware_sweep_runner(
        by_label={"sdlc:blocked": json.dumps([dup]), "sdlc:blocked": json.dumps([dup])})
    source = au.sources.GitHubSource(_config(), run=run)
    issues = au._fetch_parked_issues(source)
    assert [i["number"] for i in issues] == [42]


def test_fetch_parked_issues_survives_one_label_query_failing():
    au = _mod("auto_unpark")
    blocked_issue = _blocked_issue(42, title="blocked goal", labels=("sdlc:blocked",))
    run = _label_aware_sweep_runner(
        by_label={"sdlc:blocked": json.dumps([blocked_issue])}, fail_on=["labels=sdlc:parked"])
    source = au.sources.GitHubSource(_config(), run=run)
    issues = au._fetch_parked_issues(source)
    assert [i["number"] for i in issues] == [42]


def test_compute_unpark_actions_unparks_a_blocked_only_issue_once_its_blocker_closes():
    """`compute_unpark_actions` must recognize `goal_blocked_label` as a state to clear, not just
    `parked_label` -- otherwise a discovered blocked-only issue gets `sdlc:goal` added back
    without `sdlc:blocked` ever being removed, leaving it carrying both at once."""
    au = _mod("auto_unpark")
    issues = [_blocked_issue(42, body="blocked by #7 until the base lands", labels=("sdlc:blocked",))]
    run = _sweep_runner(states={"7": "CLOSED"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    kinds = [(a["action"], a["issue"], a["detail"]) for a in actions]
    # #1392: ONE atomic swap, not an add/remove pair -- see `triage._execute_action`
    assert ("swap-label", "42", "+sdlc:goal -sdlc:blocked") in kinds
    assert [a["remove"] for a in actions if a["action"] == "swap-label"] == [["sdlc:blocked"]]
    assert resolved == {"42": ["7"]}


def test_sweep_unpark_apply_resumes_a_blocked_only_goal_once_its_blocker_closes():
    """The real, end-to-end proof: a goal `mark_blocked` transitioned to `sdlc:blocked` (never
    `sdlc:parked`) is discovered by the sweep and correctly unparked once its blocker closes."""
    au = _mod("auto_unpark")
    blocked_issue = _blocked_issue(42, body="blocked by #7 until the base lands",
                                  labels=("sdlc:blocked",))
    run = _label_aware_sweep_runner(
        by_label={"sdlc:parked": "[]", "sdlc:blocked": json.dumps([blocked_issue])},
        states={"7": "CLOSED"})
    result = au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    assert result["checked"] == 1
    assert result["eligible"] == 1
    assert result["unparked"] == ["42"]
    kinds = {(a["action"], a["detail"]) for a in result["actions"] if a["issue"] == "42"}
    assert ("swap-label", "+sdlc:goal -sdlc:blocked") in kinds
    assert [a["remove"] for a in result["actions"] if a["action"] == "swap-label"] == [["sdlc:blocked"]]


# --------------------------------------------------------------------------- _ref_is_open


def test_ref_is_open_true_for_open_state():
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(states={"7": "OPEN"}))
    assert au._ref_is_open(source, "7", {}) is True


def test_ref_is_open_false_for_closed_state():
    # a plain closed ISSUE -- the mainstream case, and real GitHub always sets `stateReason` on a
    # genuine issue closure ("COMPLETED"/"NOT_PLANNED"); `_sweep_runner`'s own default reflects
    # that, so this stays the pre-#1186 behaviour unchanged.
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(states={"7": "CLOSED"}))
    assert au._ref_is_open(source, "7", {}) is False


# --- #1186: `_ref_is_open` is PR-aware -- merging resolves a PR-shaped blocker; a bare close does not


def test_ref_is_open_false_for_a_merged_pr():
    # `gh issue view` resolves PR numbers too and reports `state: MERGED` for one that landed --
    # merging is what actually resolves whatever the PR was blocking, so this must read as NOT
    # open. On unfixed code (`state != "CLOSED"`), MERGED reads as open (True) -- backwards, and
    # this specific blocker would never unpark.
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(states={"7": "MERGED"}))
    assert au._ref_is_open(source, "7", {}) is False


def test_ref_is_open_true_for_a_closed_but_unmerged_pr():
    # the flip side of the same inversion: a PR CLOSED WITHOUT merging never delivered the change
    # it would have unblocked, so it must NOT read as resolved just because `gh` reports the same
    # bare "CLOSED" state a genuinely-resolved issue also reports. `stateReason` is an ISSUE-only
    # GraphQL field -- empty for a PR of any state, populated for a real closed issue -- the free,
    # single-call signal `_ref_is_open` uses to tell the two apart. On unfixed code
    # (`state != "CLOSED"`), a closed-without-merge PR reads as resolved (False) and unparks
    # immediately -- also backwards, and the worse direction: it fires on a write.
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(
        _config(), run=_sweep_runner(states={"7": "CLOSED"}, state_reasons={"7": ""}))
    assert au._ref_is_open(source, "7", {}) is True


def test_ref_is_open_fails_closed_true_on_gh_error():
    """Unreadable blocker state -> treated as still open (the safe direction): a write this sweep
    can't easily undo must never fire on data it could not confirm."""
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(fail_on=["issues/7", "issue view 7"]))
    assert au._ref_is_open(source, "7", {}) is True      # GhApiError reaches the site's own arm


def test_ref_is_open_reads_rest_first_and_never_asks_for_anything_else():
    """#895: the read is `api repos/<o>/<r>/issues/7 --method GET`; no `issue view`, no other argv."""
    au = _mod("auto_unpark")
    run = _sweep_runner(states={"7": "CLOSED"})
    source = au.sources.GitHubSource(_config(), run=run)
    assert au._ref_is_open(source, "7", {}) is False
    assert run.calls == [["api", "repos/acme/widget/issues/7", "--method", "GET"]]
    assert run.fallbacks == []


def test_ref_is_open_rest_5xx_falls_back_once_to_issue_view(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)
    au = _mod("auto_unpark")
    run = _sweep_runner(states={"7": "CLOSED"}, fail_on=["issues/7"])
    # fail_on raises a plain error (kind other, no fallback); a 502 text is what earns the one fallback
    inner = run

    def flaky(args):
        if gqlfake.rest_issue_target(args):
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return inner(args)
    source = au.sources.GitHubSource(_config(), run=flaky)
    assert au._ref_is_open(source, "7", {}) is False
    assert [c[:3] for c in inner.fallbacks] == [["issue", "view", "7"]]


def test_ref_is_open_memoizes_across_calls_in_the_same_cache():
    au = _mod("auto_unpark")
    run = _sweep_runner(states={"7": "CLOSED"})
    source = au.sources.GitHubSource(_config(), run=run)
    cache = {}
    au._ref_is_open(source, "7", cache)
    au._ref_is_open(source, "7", cache)
    view_calls = [c for c in run.calls if gqlfake.rest_issue_target(c) == ("7", False)]
    assert len(view_calls) == 1
    assert not [c for c in run.calls if c[:2] == ["issue", "view"]]     # no fallback, no unexpected argv


# --------------------------------------------------------------------------- compute_unpark_actions


def test_compute_unpark_actions_ignores_a_park_with_no_blocker_reference():
    # parked for some other reason entirely (duplicate / obsoleted-by / needs_decision / manual) --
    # this sweep must never guess, only reverse the one park class it can prove is stale.
    au = _mod("auto_unpark")
    issues = [_blocked_issue(42, title="dup of an older goal", body="no dependency language here")]
    run = _sweep_runner()
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert actions == []
    assert resolved == {}


def test_compute_unpark_actions_leaves_a_still_open_blocker_parked():
    au = _mod("auto_unpark")
    issues = [_blocked_issue(42, body="blocked by #7 until the base lands")]
    run = _sweep_runner(states={"7": "OPEN"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert actions == []
    assert resolved == {}


def test_compute_unpark_actions_unparks_once_the_blocker_closes():
    au = _mod("auto_unpark")
    issues = [_blocked_issue(42, body="blocked by #7 until the base lands")]
    run = _sweep_runner(states={"7": "CLOSED"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    kinds = [(a["action"], a["issue"], a["detail"]) for a in actions]
    assert ("swap-label", "42", "+sdlc:goal -sdlc:blocked") in kinds
    assert [a["remove"] for a in actions if a["action"] == "swap-label"] == [["sdlc:blocked"]]
    assert resolved == {"42": ["7"]}


def test_compute_unpark_actions_reads_the_blocker_from_the_park_comment_not_just_the_body():
    # the mainstream real-world case: backlog_check's own automated park puts the "blocked by #N"
    # phrase in the PARK COMMENT (_offboard's "Parked by Sigma..."), never the issue body.
    au = _mod("auto_unpark")
    issues = [_blocked_issue(42, title="fix the thing", body="no marker in the body at all")]
    comment = (au.sources.PARK_COMMENT_PREFIX + "backlog cross-check: "
              "blocked by #7 (1.0; shared: widget, cache)")
    run = _sweep_runner(states={"7": "CLOSED"}, comments={"42": [comment]})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert resolved == {"42": ["7"]}
    assert any(a["action"] == "swap-label" and a["issue"] == "42"
               and a["add"] == ["sdlc:goal"] for a in actions)


# --- #1186: the park comment's OWN "needs human review" boilerplate must never itself read as a
# blocker reference -- "needs" is a `_BLOCK_RE` trigger word baked into every park comment Sigma
# ever posts, regardless of what the goal was actually parked for.


def test_compute_unpark_actions_ignores_a_ref_the_boilerplates_own_needs_word_reaches():
    # table row 1 from #1186: a needs_decision park whose free-text reason happens to mention a PR
    # number close after the fixed "needs human review: " prefix. Non-vacuous: #7 is CLOSED, so on
    # unfixed code (which reads #7 as a genuine referenced blocker via the boilerplate's own
    # "needs") this goal wrongly becomes eligible and unparks -- the worst case named in the issue,
    # since closing the referenced PR is itself something a human might do in response to the park.
    au = _mod("auto_unpark")
    issues = [_blocked_issue(42, title="fix the thing", body="no marker in the body at all")]
    comment = au.sources.PARK_COMMENT_PREFIX + "PR #7 is not approved yet (changes requested)"
    run = _sweep_runner(states={"7": "CLOSED"}, comments={"42": [comment]})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert actions == []
    assert resolved == {}


def test_compute_unpark_actions_ignores_a_ref_the_boilerplate_reaches_via_duplicate_of():
    # table row 3 from #1186: same boilerplate-anchored "needs ... #N" reach, this time landing on
    # a "duplicate of #N" reason -- a completely different, non-dependency reason for the park.
    au = _mod("auto_unpark")
    issues = [_blocked_issue(42, title="fix the thing", body="no marker in the body at all")]
    comment = au.sources.PARK_COMMENT_PREFIX + "duplicate of #7, no code change needed"
    run = _sweep_runner(states={"7": "CLOSED"}, comments={"42": [comment]})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert actions == []
    assert resolved == {}


def test_compute_unpark_actions_ignores_the_boilerplate_in_a_fail_comment_too():
    # `fail()` shares the identical `_offboard` mechanic (and the identical `parked_label`) as
    # `park()`, and its own fixed prefix ("...needs a fix (not a decision): ") carries the same
    # "needs" trigger word -- the identical defect class, just reached through the sibling verb.
    au = _mod("auto_unpark")
    issues = [_blocked_issue(42, title="fix the thing", body="no marker in the body at all")]
    comment = au.sources.FAIL_COMMENT_PREFIX + "see #7 for the same traceback"
    run = _sweep_runner(states={"7": "CLOSED"}, comments={"42": [comment]})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert actions == []
    assert resolved == {}


def test_compute_unpark_actions_requires_every_referenced_blocker_closed():
    au = _mod("auto_unpark")
    # two DISTINCT trigger phrases -- _BLOCK_RE matches one #N per trigger-phrase occurrence, so
    # "blocked by #7 and #12" (one phrase, one match) would not exercise the multi-blocker path.
    issues = [_blocked_issue(42, body="blocked by #7, depends on #12")]
    run = _sweep_runner(states={"7": "CLOSED", "12": "OPEN"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert actions == []
    assert resolved == {}


def test_compute_unpark_actions_unparks_once_all_referenced_blockers_close():
    au = _mod("auto_unpark")
    issues = [_blocked_issue(42, body="blocked by #7, depends on #12")]
    run = _sweep_runner(states={"7": "CLOSED", "12": "CLOSED"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert resolved == {"42": ["7", "12"]}
    # #1392: two labels changed, but ONE action -- the write count is what changed, never
    # which labels are named (both still appear, in the one swap).
    assert sum(1 for a in actions if a["issue"] == "42") == 1


def test_compute_unpark_actions_is_add_only_what_is_missing():
    # a blocked issue that already carries sdlc:goal (the normal shape since #1393) -- only the
    # missing half of the transition (dropping sdlc:blocked) should ever be emitted, matching
    # _picked_actions' own add-only-what's-missing contract for the identical unpark case.
    au = _mod("auto_unpark")
    issues = [_blocked_issue(42, body="blocked by #7", labels=("sdlc:blocked", "sdlc:goal"))]
    run = _sweep_runner(states={"7": "CLOSED"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert actions == [{"action": "swap-label", "issue": "42", "detail": "-sdlc:blocked",
                        "add": [], "remove": ["sdlc:blocked"],
                        "result": None, "error": None}]
    assert resolved == {"42": ["7"]}


def test_compute_unpark_actions_over_several_issues_only_acts_on_the_eligible_one():
    au = _mod("auto_unpark")
    issues = [_blocked_issue(1, body="blocked by #10"),          # still open -> skip
             _blocked_issue(2, body="blocked by #20"),           # closed -> unpark
             _blocked_issue(3, body="no marker at all")]         # not our concern -> skip
    run = _sweep_runner(states={"10": "OPEN", "20": "CLOSED"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert set(resolved) == {"2"}
    assert {a["issue"] for a in actions} == {"2"}


# --------------------------------------------------------------------------- KEEP_PARKED_MARKER (#1152)


def test_keep_parked_comment_carries_the_marker_and_an_optional_reason():
    au = _mod("auto_unpark")
    bare = au.keep_parked_comment()
    assert au.KEEP_PARKED_MARKER in bare
    with_reason = au.keep_parked_comment("sequencing checkpoint, not a real dependency")
    assert "sequencing checkpoint" in with_reason
    assert au.KEEP_PARKED_MARKER in with_reason


def test_is_exempt_true_when_marker_present_in_any_comment():
    au = _mod("auto_unpark")
    assert au._is_exempt(["some unrelated text", au.keep_parked_comment()]) is True


def test_is_exempt_false_without_the_marker():
    au = _mod("auto_unpark")
    assert au._is_exempt(["blocked by #7", "unrelated"]) is False


def test_is_exempt_false_on_no_comments():
    au = _mod("auto_unpark")
    assert au._is_exempt([]) is False


def test_compute_unpark_actions_skips_a_goal_carrying_the_keep_parked_marker():
    # a deliberate human checkpoint whose park text textually matches _BLOCK_RE exactly like a real
    # stale block would -- the marker must win regardless of what the park text says.
    au = _mod("auto_unpark")
    marker_comment = au.keep_parked_comment("waiting for the v2 API design to settle first")
    issues = [_blocked_issue(42, body="blocked by #7 until the base lands")]
    run = _sweep_runner(states={"7": "CLOSED"}, comments={"42": [marker_comment]})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert actions == []
    assert resolved == {}


def test_compute_unpark_actions_sweeps_normally_without_the_marker():
    # identical shape, no marker -- no regression to the existing sweep behavior.
    au = _mod("auto_unpark")
    issues = [_blocked_issue(42, body="blocked by #7 until the base lands")]
    run = _sweep_runner(states={"7": "CLOSED"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    kinds = [(a["action"], a["issue"], a["detail"]) for a in actions]
    assert ("swap-label", "42", "+sdlc:goal -sdlc:blocked") in kinds
    assert [a["remove"] for a in actions if a["action"] == "swap-label"] == [["sdlc:blocked"]]
    assert resolved == {"42": ["7"]}


def test_compute_unpark_actions_marker_check_runs_before_any_blocker_regex_scan(monkeypatch):
    # the exclusion must happen BEFORE _referenced_blocker_refs/_explicit_blockers run at all, never
    # as a post-hoc filter on an already-computed finding -- prove it by making the regex-matching
    # step itself explode if it is ever reached while the marker is present.
    au = _mod("auto_unpark")

    def _boom(*a, **k):
        raise AssertionError("blocker-regex scan ran despite the exemption marker")

    monkeypatch.setattr(au.backlog_check, "_referenced_blocker_refs", _boom)
    marker_comment = au.keep_parked_comment()
    issues = [_blocked_issue(42, body="blocked by #7 until the base lands")]
    run = _sweep_runner(states={"7": "CLOSED"}, comments={"42": [marker_comment]})
    source = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), source, issues, run=run)
    assert actions == []
    assert resolved == {}


def test_sweep_unpark_apply_never_touches_a_marker_exempt_issue():
    au = _mod("auto_unpark")
    marker_comment = au.keep_parked_comment()
    run = _sweep_runner(parked=json.dumps([_blocked_issue(42, body="blocked by #7")]),
                        states={"7": "CLOSED"}, comments={"42": [marker_comment]})
    result = au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    assert result["eligible"] == 0
    assert result["actions"] == []
    assert result["unparked"] == []
    edit_calls = [c for c in run.calls if c[:2] == ["issue", "edit"]]
    comment_calls = [c for c in run.calls if c[:2] == ["issue", "comment"]]
    assert edit_calls == []
    assert comment_calls == []


# --------------------------------------------------------------------------- sweep_unpark


def test_sweep_unpark_is_a_no_op_outside_github_mode():
    au = _mod("auto_unpark")
    run = _sweep_runner(parked=json.dumps([_blocked_issue(42, body="blocked by #7")]))
    result = au.sweep_unpark(".sdlc", _local_config(), apply=True, run=run)
    assert result == {"apply": True, "checked": 0, "eligible": 0, "actions": [], "unparked": []}
    assert run.calls == []


def test_sweep_unpark_nothing_parked_is_a_cheap_no_op():
    au = _mod("auto_unpark")
    run = _sweep_runner(parked="[]")
    result = au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    assert result == {"apply": True, "checked": 0, "eligible": 0, "actions": [], "unparked": []}
    # #1358/#1351: exactly the three `issue list` calls `_fetch_parked_issues` (sdlc:parked,
    # sdlc:blocked) and `_fetch_blocking_issues` (sdlc:blocking) together make -- still cheap,
    # nothing further to check or mutate.
    assert len(run.calls) == 3


def test_sweep_unpark_dry_run_issues_zero_write_shaped_gh_calls():
    au = _mod("auto_unpark")
    run = _sweep_runner(parked=json.dumps([_blocked_issue(42, body="blocked by #7")]),
                        states={"7": "CLOSED"})
    result = au.sweep_unpark(".sdlc", _config(), apply=False, run=run)
    assert result["eligible"] == 1
    assert all(a["result"] == "would" for a in result["actions"])
    write_calls = [c for c in run.calls if len(c) > 1 and c[1] in ("create", "edit", "comment")]
    assert write_calls == []
    # a dry-run mutates nothing -- #1129-followup's cooldown hook must report NOTHING was really
    # unparked yet, or `loop.py` would withhold a goal from a pick that never actually flipped it
    assert result["unparked"] == []


def test_sweep_unpark_apply_mutates_labels_and_posts_an_audit_comment():
    au = _mod("auto_unpark")
    run = _sweep_runner(parked=json.dumps([_blocked_issue(42, body="blocked by #7")]),
                        states={"7": "CLOSED"})
    result = au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    assert result["checked"] == 1
    assert result["eligible"] == 1
    assert all(a["result"] == "done" for a in result["actions"])
    comment_calls = [c for c in run.calls if c[:2] == ["issue", "comment"] and c[2] == "42"]
    assert len(comment_calls) == 1
    body = comment_calls[0][comment_calls[0].index("--body") + 1]
    assert "#7" in body and "Auto-unparked" in body
    # #1129-followup: the cooldown hook `loop.py` reads to withhold this SAME issue from this same
    # call's own pick -- the whole point of this return value existing at all.
    assert result["unparked"] == ["42"]


def test_sweep_unpark_apply_survives_a_failed_audit_comment():
    # the label mutation is the load-bearing write; the comment is best-effort audit trail only --
    # a comment failure must never flip an already-successful label edit back to "failed", and must
    # never raise out of sweep_unpark itself.
    au = _mod("auto_unpark")
    run = _sweep_runner(parked=json.dumps([_blocked_issue(42, body="blocked by #7")]),
                        states={"7": "CLOSED"}, fail_on=["issue comment"])
    result = au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    assert all(a["result"] == "done" for a in result["actions"])
    assert result["eligible"] == 1
    # the label edit is what "unparked" tracks -- a best-effort comment failure must not un-count it
    assert result["unparked"] == ["42"]


def test_sweep_unpark_apply_is_idempotent_on_a_second_pass():
    au = _mod("auto_unpark")
    first_run = _sweep_runner(parked=json.dumps([_blocked_issue(42, body="blocked by #7")]),
                              states={"7": "CLOSED"})
    first = au.sweep_unpark(".sdlc", _config(), apply=True, run=first_run)
    assert first["actions"]
    assert first["unparked"] == ["42"]

    # second pass: the issue no longer carries sdlc:parked (labels reflect the completed transition)
    second_run = _sweep_runner(
        parked=json.dumps([]),   # the label query itself would no longer surface it
        states={"7": "CLOSED"})
    second = au.sweep_unpark(".sdlc", _config(), apply=True, run=second_run)
    assert second["actions"] == []
    # nothing left THIS pass to hold back from a pick -- the realistic shape of "the very next
    # `_next()` call" in loop.py's own cooldown tests (test_loop.py's
    # test_a_just_unparked_goal_is_pickable_on_the_very_next_next_call)
    assert second["unparked"] == []


def test_sweep_unpark_a_failed_label_edit_suppresses_only_that_issues_comment():
    au = _mod("auto_unpark")
    parked = json.dumps([_blocked_issue(1, body="blocked by #10"),
                         _blocked_issue(2, body="blocked by #20")])
    run = _sweep_runner(parked=parked, states={"10": "CLOSED", "20": "CLOSED"},
                        # #1392: the label write is a graphql swap now, so the
                        # per-issue failure is injected at that issue's own node-id lookup --
                        # `_swap_labels` raises, and `apply_actions` records `failed` for it.
                        fail_on=["issue(number: 1)", "issues/1/labels"])  # #1398: the REST twin fails too
    result = au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    failed_issue_1 = [a for a in result["actions"] if a["issue"] == "1"]
    ok_issue_2 = [a for a in result["actions"] if a["issue"] == "2"]
    assert any(a["result"] == "failed" for a in failed_issue_1)
    assert all(a["result"] == "done" for a in ok_issue_2)
    comment_calls = [c for c in run.calls if c[:2] == ["issue", "comment"]]
    assert [c[2] for c in comment_calls] == ["2"]
    # only the issue whose actions ALL landed counts as genuinely unparked -- #1: a half-failed
    # transition must never be treated as safe to withhold a pick over (see loop.py's cooldown)
    assert result["unparked"] == ["2"]


# --------------------------------------------------------------------------- render_sweep


def test_render_sweep_dry_run_reports_would_do_count():
    au = _mod("auto_unpark")
    result = {"apply": False, "checked": 1, "eligible": 1,
             "actions": [{"action": "add-label", "issue": "42", "detail": "sdlc:goal",
                          "result": "would", "error": None}]}
    out = au.render_sweep(result)
    assert "DRY-RUN" in out and "would-do" in out


def test_render_sweep_apply_reports_done_and_failed_counts():
    au = _mod("auto_unpark")
    result = {"apply": True, "checked": 1, "eligible": 1,
             "actions": [{"action": "add-label", "issue": "42", "detail": "sdlc:goal",
                          "result": "done", "error": None},
                        {"action": "remove-label", "issue": "42", "detail": "sdlc:parked",
                         "result": "failed", "error": "boom"}]}
    out = au.render_sweep(result)
    assert "APPLIED" in out and "1 done, 1 failed" in out and "boom" in out


def test_render_sweep_reports_nothing_parked():
    au = _mod("auto_unpark")
    result = {"apply": True, "checked": 0, "eligible": 0, "actions": []}
    assert "nothing parked" in au.render_sweep(result)


# --------------------------------------------------------------------------- CLI


def test_sweep_cmd_dry_run_by_default_apply_flag_flips_it(capsys):
    au = _mod("auto_unpark")
    run = _sweep_runner(parked=json.dumps([_blocked_issue(42, body="blocked by #7")]),
                        states={"7": "CLOSED"})
    rc = au.sweep_cmd(".sdlc", _config(), [], run=run)
    assert rc == 0
    assert "would-do" in capsys.readouterr().out

    run2 = _sweep_runner(parked=json.dumps([_blocked_issue(42, body="blocked by #7")]),
                         states={"7": "CLOSED"})
    rc2 = au.sweep_cmd(".sdlc", _config(), ["--apply"], run=run2)
    assert rc2 == 0
    assert "done" in capsys.readouterr().out


def test_sweep_cmd_nonzero_exit_when_an_action_fails():
    au = _mod("auto_unpark")
    run = _sweep_runner(parked=json.dumps([_blocked_issue(42, body="blocked by #7")]),
                        # #1392: fail the label MUTATION itself -- the honest
                        # "the label write did not land" simulation now that it is one swap.
                        states={"7": "CLOSED"}, fail_on=["addLabelsToLabelable"])
    rc = au.sweep_cmd(".sdlc", _config(), ["--apply"], run=run)
    assert rc == 1


def test_cli_usage_mentions_sweep_verb(capsys):
    au = _mod("auto_unpark")
    rc = au.main(["auto_unpark.py", "bogus"])
    assert rc == 2
    assert "sweep <sdlc_dir>" in capsys.readouterr().err


# --- #1351: sdlc:blocking -- the derived, self-healing label on the BLOCKER issue itself, not the
# blocked goal. Auto-added while at least one open issue still references it via a live "Blocked
# by #N" marker; auto-removed once the LAST such reference resolves. Reuses the EXACT same
# exempt-check + comment-scrub + _referenced_blocker_refs + _explicit_blockers pipeline
# compute_unpark_actions already uses -- never new regex/detection logic (#1186's own lesson).


def test_fetch_blocking_issues_queries_by_the_blocking_label_and_all_states():
    # #1351 review finding (MINOR): a blocker that resolves by CLOSING (rather than its referencing
    # text being edited away) must still be visible to this query so the next sweep can strip the
    # now-stale label -- an --state open-only query would drop it from every future snapshot the
    # instant it closes, and the label would never be revisited.
    au = _mod("auto_unpark")
    run = _sweep_runner()
    source = au.sources.GitHubSource(_config(), run=run)
    au._fetch_blocking_issues(source)
    call = rest_list_calls(run)[0]
    p = gqlfake.rest_list_params(call)
    assert p["labels"] == "sdlc:blocking" and p["state"] == "all"
    assert p["sort"] == "created" and p["direction"] == "desc"
    assert run.list_fallbacks == []


def test_fetch_blocking_issues_fails_open_on_gh_error():
    au = _mod("auto_unpark")
    source = au.sources.GitHubSource(_config(), run=_sweep_runner(fail_on=[LIST_GET]))
    assert au._fetch_blocking_issues(source) == set()


def test_compute_blocking_actions_adds_blocking_to_a_fresh_live_blocker():
    """A blocked goal (sdlc:blocked) references #7, still open -- #7 must get sdlc:blocking added,
    it doesn't have it yet."""
    au = _mod("auto_unpark")
    blocked = [_blocked_issue(42, body="Blocked by #7")]
    run = _sweep_runner(states={"7": "OPEN"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), source, blocked, currently_blocking=set(), run=run)
    assert actions == [{"action": "swap-label", "issue": "7",
                        "detail": "+sdlc:blocking", "add": ["sdlc:blocking"], "remove": [],
                        "result": None, "error": None}]


def test_compute_blocking_actions_does_not_re_add_when_already_present():
    au = _mod("auto_unpark")
    blocked = [_blocked_issue(42, body="Blocked by #7")]
    run = _sweep_runner(states={"7": "OPEN"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), source, blocked,
                                          currently_blocking={"7"}, run=run)
    assert actions == []


def test_compute_blocking_actions_removes_blocking_once_the_last_reference_resolves():
    """#7 currently carries sdlc:blocking, but NO open blocked/parked issue references it anymore
    (the referencing goal is entirely ABSENT from `blocked_issues` this pass, e.g. it fully
    unparked and dropped out of the query) -- self-healing removal."""
    au = _mod("auto_unpark")
    run = _sweep_runner()
    source = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), source, [],
                                          currently_blocking={"7"}, run=run)
    assert actions == [{"action": "remove-label", "issue": "7", "detail": "sdlc:blocking",
                        "result": None, "error": None}]


def test_compute_blocking_actions_removes_blocking_when_the_referencing_issue_is_still_present_but_its_blocker_ref_closed():
    """#1351 review finding (BLOCKING): the REAL self-healing removal path. #42 is still open and
    still `sdlc:blocked` -- present in `blocked_issues`, unlike the test above -- but the SPECIFIC
    blocker it names, #7, has since closed. #7 must lose `sdlc:blocking` even though #42 itself is
    still in the list. (A one-line regression -- unioning ALL referenced refs via
    `_referenced_blocker_refs` instead of only the still-open ones `_explicit_blockers` narrows to
    -- would make every test in this suite still pass except this one.)"""
    au = _mod("auto_unpark")
    blocked = [_blocked_issue(42, body="Blocked by #7")]
    run = _sweep_runner(states={"7": "CLOSED"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), source, blocked,
                                          currently_blocking={"7"}, run=run)
    assert actions == [{"action": "remove-label", "issue": "7", "detail": "sdlc:blocking",
                        "result": None, "error": None}]


def test_compute_blocking_actions_multi_blocker_not_removed_while_another_reference_is_open():
    """#7 blocks TWO goals, both with live, still-open references to it -- sdlc:blocking must stay
    present (already labeled, still referenced -- proves idempotency, not a partial-resolution
    case; see the dedicated partial-removal test below for that)."""
    au = _mod("auto_unpark")
    blocked = [_blocked_issue(42, body="Blocked by #7"), _blocked_issue(43, body="Blocked by #7")]
    run = _sweep_runner(states={"7": "OPEN"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), source, blocked,
                                          currently_blocking={"7"}, run=run)
    assert actions == []   # already present, still referenced -- no action needed either way


def test_compute_blocking_actions_partial_removal_when_one_of_two_named_blockers_closes():
    """#1351 review finding (NOTABLE): a SINGLE blocked issue naming TWO distinct blockers, one of
    which closes while the other stays open -- #7 (closed) must be removed from sdlc:blocking while
    #12 (still open) is added, in the same pass over the same blocked issue."""
    au = _mod("auto_unpark")
    blocked = [_blocked_issue(42, body="blocked by #7, depends on #12")]
    run = _sweep_runner(states={"7": "CLOSED", "12": "OPEN"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), source, blocked,
                                          currently_blocking={"7"}, run=run)
    kinds = {(a["action"], a["issue"]) for a in actions}
    assert ("remove-label", "7") in kinds
    assert ("swap-label", "12") in kinds


def test_compute_blocking_actions_adds_exactly_one_action_for_a_blocker_shared_by_two_blocked_issues():
    """#1351 review finding (NOTABLE): #7 is referenced by BOTH #42 and #43, and neither currently
    carries sdlc:blocking -- must produce exactly ONE add-label action for #7, proving the union
    dedupes rather than emitting a duplicate/conflicting action per referencing issue. The existing
    multi-blocker test above only ever exercised this shared-reference shape with #7 already
    labeled (a no-op either way), which cannot distinguish a correct dedup from a duplicate."""
    au = _mod("auto_unpark")
    blocked = [_blocked_issue(42, body="Blocked by #7"), _blocked_issue(43, body="Blocked by #7")]
    run = _sweep_runner(states={"7": "OPEN"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), source, blocked,
                                          currently_blocking=set(), run=run)
    assert actions == [{"action": "swap-label", "issue": "7",
                        "detail": "+sdlc:blocking", "add": ["sdlc:blocking"], "remove": [],
                        "result": None, "error": None}]


def test_compute_blocking_actions_retroactively_labels_an_old_semantics_parked_issue_blocker():
    """A pre-#1350 issue is generically sdlc:parked (not sdlc:blocked) but its body still names a
    live, open blocker -- the retroactive case: that blocker gets sdlc:blocking too, the same as if
    the goal had gone through the new sdlc:blocked path from the start."""
    au = _mod("auto_unpark")
    old_semantics_parked = [_blocked_issue(50, body="blocked by #9", labels=("sdlc:parked",))]
    run = _sweep_runner(states={"9": "OPEN"})
    source = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), source, old_semantics_parked,
                                          currently_blocking=set(), run=run)
    assert actions == [{"action": "swap-label", "issue": "9",
                        "detail": "+sdlc:blocking", "add": ["sdlc:blocking"], "remove": [],
                        "result": None, "error": None}]


def test_compute_blocking_actions_reuses_hardened_detection_not_naive_regex():
    """#1186's own lesson: Sigma's own park-comment boilerplate ("Parked by Sigma — needs
    human review: ...") contains the word "needs", a _BLOCK_RE trigger word, immediately before
    whatever free-text reason follows it. A naive scan would misread a #N mentioned incidentally in
    that reason as a live blocker reference; the hardened pipeline (_strip_offboard_prefixes) must
    not."""
    au = _mod("auto_unpark")
    blocked = [_blocked_issue(42, body="some unrelated body")]
    # the boilerplate prefix + a reason that happens to mention #7 -- NOT a real "blocked by" phrase
    run = _sweep_runner(states={"7": "OPEN"},
                        comments={"42": ["Parked by Sigma — needs human review: "
                                          "PR #7 is not approved yet"]})
    source = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), source, blocked,
                                          currently_blocking=set(), run=run)
    assert actions == []   # #7 must NOT be labeled sdlc:blocking off the boilerplate's own "needs"


def test_compute_blocking_actions_still_labels_the_blocker_of_an_exempt_issue():
    """#1351 review finding (NOTABLE, corrected here): a goal carrying KEEP_PARKED_MARKER opts
    itself OUT OF AUTO-UNPARK ELIGIBILITY (compute_unpark_actions' own step 0) -- that does not also
    retract its own "Blocked by #N" text. #42 is still open and still genuinely blocked on #7
    regardless of the human's deliberate "leave this parked" opt-out, so #7 must still get
    sdlc:blocking. (The original version of this test asserted the opposite -- `actions == []` --
    which is exactly the bug: skipping exempt issues here meant an already-correct sdlc:blocking
    label on a blocker named ONLY by an exempt issue got silently stripped the very next sweep.)"""
    au = _mod("auto_unpark")
    blocked = [_blocked_issue(42, body="Blocked by #7")]
    run = _sweep_runner(states={"7": "OPEN"}, comments={"42": [au.KEEP_PARKED_MARKER]})
    source = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), source, blocked,
                                          currently_blocking=set(), run=run)
    assert actions == [{"action": "swap-label", "issue": "7",
                        "detail": "+sdlc:blocking", "add": ["sdlc:blocking"], "remove": [],
                        "result": None, "error": None}]


def test_compute_blocking_actions_does_not_strip_blocking_off_a_blocker_still_named_by_an_exempt_issue():
    """The removal-direction half of the same fix: #7 already carries sdlc:blocking; #60 (the only
    issue referencing it) is exempt via KEEP_PARKED_MARKER but is still open and still literally
    names #7 as its blocker -- #7 must NOT lose sdlc:blocking just because #60 opted out of
    auto-unpark eligibility. This is the exact scenario the review reproduced against the
    unfixed code."""
    au = _mod("auto_unpark")
    blocked = [_blocked_issue(60, body="Blocked by #7")]
    run = _sweep_runner(states={"7": "OPEN"}, comments={"60": [au.KEEP_PARKED_MARKER]})
    source = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), source, blocked,
                                          currently_blocking={"7"}, run=run)
    assert actions == []


def test_sweep_unpark_applies_blocking_actions_alongside_unpark_actions():
    """End-to-end: a real sweep_unpark(apply=True) call must compute AND apply both the unpark
    action (if any) and the blocking-label reconciliation action together, in one pass."""
    au = _mod("auto_unpark")
    blocked = _blocked_issue(42, body="Blocked by #7")
    run = _label_aware_sweep_runner(
        by_label={"sdlc:parked": "[]", "sdlc:blocked": json.dumps([blocked]),
                  "sdlc:blocking": "[]"},
        states={"7": "OPEN"})
    result = au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    add_label_calls = [c for c in run.calls if c[:2] == ["issue", "edit"] and "--add-label" in c]
    assert any(c[2] == "7" and "sdlc:blocking" in c for c in add_label_calls)


def test_sweep_unpark_skips_blocking_reconciliation_when_the_parked_fetch_is_incomplete():
    """#1351 review finding (BLOCKING): a transient failure on BOTH the sdlc:parked and
    sdlc:blocked queries must never be read as "nothing is blocked this pass" for the
    sdlc:blocking removal computation -- that would wrongly strip sdlc:blocking off every issue
    currently carrying it, on nothing more than a network blip. #7 already carries sdlc:blocking
    and must keep it; no remove-label call should even be attempted."""
    au = _mod("auto_unpark")
    run = _label_aware_sweep_runner(
        by_label={"sdlc:blocking": json.dumps([{"number": 7}])},
        fail_on=["labels=sdlc:parked", "labels=sdlc:blocked"])
    result = au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    assert result["actions"] == []
    remove_calls = [c for c in run.calls if c[:2] == ["issue", "edit"] and "--remove-label" in c]
    assert remove_calls == []


def test_sweep_unpark_still_runs_blocking_reconciliation_when_the_parked_fetch_is_complete():
    # the flip side of the test above: proves the `complete` gate isn't just always skipping --
    # a genuinely successful (if genuinely empty) fetch must still reconcile normally.
    au = _mod("auto_unpark")
    run = _label_aware_sweep_runner(
        by_label={"sdlc:parked": "[]", "sdlc:blocked": "[]",
                  "sdlc:blocking": json.dumps([{"number": 7}])})
    result = au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    assert result["actions"] == [{"action": "remove-label", "issue": "7", "detail": "sdlc:blocking",
                                  "result": "done", "error": None}]


def test_sweep_unpark_survives_compute_blocking_actions_raising(monkeypatch):
    """#1351 review finding (NOTABLE): a raise inside the blocking-reconciliation call must cost
    only this pass's sdlc:blocking bookkeeping, never the unrelated, already-computed unpark
    actions -- the same fail-open posture every other primitive in this module already has."""
    au = _mod("auto_unpark")

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(au, "compute_blocking_actions", _boom)
    run = _sweep_runner(parked=json.dumps([_blocked_issue(42, body="blocked by #7")]),
                        states={"7": "CLOSED"})
    result = au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    assert result["unparked"] == ["42"]
    assert all(a["result"] == "done" for a in result["actions"])


def test_sweep_unpark_credits_unpark_even_when_the_same_issues_blocking_label_edit_fails():
    """#1351 review finding (NOTABLE): #7 is itself sdlc:blocked (its own blocker #99 has closed --
    eligible to unpark THIS pass) and is ALSO the live blocker #42 (sdlc:blocked, "Blocked by #7")
    still needs sdlc:blocking on -- an overlapping issue-number topology. A transient failure on
    #7's UNRELATED sdlc:blocking add-label edit must never mask #7's own, fully-successful unpark
    (audit comment + cooldown credit)."""
    au = _mod("auto_unpark")
    resumable = _blocked_issue(7, body="blocked by #99")     # its own blocker closed
    blocked = _blocked_issue(42, body="Blocked by #7")        # still waiting on #7
    run = _label_aware_sweep_runner(
        # ONE list -- both are sdlc:blocked now, and two dict entries under the same key would
        # silently drop the first
        by_label={"sdlc:blocked": json.dumps([resumable, blocked]), "sdlc:blocking": "[]"},
        states={"99": "CLOSED", "7": "OPEN"},
        fail_labels=["sdlc:blocking"])
    # #1394: #7 is BLOCKED (not parked) so the sweep may resume it -- a parked #7 would be skipped
    # outright now, which would make this test about nothing.
    result = au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    # #7 is mid-unpark at classify time, so the blocking action and the unpark action are two
    # independent writes on the same issue -- which is exactly the independence this test proves.
    unpark_edits = [a for a in result["actions"]
                    if a["issue"] == "7" and "sdlc:blocking" not in (a.get("add") or [])]
    assert unpark_edits and all(a["result"] == "done" for a in unpark_edits)
    blocking_edit = next(a for a in result["actions"]
                         if a["issue"] == "7" and "sdlc:blocking" in (a.get("add") or []))
    assert blocking_edit["result"] == "failed"
    assert result["unparked"] == ["7"]
    comment_calls = [c for c in run.calls if c[:2] == ["issue", "comment"] and c[2] == "7"]
    assert len(comment_calls) == 1


def test_cli_sweep_verb_is_wired_through_main(tmp_path, monkeypatch):
    au = _mod("auto_unpark")
    # main()'s dispatch doesn't thread a `run=` through -- fake the module-level default
    # `sources._run_gh` instead, the identical idiom triage.py's own
    # `test_cli_enact_verb_is_wired_through_main` already established, so this never risks a real
    # `gh` call against a real repo.
    monkeypatch.setattr(au.sources, "_run_gh", lambda a: "[]" if gqlfake.rest_list_params(a) is not None else "")
    base = tmp_path / ".sdlc"; base.mkdir()
    (base / "config.json").write_text(json.dumps(_config()))
    rc = au.main(["auto_unpark.py", "sweep", str(base)])
    assert rc == 0


# --- #1393: membership travels with sdlc:blocking ------------------------------------------------
# `sdlc:blocking` means "other work is waiting on this issue" -- and an issue other work is waiting
# on is, by definition, waiting to be PICKED. This function was the ONLY writer of that label and it
# granted no membership, so a blocker lacking `sdlc:goal` was marked as blocking and stayed pickable
# by nothing (`_blocking_priority_pending` reuses `_fetch_pending`, whose base query always carries
# `--label sdlc:goal`, and gh ANDs). WHEN it is safe to grant is `blockers.classify`'s policy, reused
# rather than copied.


def _blocking_actions(au, blocker_labels, blocker_assignees=(), blocker_state="OPEN"):
    """One blocked issue (#42) naming one blocker (#7); #7's own labels are the variable."""
    run = _label_aware_sweep_runner(
        by_label={"sdlc:blocked": json.dumps([_blocked_issue(42, body="Blocked by #7")]),
                  "sdlc:parked": "[]", "sdlc:blocking": "[]"},
        states={"7": blocker_state})
    src = au.sources.GitHubSource(_config(), run=run)
    src._run = _view_wrapper(run, "7", blocker_labels, blocker_assignees, blocker_state)
    return au.compute_blocking_actions(
        ".sdlc", _config(), src, [_blocked_issue(42, body="Blocked by #7")], set(), run=run)


def _view_wrapper(run, number, labels, assignees, state):
    """`blockers._state` reads `issue view <n> --json labels,assignees,state`; the sweep fixtures
    answer `--json state,stateReason` only, so this layers the one extra shape on top."""
    def _run(args):
        if gqlfake.rest_issue_target(args) == (number, False):       # #895: REST first
            return gqlfake.rest_issue(args, lambda n, f: {
                "labels": [{"name": l} for l in labels],
                "assignees": [{"login": a} for a in assignees], "state": state})
        return run(args)
    return _run


def test_a_sigma_proposal_that_becomes_a_blocker_gets_membership_with_the_blocking_label():
    """The deadlock, closed on the DERIVED path: Sigma filed the follow-up, no human ever ruled
    on it, and now real work waits on it -- so it is not speculative and there is no decision to
    override."""
    au = _mod("auto_unpark")
    actions = _blocking_actions(au, ["sdlc:needs-confirmation", "sdlc:followup"])
    assert len(actions) == 1 and actions[0]["action"] == "swap-label"
    assert sorted(actions[0]["add"]) == ["sdlc:blocking", "sdlc:goal"]
    assert actions[0]["remove"] == ["sdlc:needs-confirmation"]


def test_a_blocker_owned_by_someone_else_gets_membership_too():
    au = _mod("auto_unpark")
    actions = _blocking_actions(au, [], blocker_assignees=["someone-else"])
    assert sorted(actions[0]["add"]) == ["sdlc:blocking", "sdlc:goal"]


def test_a_parked_blocker_is_never_granted_membership():
    """A park is human domain. Sigma may note that other work waits on it, but must not put it
    back in the queue -- only /sigma-unpark does that, and only with a human in the loop."""
    au = _mod("auto_unpark")
    actions = _blocking_actions(au, ["sdlc:parked"])
    assert actions[0]["add"] == ["sdlc:blocking"]
    assert "sdlc:goal" not in actions[0]["add"]


def test_a_human_filed_proposal_blocker_is_never_granted_membership():
    """No `sdlc:followup` means a person filed it and left it awaiting approval -- that IS a
    decision, and granting membership would override it."""
    au = _mod("auto_unpark")
    actions = _blocking_actions(au, ["sdlc:needs-confirmation"])
    assert actions[0]["add"] == ["sdlc:blocking"]


def test_a_third_party_blocker_is_labelled_but_never_adopted():
    au = _mod("auto_unpark")
    actions = _blocking_actions(au, ["bug"])
    assert actions[0]["add"] == ["sdlc:blocking"]


def test_a_blocker_that_is_already_a_goal_gets_only_the_blocking_label():
    au = _mod("auto_unpark")
    actions = _blocking_actions(au, ["sdlc:goal"])
    assert actions[0]["add"] == ["sdlc:blocking"]


def test_an_unreadable_blocker_degrades_to_the_blocking_label_alone():
    """Fail open toward doing LESS. A read we cannot trust must never become a guessed membership
    grant; /sigma-doctor's unreachable-blocker check is what surfaces it instead."""
    au = _mod("auto_unpark")
    run = _label_aware_sweep_runner(
        by_label={"sdlc:blocked": json.dumps([_blocked_issue(42, body="Blocked by #7")]),
                  "sdlc:parked": "[]", "sdlc:blocking": "[]"},
        states={"7": "OPEN"})
    src = au.sources.GitHubSource(_config(), run=run)
    original = src._run

    def _run(args):
        if gqlfake.rest_issue_target(args) == ("7", False) or args[:2] == ["issue", "view"]:
            raise RuntimeError("gh: 502")      # REST 5xx, and the one fallback fails too
        return original(args)
    src._run = _run
    actions = au.compute_blocking_actions(".sdlc", _config(), src,
                                          [_blocked_issue(42, body="Blocked by #7")], set(), run=run)
    assert actions[0]["add"] == ["sdlc:blocking"]


def test_membership_is_never_revoked_when_an_issue_stops_blocking():
    """An issue that stopped blocking something is still a goal -- it is simply no longer holding
    anything up. Revoking membership would be the loop deleting work from its own backlog."""
    au = _mod("auto_unpark")
    run = _label_aware_sweep_runner(
        by_label={"sdlc:parked": "[]", "sdlc:blocked": "[]", "sdlc:blocking": "[]"})
    src = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), src, [], {"7"}, run=run)
    assert actions == [{"action": "remove-label", "issue": "7", "detail": "sdlc:blocking",
                        "result": None, "error": None}]


def test_an_unpark_on_a_board_repo_also_moves_the_card():
    """#1393: `triage._execute_action` gained a `set-status` kind for exactly this and had NO
    producer anywhere in the kit -- so every automatic unpark fixed the LABEL and left the card in
    `Blocked`. On a board-authoritative repo that manufactures a permanently-unpickable goal:
    correctly labelled, invisible to `_board_queue` forever. #1391 step 4's own lesson."""
    au = _mod("auto_unpark")
    cfg = _config(project={"enabled": True, "number": 1, "owner": "acme"})
    run = _label_aware_sweep_runner(
        by_label={"sdlc:blocked": json.dumps([_blocked_issue(42, body="blocked by #7")]),
                  "sdlc:blocking": "[]"},
        states={"7": "CLOSED"})
    src = au.sources.GitHubSource(cfg, run=run)
    actions, _ = au.compute_unpark_actions(".sdlc", cfg, src,
                                           [_blocked_issue(42, body="blocked by #7")], run=run)
    assert [a["action"] for a in actions] == ["swap-label", "set-status"]
    assert actions[1]["detail"] == "Ready"


def test_a_board_less_repo_gets_no_card_action_at_all():
    """`_set_board_status` returns False for a board-DISABLED repo and `_execute_action` raises on
    False, while `sweep_unpark` credits an unpark only when EVERY action for that issue succeeded.
    Emitting the card move unconditionally would make every label-queue adopter's unparks read as
    failures and suppress their audit comment and cooldown credit."""
    au = _mod("auto_unpark")
    run = _label_aware_sweep_runner(
        by_label={"sdlc:blocked": json.dumps([_blocked_issue(42, body="blocked by #7")]),
                  "sdlc:blocking": "[]"},
        states={"7": "CLOSED"})
    src = au.sources.GitHubSource(_config(), run=run)
    actions, _ = au.compute_unpark_actions(".sdlc", _config(), src,
                                           [_blocked_issue(42, body="blocked by #7")], run=run)
    assert [a["action"] for a in actions] == ["swap-label"]


def test_an_unpark_clears_a_stale_in_progress_claim_too():
    """An issue reaching this sweep as {parked, in-progress} is a half-applied park from before the
    atomic swap -- real and measured on live boards. Restoring membership without clearing the stale
    claim hands back a goal `_fetch_pending` then refuses, i.e. an unpark that unparks nothing."""
    au = _mod("auto_unpark")
    issue = _blocked_issue(42, body="blocked by #7",
                          labels=("sdlc:blocked", "sdlc:in-progress"))
    run = _label_aware_sweep_runner(
        by_label={"sdlc:blocked": json.dumps([issue]), "sdlc:blocking": "[]"},
        states={"7": "CLOSED"})
    src = au.sources.GitHubSource(_config(), run=run)
    actions, _ = au.compute_unpark_actions(".sdlc", _config(), src, [issue], run=run)
    assert sorted(actions[0]["remove"]) == ["sdlc:blocked", "sdlc:in-progress"]


# --- #1394: default-on, and the rule that makes it safe -----------------------------------------


def test_a_human_park_is_NEVER_resumed_by_the_sweep():
    """THE test that makes default-on safe. This sweep decides on TEXTUAL SHAPE alone -- its own
    docstring says it "has no way to tell a genuinely-stale block apart from a human's deliberate
    'on purpose' checkpoint that happens to share that shape". Tolerable while it was opt-in;
    unacceptable once it runs by default, because a human parking something with "waiting on the
    pricing call, see #123" would have that park silently reversed the moment #123 closed -- making
    the guarantee this release documents to adopters ("nothing automatic ever un-parks a human's
    park") false.

    Scoped to the machine's own state, the sweep can only ever reverse what the loop itself wrote."""
    au = _mod("auto_unpark")
    parked = _parked_issue(42, body="blocked by #7")          # a human's park, blocker now closed
    run = _sweep_runner(parked=json.dumps([parked]), states={"7": "CLOSED"})
    src = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), src, [parked], run=run)
    assert actions == [] and resolved == {}


def test_the_same_issue_blocked_instead_of_parked_IS_resumed():
    """The control: identical body, identical closed blocker — only the label differs. That is the
    whole distinction, and it is the one the user's rule draws: blocked is waiting to be PICKED,
    parked is waiting for a PERSON."""
    au = _mod("auto_unpark")
    blocked = _blocked_issue(42, body="blocked by #7")
    run = _sweep_runner(parked=json.dumps([blocked]), states={"7": "CLOSED"})
    src = au.sources.GitHubSource(_config(), run=run)
    actions, resolved = au.compute_unpark_actions(".sdlc", _config(), src, [blocked], run=run)
    assert [a["action"] for a in actions] == ["swap-label"]
    assert resolved == {"42": ["7"]}


def test_a_parked_issue_still_contributes_its_blocker_to_the_blocking_label():
    """Only RESUMING narrows. A parked issue can still name a live blocker, and that blocker
    genuinely blocks something — so the sdlc:blocking derivation must keep scanning both, or
    marking a park would quietly stop crediting whatever it waits on."""
    au = _mod("auto_unpark")
    parked = _parked_issue(42, body="Blocked by #7")
    run = _sweep_runner(parked=json.dumps([parked]), states={"7": "OPEN"})
    src = au.sources.GitHubSource(_config(), run=run)
    actions = au.compute_blocking_actions(".sdlc", _config(), src, [parked], set(), run=run)
    assert [a["issue"] for a in actions] == ["7"]
    assert "sdlc:blocking" in actions[0]["add"]


# --- #895 slice 2c: the two list reads go REST first (gh_api.list_issues_gh) ---------------------
# Fail-open arms must survive: a wrongly-empty blocker set removes labels (a real write), so ANY error
# keeps `complete=False` / `set()`. Each test also asserts the REST argv WAS requested, so it cannot
# pass against the old `issue list` code.

import pytest  # noqa: E402

RATE_429 = RuntimeError("gh: API rate limit exceeded (HTTP 429)")
RATE_429.hint = "gh: API rate limit exceeded (HTTP 429)"


@pytest.fixture(autouse=False)
def gh_env(monkeypatch):
    """Site-level fallback tests set the gate explicitly; never rely on the ambient environment."""
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)


def test_rest_error_on_one_label_is_incomplete_and_requests_rest(gh_env):
    au = _mod("auto_unpark")
    run = _label_aware_sweep_runner(by_label={"sdlc:blocked": json.dumps([_blocked_issue(42)])},
                                    fail_on=["labels=sdlc:parked"])
    source = au.sources.GitHubSource(_config(), run=run)
    issues, complete = au._fetch_parked_issues_status(source)
    assert complete is False and [i["number"] for i in issues] == [42]
    # `fail_on` raises before the call is recorded, so only the label that succeeded is in the log
    assert {gqlfake.rest_list_params(c)["labels"] for c in rest_list_calls(run)} == {"sdlc:blocked"}
    assert run.list_fallbacks == []                     # a non-fallback-class error never falls back


def test_blocking_read_error_is_the_empty_set_so_no_label_is_removed(gh_env):
    au = _mod("auto_unpark")
    run = _sweep_runner(list_fail=RuntimeError("gh: Not Found (HTTP 404)"))
    source = au.sources.GitHubSource(_config(), run=run)
    assert au._fetch_blocking_issues(source) == set()
    assert len(rest_list_calls(run)) == 1 and run.list_fallbacks == []


def test_429_on_the_first_page_serves_one_fallback_and_the_sweep_completes(gh_env):
    au = _mod("auto_unpark")
    fb = json.dumps([_blocked_issue(42)])
    run = _label_aware_sweep_runner(list_fail=RATE_429, fallback_list=fb)
    source = au.sources.GitHubSource(_config(), run=run)
    issues, complete = au._fetch_parked_issues_status(source)
    assert complete is True and [i["number"] for i in issues] == [42]
    assert len(run.list_fallbacks) == 2 and len(rest_list_calls(run)) == 2   # one fallback per label query
    argv = run.list_fallbacks[0]
    assert argv[:2] == ["issue", "list"] and "--limit" in argv and "--search" not in argv


def test_429_blocking_read_falls_back_exactly_once(gh_env):
    au = _mod("auto_unpark")
    run = _sweep_runner(list_fail=RATE_429, fallback_list=json.dumps([{"number": 9}]))
    source = au.sources.GitHubSource(_config(), run=run)
    assert au._fetch_blocking_issues(source) == {"9"}
    assert len(run.list_fallbacks) == 1 and run.list_fallbacks[0][run.list_fallbacks[0].index("--state") + 1] == "all"


def test_429_in_a_cloud_session_never_falls_back_and_stays_fail_open(monkeypatch):
    au = _mod("auto_unpark")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    run = _sweep_runner(list_fail=RATE_429, fallback_list=json.dumps([{"number": 9}]))
    source = au.sources.GitHubSource(_config(), run=run)
    assert au._fetch_blocking_issues(source) == set() and au._fetch_parked_issues_status(source) == ([], False)
    assert run.list_fallbacks == []


def test_a_non_list_page_is_incomplete_without_fallback(gh_env):
    au = _mod("auto_unpark")
    run = _sweep_runner(parked=json.dumps({"oops": 1}))
    source = au.sources.GitHubSource(_config(), run=run)
    assert au._fetch_parked_issues_status(source) == ([], False)
    assert run.list_fallbacks == [] and len(rest_list_calls(run)) == 2
