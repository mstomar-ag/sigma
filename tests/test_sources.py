"""Pluggable backlog sources: LocalSource (files, zero-dep) and GitHubSource (gh CLI).
GitHubSource talks to GitHub only through an injectable runner, so these tests are hermetic —
no network, no `gh` required."""
import json, pathlib, importlib.util, re, subprocess, tempfile
import pytest

import gqlfake

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


# --- #1391 step 2: fake-runner support for the GraphQL label swap --------------------------------
# Extracted to `tests/gqlfake.py` (#1392) once test_triage/test_auto_unpark needed the identical
# fake -- one implementation, so the three modules can never drift on what GitHub does.
_gql_swap = gqlfake.swap


def _issues_verb(a):
    """#1829: normalize either the old `gh issue list --label ...` shape or the new `gh api
    repos/{owner}/{repo}/issues` REST shape to the single token "list", so every fake `run()` in
    this file can keep gating on one string regardless of which transport `_fetch_pending`/
    `list_needs_label` actually construct. Falls back to the historical `args[1]`-or-`args[0]`
    verb read for anything that isn't an issues listing (label edits, comments, project calls,
    graphql, ...) — unchanged for those."""
    if len(a) > 1 and a[0] == "issue" and a[1] == "list":
        return "list"
    if len(a) > 1 and a[0] == "api" and str(a[1]).startswith("repos/") and str(a[1]).endswith("/issues"):
        return "list"
    legacy = gqlfake.legacy_of(a)       # #895 slice 3a: a REST issue write stands for its legacy verb
    if legacy:
        return legacy[0][1]
    return a[1] if len(a) > 1 else a[0]


def _legacy(a):
    """Flattened legacy-shaped argv of a REST issue write, so a fake can keep gating on `--remove-label`."""
    return [x for c in gqlfake.legacy_of(a) for x in c]


def _rest_labels(a):
    """#1829: the full AND-ed label set a fake `run()` was asked to filter on, from EITHER the
    old repeated `--label X` flags or the new comma-joined `-f labels=a,b` REST field — same
    return shape either way (a plain list of label strings, base label first), so a test written
    against the property "which labels were queried" reads the same regardless of transport."""
    for v in a:
        if isinstance(v, str) and v.startswith("labels="):
            return v[len("labels="):].split(",")
    return [a[i + 1] for i, x in enumerate(a) if x == "--label"]


def _recording_runner(by_subcommand=None):
    """Fake `gh` runner: records every call, returns canned stdout keyed by the gh verb (args[1]),
    with the old `issue list` shape and the new `api .../issues` REST shape (#1829) both mapped to
    the same `"list"` bucket key via `_issues_verb`."""
    calls = []
    labels = set()
    by_subcommand = by_subcommand or {}
    def run(args):
        gql = _gql_swap(args, labels=labels, calls=calls, repo_args=_repo_flag(args, calls))
        if gql is not None:
            return gql
        if gqlfake.is_issue_write(args):  # #895 slice 3a: REST writes, re-recorded legacy-shaped
            return gqlfake.rest_write(args, calls=calls, repo_args=_repo_flag(args, calls) or ("--repo", "o/r"))
        calls.append(list(args))
        if "rest" in by_subcommand:      # #895: a gh-shape dict answering gh_api.read_issue's REST GETs
            rest = gqlfake.rest_issue(args, lambda n, f: by_subcommand["rest"])
            if rest is not None:
                return rest
        return by_subcommand.get(_issues_verb(args), "")
    run.calls = calls
    run.labels = labels
    return run


def _repo_flag(args, calls):
    """Reuse whatever `--repo o/r` pair the non-graphql calls in this run already carry, so the
    synthetic add/remove records satisfy assertions like `all("--repo o/r" in c)`."""
    for c in calls:
        if "--repo" in c:
            i = c.index("--repo")
            return ("--repo", c[i + 1])
    return ()


# --- source selection ---

def test_get_source_defaults_to_local():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        assert type(src.get_source(d, {})).__name__ == "LocalSource"
        assert type(src.get_source(d, {"discovery": {"source": "local-goals"}})).__name__ == "LocalSource"


def test_get_source_github_when_configured():
    src = _mod("sources")
    s = src.get_source("/tmp", {"discovery": {"source": "github"}})
    assert type(s).__name__ == "GitHubSource"


# --- GitHubSource discovery ---

def test_github_next_pending_picks_lowest_open_non_parked():
    src = _mod("sources")
    issues = [
        {"number": 7, "labels": [{"name": "sdlc:goal"}]},
        {"number": 3, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"}]},  # parked -> skip
        {"number": 5, "labels": [{"name": "sdlc:goal"}]},
    ]
    run = _recording_runner({"list": json.dumps(issues)})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.next_pending() == "5"          # 3 parked; lowest of {5,7} is 5


def test_github_next_pending_excludes_in_progress_labelled_issues():
    """#1198: `mark_in_progress` writes `sdlc:in-progress` on every pick, durably and visibly to
    every session -- but `_fetch_pending`'s own post-fetch filter only ever excluded
    `parked_label`, so an issue the loop had itself just claimed could be re-offered as the TOP
    pick on the very next read. Reproduces the issue's own repro exactly: three issues, one
    in-progress, one parked, one plain -- only the plain one is a legitimate candidate."""
    src = _mod("sources")
    issues = [
        {"number": 101, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]},
        {"number": 102, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"}]},
        {"number": 103, "labels": [{"name": "sdlc:goal"}]},
    ]
    run = _recording_runner({"list": json.dumps(issues)})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.next_pending() == "103"        # 101 in-progress, 102 parked -- 103 is the only free one


def test_github_next_pending_excludes_a_half_promoted_needs_confirmation_issue():
    """#1392: the label path and the board path used to disagree about `sdlc:needs-confirmation`.

    `_card_is_eligible` (the board half, #1391 step 3b) has ALWAYS treated it as disqualifying;
    this filter never checked it. So an issue carrying BOTH `sdlc:goal` and
    `sdlc:needs-confirmation` -- exactly what a human produces by ADDING the goal label in the
    GitHub UI instead of REMOVING the proposal one -- was PICKED here and SKIPPED there. Same repo,
    same issue, opposite answers, decided by nothing but which `queue_source` the config uses.
    `doctor._multi_state_label_scan` and `reconcile.classify` both already called that combination
    drift; only the picker disagreed, and nothing tested it, which is how it survived.

    Excluding is the only reading consistent with what the label MEANS: it is the human approval
    gate (#233), the approval gesture is REMOVING it, so while it is present approval has not
    happened -- whatever else was added alongside. `/sigma-promote` performs that removal atomically.
    """
    src = _mod("sources")
    issues = [
        {"number": 201, "labels": [{"name": "sdlc:goal"},
                                    {"name": "sdlc:needs-confirmation"}]},
        {"number": 202, "labels": [{"name": "sdlc:goal"}]},
    ]
    run = _recording_runner({"list": json.dumps(issues)})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.next_pending() == "202"


def test_the_label_path_and_the_board_path_agree_on_every_primary_label():
    """The invariant behind the fix above, stated once: for each disqualifying label, BOTH halves
    must refuse the same issue. A divergence here is not a cosmetic inconsistency -- it makes the
    same issue pickable or not depending on a config key that is supposed to control ORDER, not
    eligibility."""
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=_recording_runner())
    for label in (gh.parked_label, gh.goal_blocked_label, gh.proposed_label):
        issues = [{"number": 301, "labels": [{"name": "sdlc:goal"}, {"name": label}]},
                  {"number": 302, "labels": [{"name": "sdlc:goal"}]}]
        gh_labelled = src.GitHubSource({"discovery": {"source": "github"}},
                                       run=_recording_runner({"list": json.dumps(issues)}))
        assert gh_labelled.next_pending() == "302", f"label path still picks {label}"
        assert gh._card_is_eligible(301, {"sdlc:goal", label}) is False, \
            f"board path still picks {label}"
        assert gh._card_is_eligible(302, {"sdlc:goal"}) is True


def test_github_next_pending_picks_lowest_open_non_blocked():
    # #1205: discovery.github.blocked_label, when set, must exclude an issue from the label queue
    # exactly the way parked_label already does -- this is the repo's own configurable "do not
    # auto-pick" convention (spend/legal/third-party-wait), independent of parked_label.
    src = _mod("sources")
    issues = [
        {"number": 7, "labels": [{"name": "sdlc:goal"}]},
        {"number": 3, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:blocked"}]},  # blocked -> skip
        {"number": 5, "labels": [{"name": "sdlc:goal"}]},
    ]
    run = _recording_runner({"list": json.dumps(issues)})
    cfg = {"discovery": {"source": "github", "github": {"blocked_label": "sdlc:blocked"}}}
    gh = src.GitHubSource(cfg, run=run)
    assert gh.next_pending() == "5"          # 3 blocked; lowest of {5,7} is 5


def test_github_blocked_label_unset_by_default_changes_nothing():
    # Default (unset) must be byte-compatible with prior behavior: a label that would match under
    # an explicit blocked_label is picked exactly as before when the key is absent. Deliberately
    # NOT "sdlc:blocked" -- #1350's own, separate, ALWAYS-ON `goal_blocked_label` also defaults to
    # that exact spelling (see the goal_blocked_label-specific tests below), so a label picked here
    # must be one neither mechanism recognizes, or this would stop isolating `blocked_label` (#1205)
    # from `goal_blocked_label` (#1350) -- two independent config keys that merely happen to share a
    # default string.
    src = _mod("sources")
    issues = [{"number": 3, "labels": [{"name": "sdlc:goal"}, {"name": "team:do-not-pick"}]}]
    run = _recording_runner({"list": json.dumps(issues)})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.blocked_label is None
    assert gh.next_pending() == "3"          # not excluded -- blocked_label was never configured


def test_github_blocked_label_excluded_across_batch_refill_skips():
    # #1205 acceptance: the exclusion must hold wherever parked_label is applied in
    # _fetch_pending -- including next-batch's repeated next_pending(skip=...) refill calls, not
    # just a bare first pick. A blocked issue must never surface no matter what else is in `skip`.
    src = _mod("sources")
    issues = [
        {"number": 3, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:blocked"}]},
        {"number": 5, "labels": [{"name": "sdlc:goal"}]},
    ]
    run = _recording_runner({"list": json.dumps(issues)})
    cfg = {"discovery": {"source": "github", "github": {"blocked_label": "sdlc:blocked"}}}
    gh = src.GitHubSource(cfg, run=run)
    assert gh.next_pending() == "5"                    # first slot: blocked issue never surfaces
    assert gh.next_pending(skip={"5"}) is None          # refill slot, #5 now claimed: still no #3


def test_github_next_pending_excludes_on_in_progress_or_blocked_independently_and_together():
    """Rebase-composition proof for #1205 onto #1198: `_fetch_pending`'s exclusion is now three
    clauses in the SAME filter (parked_label OR in_progress_label OR configured blocked_label),
    not two separate filters that happen to coexist. Four issues: one excluded ONLY by
    in_progress_label (#1198's clause), one excluded ONLY by blocked_label (#1205's clause, here
    configured), one excluded by BOTH clauses at once (proving the composition doesn't short-
    circuit or double-count), and one plain issue that is the sole legitimate candidate."""
    src = _mod("sources")
    issues = [
        {"number": 201, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]},
        {"number": 202, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:blocked"}]},
        {"number": 203, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"},
                                    {"name": "sdlc:blocked"}]},
        {"number": 204, "labels": [{"name": "sdlc:goal"}]},
    ]
    run = _recording_runner({"list": json.dumps(issues)})
    cfg = {"discovery": {"source": "github", "github": {"blocked_label": "sdlc:blocked"}}}
    gh = src.GitHubSource(cfg, run=run)
    # 204 is the only issue carrying none of the three exclusion labels.
    assert gh.next_pending() == "204"
    # With 204 claimed (skip), nothing remains: 201/202/203 are each excluded by at least one
    # clause, so the composed filter -- not just one half of it -- is doing the work.
    assert gh.next_pending(skip={"204"}) is None


# --- #1358 (against #1350's own gap, sources.py:824): `_fetch_pending`'s exclusion filter never
# checked `goal_blocked_label` -- so if `mark_blocked`'s own `--remove-label sdlc:goal` call
# (sources.py:1126) hits a transient gh error while its other two calls (`--remove-label
# sdlc:in-progress`, `--add-label sdlc:blocked`) both succeed, the issue is left carrying BOTH
# `sdlc:goal` AND `sdlc:blocked` at once -- not a coherent state, and (unlike the optional,
# human-set `blocked_label` from #1205 above) `goal_blocked_label` is ALWAYS on, code-managed, and
# never guarded by config, so this filter must always check it, unconditionally.


def test_github_next_pending_excludes_goal_blocked_labelled_issues():
    """Isolates the filter itself: an issue carrying both `sdlc:goal` and `sdlc:blocked` -- the
    exact double-primary-label state a partial `mark_blocked` failure produces -- must never be
    picked, with nothing else in the backlog to fall back to."""
    src = _mod("sources")
    issues = [{"number": 999, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:blocked"}]}]
    run = _recording_runner({"list": json.dumps(issues)})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.next_pending() is None


def test_next_pending_never_repicks_a_goal_left_double_labelled_by_a_partial_mark_blocked_failure():
    """#1358's regression, re-pointed for #1391 step 2.

    ORIGINALLY this test MANUFACTURED the incoherent `{sdlc:goal, sdlc:blocked}` state by making
    `mark_blocked`'s `--remove-label sdlc:goal` call raise while its other two calls succeeded.
    Step 2 makes that specific manufacture impossible -- the three calls became ONE all-or-nothing
    graphql swap, so mark_blocked can no longer land the add without the removes. That is the point
    of step 2, not a loss of coverage.

    The state itself is still reachable from OUTSIDE this code path, and those routes are the ones
    that actually matter in the wild (measured): a human hand-editing labels, an older clone still
    running the pre-#1391 multi-call shape (14 Sigma versions are cached on this device alone),
    or a second actor's loop racing this one. So the state is now constructed DIRECTLY, and the
    load-bearing half of the assertion -- `_fetch_pending` must never re-offer it -- is unchanged."""
    src = _mod("sources")
    labels = {"sdlc:goal", "sdlc:blocked"}          # however it got here, it must not be picked
    list_run = _recording_runner({"list": json.dumps(
        [{"number": 999, "labels": [{"name": n} for n in labels]}])})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=list_run)
    assert gh.next_pending() is None


def test_mark_blocked_can_no_longer_manufacture_the_double_labelled_state():
    """The step 2 guarantee that replaces the manufacture above: if the swap fails, it fails WHOLE.
    The goal keeps `sdlc:goal` and stays pickable (it redoes work -- recoverable) instead of ending
    up carrying two lifecycle labels, or none at all."""
    src = _mod("sources")
    labels = {"sdlc:goal"}

    def run(a):
        if len(a) >= 2 and a[0] == "api" and a[1] == "graphql":
            doc = next((x[len("query="):] for x in a if str(x).startswith("query=")), "")
            if doc.startswith("mutation"):
                raise RuntimeError("gh: HTTP 502 Bad Gateway")
        gql = _gql_swap(a, labels=labels)
        if gql is not None:
            return gql
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._LABEL_SWAP_RETRY_BASE = 0
    gh.mark_blocked("999")                                  # must not raise
    assert labels == {"sdlc:goal"}                          # all-or-nothing: nothing landed
    assert "sdlc:blocked" not in labels                     # never the incoherent pair


def test_github_next_pending_none_when_empty():
    src = _mod("sources")
    run = _recording_runner({"list": "[]"})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0    # hermetic: no real backoff sleeps
    assert gh.next_pending() is None


def test_github_next_pending_none_when_empty_retries_before_giving_up():
    """F447: a genuinely empty read must still be retried a bounded number of times before
    `next_pending` trusts it — an empty result on attempt 1 is indistinguishable, from here,
    from GitHub's search index not having caught up yet on a just-labelled issue."""
    src = _mod("sources")
    run = _recording_runner({"list": "[]"})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh.next_pending() is None
    assert len(run.calls) == gh._BACKLOG_READ_RETRIES     # exhausted every attempt, not just one


def test_github_next_pending_skips_leased_issues():
    """`skip` (goals a claim lease says belong to another loop) drops out of the queue, so the loop
    passes over an issue someone else is already working and takes the next free one."""
    src = _mod("sources")
    issues = [{"number": 5, "labels": [{"name": "sdlc:goal"}]},
              {"number": 7, "labels": [{"name": "sdlc:goal"}]}]
    run = _recording_runner({"list": json.dumps(issues)})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0    # hermetic: no real backoff sleeps
    assert gh.next_pending(skip={"5"}) == "7"          # 5 leased elsewhere -> next free is 7
    assert gh.next_pending(skip={"5", "7"}) is None     # both taken -> nothing free


def test_github_next_pending_requests_oldest_first_sort():
    """F12: the fetch must ask GitHub for ascending creation order — a bare listing has no ASC
    option and defaults to newest-first, which is the root of the bug below. #1829: this is now a
    REST `sort=created&direction=asc` pair, not the graphql-search-billed `--search
    sort:created-asc` qualifier it replaced."""
    src = _mod("sources")
    run = _recording_runner({"list": "[]"})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0    # hermetic: no real backoff sleeps
    gh.next_pending()
    flat = [" ".join(c) for c in run.calls]
    assert any("sort=created" in c and "direction=asc" in c for c in flat)


def test_github_next_pending_never_uses_the_graphql_search_field():
    """#1829: `gh issue list --label ...` is graphql-search-billed regardless of whether
    `--search` is also passed (confirmed live with GH_DEBUG=api against the real repo, see
    .sdlc/research/1829-rest-backlog-pick.md) — the pick must never construct that shape again,
    on either the base fetch or any priority/blocking widening query."""
    src = _mod("sources")
    run = _recording_runner({"list": "[]"})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    gh.next_pending()
    assert not any(len(c) > 1 and c[0] == "issue" and c[1] == "list" for c in run.calls)
    assert any(c and c[0] == "api" and str(c[1]).endswith("/issues") for c in run.calls)


def test_github_rest_issue_fetch_always_forces_GET():
    """#1829: `gh api` defaults to POST the instant any `-f` field is supplied (verified live
    against a real, safe GET-only endpoint — see the research dossier) — omitting `--method GET`
    against a real issues-list endpoint would attempt to CREATE an issue. Every REST call this
    class makes for a listing must force GET explicitly."""
    src = _mod("sources")
    run = _recording_runner({"list": "[]"})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    gh.next_pending()
    api_calls = [c for c in run.calls if c and c[0] == "api"]
    assert api_calls, "expected at least one REST api call"
    assert all("--method" in c and c[c.index("--method") + 1] == "GET" for c in api_calls)


def test_github_rest_issue_fetch_filters_out_pull_requests():
    """#1829: REST's issues-list endpoint returns PR-shaped items too (verified live) — the
    graphql search() query this replaces always carried an implicit `type:issue` qualifier that
    excluded them. A PR carrying the goal label by accident must never be pickable."""
    src = _mod("sources")
    issues = [{"number": 5, "labels": [{"name": "sdlc:goal"}], "pull_request": {"url": "x"}},
              {"number": 6, "labels": [{"name": "sdlc:goal"}]}]
    run = _recording_runner({"list": json.dumps(issues)})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh.next_pending() == "6"


def test_github_rest_issue_fetch_paginates_up_to_the_cap():
    """#1829: REST's own per-page ceiling is 100, below `_BACKLOG_FETCH_CAP` (200) — a backlog
    bigger than one page must be assembled from multiple `page=`-incrementing calls, using the
    PRE-filter page length to decide whether to fetch another page (so a PR-heavy page can never
    look short and truncate pagination early)."""
    src = _mod("sources")
    all_issues = [{"number": n, "labels": [{"name": "sdlc:goal"}]} for n in range(1, 151)]  # 150 > one page

    def run(a):
        if a and a[0] == "api":
            page = int(next(v.split("=", 1)[1] for v in a if v.startswith("page=")))
            per_page = int(next(v.split("=", 1)[1] for v in a if v.startswith("per_page=")))
            start = (page - 1) * per_page
            return json.dumps(all_issues[start:start + per_page])
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    raw_count, pending = gh._fetch_pending([], ())
    assert raw_count == 150
    assert {p["number"] for p in pending} == set(range(1, 151))


def test_github_rest_issue_fetch_pagination_survives_a_filtered_item_mid_sequence():
    """POST-PR REVIEW FIX: a real bug an independent post-PR reviewer found and reproduced live,
    caught by neither Plan-Review, the pre-PR review, nor any of the ~5700 tests that existed
    before this one -- because none of them combined a MULTI-PAGE fetch with an item the PR filter
    removes. `test_github_rest_issue_fetch_paginates_up_to_the_cap` (above) proves pagination
    across pages; `test_github_rest_issue_fetch_filters_out_pull_requests` (above) proves PR
    filtering; neither proves they compose correctly, and they did not.

    The bug: an earlier version of `_fetch_issues_rest` recomputed `per_page` each iteration as
    `min(_REST_PAGE_SIZE, cap - len(collected))`, using the FILTERED count. GitHub computes a
    request's offset as `(page-1)*per_page` from THAT request's own `per_page` -- so the instant a
    page contained a filtered-out PR, `collected` fell behind the raw fetch count, `per_page`
    shrank, and the next `page=N` silently re-indexed into the WRONG raw offset. Reproduced live
    exactly as this fixture does it (a backlog spanning >2 pages with one PR-shaped item on the
    first page): the result looked complete (still `cap` items, no error) but silently DUPLICATED
    an early issue and DROPPED the true next-oldest one beyond the window -- no exception, no
    `read_degraded()` signal, indistinguishable from a correct read without checking every number.

    This fixture's fake computes each response from the REQUEST's own `page`/`per_page` fields
    (real GitHub REST offset semantics: `start = (page-1)*per_page`), not from a running index the
    fake itself tracks -- a fake that tracked its own position would paper over exactly this bug,
    since it would ignore whatever (wrong) page/per_page the code asked for. One PR-shaped item
    sits at raw position 50 (comfortably inside page 1 of 100), and the backlog is deep enough
    (220 raw items) to force 3 real pages even after the one filtered item, so a regression here
    fails whether the shrinkage happens on page 2 or page 3."""
    src = _mod("sources")
    raw_backlog = []
    for i in range(220):
        if i == 50:
            raw_backlog.append({"number": 9000, "labels": [{"name": "sdlc:goal"}],
                                 "pull_request": {"url": "https://example/pulls/9000"}})
        else:
            raw_backlog.append({"number": i + 1, "labels": [{"name": "sdlc:goal"}]})

    def run(a):
        if not (a and a[0] == "api"):
            return ""
        page = int(next(v.split("=", 1)[1] for v in a if v.startswith("page=")))
        per_page = int(next(v.split("=", 1)[1] for v in a if v.startswith("per_page=")))
        start = (page - 1) * per_page
        return json.dumps(raw_backlog[start:start + per_page])

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    result = gh._fetch_issues_rest(["sdlc:goal"], 200)
    numbers = [r["number"] for r in result]
    # Real issue numbers, in true creation order, accounting for the PR occupying raw slot 50
    # (so issue number 51 never exists in this fixture -- that slot is the PR): 1-50, then 52-201.
    expected = list(range(1, 51)) + list(range(52, 202))
    assert numbers == expected, (
        "pagination must survive a filtered item without duplicating or dropping any issue -- "
        f"got {len(numbers)} items, duplicates={sorted({n for n in numbers if numbers.count(n) > 1})}, "
        f"missing={sorted(set(expected) - set(numbers))}")
    assert 9000 not in numbers, "the PR itself must never appear in the result"


def test_github_rest_issue_fetch_ANDs_labels_via_comma_join():
    """#1829: GitHub REST's `labels=` param comma-joins as AND (verified live: identical result
    set/order to repeated `--label` flags on `gh issue list`) — an extra widening/blocking label
    must be ANDed onto the base goal label in the SAME query, never OR'd or dropped."""
    src = _mod("sources")
    run = _recording_runner({"list": "[]"})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    gh._fetch_pending(["priority:P0"], ())
    api_calls = [c for c in run.calls if c and c[0] == "api"]
    assert api_calls, "expected at least one REST api call"
    assert all(_rest_labels(c) == ["sdlc:goal", "priority:P0"] for c in api_calls)


def test_github_next_pending_true_oldest_survives_200_cap():
    """F12: a bare listing defaults to created-DESC with no ASC option, so fetching only 200 of a
    bigger backlog used to fetch the 200 NEWEST goals; sorting THOSE ascending and taking [0]
    returned the oldest-of-the-newest-200 (here, issue 51) instead of the TRUE oldest (issue 1) --
    the genuinely old, highest-priority-under-oldest-first goals starved until newer ones drained
    the backlog below 200.

    #1829: this fake now simulates REST pagination — it hands back true ascending-by-creation
    pages (as the real `sort=created&direction=asc` REST params guarantee) rather than sniffing a
    `--search` flag, over a 250-issue backlog spanning 3 REST pages (100+100+50) against the
    200-item cap. Issue number doubles as creation order (issue 1 is oldest)."""
    src = _mod("sources")
    all_issues = [{"number": n, "labels": [{"name": "sdlc:goal"}]} for n in range(1, 251)]  # 250 > 200 cap

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        page = int(next((v.split("=", 1)[1] for v in a if v.startswith("page=")), "1"))
        per_page = int(next((v.split("=", 1)[1] for v in a if v.startswith("per_page=")), "100"))
        start = (page - 1) * per_page
        return json.dumps(all_issues[start:start + per_page])

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.next_pending() == "1"          # true oldest, not "51" (oldest of the newest-200 slice)


def test_github_a_deep_p0_beyond_the_200_cap_is_still_found():
    """#815: the 200-oldest window can be entirely unprioritised (or low-priority) while a P0 sits
    beyond it, invisible to the wide query — this is the fully-general form of #813's fix, one
    layer up: sorting only re-orders what got FETCHED, and the fetch itself is capped. When the cap
    is genuinely hit, a small targeted query per tier more urgent than the window's own best pick
    must find it."""
    src = _mod("sources")
    window = [{"number": n, "labels": [{"name": "sdlc:goal"}]} for n in range(1, 201)]   # 200, unprioritised
    hidden_p0 = {"number": 999, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P0"}]}

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        labels = _rest_labels(a)
        if "priority:P0" in labels:
            return json.dumps([hidden_p0])       # the targeted, tier-specific re-query
        return json.dumps(window)                 # the base wide query: capped at 200, no P0 visible

    gh = src.GitHubSource({"discovery": {"source": "github",
                                         "blocking_priority_override": False}}, run=run)
    assert gh.next_pending() == "999"


def test_github_under_the_cap_never_issues_a_widening_query():
    """Fully inert path: when the raw fetch comes back under the cap, nothing beyond it could
    exist, so no extra per-tier queries should ever fire.

    Independent review, before merge: a first version of this test made a label-scoped re-query
    RAISE to detect it firing — but `_fetch_pending` wraps its own fetch in a broad `except
    Exception`, which swallows an `AssertionError` from inside the fake `run` exactly like it
    would a real transient gh error, converting it to a silent `(0, None)` rather than propagating
    it up through `next_pending()` — so that version could never actually fail, regardless of
    whether the widening path fired. This version records every call instead and asserts on the
    log AFTER the fact, immune to whatever exception handling sits between the fake and the
    assertion (the same call-recording shape `_recording_runner` already uses elsewhere in this
    file, rather than a novel raise-based mechanism)."""
    src = _mod("sources")
    small = [{"number": n, "labels": [{"name": "sdlc:goal"}]} for n in range(1, 51)]      # well under 200
    calls = []

    def run(a):
        calls.append(list(a))
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        return json.dumps(small)

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.next_pending() == "1"                    # the pick itself is still correct
    widening_calls = [c for c in calls if any(l.startswith("priority:") for l in _rest_labels(c))]
    assert widening_calls == [], f"a widening re-query fired even though the cap was never hit: {widening_calls}"


def test_github_order_created_ignores_a_hidden_priority_beyond_the_cap():
    """Independent review, before merge: `order: "created"`'s whole contract is that priority
    never affects the pick (`_pick_key`'s created-order branch: one bucket, number alone). Without
    an explicit guard, the widening loop doesn't know this — `best_rank` still evaluates to
    UNPRIORITISED for every window under this mode, so it would fire on every cap-hit and let ANY
    priority-labelled issue beyond the window hijack the answer, even one NEWER than the window's
    true oldest. Reproduced directly before the fix landed: this exact fixture returned "99999"
    instead of the correct "1". `order: "created"` must ignore priority entirely, cap or no cap."""
    src = _mod("sources")
    window = [{"number": n, "labels": [{"name": "sdlc:goal"}]} for n in range(1, 201)]
    hidden_p0 = {"number": 99999, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P0"}]}

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        labels = _rest_labels(a)
        if "priority:P0" in labels:
            return json.dumps([hidden_p0])
        return json.dumps(window)

    gh = src.GitHubSource({"discovery": {"source": "github", "order": "created"}}, run=run)
    assert gh.next_pending() == "1"                    # true oldest, never hijacked by priority


def test_github_a_deep_priority_beyond_the_cap_is_found_at_a_middle_tier_too():
    """Independent review, before merge: the original fixture only ever hid a P0 beyond an
    entirely-unprioritised window — the extreme case. A bug that broke checking of a MIDDLE tier
    specifically (e.g. an off-by-one skipping P2) would have shipped silently, since nothing
    exercised anything but the two ends. Window's own best is P3; a P1 (not P0) sits beyond it —
    must still be found, and the P2 tier between them must genuinely get queried too (not skipped)."""
    src = _mod("sources")
    window = [{"number": n, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P3"}]}
              for n in range(1, 201)]
    hidden_p1 = {"number": 999, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P1"}]}
    queried_tiers = []

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        labels = _rest_labels(a)
        tiers = [l for l in labels if l.startswith("priority:")]
        if tiers:
            queried_tiers.append(tiers[0])
        if "priority:P1" in labels:
            return json.dumps([hidden_p1])
        if "priority:P0" in labels:
            return json.dumps([])                       # genuinely nothing at the more-urgent P0 tier
        return json.dumps(window)

    gh = src.GitHubSource({"discovery": {"source": "github",
                                         "blocking_priority_override": False}}, run=run)
    assert gh.next_pending() == "999"
    assert "priority:P0" in queried_tiers, "P0 (more urgent than the window's own P3 best) must be checked"
    assert "priority:P1" in queried_tiers, "P1 (the tier that actually holds the hidden issue) must be checked"


def test_github_widening_search_finds_an_alias_labelled_issue_beyond_the_cap():
    """Third-round independent-review finding on #854: the widening search only ever constructed
    literal `priority:P<n>` label queries -- an issue labelled with an ALIAS spelling
    (`priority:critical`, aliased to P0) hiding beyond the 200-issue window was invisible to it,
    even though `priority_aliases` is configured and every other picking path already resolves it.
    Reopens the exact failure class #815 exists to close, specifically for the alias case."""
    src = _mod("sources")
    window = [{"number": n, "labels": [{"name": "sdlc:goal"}]} for n in range(1, 201)]
    hidden_critical = {"number": 999, "labels": [{"name": "sdlc:goal"}, {"name": "priority:critical"}]}
    queried_labels = []

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        labels = _rest_labels(a)
        tiers = [l for l in labels if l.startswith("priority:")]
        if not tiers:
            return json.dumps(window)                  # the base, unfiltered fetch
        queried_labels.append(tiers[0])
        if "priority:critical" in labels:
            return json.dumps([hidden_critical])
        return json.dumps([])                           # genuinely nothing at any literal tier

    cfg = {"discovery": {"source": "github",
                                         "blocking_priority_override": False, "priority_aliases": {"critical": "P0"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRIES = 1
    assert gh.next_pending() == "999"
    assert "priority:critical" in queried_labels, "the configured alias for P0 must be checked too"


def test_github_widening_search_unset_aliases_never_queries_alias_labels():
    """Backward compatibility: with no priority_aliases configured, the widening search must query
    only literal P0-P4 label text, exactly as before this feature existed. Every widened tier comes
    back genuinely empty (mirrors test_github_a_deep_priority_beyond_the_cap_is_found_at_a_middle_
    tier_too's own fixture shape), so the loop actually runs all four more-urgent tiers rather than
    stopping after the first."""
    src = _mod("sources")
    window = [{"number": n, "labels": [{"name": "sdlc:goal"}]} for n in range(1, 201)]
    queried_labels = []

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        labels = _rest_labels(a)
        tiers = [l for l in labels if l.startswith("priority:")]
        if tiers:
            queried_labels.append(tiers[0])
            return json.dumps([])                       # genuinely nothing at any widened tier
        return json.dumps(window)

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRIES = 1
    gh.next_pending()
    assert queried_labels == ["priority:P0", "priority:P1", "priority:P2", "priority:P3", "priority:P4"]


def test_github_widening_search_merges_canonical_and_older_alias_hit_at_same_tier():
    """#861: within a single widened tier, the loop over `label_texts = [tier_name] + [aliases...]`
    used to RETURN on the first non-empty query -- canonical (`priority:P0`) is always checked
    before any alias spelling, so whenever the canonical query alone came back non-empty the alias
    query never even ran, regardless of whether an OLDER issue was sitting under the alias spelling.
    Reproduces the issue's own scenario directly: a canonical-labelled P0 (#500, newer) and an
    alias-labelled ("critical") P0 (#300, genuinely older) both beyond the 200-cap window, at the
    same tier -- the true oldest across BOTH spellings must win, not merely the oldest within
    whichever spelling was queried first."""
    src = _mod("sources")
    # Window's own best is P1 -- only the P0 tier (strictly more urgent) needs widening.
    window = [{"number": n, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P1"}]}
              for n in range(1, 201)]
    canonical_p0 = {"number": 500, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P0"}]}
    alias_p0 = {"number": 300, "labels": [{"name": "sdlc:goal"}, {"name": "priority:critical"}]}
    queried_labels = []

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        labels = _rest_labels(a)
        tiers = [l for l in labels if l.startswith("priority:")]
        if not tiers:
            return json.dumps(window)                  # the base wide query
        queried_labels.append(tiers[0])
        if "priority:P0" in labels:
            return json.dumps([canonical_p0])           # canonical spelling: the newer issue
        if "priority:critical" in labels:
            return json.dumps([alias_p0])                # alias spelling: the genuinely older issue
        return json.dumps([])

    cfg = {"discovery": {"source": "github",
                                         "blocking_priority_override": False, "priority_aliases": {"critical": "P0"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRIES = 1
    assert gh.next_pending() == "300", (
        "the genuinely older alias-labelled issue must win, not the newer canonical one that "
        "merely happened to be queried first")
    assert "priority:P0" in queried_labels
    assert "priority:critical" in queried_labels, "the alias query must run even though canonical was non-empty"


# --- GitHubSource transitions ---

def test_github_transitions_issue_correct_gh_commands():
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    gh.mark_in_progress("5")
    gh.complete("5")
    gh.park("9", "hit a deploy gate")
    flat = [" ".join(c) for c in run.calls]
    assert any("issue edit 5" in c and "--add-label sdlc:in-progress" in c for c in flat)
    assert any("issue close 5" in c for c in flat)
    assert any("issue edit 9" in c and "--add-label sdlc:parked" in c for c in flat)
    assert any("issue comment 9" in c and "hit a deploy gate" in c for c in flat)
    assert any(c.startswith("label create") for c in flat)            # labels auto-ensured
    # CLI commands carry `--repo`; the bounded REST label inventory scopes the same repository
    # in its endpoint. Keep both forms covered without treating a read as a malformed CLI write.
    assert all("--repo o/r" in c or c.startswith("api repos/o/r/") for c in flat)


def test_github_custom_labels_respected():
    src = _mod("sources")
    run = _recording_runner({"list": "[]"})
    cfg = {"discovery": {"source": "github", "github": {"goal_label": "goal", "parked_label": "blocked"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0    # hermetic: no real backoff sleeps
    gh.next_pending()
    assert any("goal" in _rest_labels(c) for c in run.calls if c and c[0] == "api")  # custom goal label used


def test_github_next_pending_no_assignee_filter_by_default():
    src = _mod("sources")
    run = _recording_runner({"list": "[]"})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0    # hermetic: no real backoff sleeps
    gh.next_pending()
    assert not any("--assignee" in " ".join(c) for c in run.calls)   # absent config -> no filter (byte-compatible)


def test_github_next_pending_assignee_filter_when_configured():
    """#1829: the REST `assignee=` param does NOT understand the `@me` convenience value the way
    `gh issue list --assignee @me` does (verified live: a literal `@me` gets a hard 422 from the
    real API) — the query must carry the RESOLVED login instead."""
    src = _mod("sources")
    run = _recording_runner({"user": "some-login", "list": "[]"})
    cfg = {"discovery": {"source": "github", "github": {"assignee": "@me"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0    # hermetic: no real backoff sleeps
    gh.next_pending()
    flat = [" ".join(c) for c in run.calls]
    assert any("assignee=some-login" in c for c in flat)   # scopes the discovery queue to one owner
    assert not any("assignee=@me" in c for c in flat), "REST rejects a literal @me with a 422"


# --- #1216: client-side assignee check, so a missing --assignee cannot widen the pool ------------

def _assigned(number, login, labels=("sdlc:goal",)):
    return {"number": number, "labels": [{"name": l} for l in labels],
            "assignees": ([{"login": login}] if login else [])}


def test_next_pending_excludes_an_issue_assigned_to_someone_else():
    """The #1216 bug, reproduced live twice: the picker returned another collaborator's issue and
    only noticed AFTER picking -- then parked it, which dequeues it from its RIGHTFUL owner too."""
    src = _mod("sources")
    issues = [_assigned(5, "someone-else"), _assigned(6, "me-login")]
    run = _recording_runner({"user": "me-login", "list": json.dumps(issues)})
    cfg = {"discovery": {"source": "github", "github": {"assignee": "@me"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert [i["number"] for i in gh._fetch_pending([], ())[1]] == [6]


def test_at_me_is_resolved_to_a_login_not_compared_literally():
    """`@me` is a gh SERVER-SIDE alias. Comparing it literally against assignees[].login matches
    nothing and would filter the WHOLE backlog away -- a picker outage, not a safety check."""
    src = _mod("sources")
    issues = [_assigned(5, "someone-else"), _assigned(6, "me-login")]
    run = _recording_runner({"user": "me-login", "list": json.dumps(issues)})
    cfg = {"discovery": {"source": "github", "github": {"assignee": "@me"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    # BOTH halves: the alias resolves, AND the resolved value actually excludes. Without the
    # second issue this test passed even with the filter deleted.
    assert [i["number"] for i in gh._fetch_pending([], ())[1]] == [6]
    assert gh._assignee_login() == "me-login"


def test_a_login_configured_with_display_casing_still_matches():
    """S1: GitHub logins are case-insensitive for identity but `assignees[].login` is canonical
    case. Comparing exactly meant a handle copied off a profile page emptied the whole backlog."""
    src = _mod("sources")
    issues = [_assigned(5, "other"), _assigned(6, "me-login")]
    run = _recording_runner({"list": json.dumps(issues)})
    cfg = {"discovery": {"source": "github", "github": {"assignee": "Me-Login"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert [i["number"] for i in gh._fetch_pending([], ())[1]] == [6]


def test_a_resolution_failure_is_retried_not_cached_forever():
    """S2: failing open on ONE call is the trade; making it sticky and silent is not -- a loop that
    blipped once at startup would otherwise run its whole session unguarded."""
    src = _mod("sources")
    issues = [_assigned(5, "someone-else"), _assigned(6, "me-login")]
    state = {"fail": True}
    def run(argv):
        line = " ".join(argv)
        if "api user" in line:
            if state["fail"]:
                state["fail"] = False
                raise RuntimeError("gh: could not authenticate")
            return "me-login"
        return json.dumps(issues)
    cfg = {"discovery": {"source": "github", "github": {"assignee": "@me"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh._assignee_login() is None                       # first call: fails open
    assert gh._assignee_login() == "me-login"                 # retried, not cached as None
    assert [i["number"] for i in gh._fetch_pending([], ())[1]] == [6]


def test_the_query_actually_requests_assignees():
    """Regression guard, #1829-adapted: REST has no field-selection concept (every issue object
    always carries `assignees` — verified live), so the original risk ("assignees missing from
    --json, every issue looks unassigned") cannot recur under the new transport. The equivalent
    risk now is the `assignee=` REST field itself silently going missing from the constructed
    query, which would widen the candidate pool to every owner's issues instead of narrowing it —
    the opposite failure direction, so it must ship on every call once configured."""
    src = _mod("sources")
    run = _recording_runner({"user": "me-login", "list": "[]"})
    cfg = {"discovery": {"source": "github", "github": {"assignee": "@me"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    gh.next_pending()
    # scoped to the issues-listing calls specifically -- NOT the `api user` call that resolves
    # `@me`, which naturally carries no `assignee=` field of its own
    api_calls = [c for c in run.calls if c and c[0] == "api" and str(c[1]).endswith("/issues")]
    assert api_calls, "expected at least one REST issues-listing call"
    assert all(any(v == "assignee=me-login" for v in c) for c in api_calls)


def test_a_literal_login_is_matched_with_or_without_the_at_sign():
    src = _mod("sources")
    issues = [_assigned(5, "other"), _assigned(6, "someone")]
    for configured in ("someone", "@someone"):
        run = _recording_runner({"list": json.dumps(issues)})
        cfg = {"discovery": {"source": "github", "github": {"assignee": configured}}}
        gh = src.GitHubSource(cfg, run=run)
        gh._BACKLOG_READ_RETRY_BASE = 0
        assert [i["number"] for i in gh._fetch_pending([], ())[1]] == [6], configured
        assert not any("api user" in " ".join(c) for c in run.calls)   # no lookup needed


def test_no_configured_assignee_leaves_every_issue_pickable():
    """Acceptance criterion: existing multi-owner installs must behave exactly as before."""
    src = _mod("sources")
    issues = [_assigned(5, "a"), _assigned(6, "b"), _assigned(7, None)]
    run = _recording_runner({"list": json.dumps(issues)})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert [i["number"] for i in gh._fetch_pending([], ())[1]] == [5, 6, 7]


def test_the_primary_picker_fails_closed_on_a_persistent_unresolvable_login():
    """#1837: the label-queue's OWN picker (`primary=True`, the default) must REFUSE an unscoped
    read rather than silently widen the candidate pool to the whole repo, once assignee resolution
    has failed on EVERY retry attempt (not just a transient blip -- see
    `test_an_unresolvable_login_self_heals_across_retry_attempts` for that case, which is
    unaffected by this change). This is the AGENTS.md SAFETY rule directly: "refuses loudly rather
    than proceeding weakly" -- a shared-board install configuring `assignee: "@me"` is opting into
    an ownership guarantee, and a sustained `gh api user` outage must not silently drop it."""
    src = _mod("sources")
    def run(argv):
        line = " ".join(argv)
        if "api user" in line:
            raise RuntimeError("gh: could not authenticate")
        return json.dumps([_assigned(6, "me-login")])
    cfg = {"discovery": {"source": "github", "github": {"assignee": "@me"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh._assignee_login() is None
    assert gh._fetch_pending([], ()) == (0, None)     # default primary=True refuses, not just filters
    assert gh.read_degraded() is True, "a refused read is a give-up, not a genuine empty backlog"


def test_the_uncarded_safety_net_still_fails_open_on_a_persistent_unresolvable_login():
    """#1837: `next_pending`'s board-mode "labelled but uncarded" safety net (`primary=False`)
    keeps the PRE-#1837 fail-open contract on purpose, even under the same persistent failure that
    now makes the primary picker refuse above -- the board's own Ready lane is the real ownership
    boundary in that mode (see `test_board_fails_open_when_the_login_cannot_be_resolved` in
    test_github_project.py), and this fallback only ever adds to what Ready already found."""
    src = _mod("sources")
    def run(argv):
        line = " ".join(argv)
        if "api user" in line:
            raise RuntimeError("gh: could not authenticate")
        return json.dumps([_assigned(6, "me-login")])
    cfg = {"discovery": {"source": "github", "github": {"assignee": "@me"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh._assignee_login() is None
    assert [i["number"] for i in gh._fetch_pending([], (), primary=False)[1]] == [6]
    assert gh.read_degraded() is False, "the safety net's fail-open path is not a give-up"


def test_an_unresolvable_login_self_heals_across_retry_attempts():
    """#1829's actual fix, proven directly: resolving `login` fresh INSIDE the retry loop (once
    per attempt) rather than once for the whole call, so a resolution that fails on attempt 1 gets
    a genuinely fresh try on attempt 2 -- not a stale, permanently-cached `None` that would have
    left the WHOLE call (every attempt) unscoped under the pre-fix design.

    Attempt 1: the issues-list call legitimately comes back empty (an ordinary, assignee-unrelated
    "nothing yet" -- the SAME trigger `test_next_pending_recovers_from_a_stale_empty_read_on_retry`
    already covers), which is what earns this read its second attempt; resolving `login` also
    happens to fail on this same attempt, but has no chance to matter yet since there is nothing
    to filter. Attempt 2: resolution SUCCEEDS this time, and the fetch returns both `me-login`'s
    issue and `someone-else`'s. Before #1829's per-attempt fix, `login` would already be
    permanently `None` from attempt 1 (resolved once, outside the loop) regardless of attempt 2's
    own success, so `someone-else`'s issue would leak through unscoped; this proves it does not."""
    src = _mod("sources")
    calls = {"user": 0, "list": 0}
    def run(argv):
        line = " ".join(argv)
        if "api user" in line:
            calls["user"] += 1
            if calls["user"] == 1:
                raise RuntimeError("gh: could not authenticate")
            return "me-login"
        calls["list"] += 1
        if calls["list"] == 1:
            return "[]"          # attempt 1: an ordinary, assignee-unrelated empty read
        return json.dumps([_assigned(6, "me-login"), _assigned(7, "someone-else")])
    cfg = {"discovery": {"source": "github", "github": {"assignee": "@me"}}}
    gh = src.GitHubSource(cfg, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    raw_count, pending = gh._fetch_pending([], ())
    assert [i["number"] for i in pending] == [6], (
        "attempt 2's OWN successful resolution must scope attempt 2's own fetch -- #7 (someone "
        "else's issue) must never leak through just because attempt 1 could not resolve the login")
    assert gh.read_degraded() is False, "a read that succeeded on retry is not degraded"


def test_github_next_pending_tolerates_null_or_nameless_labels():
    src = _mod("sources")
    issues = [{"number": 5, "labels": None}, {"number": 6, "labels": [{}]}]  # null + a label with no name
    run = _recording_runner({"list": json.dumps(issues)})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.next_pending() == "5"          # no crash; lowest open goal


def test_park_that_cannot_write_its_labels_leaves_the_goal_PICKABLE_not_invisible():
    """#1391 step 2 -- a DELIBERATE inversion of this test's original contract, and the single most
    important behavioural consequence of the atomic swap.

    BEFORE: `_offboard` removed `sdlc:goal` first and unconditionally, so a park whose `--add-label
    sdlc:parked` failed still de-listed the goal. The issue ended up carrying NO lifecycle label at
    all -- invisible to every label query in the system, and therefore permanently stuck. Measured
    live: that zero-label class exists on both real boards and nothing can find it.

    AFTER: the removes and the add land together or not at all. A park that cannot write its labels
    leaves the goal fully labelled and therefore RE-PICKABLE. The worst case becomes redoing work,
    which the next pick recovers from on its own -- never silent invisibility, which nothing
    recovers from. This is the governing principle of the whole backbone effort, so it is asserted
    directly rather than left implicit."""
    src = _mod("sources")
    issues = {5: {"open": True, "labels": {"sdlc:goal"}}}

    def run(a):
        if a[0] == "label":
            raise RuntimeError("no labels:write")
        if len(a) >= 2 and a[0] == "api" and a[1] == "graphql":
            doc = next((x[len("query="):] for x in a if str(x).startswith("query=")), "")
            if doc.startswith("mutation"):
                raise RuntimeError("label not found")        # the whole swap fails
        gql = _gql_swap(a, labels=issues[5]["labels"])
        if gql is not None:
            return gql
        verb = _issues_verb(a)
        if verb == "list":
            want = _rest_labels(a)[0]
            return json.dumps([{"number": k, "labels": [{"name": l} for l in v["labels"]]}
                               for k, v in issues.items() if v["open"] and want in v["labels"]])
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    gh._LABEL_SWAP_RETRY_BASE = 0
    assert gh.next_pending() == "5"
    gh.park("5", "deploy gate")                       # must not raise
    assert issues[5]["labels"] == {"sdlc:goal"}       # nothing half-applied; no zero-label limbo
    assert gh.next_pending() == "5"                   # recoverable: it gets re-picked and redone


def _partial_park_failure_runner(labels, rest_labels_fails=False, remove_always_fails=False):
    """#722: simulate the partial failure `_swap_labels`' docstring admits is possible -- the swap's
    ADD (`+sdlc:parked`) commits, then its REMOVE half fails. GraphQL root mutations do not roll
    back, so this is a real end state, not a contrivance. A mutation document carrying BOTH halves
    applies only the add to `labels` and raises a transient 502 (so the swap's own retries also
    fail identically); a REMOVE-ONLY document (what the repair sends) succeeds unless
    `remove_always_fails`. `issue_reads` counts REST reads of the issue's labels."""
    import gqlfake
    stats = {"issue_reads": 0, "mutations": []}

    def run(a):
        doc = next((x[len("query="):] for x in a if str(x).startswith("query=")), "")
        if len(a) >= 2 and a[0] == "api" and a[1] == "graphql" and doc.startswith("mutation"):
            has_add, has_rm = "a: addLabelsToLabelable" in doc, "r: removeLabelsFromLabelable" in doc
            stats["mutations"].append(("add" if has_add else "") + ("rm" if has_rm else ""))
            if has_add and has_rm:
                labels.add("sdlc:parked")                       # only the add half committed
                raise RuntimeError("gh: HTTP 502 Bad Gateway")
            if has_rm and remove_always_fails:
                raise RuntimeError("gh: HTTP 502 Bad Gateway")
        if a[0] == "api" and len(a) >= 2 and a[1].endswith("/comments"):
            return json.dumps({"id": 1})                        # #895 slice 8: note()'s REST POST is no read
        if a[0] == "api" and len(a) >= 2 and a[1].startswith("repos/") and "/issues/" in a[1]:
            stats["issue_reads"] += 1
            if rest_labels_fails:
                raise RuntimeError("gh: HTTP 502 Bad Gateway")
            return json.dumps({"body": "", "labels": [{"name": n} for n in labels]})
        gql = gqlfake.swap(a, labels=labels)
        return gql if gql is not None else ""
    return run, stats


def test_partial_park_failure_is_repaired_not_left_as_goal_plus_parked():
    """#722: the add of `sdlc:parked` lands, the remove of `sdlc:goal` does not -> goal+parked, the
    contradiction triage's hygiene bucket detects. `_offboard` must repair it itself (no human, no
    triage plan): re-read the labels over REST, see parked present, and finish the removal."""
    src = _mod("sources")
    labels = {"sdlc:goal", "sdlc:in-progress"}
    run, stats = _partial_park_failure_runner(labels)
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    gh._LABEL_SWAP_RETRY_BASE = 0
    gh.park("5", "deploy gate")
    assert labels == {"sdlc:parked"}, labels
    assert stats["mutations"][-1] == "rm", "the repair is a REMOVE-ONLY swap, never a second add-first one"


def test_partial_park_repair_never_delists_a_goal_whose_park_marker_did_not_land():
    """#722 / #1391 invariant: if the read shows `sdlc:parked` ABSENT, the add never landed -- removing
    the goal label now would produce the invisible zero-marker limbo. Repair must do nothing."""
    src = _mod("sources")
    labels = {"sdlc:goal"}

    def run(a):
        import gqlfake
        doc = next((x[len("query="):] for x in a if str(x).startswith("query=")), "")
        if doc.startswith("mutation") and "a: addLabelsToLabelable" in doc:
            raise RuntimeError("gh: HTTP 502 Bad Gateway")        # the swap fails, nothing lands
        # a remove-only mutation WOULD succeed -- so only the parked-present guard stands between
        # this goal and being de-listed with no park marker
        if a[0] == "api" and len(a) >= 2 and a[1].startswith("repos/"):
            return json.dumps({"body": "", "labels": [{"name": n} for n in labels]})
        gql = gqlfake.swap(a, labels=labels)
        return gql if gql is not None else ""
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    gh._LABEL_SWAP_RETRY_BASE = 0
    gh.park("5", "deploy gate")
    assert labels == {"sdlc:goal"}


def test_partial_park_repair_is_bounded_and_never_raises():
    """#722 RESILIENCY: a repair that cannot read, or cannot write, must not raise out of the park
    path and must not loop -- one read, one bounded swap, then stop (hygiene still flags it)."""
    src = _mod("sources")
    for kwargs in ({"rest_labels_fails": True}, {"remove_always_fails": True}):
        labels = {"sdlc:goal"}
        run, stats = _partial_park_failure_runner(labels, **kwargs)
        gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
        gh._LABEL_SWAP_RETRY_BASE = 0
        gh.park("5", "deploy gate")                                # must not raise
        assert stats["issue_reads"] == 1
        want = 0 if kwargs.get("rest_labels_fails") else gh._LABEL_SWAP_RETRIES
        assert stats["mutations"].count("rm") == want


def test_park_excludes_issue_even_if_the_comment_raises():
    """F4: `_offboard` used to post the comment FIRST — a raising `issue comment` (a transient
    502/rate-limit) left the goal label untouched AND crashed the caller before the label-removal
    line ever ran. De-listing must happen regardless of what the comment does."""
    src = _mod("sources")
    issues = {5: {"open": True, "labels": {"sdlc:goal"}}}

    def run(a):
        gql = _gql_swap(a, labels=issues[5]["labels"])   # #1391 step 2: one graphql swap
        if gql is not None:
            return gql
        verb = _issues_verb(a)
        if verb == "list":
            want = _rest_labels(a)[0]
            return json.dumps([{"number": k, "labels": [{"name": l} for l in v["labels"]]}
                               for k, v in issues.items() if v["open"] and want in v["labels"]])
        if verb == "edit" and "--remove-label" in a:
            issues[int(a[2])]["labels"].discard(a[a.index("--remove-label") + 1])
            return ""
        if verb == "comment":
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0    # hermetic: no real backoff sleeps
    gh._NOTE_RETRY_BASE = 0            # hermetic: the park comment now retries via note() (#1657 follow-up)
    assert gh.next_pending() == "5"
    gh.park("5", "deploy gate")               # must not raise, despite the comment call failing
    assert gh.next_pending() is None          # goal label removed -> excluded despite the comment failure


def test_park_comment_has_no_tier_suffix_when_no_tier_is_given():
    """#953 regression pin: `park()`'s default call shape (no `tier` — every caller predating
    decision_tier.py, and every non-`needs_decision` park after it) must post the EXACT SAME comment
    text it always has, byte-for-byte."""
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.park("5", "deploy gate")
    comments = [c for c in run.calls if len(c) > 1 and c[1] == "comment"]
    assert len(comments) == 1
    body = comments[0][comments[0].index("--body") + 1]
    assert body == "Parked by Sigma — needs human review: deploy gate"


def test_park_comment_appends_the_decision_tier_when_given():
    """#953: when a decision tier was computed for a `needs_decision` park, it rides along in the
    SAME comment the park already posts — no new `gh` call."""
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.park("5", "PR #1 changes requested", tier="escalate_l0")
    comments = [c for c in run.calls if len(c) > 1 and c[1] == "comment"]
    assert len(comments) == 1
    body = comments[0][comments[0].index("--body") + 1]
    assert body == ("Parked by Sigma — needs human review: PR #1 changes requested "
                     "[decision tier: escalate_l0]")


def test_park_with_a_reason_that_only_restates_the_verb_is_flagged_not_published_verbatim():
    """#1344: `park()` used to concatenate ANY string into the published comment with zero
    validation -- a caller (observed downstream) parking with the literal reason "parked" produced
    "needs human review: parked", a park with no recoverable cause. A reason that just restates the
    terminal state (case/punctuation-insensitive: "Parked.", "PARKED", "n/a" all match) is replaced
    with an explicit marker so the gap is visible in triage instead of reading like a considered
    decision; the caller's original text still rides along in parens rather than being discarded."""
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.park("5", "parked")
    comments = [c for c in run.calls if len(c) > 1 and c[1] == "comment"]
    assert len(comments) == 1
    body = comments[0][comments[0].index("--body") + 1]
    assert body == ("Parked by Sigma — needs human review: "
                     "⚠ no reason supplied by the caller (caller passed: 'parked')")


def test_fail_with_an_empty_reason_is_flagged_not_left_blank():
    """Same guard, `fail()` side, and the true-empty case (nothing at all, distinct from a caller
    who typed the verb) -- the marker carries no parenthetical since there was nothing to quote."""
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.fail("5", "")
    comments = [c for c in run.calls if len(c) > 1 and c[1] == "comment"]
    assert len(comments) == 1
    body = comments[0][comments[0].index("--body") + 1]
    assert body == ("Failed in the Sigma loop — needs a fix (not a decision): "
                     "⚠ no reason supplied by the caller")


def test_park_with_a_genuinely_terse_real_reason_still_passes_through_unchanged():
    """The guard must not overreach: a short reason that is NOT just the verb ("n/a" is degenerate,
    but a real two-word cause is not) is published byte-for-byte, exactly as before #1344."""
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.park("5", "deploy gate")
    comments = [c for c in run.calls if len(c) > 1 and c[1] == "comment"]
    body = comments[0][comments[0].index("--body") + 1]
    assert body == "Parked by Sigma — needs human review: deploy gate"


def test_mark_in_progress_survives_a_raising_add_label():
    """F4: a transient gh error setting the (best-effort) in-progress label must not stop the goal
    from being picked — the loop's own state/ledger track real progress regardless of this label."""
    src = _mod("sources")

    def run(a):
        verb = _issues_verb(a)
        if verb == "edit" and "--add-label" in a:
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.mark_in_progress("5")                  # must not raise


def test_next_pending_survives_a_raising_list_call():
    """F4: `next_pending` is called first, every iteration of run_loop's while-loop — an unguarded
    raise here crashed the ENTIRE drain before a single goal could be picked, not just one goal."""
    src = _mod("sources")
    calls = []

    def run(a):
        calls.append(list(a))
        verb = _issues_verb(a)
        if verb == "list":
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0    # hermetic: no real backoff sleeps
    assert gh.next_pending() is None          # degrades to "nothing pending", never a traceback
    assert len(calls) == gh._BACKLOG_READ_RETRIES     # transient (502) -> retried every attempt


def test_next_pending_fails_fast_on_a_non_transient_list_error():
    """A permanent error (bad repo, no auth) must not pay the full retry+backoff cost — it can
    never succeed on a later attempt, so `next_pending` gives up on the first try, exactly as
    before the F447 retry existed."""
    src = _mod("sources")
    calls = []

    def run(a):
        calls.append(list(a))
        verb = _issues_verb(a)
        if verb == "list":
            raise RuntimeError("gh: HTTP 404 Not Found (repo does not exist)")
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh.next_pending() is None
    assert len(calls) == 1     # not transient -> no retry


# --- #905: read_degraded() -- was the MOST RECENT _fetch_pending call a genuine empty read, or a
# give-up after exhausting retries? `_next()`'s DONE branch checks this before writing a run_stop
# `reason_class='backlog-empty'` row, so a masked GraphQL failure (this repo's own documented
# GraphQL-quota-exhaustion history) records an honest absence instead of a false "backlog drained".


def test_read_degraded_true_after_a_non_transient_list_error():
    src = _mod("sources")

    def run(a):
        verb = _issues_verb(a)
        if verb == "list":
            raise RuntimeError("gh: HTTP 404 Not Found (repo does not exist)")
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh.next_pending() is None
    assert gh.read_degraded() is True


def test_read_degraded_true_after_exhausting_transient_retries():
    src = _mod("sources")

    def run(a):
        verb = _issues_verb(a)
        if verb == "list":
            raise RuntimeError("gh: HTTP 502 Bad Gateway")   # transient, but never recovers
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh.next_pending() is None
    assert gh.read_degraded() is True


def test_read_degraded_false_after_a_genuinely_empty_successful_read():
    src = _mod("sources")

    def run(a):
        verb = _issues_verb(a)
        return json.dumps([]) if verb == "list" else ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh.next_pending() is None
    assert gh.read_degraded() is False


def test_read_degraded_false_after_a_successful_non_empty_read():
    src = _mod("sources")
    issues = [{"number": 9, "labels": [{"name": "sdlc:goal"}]}]

    def run(a):
        verb = _issues_verb(a)
        return json.dumps(issues) if verb == "list" else ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.next_pending() == "9"
    assert gh.read_degraded() is False


def test_read_degraded_reflects_only_the_most_recent_read_not_sticky():
    """A degraded read followed by a later genuinely successful one clears the flag -- this is "was
    the LAST read trustworthy", not a permanent "this source is broken" latch."""
    src = _mod("sources")
    attempts = {"n": 0}

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("gh: HTTP 404 Not Found (repo does not exist)")
        return json.dumps([])

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh.next_pending() is None
    assert gh.read_degraded() is True
    assert gh.next_pending() is None       # a fresh call, genuinely empty this time
    assert gh.read_degraded() is False


def test_next_pending_recovers_from_a_transient_error_on_retry():
    """F447: the failure mode this pins — a transient read error on the first attempt must not be
    the final word when a later attempt would have succeeded. Before this fix, ANY exception on
    this call (transient or not) immediately returned None, byte-identical to a drained backlog."""
    src = _mod("sources")
    issues = [{"number": 9, "labels": [{"name": "sdlc:goal"}]}]
    attempts = {"n": 0}

    def run(a):
        verb = _issues_verb(a)
        if verb == "list":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("gh: HTTP 502 Bad Gateway")     # one transient blip...
            return json.dumps(issues)                              # ...then the read succeeds
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github",
                                         "blocking_priority_override": False}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0    # hermetic: no real backoff sleeps
    assert gh.next_pending() == "9"    # recovered on retry, not falsely reported as "nothing pending"
    assert attempts["n"] == 2


def test_next_pending_recovers_from_a_stale_empty_read_on_retry():
    """F447's actual root cause, pinned directly: `gh issue list` (this query, confirmed via
    GH_DEBUG=api) resolves through GitHub's asynchronously-indexed search backend, so a
    just-labelled goal can legitimately come back EMPTY (no exception, a clean successful read of
    zero matches) on the first read and then appear moments later with no other state change at
    all — exactly what an isolated repro against a real scratch repo measured (1-5s of lag with
    zero concurrent load). Before this fix, `next_pending` trusted the FIRST empty read as final
    and `_next()` reported a bare DONE despite the goal genuinely existing and being unparked."""
    src = _mod("sources")
    issues = [{"number": 446, "labels": [{"name": "sdlc:goal"}]}]
    attempts = {"n": 0}

    def run(a):
        verb = _issues_verb(a)
        if verb == "list":
            attempts["n"] += 1
            # first read: the search index hasn't caught up yet -> a clean, successful, EMPTY page
            return "[]" if attempts["n"] == 1 else json.dumps(issues)
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github",
                                         "blocking_priority_override": False}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0    # hermetic: no real backoff sleeps
    assert gh.next_pending() == "446"      # found on the retry, not silently reported as "nothing pending"
    assert attempts["n"] == 2


def test_github_note_comments_on_the_issue():
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    gh.note("5", "research: 3 affected files")
    flat = [" ".join(c) for c in run.calls]
    assert any("issue comment 5" in c and "research: 3 affected files" in c and "--repo o/r" in c for c in flat)


def test_github_note_retries_a_transient_failure_until_the_comment_lands():
    """#1986 / #895 slice 8: a transient server 5xx on the REST POST must not erase `sigma-goal-review`'s
    REJECT verdict (it writes nothing else): re-POSTed over REST, never via `gh issue comment`."""
    src = _mod("sources")
    posts = []

    def flaky(a):
        assert a[0] == "api", a                      # no `issue comment`, no other call
        posts.append(a)
        if len(posts) <= 2:
            e = RuntimeError("gh api failed")
            e.hint = "gh: Server Error (HTTP 503)"
            raise e
        return json.dumps({"id": 1})
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=flaky)
    gh._NOTE_RETRY_BASE = 0     # hermetic: no real backoff sleeps
    gh.note("5", "goal-review: REJECTED -- findings")     # must not raise
    assert len(posts) == 3                                # 2 transient failures were retried, then success


def test_github_note_raises_on_final_failure_instead_of_swallowing():
    """#1986: this method must RAISE once it cannot land the comment -- the caller (the CLI verb, or an
    internal best-effort wrapper) decides what that means; staying quiet is how a REJECT verdict lost
    its only comment with zero trace. #895 slice 8: the raised type is `GhApiError`."""
    src = _mod("sources")

    def always_fails(a):
        e = RuntimeError("gh: authentication token drifted (simulated)")
        e.hint = "gh: Bad credentials (HTTP 401)"
        raise e
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=always_fails)
    gh._NOTE_RETRIES, gh._NOTE_RETRY_BASE = 1, 0     # hermetic: fail fast, no real backoff sleeps
    with pytest.raises(Exception) as ei:
        gh.note("5", "goal-review: REJECTED -- findings")
    assert type(ei.value).__name__ == "GhApiError" and "drifted" in str(ei.value)


def test_github_note_primary_rate_limit_uses_the_one_policy_fallback_after_the_rest_post():
    """#895 slice 8 (inverts #1657's GraphQL-first shape): REST first, so a primary rate limit on the POST
    is answered by `comment_issue`'s ONE `gh issue comment` fallback, never a retry loop."""
    src = _mod("sources")
    calls = []

    def limited(a):
        calls.append(a)
        if a[0] == "api":
            e = RuntimeError("gh api failed")
            e.hint = "gh: API rate limit exceeded (HTTP 429)"
            raise e
        return ""
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=limited)
    gh._NOTE_RETRY_BASE = 0
    gh.note("5", "goal-review: REJECTED -- quota exhausted")     # must not raise
    assert [c[0] for c in calls] == ["api", "issue"]
    assert calls[0][1] == "repos/o/r/issues/5/comments" and calls[0][calls[0].index("--method") + 1] == "POST"
    assert "body=goal-review: REJECTED -- quota exhausted" in calls[0]
    assert calls[1][:3] == ["issue", "comment", "5"]


def test_github_note_with_no_resolvable_repo_posts_the_placeholder_endpoint():
    """`discovery.github.repo` ships EMPTY in the sigma-init template: the REST POST must address the literal
    `{owner}/{repo}` placeholder (`gh api` infers it from the git remote at zero extra requests) and must
    never resolve it through the GraphQL-billed `gh repo view` (#1657 follow-up, kept under slice 8)."""
    src = _mod("sources")
    calls = []

    def run(a):
        calls.append(a)
        if a[:2] == ["repo", "view"]:
            raise RuntimeError("gh repo view is itself GraphQL-billed and must never be called here")
        assert a[0] == "api", a
        return json.dumps({"id": 1})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)  # no repo configured
    gh.note("5", "goal-review: REJECTED")     # must NOT raise

    assert not any(c[:2] == ["repo", "view"] for c in calls)
    assert len(calls) == 1 and calls[0][1] == "repos/{owner}/{repo}/issues/5/comments"
    assert calls[0][calls[0].index("--method") + 1] == "POST"
    assert "body=goal-review: REJECTED" in calls[0]


# --- #895 slice 8: note() is REST-first. Raw `run=` runners (a[0] == "api"): `_recording_runner`
# would re-record the REST POST as a legacy `issue comment` and make every count below vacuous.
# Unknown argv RAISES here, never returns "" (a silent pass for a call the test did not expect).

def _hinted(msg, hint=None):
    e = RuntimeError(msg)
    if hint:
        e.hint = hint
    return e


def _note_world(monkeypatch, post, fallback=None, repo="o/r", cloud=False):
    """-> (GitHubSource, calls). `post(n)` answers the n-th REST POST (1-based: str, or raise);
    `fallback(n)` the n-th `issue comment` (default: raises, so an unexpected GraphQL call is loud)."""
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)
    if cloud:
        monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    else:
        monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    calls = []

    def run(a):
        calls.append(list(a))
        if a[0] == "api" and "--method" in a and a[a.index("--method") + 1] == "POST":
            return post(len([c for c in calls if c[0] == "api"]))
        if a[:2] == ["issue", "comment"]:
            if fallback is None:
                raise AssertionError("unexpected `gh issue comment`: %r" % (a,))
            return fallback(len([c for c in calls if c[:2] == ["issue", "comment"]]))
        raise AssertionError("unexpected argv %r" % (a,))
    cfg = {"discovery": {"source": "github"}}
    if repo:
        cfg["discovery"]["github"] = {"repo": repo}
    gh = _mod("sources").GitHubSource(cfg, run=run)
    gh._NOTE_RETRY_BASE = 0
    return gh, calls


def _posts(calls):
    return [c for c in calls if c[0] == "api"]


def _graphql_comments(calls):
    return [c for c in calls if c[:2] == ["issue", "comment"]]


def _raises_gh_api_error(gh, text="x"):
    with pytest.raises(Exception) as ei:
        gh.note("5", text)
    assert type(ei.value).__name__ == "GhApiError"
    return ei.value


def test_note_first_attempt_is_the_rest_post(monkeypatch):
    gh, calls = _note_world(monkeypatch, lambda n: json.dumps({"id": 1}))
    assert gh.note("5", "goal-review: REJECTED") is None
    assert len(_posts(calls)) == 1 and _posts(calls)[0][1] == "repos/o/r/issues/5/comments"
    assert "body=goal-review: REJECTED" in _posts(calls)[0]
    assert _graphql_comments(calls) == []


def test_note_rest_timeout_retries_rest_only(monkeypatch):
    def timed_out(n):
        e = _hinted("gh api timed out")
        try:
            raise subprocess.TimeoutExpired("gh", 120)
        except Exception as t:
            e.__cause__ = t
        raise e
    gh, calls = _note_world(monkeypatch, timed_out)
    _raises_gh_api_error(gh)
    assert len(_posts(calls)) == gh._NOTE_RETRIES and _graphql_comments(calls) == []
    gh, calls = _note_world(monkeypatch, timed_out)
    gh._NOTE_RETRIES = 1
    _raises_gh_api_error(gh)
    assert len(_posts(calls)) == 1 and _graphql_comments(calls) == []


def test_note_in_cloud_never_makes_a_graphql_attempt(monkeypatch):
    def limited(n):
        raise _hinted("gh api failed", "gh: API rate limit exceeded (HTTP 429)")
    gh, calls = _note_world(monkeypatch, limited, cloud=True)
    _raises_gh_api_error(gh)
    assert len(_posts(calls)) == 1 and _graphql_comments(calls) == []


def test_note_primary_rate_limit_makes_exactly_one_fallback(monkeypatch):
    def limited(n):
        raise _hinted("gh api failed", "gh: API rate limit exceeded (HTTP 429)")
    gh, calls = _note_world(monkeypatch, limited, fallback=lambda n: "")
    assert gh.note("5", "x") is None
    assert len(_posts(calls)) == 1 and len(_graphql_comments(calls)) == 1
    # the policy fallback itself failing 5xx is raised: no second POST, no second fallback
    def bad(n):
        raise _hinted("gh issue comment failed", "gh: Server Error (HTTP 502)")
    gh, calls = _note_world(monkeypatch, limited, fallback=bad)
    _raises_gh_api_error(gh)
    assert len(_posts(calls)) == 1 and len(_graphql_comments(calls)) == 1


@pytest.mark.parametrize("hint", ["gh: Not Found (HTTP 404)", "gh: Unprocessable Entity (HTTP 422)",
                                  "gh: Bad credentials (HTTP 401)", "gh: Forbidden (HTTP 403)",
                                  "gh: You have exceeded a secondary rate limit (HTTP 403)"])
def test_note_4xx_and_secondary_limit_are_not_retried(monkeypatch, hint):
    def refused(n):
        raise _hinted("gh api failed", hint)
    gh, calls = _note_world(monkeypatch, refused)
    _raises_gh_api_error(gh)
    assert len(_posts(calls)) == 1 and _graphql_comments(calls) == []


def test_note_5xx_retries_rest_only_up_to_the_cap_then_raises(monkeypatch):
    def bad(n):
        raise _hinted("gh api failed", "gh: Server Error (HTTP 502)")
    gh, calls = _note_world(monkeypatch, bad)
    _raises_gh_api_error(gh)
    assert len(_posts(calls)) == gh._NOTE_RETRIES and _graphql_comments(calls) == []

    def flaky(n):
        if n == 1:
            raise _hinted("gh api failed", "gh: Server Error (HTTP 502)")
        return json.dumps({"id": 2})
    gh, calls = _note_world(monkeypatch, flaky)
    assert gh.note("5", "x") is None
    assert len(_posts(calls)) == 2 and _graphql_comments(calls) == []


def test_note_body_saying_http_502_is_not_retried(monkeypatch):
    """The `.kind` / `str(exc)` trap: with no `.hint`, `classify()` reads `str(exc)`, which embeds the
    comment BODY. A body saying `HTTP 502` makes `exc.kind == "server"` (a retry), while `_write_case`
    (hint and cause chain only) says other_unparsed (no retry). Pin the second."""
    def body_echo(n):
        raise _hinted("gh api repos/o/r/issues/5/comments -f body=the proxy said HTTP 502 failed")  # no .hint
    gh, calls = _note_world(monkeypatch, body_echo)
    err = _raises_gh_api_error(gh, "the proxy said HTTP 502")
    assert err.kind == "server"                        # the trap is real: a str-based read WOULD retry
    assert len(_posts(calls)) == 1 and _graphql_comments(calls) == []


def test_note_foreign_issue_returns_before_any_call(monkeypatch):
    gh, calls = _note_world(monkeypatch, lambda n: (_ for _ in ()).throw(AssertionError("no call")))
    monkeypatch.setattr(gh.note.__globals__["hsg"], "blocks_issue_write", lambda *a, **k: True)
    assert gh.note("5", "x") is None and calls == []


def test_github_append_to_body_appends_without_overwriting():
    """#376: the machine-readable channel `backlog_check.py`'s `_explicit_blockers()` actually
    reads (title+body only, comments are never fetched -- see mirror.py). Must APPEND to the
    existing body, never replace it -- `gh issue edit --body` overwrites wholesale, so the current
    body has to be read first."""
    src = _mod("sources")
    run = _recording_runner({"rest": {"body": "Existing body text."}})
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    gh.append_to_body("5", "**Blocked by:** #61")
    assert ["api", "repos/o/r/issues/5", "--method", "GET"] in run.calls    # #895: REST first
    edit_call = next(c for c in run.calls if c[0] == "issue" and c[1] == "edit")
    body = edit_call[edit_call.index("--body") + 1]
    assert body == "Existing body text.\n\n**Blocked by:** #61\n"


def test_github_append_to_body_handles_an_empty_body():
    src = _mod("sources")
    run = _recording_runner({"rest": {"body": None}})       # REST sends null for an empty body
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    gh.append_to_body("5", "**Blocked by:** #61")
    edit_call = next(c for c in run.calls if c[0] == "issue" and c[1] == "edit")
    body = edit_call[edit_call.index("--body") + 1]
    assert body == "\n\n**Blocked by:** #61\n"


def test_local_source_read_degraded_is_always_false():
    """#1084: LocalSource has no read-failure-vs-genuinely-empty ambiguity to report today --
    `read_degraded()` is a hardcoded `False`, symmetrically with GitHubSource.read_degraded()
    (sources.py:1880). Calling next_pending() first proves it is not accidentally wired to
    anything stateful -- it stays False regardless of what the prior read found."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        local = src.LocalSource(str(base))
        assert local.read_degraded() is False
        local.next_pending()          # empty goals/ dir -> None
        assert local.read_degraded() is False


def test_local_note_appends_journey_log():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0001-x.md"; g.write_text("---\nstatus: pending\n---\n")
        local = src.get_source(str(base), {})
        local.note(str(g), "plan: 4 steps, TDD")
        jlog = base / "journey" / "0001-x.md"
        assert jlog.exists() and "plan: 4 steps, TDD" in jlog.read_text()
        local.note(str(g), "review: tests green")
        assert jlog.read_text().count("## ") == 2          # appended across phases, not overwritten


def test_local_release_sets_status_back_to_pending_and_notes_journey():
    """#841: the local counterpart to GitHubSource.release — undoes a claim that was never
    started (mark_in_progress ran, nothing ever called complete/park/fail). Sets status back to
    `pending` (so next_pending offers it again exactly like a goal that was never picked) and
    appends an audit note to the journey log, the local mirror of the GitHub issue comment."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0001-x.md"; g.write_text("---\nstatus: in_progress\n---\n")
        local = src.get_source(str(base), {})
        local.release(str(g), "next-batch picked 3, only 1 got dispatched")
        assert "status: pending" in g.read_text()
        jlog = base / "journey" / "0001-x.md"
        assert jlog.exists()
        assert "Released" in jlog.read_text()
        assert "next-batch picked 3, only 1 got dispatched" in jlog.read_text()


def test_local_release_with_no_reason_still_notes_journey():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0001-x.md"; g.write_text("---\nstatus: in_progress\n---\n")
        local = src.get_source(str(base), {})
        local.release(str(g), "")
        assert "status: pending" in g.read_text()
        jlog = base / "journey" / "0001-x.md"
        assert jlog.exists() and "Released" in jlog.read_text()


def test_local_release_refuses_an_already_done_goal_leaving_it_untouched():
    """PR #1107 review, Finding 3: release must never silently un-complete a goal that already
    reached a terminal SDLC outcome. Still journals the attempt (so it is never silently invisible)
    but with an honest no-op message, and returns False instead of True."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0001-x.md"; g.write_text("---\nstatus: done\n---\n")
        local = src.get_source(str(base), {})
        result = local.release(str(g), "stale cleanup sweep")
        assert result is False
        assert "status: done" in g.read_text()
        jlog = base / "journey" / "0001-x.md"
        assert jlog.exists()
        text = jlog.read_text()
        assert "already done/parked/failed" in text and "stale cleanup sweep" in text
        assert "claimed but not started" not in text   # must not use the "really released" wording


def test_local_append_to_body_appends_after_the_frontmatter_without_touching_it():
    """#726, local counterpart to test_github_append_to_body_appends_without_overwriting: the
    marker has to land in the goal's own BODY, after the frontmatter fence -- that's the exact
    region backlog_check._build_corpus() reads (via frontmatter.strip()) and _explicit_blockers()
    regexes, so a marker written anywhere else (or one that clobbers the fence) is invisible to a
    later precheck run. Must APPEND, never overwrite the existing body."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0001-x.md"
        g.write_text('---\nid: 0001\ntitle: "x"\nstatus: pending\n---\nExisting body text.\n')
        local = src.get_source(str(base), {})
        local.append_to_body(str(g), "**Blocked by:** #61")
        assert g.read_text() == (
            '---\nid: 0001\ntitle: "x"\nstatus: pending\n---\n'
            'Existing body text.\n\n**Blocked by:** #61\n')


def test_local_append_to_body_never_overwrites_a_prior_marker():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0001-x.md"
        g.write_text("---\nid: 0001\nstatus: pending\n---\nbody\n")
        local = src.get_source(str(base), {})
        local.append_to_body(str(g), "**Blocked by:** #61")
        local.append_to_body(str(g), "**Blocked by:** #62")
        text = g.read_text()
        assert "**Blocked by:** #61" in text and "**Blocked by:** #62" in text
        assert text.count("id: 0001") == 1                 # frontmatter never duplicated


def test_local_append_to_body_handles_an_empty_body():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0002-x.md"
        g.write_text("---\nid: 0002\nstatus: pending\n---\n")
        local = src.get_source(str(base), {})
        local.append_to_body(str(g), "**Blocked by:** #61")
        assert g.read_text() == "---\nid: 0002\nstatus: pending\n---\n\n\n**Blocked by:** #61\n"


def test_local_append_to_body_with_no_frontmatter_fence_appends_to_the_raw_text():
    """A goal file somehow missing its fence entirely degrades the same way frontmatter.strip()
    itself does -- the whole file is treated as body rather than raising or silently dropping the
    marker."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0003-x.md"
        g.write_text("just plain text, no fences\n")
        local = src.get_source(str(base), {})
        local.append_to_body(str(g), "**Blocked by:** #61")
        assert g.read_text() == "just plain text, no fences\n\n**Blocked by:** #61\n"


def test_local_append_to_body_missing_file_raises():
    """Unlike fetch_title_body (which degrades to empty strings for a read-only classifier that
    fails open by design), append_to_body must RAISE on a missing/unreadable goal file -- the
    caller (handoff.create_tracked_issue) already wraps this call in a try/except that turns any
    raised exception into a report["warnings"] entry, so raising is what makes that existing
    warning path fire instead of a quiet no-op (#726)."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        local = src.get_source(str(base), {})
        try:
            local.append_to_body(str(base / "goals" / "nope.md"), "**Blocked by:** #61")
            assert False, "expected a raised exception on a missing file"
        except OSError:
            pass


def test_local_append_to_body_resolves_a_bare_numeric_id_from_create_dependency(tmp_path):
    """#921 (sigma-scope epic #902's own integration validation surfaced this): `create_dependency`
    returns a bare int id (`gid`), matching `GitHubSource.create_dependency`'s bare-issue-number
    return by design -- but until this fix, chaining that return straight into `append_to_body`
    (exactly what `compile_plan.py`'s own `_patch_epic_with_subs` does for the epic's "Tracks #N"
    back-reference) raised, because `Path(1)` is never a real file. `append_to_body` must resolve the
    bare id to the real goal file it names, the same way GitHubSource's own append_to_body already
    accepts a bare issue number directly."""
    src = _mod("sources")
    base = tmp_path / ".sdlc"
    (base / "goals").mkdir(parents=True)
    local = src.get_source(str(base), {})
    gid = local.create_dependency("Config editor", "Tracking issue.", None, labels=["epic"], goal_label=False)

    local.append_to_body(str(gid), "Tracks #2")

    files = list((base / "goals").glob("*.md"))
    assert len(files) == 1
    assert "Tracks #2" in files[0].read_text()


def test_local_append_to_body_bare_id_matching_nothing_still_raises(tmp_path):
    """The bare-id resolution is additive only -- a numeric id that matches no real goal file falls
    through to the unresolved literal path exactly as before, so a genuinely bad reference still
    raises rather than silently no-op-ing."""
    src = _mod("sources")
    base = tmp_path / ".sdlc"
    (base / "goals").mkdir(parents=True)
    local = src.get_source(str(base), {})
    try:
        local.append_to_body("999", "**Blocked by:** #1")
        assert False, "expected a raised exception -- no goal file has id 999"
    except OSError:
        pass


def test_local_note_resolves_a_bare_numeric_id_to_the_real_goal_stem(tmp_path):
    """`note()` chains through the same `_resolve_ref` -- a hand-off comment posted via
    `assign.py`'s `PATH_START_HANDOFF` against a just-created local goal (identified only by the
    bare id `create_dependency` returned) must land in THAT goal's own journey file
    (`.sdlc/journey/<real-stem>.md`), not a mismatched `<bare-id>.md` no later reader would ever
    find by the goal's real stem."""
    src = _mod("sources")
    base = tmp_path / ".sdlc"
    (base / "goals").mkdir(parents=True)
    local = src.get_source(str(base), {})
    gid = local.create_dependency("Config editor UI", "body", None, goal_label=False)
    real_stem = next((base / "goals").glob("*.md")).stem

    local.note(str(gid), "Starting hand-off")

    journal = base / "journey" / f"{real_stem}.md"
    assert journal.exists() and "Starting hand-off" in journal.read_text()
    assert not (base / "journey" / f"{gid}.md").exists()


def test_local_append_to_body_is_visible_to_the_real_explicit_blockers_regex():
    """The end-to-end promise #726's own issue names: backlog_check._explicit_blockers() must
    actually be able to see a local blocker marker, not merely a well-formed one. Imports the real
    regex rather than hand-copying it (same discipline as
    test_handoff_narrative_wording_actually_matches_the_auto_skip_regex)."""
    src = _mod("sources")
    backlog_check = _mod("backlog_check")
    frontmatter = _mod("frontmatter")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0001-x.md"
        g.write_text('---\nid: 0001\ntitle: "x"\nstatus: pending\n---\ncurrent work\n')
        local = src.get_source(str(base), {})
        local.append_to_body(str(g), "**Blocked by:** #61")
        # the same read _build_corpus() performs: frontmatter-stripped body, nothing else
        stripped_body = frontmatter.strip(g.read_text())
        assert backlog_check._BLOCK_RE.search(stripped_body)


def test_run_gh_raises_clear_error_on_failure():
    src = _mod("sources")
    # a failing gh invocation (gh subcommand that doesn't exist) must raise a helpful RuntimeError,
    # not a bare CalledProcessError. Uses a fake binary so it works without gh installed.
    try:
        src._run_gh(["definitely-not-a-real-subcommand-xyz"], binary="false")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "gh" in str(e)


def _fake_gh_script(tmp_path, stderr_text):
    """A tiny stand-in `gh` binary (same technique as the `binary="false"` test above, but printing
    controlled stderr text instead of nothing) so #78's proxy-block detection can be proven against a
    REAL failing subprocess call — not just a hand-built exception — without depending on the real
    `gh` CLI or network."""
    script = tmp_path / "fake-gh"
    script.write_text(f"#!/bin/sh\necho {stderr_text!r} >&2\nexit 1\n")
    script.chmod(0o755)
    return str(script)


def test_run_gh_diagnoses_a_claude_code_remote_session_proxy_block(tmp_path):
    """#78: a `gh` call blocked by a Claude Code Remote session's proxy (the "GitHub access is not
    enabled..." shape) must raise with the CORRECTED diagnosis on `.hint` — connect the Claude
    GitHub App — not the raw 403 read as an ordinary permission/auth failure."""
    src = _mod("sources")
    gh_session = _mod("gh_session")
    proxy_text = ("GitHub access is not enabled for this session. An org admin must connect the "
                  "Claude GitHub App for this organization.")
    fake_gh = _fake_gh_script(tmp_path, proxy_text)
    try:
        src._run_gh(["pr", "create", "--title", "x"], binary=fake_gh)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert e.hint == gh_session.REMEDIATION
        assert "claude github app" in str(e).lower()


def test_run_gh_diagnoses_the_graphql_pinned_ops_proxy_shape(tmp_path):
    """The OTHER confirmed proxy shape (#78) — `gh auth status`/`gh pr list`'s GraphQL 403 — must be
    recognized too, not just the REST/App-connection one."""
    src = _mod("sources")
    gh_session = _mod("gh_session")
    proxy_text = ("This GraphQL query (PullRequestList, sent by gh pr list) is not enabled for "
                  "this session - only the pinned set of PR-review operations is served.")
    fake_gh = _fake_gh_script(tmp_path, proxy_text)
    try:
        src._run_gh(["pr", "list"], binary=fake_gh)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert e.hint == gh_session.REMEDIATION


def test_run_gh_keeps_the_raw_hint_for_a_real_gh_failure(tmp_path):
    """An ordinary (non-proxy) gh failure must be completely unaffected by #78's fix — `.hint` stays
    the raw stderr, exactly as before."""
    src = _mod("sources")
    fake_gh = _fake_gh_script(tmp_path, "HTTP 404: Not Found (https://api.github.com/repos/x/y)")
    try:
        src._run_gh(["repo", "view"], binary=fake_gh)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert e.hint == "HTTP 404: Not Found (https://api.github.com/repos/x/y)"


def test_github_fail_comments_fix_not_decision_and_excludes_issue():
    src = _mod("sources")
    run = _recording_runner({})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.fail("7", "red suite")
    joined = [" ".join(c) for c in run.calls]
    assert any("needs a fix (not a decision): red suite" in c for c in joined)
    assert any("--remove-label" in c and "sdlc:goal" in c for c in joined)


# --- #389: fetch_comments() -- the one shared, bounded comment-read primitive #385 will later
# consume too (see the plan doc). Module-level, not a GitHubSource method: backlog_check.cross_check
# only has config/run, no source instance -- same module-level + injectable-run shape as every other
# read in this file.

def test_fetch_comments_shapes_id_author_body_created_at():
    src = _mod("sources")
    # out of order by createdAt on purpose -- the function must sort, not trust gh's own order
    payload = {"comments": [
        {"id": "IC_2", "author": {"login": "bob"}, "body": "second", "createdAt": "2026-08-02T00:00:00Z"},
        {"id": "IC_1", "author": {"login": "amy"}, "body": "first", "createdAt": "2026-08-01T00:00:00Z"},
    ]}
    run = _recording_runner({"rest": payload})
    out = src.fetch_comments({}, "5", run=run)
    assert out == [
        {"id": "IC_1", "author": "amy", "body": "first", "created_at": "2026-08-01T00:00:00Z", "association": ""},
        {"id": "IC_2", "author": "bob", "body": "second", "created_at": "2026-08-02T00:00:00Z", "association": ""},
    ]
    assert run.calls[0] == ["api", "repos/{owner}/{repo}/issues/5", "--method", "GET"]     # #895: REST
    assert run.calls[1][:2] == ["api", "repos/{owner}/{repo}/issues/5/comments"]


def test_fetch_comments_respects_limit_keeping_the_most_recent():
    src = _mod("sources")
    comments = [{"id": f"IC_{i}", "author": {"login": "amy"}, "body": str(i),
                "createdAt": f"2026-08-0{i}T00:00:00Z"} for i in range(1, 6)]   # 5 comments, days 1..5
    run = _recording_runner({"rest": {"comments": comments}})
    out = src.fetch_comments({}, "5", run=run, limit=2)
    assert [c["id"] for c in out] == ["IC_4", "IC_5"]   # the 2 newest, still oldest-first between them


def test_fetch_comments_limit_reads_only_the_tail_pages():
    """#895 review finding 2: on a 250-comment issue, limit=k fetches the issue GET plus only the
    comment pages holding the newest k (<= 3 requests), not all 3 pages + the issue."""
    src = _mod("sources")
    comments = [{"id": f"IC_{i}", "author": {"login": "amy"}, "body": str(i),
                 "createdAt": f"2026-08-01T00:{i // 60:02d}:{i % 60:02d}Z"} for i in range(250)]
    run = _recording_runner({"rest": {"comments": comments}})
    out = src.fetch_comments({}, "5", run=run, limit=10)
    assert [c["id"] for c in out] == [f"IC_{i}" for i in range(240, 250)]
    assert len(run.calls) <= 3, run.calls
    assert not any("page=1" in map(str, c) for c in run.calls), run.calls   # the oldest page is never read


def test_fetch_comments_accepts_a_hash_prefixed_ref():
    """#895 review finding 4: `#42` reaches the REST path as issue 42, as `gh` used to accept it."""
    src = _mod("sources")
    run = _recording_runner({"rest": {"comments": []}})
    src.fetch_comments({}, "#42", run=run)
    assert run.calls[0] == ["api", "repos/{owner}/{repo}/issues/42", "--method", "GET"]
    run2 = _recording_runner({"rest": {"comments": []}})
    src.GitHubSource({"discovery": {"source": "github"}}, run=run2)._read_issue("#42", ["comments"])
    assert run2.calls[0][1] == "repos/{owner}/{repo}/issues/42"


def test_fetch_comments_fails_open_on_gh_error_bad_json_and_non_dict_payload():
    src = _mod("sources")

    def raising(args):
        raise RuntimeError("gh: HTTP 502 Bad Gateway")

    assert src.fetch_comments({}, "5", run=raising) == []                                    # gh raised
    assert src.fetch_comments({}, "5", run=_recording_runner({"view": "not json"})) == []     # bad JSON
    assert src.fetch_comments({}, "5", run=_recording_runner({"view": "[]"})) == []           # a list, not a dict


def test_fetch_comments_passes_repo_flag_when_configured():
    src = _mod("sources")
    run = _recording_runner({"rest": {"comments": []}})
    src.fetch_comments({"discovery": {"github": {"repo": "o/r"}}}, "5", run=run)
    assert run.calls[0][1] == "repos/o/r/issues/5"
    run2 = _recording_runner({"rest": {"comments": []}})
    src.fetch_comments({}, "5", run=run2)
    assert run2.calls[0][1] == "repos/{owner}/{repo}/issues/5"


def test_fetch_comments_maps_a_missing_id_to_empty_string():
    """Plan-review §8.6: a comment with no `id` field must map to id == "" -- never crash, never
    silently drop the field -- so a future id-based-dedup consumer's handling of it (comment_watch.py,
    #385, not built here) is a deliberate choice made against a proven contract, not an accident
    discovered live. #389 itself never consumes `id`, but the shared helper's FULL contract is tested
    in this PR since another PR relies on it unchanged."""
    src = _mod("sources")
    payload = {"comments": [{"author": {"login": "amy"}, "body": "no id here", "createdAt": "2026-08-01T00:00:00Z"}]}
    run = _recording_runner({"rest": payload})
    out = src.fetch_comments({}, "5", run=run)
    assert out == [{"id": "", "author": "amy", "body": "no id here", "created_at": "2026-08-01T00:00:00Z", "association": ""}]


# --- #500: pinning safe-by-construction Path(goal).stem sites (no work.stem() reduction needed) ---

def test_local_note_safe_from_traversal_via_stem_extraction():
    """#500: LocalSource.note() uses Path(goal).stem to extract only the filename, preventing any
    `../`-bearing goal from escaping .sdlc/journey/. Path(goal).stem removes both directory components
    and file extensions, so even if goal is `../../../etc/passwd.md`, stem is just `passwd`."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0001-x.md"; g.write_text("---\nstatus: pending\n---\n")
        local = src.get_source(str(base), {})
        # a traversal attempt should write to .sdlc/journey/, not escape it
        local.note("../../../etc/passwd", "traversal attempt")
        jlog = base / "journey" / "passwd.md"
        assert jlog.exists(), "goal with ../ should write to journey directory, not escape it"
        assert "traversal attempt" in jlog.read_text()
        # confirm the escape directory DOES NOT exist (proof the traversal failed)
        assert not (pathlib.Path(d) / "etc" / "passwd.md").exists(), "traversal must not create files outside .sdlc/journey/"


# --- #506: in_progress label removal on completion and parking ---

def test_complete_removes_in_progress_label():
    """#506: `complete()` must remove the in-progress label when closing an issue. The label is a
    best-effort visibility tag — a transient gh error must not fail the goal completion."""
    src = _mod("sources")
    calls = []

    def run(a):
        gql = _gql_swap(a, calls=calls)            # #1391 step 2: transitions swap via graphql now
        if gql is not None:
            return gql
        if gqlfake.is_issue_write(a):
            return gqlfake.rest_write(a, calls=calls)
        calls.append(list(a))
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.complete("42")

    # Verify that issue close and in-progress label removal both happened
    assert any(c[0:2] == ["issue", "close"] and "42" in c for c in calls), "issue close call missing"
    assert any(c[0:2] == ["issue", "edit"] and "42" in c and "--remove-label" in c and
               "sdlc:in-progress" in c for c in calls), "in-progress label removal missing"


def test_complete_survives_in_progress_label_removal_failure():
    """#506: if the in-progress label cannot be removed (transient gh error), the goal completion
    must still succeed — the label removal is best-effort."""
    src = _mod("sources")
    
    def run(a):
        verb = _issues_verb(a)
        if verb == "edit" and "--remove-label" in a and "sdlc:in-progress" in " ".join(a):
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.complete("42")  # must not raise


def test_offboard_removes_in_progress_label():
    """#506: `_offboard()` (called by park() and fail()) must remove the in-progress label along with
    the goal label. The in-progress label removal happens after goal-label removal (de-list first)."""
    src = _mod("sources")
    calls = []

    def run(a):
        gql = _gql_swap(a, calls=calls)            # #1391 step 2: transitions swap via graphql now
        if gql is not None:
            return gql
        calls.append(list(a))
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.park("42", "needs review")

    # Verify goal label and in-progress label removal both happened
    assert any(c[0:2] == ["issue", "edit"] and "42" in c and "--remove-label" in c and
               "sdlc:goal" in c for c in calls), "goal label removal missing"
    assert any(c[0:2] == ["issue", "edit"] and "42" in c and "--remove-label" in c and
               "sdlc:in-progress" in c for c in calls), "in-progress label removal missing"

    # Verify goal label removal happens before in-progress label removal
    goal_label_idx = next(i for i, c in enumerate(calls) if c[0:2] == ["issue", "edit"] and
                          "42" in c and "--remove-label" in c and "sdlc:goal" in c)
    in_progress_idx = next(i for i, c in enumerate(calls) if c[0:2] == ["issue", "edit"] and
                           "42" in c and "--remove-label" in c and "sdlc:in-progress" in c)
    assert goal_label_idx < in_progress_idx, "goal label removal must happen before in-progress removal"


def test_offboard_survives_in_progress_label_removal_failure():
    """#506: if the in-progress label cannot be removed (transient gh error), the park/fail operation
    must still succeed — the label removal is best-effort like the parked label addition."""
    src = _mod("sources")
    
    def run(a):
        verb = _issues_verb(a)
        if verb == "edit" and "--remove-label" in a and "sdlc:in-progress" in " ".join(a):
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.park("42", "deploy gate")  # must not raise


def _label_set_after_claim_then(method_name, *args):
    """#443 (duplicate of #506, fixed by PR #512/commit 160f4d8f): simulates the REAL GitHub label
    set through a claim -> park/fail lifecycle -- applying every --add-label/--remove-label call to
    an actual set, the way GitHub itself would, rather than grepping the call log the way the #506
    tests above do. Starts from {sdlc:goal}, next_pending's own starting state for any open,
    unclaimed goal, then claims it (mark_in_progress adds sdlc:in-progress) before calling
    park()/fail() by name so both wrappers of _offboard() get an identical, independent check --
    the literal "test claiming then parking (and separately, failing) a goal, asserting
    sdlc:in-progress is absent from the final label set" #443's own verification section asked for."""
    src = _mod("sources")
    labels = {"sdlc:goal"}

    def run(a):
        # #1391 step 2: lifecycle transitions now issue ONE graphql swap instead of separate
        # --add-label/--remove-label edits; `_gql_swap` applies it to this SAME tracked set, so this
        # helper keeps simulating the real GitHub label set exactly as before.
        gql = _gql_swap(a, labels=labels)
        if gql is not None:
            return gql
        # #1358 review: answer `issue view --json labels` (the read _offboard now does before
        # deciding whether to skip re-adding parked_label) from this SAME tracked set, so every
        # caller of this helper gets a realistic simulation of the labels-read, not a silent "" ->
        # {} -> empty-set fallthrough that happens to coincide with the right answer.
        if a[0:2] == ["issue", "view"] and "--json" in a and "labels" in a:
            return json.dumps({"labels": [{"name": n} for n in labels]})
        if "--add-label" in a:
            labels.add(a[a.index("--add-label") + 1])
        elif "--remove-label" in a:
            labels.discard(a[a.index("--remove-label") + 1])
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.mark_in_progress("42")
    getattr(gh, method_name)("42", *args)
    return labels


def test_park_leaves_exactly_the_parked_label_after_a_claim():
    """#443: a goal claimed then parked must show exactly one lifecycle label -- sdlc:in-progress
    and sdlc:goal both gone, only sdlc:parked left. Not just "a remove-label call happened
    somewhere" (the #506 tests above) -- the actual final label set."""
    assert _label_set_after_claim_then("park", "needs review") == {"sdlc:parked"}


def test_fail_leaves_exactly_the_parked_label_after_a_claim():
    """#443: same contract for fail() -- the OTHER _offboard() caller, independently verified per
    the issue's own acceptance criteria ("a test claiming then parking (and separately, failing) a
    goal")."""
    assert _label_set_after_claim_then("fail", "red suite") == {"sdlc:parked"}


def test_mark_blocked_keeps_membership_and_drops_only_the_occupancy_marker():
    """#1393 (revising #1350): filing a blocking dependency mid-goal transitions the goal to
    `sdlc:blocked` -- but KEEPS `sdlc:goal`.

    `sdlc:blocked` is a temporary OVERLAY, not an exit from Sigma's world: the same
    relationship `sdlc:in-progress` has to `sdlc:goal`, for the same load-bearing reason. Every
    sweep, census, mirror and reclaim path queries `--label sdlc:goal`, so an issue that drops it is
    reachable only by whichever query happens to ask for the overlay -- exactly one sweep asks for
    `sdlc:blocked`, and if that label is ever lost the issue is an orphan with no route back.

    Eligibility never depended on the removal: see the companion test below."""
    assert _label_set_after_claim_then("mark_blocked") == {"sdlc:goal", "sdlc:blocked"}


def test_a_blocked_goal_is_visible_to_a_sweep_and_still_never_picked():
    """The whole point of #1393, stated as the two halves that must BOTH hold: a blocked goal is
    findable by the membership query every sweep uses, and is picked by neither queue path."""
    src = _mod("sources")
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:blocked"}]},
              {"number": 43, "labels": [{"name": "sdlc:goal"}]}]
    run = _recording_runner({"list": json.dumps(issues)})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    # visible: the membership query returned it (it is in the fetch the picker filtered)
    assert any(i["number"] == 42 for i in issues)
    # not picked: on the label path...
    assert gh.next_pending() == "43"
    # ...and not on the board path either
    assert gh._card_is_eligible(42, {"sdlc:goal", "sdlc:blocked"}) is False
    assert gh._card_is_eligible(43, {"sdlc:goal"}) is True


def test_mark_blocked_survives_a_transient_gh_failure_on_any_step():
    """#1358 review: the original version of this test only ever injected the failure on the final
    --add-label step, so the two --remove-label except-branches were never actually exercised
    despite the test's own name claiming "any step". Parametrized (by hand, no pytest.mark needed
    for three cases) over all three independent try/except calls inside mark_blocked."""
    src = _mod("sources")
    trigger_by_case = (
        ("goal_label_removal", lambda a: "--remove-label" in a and "sdlc:goal" in a),
        ("in_progress_label_removal", lambda a: "--remove-label" in a and "sdlc:in-progress" in a),
        ("blocked_label_add", lambda a: "--add-label" in a and "sdlc:blocked" in a),
    )
    for name, should_fail in trigger_by_case:
        def run(a, should_fail=should_fail):
            if should_fail(a):
                raise RuntimeError("gh: HTTP 502 Bad Gateway")
            return ""
        gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
        gh.mark_in_progress("42")
        gh.mark_blocked("42")   # must not raise, for every one of the three cases above


def test_mark_blocked_creates_the_label_if_missing():
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.mark_blocked("42")
    creates = [c for c in run.calls if c[0:2] == ["label", "create"]]
    assert any("sdlc:blocked" in c for c in creates)


# --- #1826: mark_designed -- goal-review's own write-back for sdlc:designed, a PURE add -----------

def test_mark_designed_creates_the_label_if_missing():
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.mark_designed("1826")
    creates = [c for c in run.calls if c[0:2] == ["label", "create"]]
    assert any("sdlc:designed" in c for c in creates)


def test_mark_designed_is_a_pure_add_never_touches_other_labels_or_the_board():
    """Deliberately unlike mark_blocked/mark_needs_label: sdlc:designed never has a board column of
    its own, and restoring sdlc:goal on a parked retrofit target is /sigma-unpark's own, separate
    gesture -- never bundled into this write."""
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    board_calls = []
    gh._set_board_status = lambda goal, status_name: board_calls.append((goal, status_name))
    gh.mark_designed("1826")
    removes = [c for c in run.calls if "--remove-label" in c]
    assert removes == []
    assert board_calls == []


def test_mark_designed_survives_a_raising_run():
    src = _mod("sources")

    def run(a):
        raise RuntimeError("gh: HTTP 502 Bad Gateway")

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.mark_designed("1826") is False   # must not raise


def test_mark_designed_returns_the_swap_landed_bool():
    src = _mod("sources")
    gh_ok = src.GitHubSource({"discovery": {"source": "github"}}, run=_recording_runner())
    assert gh_ok.mark_designed("1826") is True

    def boom(a):
        raise RuntimeError("boom")
    gh_fail = src.GitHubSource({"discovery": {"source": "github"}}, run=boom)
    assert gh_fail.mark_designed("1826") is False


def test_mark_blocked_moves_the_board_card_like_every_sibling_label_transition():
    """#1358 review: mark_in_progress, complete, release, and _offboard all move the board card;
    mark_blocked must too, or an interrupted session between mark_blocked and the record-parked
    call that normally follows it (per SKILL.md's own flow) leaves the board showing the wrong
    column while the label already reads sdlc:blocked."""
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=_recording_runner())
    calls = []
    gh._set_board_status = lambda goal, status_name: calls.append((goal, status_name))
    gh.mark_blocked("42")
    assert calls == [("42", "Blocked")]   # self.col["blocked"]'s own default display column name


def test_offboard_final_label_set_is_exactly_blocked_not_blocked_plus_parked():
    """The real, end-to-end proof: simulate the actual GitHub label set through claim -> mark_blocked
    -> park, exactly like `_label_set_after_claim_then` does for the ordinary park case -- but this
    helper needs `_offboard` to also READ the current label set (a plain add/remove-tracking fake
    can't answer an `issue view --json labels` read), so it's built out here rather than reusing
    the existing helper unchanged."""
    src = _mod("sources")
    labels = {"sdlc:goal"}

    def run(a):
        gql = _gql_swap(a, labels=labels)          # #1391 step 2: transitions swap via graphql now
        if gql is not None:
            return gql
        if a[0:2] == ["issue", "view"] and "--json" in a and "labels" in a:
            return json.dumps({"labels": [{"name": n} for n in labels]})
        if "--add-label" in a:
            labels.add(a[a.index("--add-label") + 1])
        elif "--remove-label" in a:
            labels.discard(a[a.index("--remove-label") + 1])
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.mark_in_progress("42")
    gh.mark_blocked("42")
    # #1393: blocked is an overlay -- membership is kept, occupancy is dropped
    assert labels == {"sdlc:goal", "sdlc:blocked"}
    gh.park("42", "needs a human decision now that the blocker is filed")
    # A park is the EXIT: it gives up membership and clears whatever overlay was in play, in ONE
    # swap. Exactly one lifecycle label survives -- the #1350 collision this test was written for
    # is still impossible, now by construction rather than by a conditional skip.
    assert labels == {"sdlc:parked"}


def test_offboard_still_posts_the_park_comment_even_when_already_blocked():
    """The label-mutation skip must not also skip the human-visible audit trail -- the park reason
    (e.g. "needs a human decision") is real, additional context beyond the earlier blocker
    narrative, and must still land as a comment."""
    src = _mod("sources")
    run = _recording_runner_by_json({"labels": json.dumps({"labels": [{"name": "sdlc:blocked"}]})})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.park("42", "needs a human decision")
    comments = [c for c in run.calls if c[0] == "api" and c[1].endswith("/issues/42/comments")]   # REST POST
    assert len(comments) == 1
    assert "body=Parked by Sigma — needs human review: needs a human decision" in comments[0]


def test_offboard_adds_parked_normally_when_not_already_blocked():
    """The fix must be narrowly scoped -- every OTHER park reason (duplicate, obsolete, budget,
    review-cap, needs-decision) must still get plain sdlc:parked exactly as before; this is not a
    behavior change for the overwhelmingly common, non-blocked case."""
    assert _label_set_after_claim_then("park", "duplicate of #99") == {"sdlc:parked"}


def test_offboard_falls_back_to_adding_parked_when_the_labels_read_fails():
    """Fail-open in the SAME direction as every other best-effort step in this function: if the
    issue's current labels can't even be read (transient gh error), _offboard must fall through to
    its pre-existing behavior (add sdlc:parked) rather than silently skipping the parked label on a
    read failure and leaving the issue with no park signal at all. Must also not raise."""
    src = _mod("sources")
    calls = []

    def run(a):
        gql = _gql_swap(a, calls=calls)                  # #1391 step 2: one graphql swap
        if gql is not None:
            return gql
        calls.append(list(a))
        if a[0:2] == ["issue", "view"]:
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.park("42", "ordinary park, but the labels read itself failed")   # must not raise
    assert any("--add-label" in c and "sdlc:parked" in c for c in calls)


def test_github_release_removes_in_progress_label_and_posts_audit_comment():
    """#841: the sanctioned replacement for a manual `gh issue edit --remove-label` + hand-written
    comment (observed live on #813/#815/#817/#821 before this verb existed). Mirrors
    mark_in_progress's own label call and _offboard's own comment call, but touches ONLY the
    in-progress label — never the goal label, never the parked label, never closes the issue: the
    goal is not done and not blocked on a human decision, it just goes back to being an ordinary
    pending goal."""
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.release("42", "claimed by next-batch but never dispatched")

    remove_calls = [c for c in run.calls if c[0:2] == ["issue", "edit"] and "--remove-label" in c]
    assert any("sdlc:in-progress" in c for c in remove_calls), "in-progress label removal missing"
    assert not any("sdlc:goal" in c for c in remove_calls), "must never remove the goal label"
    assert not any("--add-label" in c and "sdlc:parked" in c for c in run.calls), \
        "must never add the parked label"
    assert not any(c[0:2] == ["issue", "close"] for c in run.calls), "must never close the issue"

    comments = [c for c in run.calls if len(c) > 1 and c[1] == "comment"]
    assert len(comments) == 1
    body = comments[0][comments[0].index("--body") + 1]
    assert body == ("Released by Sigma — claimed but not started: "
                     "claimed by next-batch but never dispatched")


def test_github_release_comment_has_no_trailing_colon_when_no_reason_is_given():
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.release("42", "")
    comments = [c for c in run.calls if len(c) > 1 and c[1] == "comment"]
    body = comments[0][comments[0].index("--body") + 1]
    assert body == "Released by Sigma — claimed but not started"


def test_github_release_survives_a_raising_label_removal(capsys):
    """A transient gh error removing the (best-effort) in-progress label must not stop the
    release — the audit comment still goes out. Seen live in the predecessor 2026-09-28 (releasing
    a stale claim during a migration): this used to be a bare `try/except: pass`, so the failure
    was truly invisible even though `release()` still reported success. It must now write a
    greppable stderr line and be visible through `release_warnings()`."""
    src = _mod("sources")

    def run(a):
        verb = _issues_verb(a)
        if verb == "edit" and "--remove-label" in _legacy(a):
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return gqlfake.rest_write(a) or ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    result = gh.release("42", "skipped this run")   # must not raise
    assert result is True

    err = capsys.readouterr().err
    assert "release label removal failed" in err
    assert "#42" in err
    assert "gh: HTTP 502 Bad Gateway" in err

    warnings = gh.release_warnings()
    assert len(warnings) == 1
    assert "label removal failed" in warnings[0]


def test_github_release_survives_a_raising_comment(capsys):
    """A transient gh error posting the audit comment must not raise into the caller — matches
    mark_in_progress/_offboard's own best-effort posture on every individual `gh` call. Same live
    incident as the label-removal case above: a swallowed comment failure must still surface."""
    src = _mod("sources")

    def run(a):
        verb = _issues_verb(a)
        if verb == "comment":
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return gqlfake.rest_write(a) or ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    result = gh.release("42", "skipped this run")   # must not raise
    assert result is True

    err = capsys.readouterr().err
    assert "release audit comment failed" in err
    assert "#42" in err
    assert "gh: HTTP 502 Bad Gateway" in err

    warnings = gh.release_warnings()
    assert len(warnings) == 1
    assert "audit comment failed" in warnings[0]


def test_github_release_warnings_empty_and_silent_on_a_clean_release(capsys):
    """The escalation is reserved for a genuine failure -- a plain, successful release writes
    nothing to stderr and reports no warnings, mirroring
    test_mark_in_progress_does_not_escalate_on_a_clean_success."""
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.release("42", "claimed but never dispatched") is True
    assert capsys.readouterr().err == ""
    assert gh.release_warnings() == []


def test_github_release_warnings_reset_on_each_call():
    """release_warnings() reflects only the MOST RECENT call, not a sticky latch -- a later,
    genuinely clean release must clear a warning left by an earlier failed one."""
    src = _mod("sources")
    calls = {"n": 0}

    def run(a):
        verb = _issues_verb(a)
        if verb == "edit" and "--remove-label" in _legacy(a) and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return gqlfake.rest_write(a) or ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.release("42", "first attempt")
    assert gh.release_warnings() != []

    gh.release("43", "second attempt, clean")
    assert gh.release_warnings() == []


def test_github_release_no_op_returns_false_and_clears_an_earlier_release_warning(capsys):
    """The return contract is unchanged (True released / False no-op) and the getter is per call:
    a no-op release on a closed issue reports False, writes nothing, and holds no warning left
    by an earlier failed call."""
    src = _mod("sources")
    state = {"closed": False}

    def run(a):
        verb = _issues_verb(a)
        rest = gqlfake.rest_issue(a, lambda n, f: {"state": "CLOSED" if state["closed"] else "OPEN", "labels": []})
        if rest is not None:
            return rest
        if verb == "edit" and "--remove-label" in _legacy(a):
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return gqlfake.rest_write(a) or ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.release("42", "first, label removal fails") is True
    assert len(gh.release_warnings()) == 1
    capsys.readouterr()
    state["closed"] = True
    assert gh.release("43", "already closed") is False
    assert gh.release_warnings() == []
    assert capsys.readouterr().err == ""


def _release_lines(err):
    """The `sigma: release ...` lines on stderr: a count of these does not depend on unrelated
    output (a silent path that later learns to warn) the way counting newlines would."""
    return [line for line in err.splitlines() if line.startswith("sigma: release")]


def test_github_release_records_both_failures_in_order_and_still_returns_true(capsys):
    src = _mod("sources")

    def run(a):
        verb = _issues_verb(a)
        if (verb == "edit" and "--remove-label" in _legacy(a)) or verb == "comment":
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return gqlfake.rest_write(a) or ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.release("42", "skipped this run") is True
    warnings = gh.release_warnings()
    assert [w.split(" failed")[0] for w in warnings] == ["label removal", "audit comment"]
    assert len(_release_lines(capsys.readouterr().err)) == 2      # one line per failure


def _release_leak_body(head, tail):
    """A multi-line stale-resume reason with a marker at each end: `head` lands inside the first
    200 chars of a `str(exc)` built from the argv, `tail` far beyond them."""
    return head + "\n" + "diagnosis line\n" * 40 + tail


def test_github_release_warning_never_echoes_the_comment_body_on_a_real_gh_error(tmp_path, capsys):
    """AGENTS.md SAFETY: `_run_gh` puts the whole argv, `--body` included, in `str(exc)` and gh's
    own stderr alone on `.hint`. The audit-comment warning must carry the hint, not the body, on
    stderr and in `release_warnings()`. A cap alone is not enough: `head` sits inside it."""
    src = _mod("sources")
    fake_gh = _fake_gh_script(tmp_path, "HTTP 502 Bad Gateway")
    gh = src.GitHubSource({"discovery": {"source": "github"}},
                          run=lambda a: src._run_gh(a, binary=fake_gh))
    assert gh.release("42", _release_leak_body("SECRET-HEAD", "SECRET-TAIL")) is True
    err = capsys.readouterr().err
    warnings = gh.release_warnings()
    assert len(warnings) == 2 and len(_release_lines(err)) == 2, (warnings, err)
    for text in (err, *warnings):
        assert "HTTP 502 Bad Gateway" in text
        assert "SECRET-HEAD" not in text and "SECRET-TAIL" not in text


def test_github_release_warning_is_one_capped_line_when_the_error_has_no_hint(capsys):
    """A runner that raises a plain error (no `.hint`) with a long multi-line message: whitespace
    collapses to one line and the text is capped, so the tail of a `--body` cannot leak."""
    src = _mod("sources")
    long_text = _release_leak_body("head", "SECRET-TAIL")

    def run(a):
        if _issues_verb(a) == "comment":
            raise RuntimeError(long_text)
        return gqlfake.rest_write(a) or ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.release("42", "skipped") is True
    err = capsys.readouterr().err
    (warning,) = gh.release_warnings()
    (line,) = _release_lines(err)
    assert line.endswith("no audit-trail comment was posted") and "\n" not in warning
    assert "SECRET-TAIL" not in err and "SECRET-TAIL" not in warning
    assert len(warning) <= 250, len(warning)


def test_github_release_warnings_returns_a_copy():
    src = _mod("sources")

    def run(a):
        if _issues_verb(a) == "comment":
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return gqlfake.rest_write(a) or ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.release("42", "skipped")
    held = gh.release_warnings()
    assert len(held) == 1
    held.append("caller scribbled here")
    held.clear()
    assert len(gh.release_warnings()) == 1
    assert gh.release_warnings() is not gh.release_warnings()


def test_github_release_label_failure_leaves_the_audit_comment_body_untouched():
    """A failed label removal must not change what the audit comment says: the warning's own
    text is a different variable from the caller's `reason`, which the comment body is built from."""
    src = _mod("sources")
    run = _recording_runner()

    def failing_label(a):
        if _issues_verb(a) == "edit" and "--remove-label" in a:
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return run(a)

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=failing_label)
    assert gh.release("42", "the caller's own reason") is True
    comments = [c for c in run.calls if len(c) > 1 and c[1] == "comment"]
    assert len(comments) == 1
    assert comments[0][comments[0].index("--body") + 1] == (
        "Released by Sigma — claimed but not started: the caller's own reason")


# --- PR #1107 review, Finding 3: release must refuse on an already-terminal issue ---

def test_github_release_returns_true_when_it_actually_releases():
    src = _mod("sources")
    run = _recording_runner({"rest": {"state": "OPEN", "labels": []}})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.release("42", "claimed but never dispatched") is True


def test_github_release_refuses_when_the_issue_is_already_closed():
    """Reproduced live pre-fix: calling release on an already-closed issue still removed the
    in-progress label and posted the "claimed but not started" comment regardless. The new probe
    (mirrors complete()'s own #505 already-closed check) must stop all three mutations."""
    src = _mod("sources")
    run = _recording_runner({"rest": {"state": "CLOSED", "labels": []}})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    result = gh.release("42", "stale cleanup sweep")

    assert result is False
    assert not any(c[0:2] == ["issue", "edit"] for c in run.calls), "must not touch any label"
    assert not any(len(c) > 1 and c[1] == "comment" for c in run.calls), "must not post a comment"
    assert not any(len(c) > 1 and c[1] == "close" for c in run.calls)


def test_github_release_refuses_when_the_issue_carries_the_parked_label():
    """Same guard, the parked/failed half -- GitHubSource represents both with one label
    (`_offboard` gives park() and fail() the identical `parked_label`, distinguished only by
    comment text), so one label check on the probe's `labels` covers both terminal outcomes."""
    src = _mod("sources")
    run = _recording_runner({"rest": {"state": "OPEN", "labels": [{"name": "sdlc:parked"}, {"name": "sdlc:goal"}]}})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    result = gh.release("42", "stale cleanup sweep")

    assert result is False
    assert not any(c[0:2] == ["issue", "edit"] and "--remove-label" in c for c in run.calls)
    assert not any(len(c) > 1 and c[1] == "comment" for c in run.calls)


def test_github_release_falls_back_to_releasing_when_the_state_probe_fails(capsys):
    """Mirrors complete()'s own state-probe fail-open posture (#505): a transient gh error on the
    NEW probe read must never block a legitimate release -- fall through to the pre-existing
    (fire-anyway) behavior rather than refuse on a state it could not actually determine."""
    src = _mod("sources")

    def run(a):
        if gqlfake.is_issue_read(a):      # #895: the REST probe AND its one fallback both fail
            raise RuntimeError("gh: HTTP 503 Service Unavailable")
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    result = gh.release("42", "skipped this run")   # must not raise
    assert result is True
    # The probe is not one of the two calls release() surfaces: release() itself stays silent. The
    # one line gh_api.read_issue prints for a fallback-class REST failure (#895) is the only output.
    assert gh.release_warnings() == []
    err = capsys.readouterr().err.splitlines()
    assert [l for l in err if not l.startswith("sigma: gh REST issue #42 read failed")] == []


# --- #505: complete()'s completion comment must post even when the issue is already closed ---
# `gh issue close --comment` on an issue GitHub already auto-closed (via the merged PR's
# "Fixes #N"/"Closes #N") exits 0 but never posts the --comment text -- verified live against the
# real gh CLI (v2.97.0): stdout empty, stderr "! Issue ... is already closed", comment count stays
# 0. complete() now probes the issue's state first and routes the comment through whichever call
# will actually post it.

def test_complete_comments_then_closes():
    """#895 slice 3a: when the state-probe finds the issue still OPEN, complete() posts the audit comment
    over REST and THEN closes it (`state_reason=completed`) -- two mutations, comment first, as
    `gh issue close --comment` was; no combined close call exists any more."""
    src = _mod("sources")
    run = _recording_runner({"rest": {"state": "OPEN"}})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.complete("42")

    close_calls = [c for c in run.calls if len(c) > 1 and c[1] == "close"]
    comment_calls = [c for c in run.calls if len(c) > 1 and c[1] == "comment"]
    assert len(close_calls) == 1 and "--reason" in close_calls[0] and "completed" in close_calls[0]
    assert len(comment_calls) == 1 and "Completed by the Sigma SDLC loop." in comment_calls[0]
    assert run.calls.index(comment_calls[0]) < run.calls.index(close_calls[0])
    assert not any(c[:2] == ["issue", "close"] and "--comment" in c for c in run.calls)


def test_complete_posts_comment_via_fallback_when_already_closed():
    """When the state-probe finds the issue already CLOSED (GitHub auto-closed it via the merged
    PR's "Fixes #N"), complete() must skip the now-redundant 'issue close' call and post the
    completion comment via a standalone 'issue comment' call instead, so the audit-trail comment
    is never silently lost.

    Non-vacuous: confirmed to FAIL against the pre-fix code (issue close was called
    unconditionally and no standalone comment call existed) before the fix was applied."""
    src = _mod("sources")
    run = _recording_runner({"rest": {"state": "CLOSED"}})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.complete("42")

    close_calls = [c for c in run.calls if len(c) > 1 and c[1] == "close"]
    comment_calls = [c for c in run.calls if len(c) > 1 and c[1] == "comment"]
    assert close_calls == [], "issue close must not be called when the issue is already closed"
    assert len(comment_calls) == 1, "standalone issue comment call missing"
    assert "--body" in comment_calls[0] and "Completed by the Sigma SDLC loop." in comment_calls[0]


def test_complete_survives_fallback_comment_failure_when_already_closed():
    """The standalone fallback 'issue comment' call (already-closed branch) must be best-effort.
    run_loop downgrades ANY exception out of complete() into a park() -- misleadingly
    parking/blocking an issue that is already genuinely closed over a mere transient gh error
    would be worse than the original silent-comment bug this fix closes."""
    src = _mod("sources")

    def run(a):
        verb = _issues_verb(a)
        rest = gqlfake.rest_issue(a, lambda n, f: {"state": "CLOSED"})      # #895: REST state probe
        if rest is not None:
            return rest
        if verb == "comment":
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._NOTE_RETRY_BASE = 0    # hermetic: the fallback comment now retries via note() (#1657 follow-up)
    gh.complete("42")   # must not raise


def test_complete_falls_back_to_rest_when_the_already_closed_comment_hits_exhausted_graphql():
    """#1657 follow-up (scope decision): the reviewer's finding was that `complete()`'s already-
    closed comment and `_offboard()`'s park/fail comment post via a bare `self._run([...])` in a
    `try/except: pass`, NOT through `note()`, so they stayed just as vulnerable to silent loss under
    a genuinely exhausted GraphQL quota as `note()` itself was before the original #1657 fix.
    Decision: extend the fix here too, by routing both through `note()` -- the same GraphQL-retry-
    then-REST-fallback chokepoint -- since it is a small, safe reuse (best-effort try/except at the
    call site is unchanged; `note()` already raises exactly when there is nothing left to try, and
    the wrapping try/except swallows that as before). This proves the already-closed branch: with
    every GraphQL comment attempt exhausted, the comment must still land via REST, not be lost."""
    src = _mod("sources")
    calls = []

    def run(a):
        calls.append(a)
        verb = _issues_verb(a)
        rest = gqlfake.rest_issue(a, lambda n, f: {"state": "CLOSED"})      # #895: REST state probe
        if rest is not None:
            return rest
        if verb == "comment" and a[0] == "api":          # #895 slice 8: the REST POST is rate limited
            e = RuntimeError("gh api failed")
            e.hint = "gh: API rate limit exceeded (HTTP 429)"
            raise e
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    gh._NOTE_RETRY_BASE = 0
    gh.complete("42")   # must not raise -- and must not silently lose the comment either

    fallbacks = [c for c in calls if c[:2] == ["issue", "comment"]]
    # excludes the SEPARATE `api graphql` label-removal call `complete()` also issues -- that one
    # is unrelated to this comment's own policy fallback and this test does not stub it out.
    rest_attempts = [c for c in calls if c[:2] == ["api", "repos/o/r/issues/42/comments"]]
    assert len(rest_attempts) == 1                       # REST first, never retried on a rate limit
    assert len(fallbacks) == 1                           # then exactly one policy fallback, not lost
    assert "body=Completed by the Sigma SDLC loop." in rest_attempts[0]
    assert fallbacks[0][fallbacks[0].index("--body") + 1] == "Completed by the Sigma SDLC loop."


def test_park_falls_back_to_rest_when_the_comment_hits_exhausted_graphql():
    """#1657 follow-up (scope decision), park()/fail() half: `_offboard()`'s comment is the ONLY
    durable record of WHY a goal was parked -- a park is a human's deliberate checkpoint that only
    /sigma-unpark reopens, so losing this comment to an exhausted GraphQL quota is exactly as bad as
    losing a REJECT verdict's comment. Now routed through `note()`, so it gets the identical
    retry-then-REST treatment."""
    src = _mod("sources")
    calls = []

    def run(a):
        gql = _gql_swap(a, calls=calls)
        if gql is not None:
            return gql
        calls.append(a)
        if a[:2] == ["api", "repos/o/r/issues/42/comments"]:      # #895 slice 8: the REST POST is rate limited
            e = RuntimeError("gh api failed")
            e.hint = "gh: API rate limit exceeded (HTTP 429)"
            raise e
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    gh._NOTE_RETRY_BASE = 0
    gh.park("42", "deploy gate")   # must not raise -- and must not silently lose the comment either

    fallbacks = [c for c in calls if c[:2] == ["issue", "comment"]]
    rest_attempts = [c for c in calls if c[:2] == ["api", "repos/o/r/issues/42/comments"]]
    assert len(rest_attempts) == 1                       # REST first, never retried on a rate limit
    assert len(fallbacks) == 1                           # then exactly one policy fallback
    assert "body=Parked by Sigma — needs human review: deploy gate" in rest_attempts[0]
    assert fallbacks[0][fallbacks[0].index("--body") + 1] == "Parked by Sigma — needs human review: deploy gate"


def test_complete_still_comments_then_closes_when_state_probe_fails():
    """If the state probe itself fails (transient gh error), complete() falls through to the comment-then-
    close path -- never the standalone-comment-only path -- and never raises."""
    src = _mod("sources")
    calls = []

    def run(a):
        calls.append(list(a))
        if gqlfake.is_issue_read(a):      # #895: the REST probe AND its one fallback both fail
            raise RuntimeError("gh: HTTP 503 Service Unavailable")
        return gqlfake.rest_write(a, calls=calls) or ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.complete("42")   # must not raise

    assert len([c for c in calls if c[:2] == ["issue", "close"]]) == 1
    assert len([c for c in calls if c[:2] == ["issue", "comment"]]) == 1


def test_complete_raises_and_leaves_the_issue_open_when_the_close_fails_after_the_comment():
    """RAISE contract: the comment landed, the close PATCH failed -> complete() raises (run_loop parks)."""
    src = _mod("sources")
    calls = []

    def run(a):
        if gqlfake.is_issue_read(a):
            return gqlfake.rest_issue(a, lambda n, f: {"state": "OPEN"}) or ""
        if gqlfake.is_issue_write(a) and "PATCH" in a:
            e = RuntimeError("gh: HTTP 422")
            e.hint = "gh: Unprocessable (HTTP 422)"
            raise e
        return gqlfake.rest_write(a, calls=calls) or ""

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    import pytest
    with pytest.raises(Exception):
        gh.complete("42")
    assert [c[:2] for c in calls] == [["issue", "comment"]]


# --- #505 + #506 combined: the already-closed fallback branch (#505) must still remove the
# in-progress label (#506) -- these two fixes landed independently and were reconciled by hand at
# a rebase conflict in complete(); neither original PR's own tests cover this specific combined
# path (the #506 tests never mock the state-probe read #505 added, so they only exercise the
# not-already-closed branch; the #505 tests never assert on label removal), so it needed its own
# test rather than trusting that two independently-correct diffs compose correctly by construction.

def test_complete_removes_in_progress_label_even_when_already_closed():
    """The in-progress label removal (#506) must run in EITHER branch of the already-closed check
    (#505) -- not just the not-already-closed/combined-call path. Non-vacuous: this genuinely
    exercises the already-closed branch (unlike #506's own tests) and asserts on label removal
    (unlike #505's own tests) -- the two existing test suites' blind spots don't overlap, so this
    combined case was previously untested by either."""
    src = _mod("sources")
    run = _recording_runner({"rest": {"state": "CLOSED"}})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh.complete("42")

    assert any(c[0:2] == ["issue", "edit"] and "42" in c and "--remove-label" in c and
               "sdlc:in-progress" in c for c in run.calls), \
        "in-progress label removal missing on the already-closed branch"


# --- #519: fetch_title_body() -- the read-only title+body primitive goal_decompose's decompose_check
# verb drives, routed through each source's own chokepoint (GitHubSource._run / a plain file read) so
# a test can prove the verb never bypasses it with a module-level shell-out.

def test_github_fetch_title_body_reads_via_run_chokepoint():
    """#1808: REST (`gh api repos/{owner}/{repo}/issues/<n>`), never `gh issue view` -- `issue view`
    is GraphQL under the hood and shares GitHub's SEPARATE, much more easily exhausted hourly
    `graphql` budget with every other `gh issue view`/`issue list`/board call this loop makes,
    confirmed live via direct reproduction against goal #1756/PR #1806 ("GraphQL: API rate limit
    already exceeded ..."). `work.py`'s `_declared_unit` already makes the identical read
    (title/body/labels of one issue) via REST for exactly this reason; this brings
    `fetch_title_body` in line with that precedent instead of carrying its own GraphQL dependency."""
    src = _mod("sources")
    run = _recording_runner({"repos/o/r/issues/42":
                              json.dumps({"title": "an epic issue", "body": "line one\nline two"})})
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    result = gh.fetch_title_body("42")
    assert result == {"title": "an epic issue", "body": "line one\nline two"}
    flat = [" ".join(c) for c in run.calls]
    assert any(c.startswith("api repos/o/r/issues/42") for c in flat), flat
    assert not any(c.startswith("issue view") for c in flat), flat   # never GraphQL


def test_github_fetch_title_body_with_no_configured_repo_uses_the_owner_repo_placeholder():
    """Mirrors `_fetch_issues_rest`/`note`'s own fallback: an unset `repo` uses the literal
    `{owner}/{repo}` placeholder, which `gh api` resolves from the working directory's git remote
    at zero extra requests -- never `_owner_name()`'s own GraphQL `gh repo view`."""
    src = _mod("sources")
    run = _recording_runner({"repos/{owner}/{repo}/issues/42":
                              json.dumps({"title": "t", "body": "b"})})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.fetch_title_body("42") == {"title": "t", "body": "b"}


def test_github_fetch_title_body_degrades_on_malformed_json():
    src = _mod("sources")
    run = _recording_runner({"repos/{owner}/{repo}/issues/42": "not json"})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.fetch_title_body("42") == {"title": "", "body": ""}


def test_github_fetch_title_body_degrades_on_a_non_object_payload():
    src = _mod("sources")
    run = _recording_runner({"repos/{owner}/{repo}/issues/42": json.dumps(["not", "an", "object"])})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.fetch_title_body("42") == {"title": "", "body": ""}


# --- #2435: fetch_body_labels_rest() -- the REST twin of fetch_body_labels, for a caller that
# reads the same question on a much higher-frequency trigger than attach_at_pick's once-per-pick
# GraphQL read can afford (loop._ensure_unit_tracking).

def test_github_fetch_body_labels_rest_reads_via_run_chokepoint():
    """Same endpoint shape as `fetch_title_body` (#1808's own REST-not-GraphQL reasoning), but
    returning `body`+`labels` -- `features.read`'s accepted payload -- rather than `title`+`body`."""
    src = _mod("sources")
    run = _recording_runner({"repos/o/r/issues/42": json.dumps(
        {"title": "ignored", "body": "Feature: voice\n",
         "labels": [{"name": "feature:voice"}, {"name": "sdlc:goal"}]})})
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    result = gh.fetch_body_labels_rest("42")
    assert result == {"body": "Feature: voice\n",
                       "labels": [{"name": "feature:voice"}, {"name": "sdlc:goal"}]}
    flat = [" ".join(c) for c in run.calls]
    assert any(c.startswith("api repos/o/r/issues/42") for c in flat), flat
    assert not any(c.startswith("issue view") for c in flat), flat   # never GraphQL


def test_github_fetch_body_labels_rest_with_no_configured_repo_uses_the_owner_repo_placeholder():
    src = _mod("sources")
    run = _recording_runner({"repos/{owner}/{repo}/issues/42":
                              json.dumps({"body": "b", "labels": [{"name": "feature:x"}]})})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.fetch_body_labels_rest("42") == {"body": "b", "labels": [{"name": "feature:x"}]}


def test_github_fetch_body_labels_rest_degrades_on_malformed_json():
    src = _mod("sources")
    run = _recording_runner({"repos/{owner}/{repo}/issues/42": "not json"})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.fetch_body_labels_rest("42") == {"body": "", "labels": []}


def test_github_fetch_body_labels_rest_degrades_on_a_non_object_payload():
    src = _mod("sources")
    run = _recording_runner({"repos/{owner}/{repo}/issues/42": json.dumps(["not", "an", "object"])})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.fetch_body_labels_rest("42") == {"body": "", "labels": []}


def test_local_fetch_title_body_prefers_frontmatter_title_and_strips_the_fence():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0001-x.md"
        g.write_text("---\nid: 0001\nstatus: pending\ntitle: Add an exclaim\n---\n\nMake greet() louder.\n")
        local = src.get_source(str(base), {})
        result = local.fetch_title_body(str(g))
        assert result["title"] == "Add an exclaim"
        assert result["body"].strip() == "Make greet() louder."
        assert "title:" not in result["body"]           # frontmatter keys never leak into "body"


def test_local_fetch_title_body_falls_back_to_filename_stem_without_a_title():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0002-no-title.md"
        g.write_text("---\nid: 0002\nstatus: pending\n---\n\nbody only\n")
        local = src.get_source(str(base), {})
        result = local.fetch_title_body(str(g))
        assert result["title"] == "0002-no-title"
        assert result["body"].strip() == "body only"


def test_local_fetch_title_body_with_no_frontmatter_fence_returns_the_raw_text_as_body():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
        g = base / "goals" / "0003.md"
        g.write_text("just plain text, no fences\n")
        local = src.get_source(str(base), {})
        result = local.fetch_title_body(str(g))
        assert result["title"] == "0003"
        assert result["body"] == "just plain text, no fences\n"


def test_local_fetch_title_body_missing_file_returns_empty_strings():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"
        local = src.get_source(str(base), {})
        assert local.fetch_title_body(str(base / "goals" / "nope.md")) == {"title": "", "body": ""}


# --- #522: fetch_comments_strict() -- the STRICT read decompose_check's `file`-mode idempotency
# check drives. Deliberately asymmetric vs fetch_title_body above: raises on a transport failure
# AND on a malformed/non-object payload, rather than degrading to empty -- the caller must be able
# to tell "could not read the timeline" apart from "read it, found no marker", or a second
# concurrent run could silently file a duplicate meta-issue.

def _recording_runner_by_json(by_json=None):
    """Fake `gh` runner keyed on the `--json` argument's VALUE, not `args[1]` -- fetch_title_body
    and fetch_comments_strict both issue an `issue view ... --json <fields>` call, so the plain
    args[1]-keyed _recording_runner above cannot tell them apart (#522 review, test-infra note). A
    canned value that is an Exception INSTANCE is raised instead of returned, for transport-failure
    tests.

    #895: the REST issue read (no `--json`) answers a 502, so every test on this runner drives the
    ONE-call `gh issue view` FALLBACK path and its payload checks. REST-path cases use
    `_recording_runner({"rest": ...})`."""
    calls = []
    by_json = by_json or {}
    def run(args):
        calls.append(list(args))
        if gqlfake.rest_issue_target(args) is not None:
            exc = RuntimeError("gh api failed")
            exc.hint = "gh: Server Error (HTTP 502)"
            raise exc
        key = args[args.index("--json") + 1] if "--json" in args else None
        val = by_json.get(key, "")
        if isinstance(val, Exception):
            raise val
        return val
    run.calls = calls
    return run


def test_github_fetch_comments_strict_reads_via_run_chokepoint():
    src = _mod("sources")
    run = _recording_runner_by_json({"comments,labels": json.dumps({
        "comments": [{"id": "1", "body": "hi"}], "labels": [{"name": "area:engine"}]})})
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    result = gh.fetch_comments_strict("42")
    assert result == {"comments": [{"id": "1", "body": "hi"}], "labels": [{"name": "area:engine"}]}
    flat = [" ".join(c) for c in run.calls]
    assert any("issue view 42" in c and "--json comments,labels" in c and "--repo o/r" in c for c in flat)


def test_github_fetch_comments_strict_raises_on_transport_failure():
    src = _mod("sources")
    run = _recording_runner_by_json({"comments,labels": RuntimeError("gh: not authenticated")})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    try:
        gh.fetch_comments_strict("42")
        assert False, "expected an exception -- a transport failure must never degrade to empty here"
    except Exception as exc:          # #895: GhApiError naming both the REST and the fallback failure
        assert "not authenticated" in str(exc)


def test_github_fetch_comments_strict_raises_on_malformed_json():
    src = _mod("sources")
    run = _recording_runner_by_json({"comments,labels": "not json"})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    try:
        gh.fetch_comments_strict("42")
        assert False, "expected an exception -- malformed JSON must never degrade to empty here"
    except Exception:
        pass


def test_github_fetch_comments_strict_raises_on_a_non_object_payload():
    src = _mod("sources")
    run = _recording_runner_by_json({"comments,labels": json.dumps(["not", "an", "object"])})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    try:
        gh.fetch_comments_strict("42")
        assert False, "expected an exception -- a non-object payload must never degrade to empty here"
    except ValueError:
        pass


def test_github_fetch_comments_strict_raises_on_an_empty_response():
    """Deliberately does NOT mirror fetch_title_body's `raw or "{}"` leniency -- an empty response
    is exactly the shape a transient blip produces, and this read must fail closed on it too."""
    src = _mod("sources")
    run = _recording_runner_by_json({"comments,labels": ""})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    try:
        gh.fetch_comments_strict("42")
        assert False, "expected an exception -- an empty response must never degrade to empty here"
    except Exception:
        pass


def test_github_fetch_comments_strict_raises_when_the_comments_key_is_entirely_absent():
    """#522 review fix 2: a real `gh issue view --json comments,labels` call always returns the
    requested field, even as an empty array -- a bare {} response (the key entirely ABSENT, not
    just empty) is itself a signal something is wrong (a truncated/malformed transport), not
    "genuinely zero comments" (which looks like {"comments": [], ...}). Raising here keeps this
    method strict at its OWN layer, consistent with decompose_check's own new distrust of a missing
    "comments" key (loop.py's `not isinstance(strict, dict) or "comments" not in strict` check) --
    defaulting it away here would just move the same fail-open hole one layer down instead of
    closing it."""
    src = _mod("sources")
    run = _recording_runner_by_json({"comments,labels": json.dumps({})})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    try:
        gh.fetch_comments_strict("42")
        assert False, "expected an exception -- a response missing the comments key entirely must never degrade to empty"
    except ValueError:
        pass


def test_github_fetch_comments_strict_defaults_a_present_but_empty_comments_list():
    """The "comments" key itself is the critical field this method protects (decompose_check's
    marker check reads it); "labels" absence alone -- with "comments" genuinely present, even as an
    empty list -- still defaults to [] rather than raising, since decompose_check's own area/
    priority extraction already tolerates missing/empty labels gracefully."""
    src = _mod("sources")
    run = _recording_runner_by_json({"comments,labels": json.dumps({"comments": []})})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.fetch_comments_strict("42") == {"comments": [], "labels": []}


# --- local work-item CREATION parity: a local backlog must file, not only advance -----------------

def _local(d):
    """A LocalSource over an empty .sdlc/goals/ tree."""
    base = pathlib.Path(d) / ".sdlc"
    (base / "goals").mkdir(parents=True)
    return _mod("sources").LocalSource(str(base)), base


def test_local_create_writes_a_pickable_goal():
    """The strongest guarantee: the file we wrote is consumable by the REAL reader, not merely
    well-formed. A created goal that next_pending() cannot see is not a work item."""
    with tempfile.TemporaryDirectory() as d:
        src, base = _local(d)
        gid = src.create_dependency("Fix the retry backoff", "Body text here.", "alice")
        assert gid == 1
        picked = _mod("discovery").next_pending(str(base / "goals"))
        assert picked and "fix-the-retry-backoff" in picked
        text = pathlib.Path(picked).read_text()
        fm = _mod("frontmatter").parse(text)
        assert fm["status"] == "pending"
        assert fm["title"] == "Fix the retry backoff"
        assert "Body text here." in text


def test_local_create_queued_is_not_auto_picked():
    """goal_label=False is the 'filed but not auto-picked' contract. Locally that is status:
    proposed — discovery already skips it, awaiting human promotion."""
    with tempfile.TemporaryDirectory() as d:
        src, base = _local(d)
        src.create_dependency("Queued work", "Body.", "alice", goal_label=False)
        fm = _mod("frontmatter").parse(sorted((base / "goals").glob("*.md"))[0].read_text())
        assert fm["status"] == "proposed"
        assert _mod("discovery").next_pending(str(base / "goals")) is None


def test_local_create_allocates_the_next_id():
    with tempfile.TemporaryDirectory() as d:
        src, base = _local(d)
        (base / "goals" / "0007-existing.md").write_text(
            "---\nid: 0007\nstatus: done\n---\nold\n", encoding="utf-8")
        (base / "goals" / "README.md").write_text("not a goal\n", encoding="utf-8")
        assert src.create_dependency("Next one", "b", "alice") == 8
        assert (base / "goals" / "0008-next-one.md").is_file()


def test_local_create_survives_a_hostile_title():
    """Frontmatter is line-based and the parser splits on the FIRST colon, then strips surrounding
    quotes. A title carrying a colon, a quote or a newline must still round-trip."""
    with tempfile.TemporaryDirectory() as d:
        src, base = _local(d)
        src.create_dependency('Plan 1B: the "big" fix\nsecond line', "b", "alice")
        path = sorted((base / "goals").glob("*.md"))[0]
        fm = _mod("frontmatter").parse(path.read_text())
        assert fm["title"] == "Plan 1B: the big fix second line"
        assert _mod("discovery").next_pending(str(base / "goals")) == str(path)


def test_local_create_records_assignee_and_labels_rather_than_dropping_them():
    """Local mode has no assignee routing and no labels. Recording them in the body is honest;
    discarding an owner a caller explicitly passed is not."""
    with tempfile.TemporaryDirectory() as d:
        src, base = _local(d)
        src.create_dependency("T", "b", "alice", labels=("area:api", "priority:P2"))
        text = sorted((base / "goals").glob("*.md"))[0].read_text()
        assert "alice" in text and "area:api" in text and "priority:P2" in text


def test_local_create_matches_the_github_signature():
    """handoff.create_tracked_issue calls this by duck-typing. A signature that drifts from
    GitHubSource's fails at runtime in local mode only — exactly where nobody is looking."""
    import inspect
    sources = _mod("sources")
    local = inspect.signature(sources.LocalSource.create_dependency).parameters
    github = inspect.signature(sources.GitHubSource.create_dependency).parameters
    assert list(local) == list(github)


# --- #698: priority ordering (GitHub) ------------------------------------------------------------
# `priority:P*` was written by handoff.py, rendered into TEAM.md, and read by NOTHING: next_pending
# sorted on issue number alone. Measured on this repo's own queue, the P1 board bugs sat at
# positions 40-41 of 44 behind two dozen P2s.

def _pissue(number, *labels):
    return {"number": number, "labels": [{"name": "sdlc:goal"}] + [{"name": l} for l in labels]}


def _gh_with(issues, **discovery):
    src = _mod("sources")
    run = _recording_runner({"list": json.dumps(issues)})
    cfg = {"discovery": {"source": "github", **discovery}}
    return src.GitHubSource(cfg, run=run)


def test_github_priority_beats_issue_number():
    assert _gh_with([_pissue(3, "priority:P2"), _pissue(9, "priority:P0")]).next_pending() == "9"


def test_github_most_urgent_label_wins_when_an_issue_carries_several():
    """Not hypothetical: issue #381 on this repo carries BOTH priority:P0 and priority:P1."""
    issues = [_pissue(3, "priority:P2"), _pissue(9, "priority:P1", "priority:P0")]
    assert _gh_with(issues).next_pending() == "9"


def test_github_unprioritised_sorts_after_every_priority():
    assert _gh_with([_pissue(3), _pissue(9, "priority:P4")]).next_pending() == "9"


def test_github_no_priority_labels_anywhere_is_identical_to_lowest_number():
    """Backward compatibility: nothing prioritised -> one bucket -> the historical key alone."""
    assert _gh_with([_pissue(9), _pissue(3), _pissue(5)]).next_pending() == "3"


def test_github_ties_within_a_priority_fall_back_to_issue_number():
    issues = [_pissue(9, "priority:P1"), _pissue(4, "priority:P1")]
    assert _gh_with(issues).next_pending() == "4"


def test_github_bug_beats_a_larger_number_within_the_same_priority_tier():
    """#813: bare issue-number was the ONLY tie-break within a tier — strictly oldest-first,
    which is exactly what let a tech-debt/enhancement goal outrank a same-tier production bug
    filed later. `bug` is an existing, already-asserted label (not a new taxonomy): within one
    priority tier, a bug-labeled issue now sorts ahead of a non-bug one, number-tie-break only
    applying WITHIN that split."""
    issues = [_pissue(3, "priority:P1"), _pissue(9, "priority:P1", "bug")]
    assert _gh_with(issues).next_pending() == "9"          # the bug wins despite the higher number


def test_github_bug_tie_break_only_matters_within_one_priority_tier():
    """A P2 bug must NOT outrank a P1 non-bug — priority still leads every other signal."""
    issues = [_pissue(3, "priority:P1"), _pissue(9, "priority:P2", "bug")]
    assert _gh_with(issues).next_pending() == "3"


def test_github_malformed_priority_label_never_raises_and_sorts_last():
    issues = [_pissue(3, "priority:urgent"), _pissue(9, "priority:P3")]
    assert _gh_with(issues).next_pending() == "9"


def test_github_priority_aliases_resolve_english_vocabulary_labels():
    issues = [_pissue(3, "priority:low"), _pissue(9, "priority:critical")]
    aliases = {"critical": "P0", "high": "P1", "medium": "P2", "low": "P3"}
    assert _gh_with(issues, priority_aliases=aliases).next_pending() == "9"


def test_github_priority_aliases_do_not_change_unaliased_typos():
    issues = [_pissue(3, "priority:urgent"), _pissue(9, "priority:P3")]
    aliases = {"critical": "P0"}     # "urgent" is not in this map
    assert _gh_with(issues, priority_aliases=aliases).next_pending() == "9"


def test_github_order_created_restores_strict_issue_number_order():
    issues = [_pissue(3, "priority:P4"), _pissue(9, "priority:P0")]
    assert _gh_with(issues, order="created").next_pending() == "3"


def test_github_priority_label_prefix_is_configurable():
    issues = [_pissue(3, "sev:P3"), _pissue(9, "sev:P0")]
    gh = _gh_with(issues, github={"priority_label_prefix": "sev:"})
    assert gh.next_pending() == "9"


def test_github_priority_never_promotes_a_parked_goal():
    """Ordering must not widen eligibility — parked stays unpickable at any priority."""
    issues = [_pissue(3, "priority:P0", "sdlc:parked"), _pissue(9, "priority:P4")]
    assert _gh_with(issues).next_pending() == "9"


def test_github_skip_still_applies_under_priority_order():
    issues = [_pissue(3, "priority:P0"), _pissue(9, "priority:P1")]
    assert _gh_with(issues).next_pending(skip={"3"}) == "9"


def test_local_source_honours_the_configured_order():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        goals = pathlib.Path(d) / "goals"; goals.mkdir()
        (goals / "0001.md").write_text("---\nid: 1\nstatus: pending\npriority: P4\n---\nx\n")
        (goals / "0002.md").write_text("---\nid: 2\nstatus: pending\npriority: P0\n---\nx\n")
        cfg = {"discovery": {"source": "local-goals"}}
        assert src.get_source(d, cfg).next_pending().endswith("0002.md")
        cfg["discovery"]["order"] = "created"
        assert src.get_source(d, cfg).next_pending().endswith("0001.md")


def test_local_source_honours_configured_priority_aliases():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        goals = pathlib.Path(d) / "goals"; goals.mkdir()
        (goals / "0001.md").write_text("---\nid: 1\nstatus: pending\npriority: Low\n---\nx\n")
        (goals / "0002.md").write_text("---\nid: 2\nstatus: pending\npriority: Critical\n---\nx\n")
        cfg = {"discovery": {"priority_aliases": {"critical": "P0", "low": "P3"}}}
        assert src.get_source(d, cfg).next_pending().endswith("0002.md")


def test_local_source_unset_aliases_leaves_english_priorities_unranked():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        goals = pathlib.Path(d) / "goals"; goals.mkdir()
        (goals / "0001.md").write_text("---\nid: 1\nstatus: pending\npriority: Critical\n---\nx\n")
        (goals / "0002.md").write_text("---\nid: 2\nstatus: pending\n---\nx\n")
        cfg = {"discovery": {"source": "local-goals"}}
        assert src.get_source(d, cfg).next_pending().endswith("0001.md")   # falls to filename order


def test_local_source_still_constructs_with_a_bare_directory():
    """Backward compatibility: LocalSource(sdlc_dir) with no config keeps working."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        goals = pathlib.Path(d) / "goals"; goals.mkdir()
        (goals / "0001.md").write_text("---\nid: 1\nstatus: pending\n---\nx\n")
        assert src.LocalSource(d).next_pending().endswith("0001.md")


def test_local_source_park_threads_the_optional_tier_through_to_the_queue():
    """#953: LocalSource.park() is the real wiring between loop.py's _record() and state.py's
    review-queue.md render -- proves the tier actually reaches the queue file, not just that
    state.park() itself (already covered in test_state.py) accepts one."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"
        (base / "goals").mkdir(parents=True); (base / "state").mkdir()
        goal = base / "goals" / "0001.md"
        goal.write_text("---\nid: 1\nstatus: pending\n---\nx\n")
        local = src.LocalSource(str(base))
        local.park(str(goal), "PR #1 changes requested", tier="escalate_l0")
        q = (base / "state" / "review-queue.md").read_text()
        assert "- tier: escalate_l0" in q


def test_local_source_park_with_no_tier_omits_the_queue_line():
    """Same call site, default (no-tier) shape -- proves nothing changed for the common case."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"
        (base / "goals").mkdir(parents=True); (base / "state").mkdir()
        goal = base / "goals" / "0001.md"
        goal.write_text("---\nid: 1\nstatus: pending\n---\nx\n")
        local = src.LocalSource(str(base))
        local.park(str(goal), "hit a deploy gate")
        q = (base / "state" / "review-queue.md").read_text()
        assert "tier" not in q


# --- _blocker_promotion (#900) ---

def test_blocker_promotion_defaults_to_off_when_unset():
    src = _mod("sources")
    assert src._blocker_promotion({}) == "off"
    assert src._blocker_promotion(None) == "off"
    assert src._blocker_promotion({"discovery": {}}) == "off"


def test_blocker_promotion_reads_the_configured_mode():
    src = _mod("sources")
    for mode in ("off", "smart", "always"):
        cfg = {"discovery": {"blocker_promotion": {"mode": mode}}}
        assert src._blocker_promotion(cfg) == mode


def test_blocker_promotion_falls_back_to_off_on_an_unrecognised_value():
    src = _mod("sources")
    cfg = {"discovery": {"blocker_promotion": {"mode": "urgent-ish"}}}
    assert src._blocker_promotion(cfg) == "off"


def test_blocker_promotion_falls_back_to_off_on_a_malformed_block():
    src = _mod("sources")
    assert src._blocker_promotion({"discovery": {"blocker_promotion": "always"}}) == "off"
    assert src._blocker_promotion({"discovery": {"blocker_promotion": None}}) == "off"


# --- #900 s1: GitHubSource wiring -- blocker_promotion_mode + sdlc_dir --------------------------
# s0 built the pure ranking rule (discovery.blocker_promotion_rank) and the config resolver
# (_blocker_promotion, tested above). This slice wires the resolved mode onto GitHubSource itself
# (matching how self.priority_aliases is already stored) and threads sdlc_dir through so the
# mechanism can read the shared ledger for its second edge-detection channel -- the board-touch
# mechanics themselves (the reverse lookup, the transitive walk, the write) are exercised in
# test_github_project.py, where the board simulator + _sync_backlog already live.

def test_github_source_stores_the_resolved_blocker_promotion_mode():
    src = _mod("sources")
    for mode in ("off", "smart", "always"):
        cfg = {"discovery": {"blocker_promotion": {"mode": mode}}}
        gh = src.GitHubSource(cfg, run=_recording_runner())
        assert gh.blocker_promotion_mode == mode


def test_github_source_blocker_promotion_mode_defaults_to_off():
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=_recording_runner())
    assert gh.blocker_promotion_mode == "off"


def test_github_source_defaults_sdlc_dir_to_none():
    """Every existing construction site (triage.py, every test in this file so far) constructs
    GitHubSource(config, run=...) with no third argument -- sdlc_dir has to default to None so none
    of them break, and the ledger-edge channel degrades to 'unavailable' rather than raising when
    it's unset (proven in test_github_project.py)."""
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=_recording_runner())
    assert gh.sdlc_dir is None



# --- _auto_unpark (#1129) ---

def test_auto_unpark_defaults_to_ON_when_unset():
    src = _mod("sources")
    assert src._auto_unpark({}) == "on"
    assert src._auto_unpark(None) == "on"
    assert src._auto_unpark({"discovery": {}}) == "on"


def test_auto_unpark_reads_the_configured_mode():
    src = _mod("sources")
    for mode in ("off", "on"):
        cfg = {"discovery": {"auto_unpark": {"mode": mode}}}
        assert src._auto_unpark(cfg) == mode


def test_auto_unpark_falls_back_to_the_default_on_an_unrecognised_value():
    src = _mod("sources")
    cfg = {"discovery": {"auto_unpark": {"mode": "always"}}}   # a blocker_promotion value, not ours
    assert src._auto_unpark(cfg) == "on"


def test_auto_unpark_falls_back_to_the_default_on_a_malformed_block():
    src = _mod("sources")
    assert src._auto_unpark({"discovery": {"auto_unpark": "on"}}) == "on"
    assert src._auto_unpark({"discovery": {"auto_unpark": None}}) == "on"


# --- _blocking_priority_override (#1352) ---

def test_blocking_priority_override_defaults_to_TRUE_when_unset():
    src = _mod("sources")
    assert src._blocking_priority_override({}) is True
    assert src._blocking_priority_override(None) is True
    assert src._blocking_priority_override({"discovery": {}}) is True


def test_blocking_priority_override_reads_the_configured_boolean():
    src = _mod("sources")
    assert src._blocking_priority_override({"discovery": {"blocking_priority_override": True}}) is True
    assert src._blocking_priority_override({"discovery": {"blocking_priority_override": False}}) is False


def test_blocking_priority_override_falls_back_to_the_default_on_a_non_boolean_value():
    """#1394 inverted this with the default: only the literal boolean `False` turns it OFF. The
    property that mattered is preserved -- a typo'd string/number/null must never silently CHANGE
    behaviour -- it just lands on the default, which is now on."""
    src = _mod("sources")
    assert src._blocking_priority_override({"discovery": {"blocking_priority_override": "true"}}) is True
    assert src._blocking_priority_override({"discovery": {"blocking_priority_override": 1}}) is True
    assert src._blocking_priority_override({"discovery": {"blocking_priority_override": False}}) is False
    assert src._blocking_priority_override({"discovery": {"blocking_priority_override": None}}) is True


# --- #1352: the label-queue half of the "blocking work sorts first" structural override. The
# board-authoritative half lives in test_github_project.py (_board_queue/_card_rank), since it
# needs that file's board-world fixtures.

def test_github_blocking_priority_override_sorts_a_low_priority_blocking_issue_first():
    """A P3 issue carrying sdlc:blocking must outrank a plain P0 issue when the override is on --
    a structural sort, not a priority nudge."""
    src = _mod("sources")
    p0_issue = {"number": 9, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P0"}]}
    blocking_issue = {"number": 3, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P3"},
                                              {"name": "sdlc:blocking"}]}

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        labels = _rest_labels(a)
        if "sdlc:blocking" in labels:
            return json.dumps([blocking_issue])
        return json.dumps([p0_issue, blocking_issue])

    gh = src.GitHubSource({"discovery": {"source": "github", "blocking_priority_override": True}}, run=run)
    assert gh.next_pending() == "3"


def test_github_blocking_priority_override_off_by_default_leaves_priority_order_alone():
    src = _mod("sources")
    p0_issue = {"number": 9, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P0"}]}
    blocking_issue = {"number": 3, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P3"},
                                              {"name": "sdlc:blocking"}]}
    run = _recording_runner({"list": json.dumps([p0_issue, blocking_issue])})
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.next_pending() == "9"          # override not configured -> plain priority order wins


def test_github_blocking_priority_override_never_fires_a_query_when_explicitly_off():
    """#1394: the override is ON by default now, so an adopter who wants the old ordering turns it
    OFF -- and turning it off must genuinely cost zero extra API calls, not merely be harmless when
    the query fires."""
    src = _mod("sources")
    calls = []

    def run(a):
        calls.append(list(a))
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        return json.dumps([{"number": 5, "labels": [{"name": "sdlc:goal"}]}])

    gh = src.GitHubSource({"discovery": {"source": "github",
                                         "blocking_priority_override": False}}, run=run)
    assert gh.next_pending() == "5"
    assert not any("sdlc:blocking" in _rest_labels(c) for c in calls)


def test_github_blocking_priority_override_fires_its_query_by_default():
    """The counterpart: on a stock config the override IS live, which is one extra `issue list` per
    pick on the label path. Named here rather than left as a surprise in someone's rate-limit
    graph -- it is the cost of the chain flow being the default."""
    src = _mod("sources")
    calls = []

    def run(a):
        calls.append(list(a))
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        if "sdlc:blocking" in _rest_labels(a):
            return "[]"
        return json.dumps([{"number": 5, "labels": [{"name": "sdlc:goal"}]}])

    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.next_pending() == "5"
    assert any("sdlc:blocking" in _rest_labels(c) for c in calls)


def test_github_blocking_priority_override_falls_through_when_nothing_is_currently_blocking():
    src = _mod("sources")
    plain_issue = {"number": 9, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P2"}]}

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        labels = _rest_labels(a)
        if "sdlc:blocking" in labels:
            return json.dumps([])
        return json.dumps([plain_issue])

    gh = src.GitHubSource({"discovery": {"source": "github", "blocking_priority_override": True}}, run=run)
    assert gh.next_pending() == "9"


def test_github_blocking_priority_override_never_engages_under_order_created():
    """#1352 review finding (notable): `order: "created"`'s own, twice-independently-reviewed
    contract is strict, priority-blind FIFO. The override is fundamentally a priority-tier concept
    ("sorts ahead of priority ordering") and must never engage under this mode, exactly like the
    tier-widening backfill logic a few lines below in the real code already refuses to."""
    src = _mod("sources")
    older = {"number": 3, "labels": [{"name": "sdlc:goal"}]}
    newer_blocking = {"number": 9, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:blocking"}]}

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        labels = _rest_labels(a)
        if "sdlc:blocking" in labels:
            return json.dumps([newer_blocking])
        return json.dumps([older, newer_blocking])

    gh = src.GitHubSource({"discovery": {"source": "github", "order": "created",
                                         "blocking_priority_override": True}}, run=run)
    assert gh.next_pending() == "3"          # strict oldest-first, blocking status ignored


def test_github_blocking_priority_override_query_requires_both_labels_not_just_blocking():
    """#1352 review finding (notable): `_fetch_pending`'s base query always includes `--label
    sdlc:goal`; the override's extra `--label sdlc:blocking` must AND with it (gh's repeated-flag
    semantics), never OR. An issue carrying ONLY `sdlc:blocking` (e.g. a tracking/epic issue that
    isn't itself a pickable goal) must never surface as "the next goal to work" just because it
    matches the blocking label alone. This fake implements REAL AND semantics (every requested
    label must be present), unlike the other override tests' simpler canned-response fakes, so it
    would actually catch a regression that dropped `goal_label` from the override's own query."""
    src = _mod("sources")
    non_goal_blocking = {"number": 50, "labels": [{"name": "sdlc:blocking"}]}   # no sdlc:goal
    real_goal = {"number": 9, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P2"}]}

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        labels = _rest_labels(a)
        matches = []
        for issue in (non_goal_blocking, real_goal):
            names = {l["name"] for l in issue["labels"]}
            if all(l in names for l in labels):
                matches.append(issue)
        return json.dumps(matches)

    gh = src.GitHubSource({"discovery": {"source": "github", "blocking_priority_override": True}}, run=run)
    assert gh.next_pending() == "9"          # the non-goal blocking issue must never be offered


def test_github_blocking_priority_override_still_excludes_a_parked_blocking_issue():
    """#1352 review finding (notable): the override's own query reuses `_fetch_pending`
    UNCHANGED, so it must still honor the existing parked/in-progress/blocked exclusion filter --
    a blocking-labelled issue that is ALSO `sdlc:parked` must never win the top pick just because
    it matches the override's label filter."""
    src = _mod("sources")
    parked_but_blocking = {"number": 50, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:blocking"},
                                                     {"name": "sdlc:parked"}]}
    real_goal = {"number": 9, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P2"}]}
    run = _recording_runner({"list": json.dumps([parked_but_blocking, real_goal])})
    gh = src.GitHubSource({"discovery": {"source": "github", "blocking_priority_override": True}}, run=run)
    assert gh.next_pending() == "9"


def test_github_blocking_priority_override_ties_among_blocking_issues_resolve_by_priority():
    """The override only decides the FIRST split; ordering AMONG multiple simultaneously-blocking
    issues is unaffected -- still priority, then bug, then oldest-first."""
    src = _mod("sources")
    low = {"number": 9, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P2"}, {"name": "sdlc:blocking"}]}
    high = {"number": 3, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P0"}, {"name": "sdlc:blocking"}]}

    def run(a):
        verb = _issues_verb(a)
        if verb != "list":
            return ""
        return json.dumps([low, high])

    gh = src.GitHubSource({"discovery": {"source": "github", "blocking_priority_override": True}}, run=run)
    assert gh.next_pending() == "3"


def test_get_source_forwards_sdlc_dir_into_github_source():
    """get_source(sdlc_dir, config) already receives sdlc_dir -- it was just never forwarded to
    GitHubSource (LocalSource is the only one that used it). Local mode's own use of sdlc_dir is
    unaffected; this only adds a previously-dropped forward for the github branch."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        gh = src.get_source(d, {"discovery": {"source": "github"}})
        assert type(gh).__name__ == "GitHubSource"
        assert gh.sdlc_dir == d


def test_get_source_local_mode_unaffected_by_the_sdlc_dir_forward():
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        goals = pathlib.Path(d) / "goals"; goals.mkdir()
        local = src.get_source(d, {})
        assert type(local).__name__ == "LocalSource"
        assert local.sdlc_dir == d


# --- feature_rank (#2262): D-3's tie-break inserted into _pick_key, `.sdlc/design/2253.md` -------
# `(priority_rank, feature_rank, not_a_bug, number)` -- feature_rank resolves through
# `discovery.priority_rank`'s existing UNPRIORITISED sentinel and is gated OFF by default
# (`discovery.feature_priority`), so a repo that never turns it on sorts byte-identically to
# before this term existed (D-2: consulted at EVERY tier once on, no `cut`).

def _fissue(number, body="", *labels):
    """Like `_pissue` above, plus a `body` -- `feature_rank` resolves membership from the
    DECLARATION (`features.read`), never the label alone, exactly as `_issue_in_feature` already
    does and for the same reason: the `feature:<name>` label is only attached AT PICK."""
    return {"number": number, "body": body,
            "labels": [{"name": "sdlc:goal"}] + [{"name": l} for l in labels]}


def _registry_with(d, **priorities):
    """Write one unit per `name=priority` kwarg into `d`'s `.sdlc/features/` registry (#2261's
    `priority` field), e.g. `_registry_with(d, alpha="P0", beta="P2", gamma=None)` -- `gamma` is a
    real, registered unit that simply carries no priority."""
    fr = _mod("feature_registry")
    for name, priority in priorities.items():
        fr.write_unit(fr.registry_dir(d), name, {"priority": priority})


def test_feature_priority_defaults_to_on_when_unset():
    """#2284: the default flipped from off to on. A feature-priority tie-break can structurally
    never change anything unless a unit ALSO has a priority actually recorded on it
    (`define.py set-priority`, #2266) -- a unit with none resolves to the same `UNPRIORITISED`
    sentinel a feature-less issue already gets -- so turning it on by default costs an existing
    install nothing until the day someone calls `set-priority` for the first time."""
    src = _mod("sources")
    assert src._feature_priority({}) is True
    assert src._feature_priority(None) is True
    assert src._feature_priority({"discovery": {}}) is True
    assert src._feature_priority({"discovery": {"feature_priority": {}}}) is True


def test_feature_priority_reads_the_configured_enabled_flag():
    src = _mod("sources")
    assert src._feature_priority({"discovery": {"feature_priority": {"enabled": True}}}) is True
    assert src._feature_priority({"discovery": {"feature_priority": {"enabled": False}}}) is False


def test_feature_priority_only_the_literal_false_disables_it():
    """#2284's inverted defensive shape, mirroring `_blocking_priority_override`'s own #1394
    precedent exactly: only the literal boolean `False` turns it OFF now -- a typo'd string, a
    number, a bare (non-dict) value, or `None` must never silently DISABLE a tie-break nobody asked
    to turn off; each lands on the (now-on) default rather than raising or going quiet."""
    src = _mod("sources")
    assert src._feature_priority({"discovery": {"feature_priority": {"enabled": "false"}}}) is True
    assert src._feature_priority({"discovery": {"feature_priority": {"enabled": 0}}}) is True
    assert src._feature_priority({"discovery": {"feature_priority": True}}) is True            # not a dict
    assert src._feature_priority({"discovery": {"feature_priority": "on"}}) is True            # not a dict
    assert src._feature_priority({"discovery": {"feature_priority": None}}) is True
    # the one value that DOES disable it:
    assert src._feature_priority({"discovery": {"feature_priority": {"enabled": False}}}) is False


# --------------------------------------------------------------------- #2263: no-dangling-goal config

def test_no_dangling_goal_enabled_defaults_to_off_when_unset():
    src = _mod("sources")
    assert src._no_dangling_goal_enabled({}) is False
    assert src._no_dangling_goal_enabled(None) is False
    assert src._no_dangling_goal_enabled({"discovery": {}}) is False
    assert src._no_dangling_goal_enabled({"discovery": {"no_dangling_goal": {}}}) is False


def test_no_dangling_goal_enabled_reads_the_configured_flag():
    src = _mod("sources")
    assert src._no_dangling_goal_enabled(
        {"discovery": {"no_dangling_goal": {"enabled": True}}}) is True
    assert src._no_dangling_goal_enabled(
        {"discovery": {"no_dangling_goal": {"enabled": False}}}) is False


def test_no_dangling_goal_enabled_falls_back_to_off_on_a_malformed_value():
    """Same defensive shape as `_feature_priority`/`_blocker_promotion`: only the literal `True`
    engages it -- a typo'd string, a number, a bare (non-dict) value, or `None` must never silently
    set aside (or re-attribute) a goal nobody asked to touch."""
    src = _mod("sources")
    assert src._no_dangling_goal_enabled(
        {"discovery": {"no_dangling_goal": {"enabled": "true"}}}) is False
    assert src._no_dangling_goal_enabled(
        {"discovery": {"no_dangling_goal": {"enabled": 1}}}) is False
    assert src._no_dangling_goal_enabled({"discovery": {"no_dangling_goal": True}}) is False
    assert src._no_dangling_goal_enabled({"discovery": {"no_dangling_goal": "on"}}) is False
    assert src._no_dangling_goal_enabled({"discovery": {"no_dangling_goal": None}}) is False


def test_no_dangling_goal_core_defaults_to_none_when_unset():
    src = _mod("sources")
    assert src._no_dangling_goal_core({}) is None
    assert src._no_dangling_goal_core(None) is None
    assert src._no_dangling_goal_core({"discovery": {"no_dangling_goal": {}}}) is None
    # the scaffolded config template's own default ("") reads the same as unset
    assert src._no_dangling_goal_core({"discovery": {"no_dangling_goal": {"core": ""}}}) is None


def test_no_dangling_goal_core_reads_the_configured_name():
    src = _mod("sources")
    assert src._no_dangling_goal_core(
        {"discovery": {"no_dangling_goal": {"core": "core"}}}) == "core"
    # stripped, so a hand-edited config with trailing whitespace still selects attribution mode
    assert src._no_dangling_goal_core(
        {"discovery": {"no_dangling_goal": {"core": "  core  "}}}) == "core"


def test_no_dangling_goal_core_falls_back_to_none_on_a_malformed_value():
    """A malformed `core` must fail towards the LOUDER of the two behaviours (set-aside, a human is
    asked) -- never towards silently attributing real work to a name nobody typed correctly."""
    src = _mod("sources")
    assert src._no_dangling_goal_core({"discovery": {"no_dangling_goal": {"core": "   "}}}) is None
    assert src._no_dangling_goal_core({"discovery": {"no_dangling_goal": {"core": 1}}}) is None
    assert src._no_dangling_goal_core({"discovery": {"no_dangling_goal": {"core": True}}}) is None
    assert src._no_dangling_goal_core({"discovery": {"no_dangling_goal": None}}) is None
    assert src._no_dangling_goal_core({"discovery": {"no_dangling_goal": "core"}}) is None  # not a dict


def test_github_source_resolves_both_no_dangling_goal_attributes_once():
    """Mirrors `feature_priority_enabled`'s own resolve-once contract: `GitHubSource.__init__`
    stores both, so `feature_labels._handle_no_unit_at_pick` reads plain attributes rather than
    re-resolving config on every pick."""
    src = _mod("sources")
    cfg = {"discovery": {"source": "github", "github": {"repo": "o/r"},
                         "no_dangling_goal": {"enabled": True, "core": "core"}}}
    gh = src.GitHubSource(cfg, run=lambda a: "")
    assert gh.no_dangling_goal_enabled is True
    assert gh.no_dangling_goal_core == "core"
    off = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}},
                           run=lambda a: "")
    assert off.no_dangling_goal_enabled is False
    assert off.no_dangling_goal_core is None


# ------------------------------------------------------------- #2380: live-judge config (nested)

def test_no_dangling_goal_live_judge_enabled_defaults_to_off_when_unset():
    src = _mod("sources")
    assert src._no_dangling_goal_live_judge_enabled({}) is False
    assert src._no_dangling_goal_live_judge_enabled(None) is False
    assert src._no_dangling_goal_live_judge_enabled(
        {"discovery": {"no_dangling_goal": {"enabled": True}}}) is False
    assert src._no_dangling_goal_live_judge_enabled(
        {"discovery": {"no_dangling_goal": {"live_judge": {}}}}) is False


def test_no_dangling_goal_live_judge_enabled_reads_the_configured_flag():
    src = _mod("sources")
    assert src._no_dangling_goal_live_judge_enabled(
        {"discovery": {"no_dangling_goal": {"live_judge": {"enabled": True}}}}) is True
    assert src._no_dangling_goal_live_judge_enabled(
        {"discovery": {"no_dangling_goal": {"live_judge": {"enabled": False}}}}) is False


def test_no_dangling_goal_live_judge_enabled_falls_back_to_off_on_a_malformed_value():
    """Same defensive shape as `_no_dangling_goal_enabled` itself, doubly important here: only the
    literal `True` may ever start spending real money nobody asked for."""
    src = _mod("sources")
    assert src._no_dangling_goal_live_judge_enabled(
        {"discovery": {"no_dangling_goal": {"live_judge": {"enabled": "true"}}}}) is False
    assert src._no_dangling_goal_live_judge_enabled(
        {"discovery": {"no_dangling_goal": {"live_judge": {"enabled": 1}}}}) is False
    assert src._no_dangling_goal_live_judge_enabled(
        {"discovery": {"no_dangling_goal": {"live_judge": True}}}) is False
    assert src._no_dangling_goal_live_judge_enabled(
        {"discovery": {"no_dangling_goal": {"live_judge": None}}}) is False


def test_no_dangling_goal_live_judge_rounds_defaults_to_three():
    src = _mod("sources")
    assert src._no_dangling_goal_live_judge_rounds({}) == 3
    assert src._no_dangling_goal_live_judge_rounds(
        {"discovery": {"no_dangling_goal": {"live_judge": {}}}}) == 3
    assert src._no_dangling_goal_live_judge_rounds(
        {"discovery": {"no_dangling_goal": {"live_judge": {"rounds": 0}}}}) == 3
    assert src._no_dangling_goal_live_judge_rounds(
        {"discovery": {"no_dangling_goal": {"live_judge": {"rounds": -1}}}}) == 3
    assert src._no_dangling_goal_live_judge_rounds(
        {"discovery": {"no_dangling_goal": {"live_judge": {"rounds": "many"}}}}) == 3


def test_no_dangling_goal_live_judge_rounds_of_one_falls_back_to_three():
    """#2428, a real bug found live and fixed here: `rounds: 1` used to pass the old `value > 0`
    guard verbatim, even though `feature_judge._majority`'s own `top_count < 2` gate can NEVER be
    satisfied by a single round -- every call would guarantee an abstain while still spending real
    money on the `ask_claude` subprocess. `rounds: 1` must fall back to the safe default exactly
    like `rounds: 0`/a negative value already did."""
    src = _mod("sources")
    assert src._no_dangling_goal_live_judge_rounds(
        {"discovery": {"no_dangling_goal": {"live_judge": {"rounds": 1}}}}) == 3


def test_no_dangling_goal_live_judge_rounds_reads_the_configured_value():
    src = _mod("sources")
    assert src._no_dangling_goal_live_judge_rounds(
        {"discovery": {"no_dangling_goal": {"live_judge": {"rounds": 5}}}}) == 5
    # #2428: 2 is the lowest value that can ever reach `_majority`'s own `top_count < 2` threshold
    # (both rounds agreeing), so it is accepted verbatim rather than folded into the 1/0 rejection.
    assert src._no_dangling_goal_live_judge_rounds(
        {"discovery": {"no_dangling_goal": {"live_judge": {"rounds": 2}}}}) == 2


def test_no_dangling_goal_live_judge_spend_ceiling_defaults_to_five_dollars_per_day():
    src = _mod("sources")
    assert src._no_dangling_goal_live_judge_spend_ceiling_usd_per_day({}) == 5.0
    assert src._no_dangling_goal_live_judge_spend_ceiling_usd_per_day(
        {"discovery": {"no_dangling_goal": {"live_judge": {}}}}) == 5.0
    assert src._no_dangling_goal_live_judge_spend_ceiling_usd_per_day(
        {"discovery": {"no_dangling_goal": {
            "live_judge": {"spend_ceiling_usd_per_day": 0}}}}) == 5.0
    assert src._no_dangling_goal_live_judge_spend_ceiling_usd_per_day(
        {"discovery": {"no_dangling_goal": {
            "live_judge": {"spend_ceiling_usd_per_day": "lots"}}}}) == 5.0


def test_no_dangling_goal_live_judge_spend_ceiling_reads_the_configured_value():
    src = _mod("sources")
    assert src._no_dangling_goal_live_judge_spend_ceiling_usd_per_day(
        {"discovery": {"no_dangling_goal": {
            "live_judge": {"spend_ceiling_usd_per_day": 12.5}}}}) == 12.5


def test_github_source_resolves_all_three_live_judge_attributes_once():
    """Mirrors `test_github_source_resolves_both_no_dangling_goal_attributes_once` -- resolved once
    in `GitHubSource.__init__`, so `feature_judge.live_judge` reads plain attributes rather than
    re-parsing config itself (per #2380's own instructions: `feature_judge.py` never reads
    `discovery.*` directly)."""
    src = _mod("sources")
    cfg = {"discovery": {"source": "github", "github": {"repo": "o/r"},
                         "no_dangling_goal": {"enabled": True, "core": "core", "live_judge": {
                             "enabled": True, "rounds": 5, "spend_ceiling_usd_per_day": 12.5}}}}
    gh = src.GitHubSource(cfg, run=lambda a: "")
    assert gh.no_dangling_goal_live_judge_enabled is True
    assert gh.no_dangling_goal_live_judge_rounds == 5
    assert gh.no_dangling_goal_live_judge_spend_ceiling_usd_per_day == 12.5
    off = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}},
                           run=lambda a: "")
    assert off.no_dangling_goal_live_judge_enabled is False
    assert off.no_dangling_goal_live_judge_rounds == 3
    assert off.no_dangling_goal_live_judge_spend_ceiling_usd_per_day == 5.0


def test_github_feature_priority_engages_by_default_when_unset():
    """#2284: THE NEW CONTROL TEST -- the default flipped to on. Two issues at the SAME priority
    tier, from two DIFFERENT units whose registry entries carry DIFFERENT priorities -- with
    `discovery.feature_priority` genuinely UNSET (no key at all, not even an explicit `true`), the
    higher-priority unit's issue must still win the tie, proving the default itself engages the
    term rather than merely being documented to.

    Run for real, not just asserted (AGENTS.md, "run the control"): temporarily hard-coding
    `_feature_priority` to `return False` unconditionally turned this test red (`'3' == '9'`
    failed, got `'3'`) before being reverted -- the mirror image of #2262's own original dance,
    now proving the OPPOSITE default."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0", beta="P2")
        issues = [_fissue(9, "Feature: alpha\n", "priority:P1"),
                  _fissue(3, "Feature: beta\n", "priority:P1")]
        run = _recording_runner({"list": json.dumps(issues)})
        gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run, sdlc_dir=d)
        assert gh.next_pending() == "9"          # alpha (P0) beats beta (P2) with NO config at all


def test_github_feature_priority_off_when_explicitly_disabled_is_byte_identical_to_before_2262():
    """THE CONTROL TEST for the escape hatch (#2284; #2262's original claim, now conditional on an
    EXPLICIT `false` rather than on being merely unset). Same disambiguating pair as above, but
    `discovery.feature_priority.enabled` is explicitly `false` -- the pick must be exactly the
    pre-#2262 key, (priority_rank, not_a_bug, number): plain oldest-first within the tier,
    unaffected by either unit's registered priority.

    This is the test this suite's own history proves is not decorative (AGENTS.md, "run the
    control"): hard-coding `_feature_rank` to always compute a real rank regardless of
    `self.feature_priority_enabled` turns this red (asserting the feature-engaged order instead of
    the oldest-first one asserted here) -- restoring the config gate turns it green again."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0", beta="P2")
        issues = [_fissue(9, "Feature: alpha\n", "priority:P1"),
                  _fissue(3, "Feature: beta\n", "priority:P1")]
        run = _recording_runner({"list": json.dumps(issues)})
        cfg = {"discovery": {"source": "github", "feature_priority": {"enabled": False}}}
        gh = src.GitHubSource(cfg, run=run, sdlc_dir=d)
        assert gh.next_pending() == "3"          # oldest-first: alpha's P0 has NO effect when off


def test_github_feature_rank_breaks_a_same_tier_tie_by_unit_priority():
    """feature_rank engaged (config ON): two issues in the SAME priority tier, from different
    units carrying different registered priorities -- the higher-priority unit's issue sorts
    first, even though it carries the LARGER issue number (so this cannot be number tie-break)."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0", beta="P2")
        issues = [_fissue(9, "Feature: alpha\n", "priority:P1"),
                  _fissue(3, "Feature: beta\n", "priority:P1")]
        run = _recording_runner({"list": json.dumps(issues)})
        cfg = {"discovery": {"source": "github", "feature_priority": {"enabled": True}}}
        gh = src.GitHubSource(cfg, run=run, sdlc_dir=d)
        assert gh.next_pending() == "9"          # alpha (P0) beats beta (P2) despite the number


def test_github_feature_rank_is_consulted_at_every_tier_not_just_the_top():
    """D-2: no cut, no tier gating -- two P2 issues (deliberately NOT P0) from units with
    different registered priorities still get broken correctly, proving the term isn't
    special-cased to the top tier alone."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0", beta="P1")
        issues = [_fissue(9, "Feature: alpha\n", "priority:P2"),
                  _fissue(3, "Feature: beta\n", "priority:P2")]
        run = _recording_runner({"list": json.dumps(issues)})
        cfg = {"discovery": {"source": "github", "feature_priority": {"enabled": True}}}
        gh = src.GitHubSource(cfg, run=run, sdlc_dir=d)
        assert gh.next_pending() == "9"


def test_github_feature_rank_no_unit_declared_sorts_as_unprioritised_even_when_enabled():
    """An issue declaring no unit at all is unaffected by the term -- it ranks UNPRIORITISED, the
    same sentinel `discovery.priority_rank` already uses for an issue's own unrecognised
    priority, so it sorts LAST among an otherwise-tied tier even with the config ON."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0")
        issues = [_fissue(9, "Feature: alpha\n", "priority:P1"),
                  _fissue(3, "", "priority:P1")]                      # no `Feature:` line at all
        run = _recording_runner({"list": json.dumps(issues)})
        cfg = {"discovery": {"source": "github", "feature_priority": {"enabled": True}}}
        gh = src.GitHubSource(cfg, run=run, sdlc_dir=d)
        assert gh.next_pending() == "9"          # alpha's real priority beats the sentinel


def test_github_feature_rank_a_unit_with_no_registered_priority_behaves_like_no_unit():
    """A unit that exists in the registry but carries no `priority` field collapses to the SAME
    sentinel as declaring no unit at all -- not a crash, not a fabricated rank (mirrors
    `feature_registry.normalise_entry`'s own "absent is absent" rule for this field)."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0", gamma=None)          # gamma is real but unprioritised
        issues = [_fissue(9, "Feature: alpha\n", "priority:P1"),
                  _fissue(3, "Feature: gamma\n", "priority:P1")]
        run = _recording_runner({"list": json.dumps(issues)})
        cfg = {"discovery": {"source": "github", "feature_priority": {"enabled": True}}}
        gh = src.GitHubSource(cfg, run=run, sdlc_dir=d)
        assert gh.next_pending() == "9"


def test_github_feature_rank_never_overrides_the_issues_own_priority_tier():
    """D-3: a genuine tie-break, never a replacement for issue priority -- a P0 issue in an
    unprioritised (or no) unit still beats a P1 issue in the highest-priority unit. Lexicographic
    tuple comparison gives this for free; asserted directly so a future weighted-sum rewrite (the
    other shape D-3's own design explicitly rejected) would fail it."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0")
        issues = [_fissue(9, "Feature: alpha\n", "priority:P1"),   # top unit, but tier P1
                  _fissue(3, "", "priority:P0")]                    # no unit, but tier P0
        run = _recording_runner({"list": json.dumps(issues)})
        cfg = {"discovery": {"source": "github", "feature_priority": {"enabled": True}}}
        gh = src.GitHubSource(cfg, run=run, sdlc_dir=d)
        assert gh.next_pending() == "3"          # the P0 issue wins regardless of unit priority


def test_github_feature_rank_never_engages_under_order_created():
    """`order: 'created'` is strict, priority-blind FIFO -- `_pick_key`'s own `order == "created"`
    branch already collapses to a single UNPRIORITISED bucket. feature_rank is a priority-tier
    concept exactly like `blocking_priority_override` (see
    `test_github_blocking_priority_override_never_engages_under_order_created` immediately
    above) and must never engage here either, config on or off."""
    src = _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        _registry_with(d, alpha="P0")
        issues = [_fissue(9, "Feature: alpha\n", "priority:P1"), _fissue(3, "", "priority:P4")]
        run = _recording_runner({"list": json.dumps(issues)})
        cfg = {"discovery": {"source": "github", "order": "created",
                             "feature_priority": {"enabled": True}}}
        gh = src.GitHubSource(cfg, run=run, sdlc_dir=d)
        assert gh.next_pending() == "3"          # strict oldest-first, feature priority ignored


def test_github_feature_rank_degrades_gracefully_with_no_sdlc_dir():
    """`GitHubSource(config, run=...)` with no third argument is every pre-#900 construction site
    (triage.py, most of this file) -- feature_rank must degrade to UNPRIORITISED rather than
    raising when there is no `.sdlc` to read a registry from at all, even with the config on, and
    even when the issue payload carries no `body` key whatsoever (a real REST shape, not just an
    empty string)."""
    src = _mod("sources")
    issues = [_pissue(9, "priority:P1"), _pissue(3, "priority:P1")]     # no "body" key at all
    run = _recording_runner({"list": json.dumps(issues)})
    cfg = {"discovery": {"source": "github", "feature_priority": {"enabled": True}}}
    gh = src.GitHubSource(cfg, run=run)              # sdlc_dir defaults to None
    assert gh.next_pending() == "3"                  # falls back to plain oldest-first


# --- #1391 step 1: the atomic-ish label swap primitive -------------------------------------------
# Collapses a lifecycle transition from 3-5 independently-swallowed `gh issue edit` calls into ONE
# GraphQL mutation request carrying two ALIASED root mutations. ADD is aliased FIRST on purpose: a
# partial application then leaves BOTH lifecycle labels (visible, detectable, correctable) instead
# of NEITHER (zero-label limbo, which no label query in the system can ever return).


def _graphql_runner(labels=None, issue_node="I_node1", fail_times=0, fail_with="502 bad gateway",
                    errors_on_mutation=None):
    """Fake `gh` runner that answers the three GraphQL shapes `_swap_labels` issues.

    `fail_times`: make the MUTATION raise this many times before succeeding (transient-retry path).
    `errors_on_mutation`: return HTTP-200-with-an-`errors`-key instead (the partial-application
    shape GitHub really produces, which must NOT read as success)."""
    labels = labels if labels is not None else {"sdlc:goal": "L_goal", "sdlc:parked": "L_parked",
                                                "sdlc:in-progress": "L_inprog"}
    calls = []
    state = {"mutation_attempts": 0}

    def run(args):
        calls.append(list(args))
        doc = next((a[len("query="):] for a in args if str(a).startswith("query=")), "")
        if doc.startswith("mutation"):
            state["mutation_attempts"] += 1
            if state["mutation_attempts"] <= fail_times:
                raise RuntimeError(fail_with)
            if errors_on_mutation:
                return json.dumps({"data": None, "errors": errors_on_mutation})
            return json.dumps({"data": {"a": {"clientMutationId": None}}})
        if "label(name:" in doc:
            # #1393 review bug_001: ids are fetched BY NAME now, one aliased field per label. A
            # label the repo does not have answers as a NULL field -- which is exactly the signal
            # `_swap_labels` turns into "unknown label(s) on this repo".
            fields = {}
            # the name is a GraphQL string literal, so a quote inside it arrives ESCAPED
            for alias, lbl in re.findall(r'(a\d+): label\(name: "((?:[^"\\]|\\.)*)"\)', doc):
                lbl = lbl.replace('\\"', '"').replace("\\\\", "\\")
                fields[alias] = ({"id": labels[lbl], "name": lbl} if lbl in labels else None)
            return json.dumps({"data": {"repository": fields}})
        if "issue(number" in doc:
            return json.dumps({"data": {"repository": {"issue": {"id": issue_node}}}})
        return "{}"
    run.calls = calls
    run.state = state
    return run


def _swap_source(run, repo="acme/widget"):
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": repo}}}, run=run)
    gh._LABEL_SWAP_RETRY_BASE = 0
    return gh


def _mutation_docs(run):
    return [a[len("query="):] for c in run.calls for a in c
            if str(a).startswith("query=") and a[len("query="):].startswith("mutation")]


def test_swap_labels_issues_exactly_one_mutation_request():
    run = _graphql_runner()
    _swap_source(run)._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"])
    assert len(_mutation_docs(run)) == 1


def test_swap_labels_aliases_add_before_remove():
    """The safety lever: a partial application must leave BOTH labels, never neither."""
    run = _graphql_runner()
    _swap_source(run)._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"])
    doc = _mutation_docs(run)[0]
    assert doc.index("addLabelsToLabelable") < doc.index("removeLabelsFromLabelable")


def test_swap_labels_sends_only_the_named_labels_so_unrelated_ones_survive():
    """The whole reason this is GraphQL deltas and not a REST full-set replace: a set-replace forces
    read-modify-write with no compare-and-swap, and really does destroy a concurrent label."""
    run = _graphql_runner(labels={"sdlc:goal": "L_goal", "sdlc:parked": "L_parked",
                                  "priority:P4": "L_p4", "bug": "L_bug"})
    _swap_source(run)._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"])
    doc = _mutation_docs(run)[0]
    assert "L_parked" in doc and "L_goal" in doc
    assert "L_p4" not in doc and "L_bug" not in doc


def test_swap_labels_is_a_no_op_when_nothing_to_change():
    run = _graphql_runner()
    assert _swap_source(run)._swap_labels("42", add=[], remove=[]) is False
    assert _mutation_docs(run) == []


def test_swap_labels_caches_label_ids_across_swaps():
    run = _graphql_runner()
    gh = _swap_source(run)
    gh._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"])
    gh._swap_labels("43", add=["sdlc:parked"], remove=["sdlc:goal"])
    label_lookups = [c for c in run.calls if any("label(name:" in str(a) for a in c)]
    assert len(label_lookups) == 1


def test_label_ids_are_fetched_by_name_never_by_an_unpaginated_page():
    """#1393 cloud-review bug_001, and it was release-defeating.

    Ids used to come from `labels(first: 100)` with no pagination and no explicit ordering. GitHub
    caps a label page at 100 nodes, so on any repo carrying more than 100 labels -- ordinary on a
    mature project, which is exactly the adopter this kit targets -- the `sdlc:*` labels could
    simply not be in the window. `_swap_labels`' one-shot "created since the cache warmed" refresh
    could not rescue it either: it re-ran the IDENTICAL query. The swap then raised, and
    `_swap_labels_best_effort` swallowed the raise -- so EVERY lifecycle transition silently stopped
    writing labels, with reconcile.py's repair path (same primitive) equally dead.

    Fetching by name is constant in the number of labels actually written, so it cannot regress as a
    repo grows."""
    run = _graphql_runner()
    _swap_source(run)._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"])
    docs = [a for c in run.calls for a in c if str(a).startswith("query=")]
    assert not any("labels(first" in d for d in docs), "still fetching an unpaginated label page"
    lookup = next(d for d in docs if "label(name:" in d)
    assert 'label(name: "sdlc:parked")' in lookup and 'label(name: "sdlc:goal")' in lookup


def test_a_label_beyond_the_first_hundred_is_still_found():
    """The concrete regression: a repo with 150 labels where the sdlc:* ones sort last. The old
    page query would not have returned them; a by-name lookup does not care how many exist."""
    labels = {"area:%03d" % i: "L_a%d" % i for i in range(150)}
    labels.update({"sdlc:goal": "L_goal", "sdlc:parked": "L_parked"})
    run = _graphql_runner(labels=labels)
    assert _swap_source(run)._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"]) is True


def test_a_genuinely_absent_label_still_raises():
    """The by-name query answers a NULL field for a name the repo does not have, and that must stay
    distinguishable from "found" -- it is what tells a caller the swap cannot be performed."""
    run = _graphql_runner(labels={"sdlc:goal": "L_goal"})
    try:
        _swap_source(run)._swap_labels("42", add=["sdlc:nope"], remove=["sdlc:goal"])
    except RuntimeError as exc:
        assert "unknown label(s) on this repo: sdlc:nope" in str(exc)
    else:
        raise AssertionError("a missing label must raise, not silently skip")


def test_a_label_created_mid_run_is_picked_up_without_invalidating_the_cache():
    """Only labels that were FOUND are cached, so a name missing on one call is re-queried on the
    next -- which is what makes `_ensure_labels` creating a label mid-run self-healing, and is why
    the old invalidate-and-refetch dance could be deleted rather than kept."""
    labels = {"sdlc:goal": "L_goal"}
    run = _graphql_runner(labels=labels)
    gh = _swap_source(run)
    try:
        gh._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"])
    except RuntimeError:
        pass
    labels["sdlc:parked"] = "L_parked"                  # _ensure_labels creates it
    assert gh._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"]) is True


def test_a_label_name_with_a_quote_cannot_break_out_of_the_query():
    """Label names come from adopter config, so they are not this module's to trust."""
    run = _graphql_runner(labels={'sdlc:we"ird': "L_w"})
    _swap_source(run)._swap_labels("42", add=['sdlc:we"ird'])
    lookup = next(a for c in run.calls for a in c
                  if str(a).startswith("query=") and "label(name:" in str(a))
    assert r'label(name: "sdlc:we\"ird")' in lookup


def test_a_control_character_in_a_label_name_is_refused():
    run = _graphql_runner()
    try:
        _swap_source(run)._swap_labels("42", add=["sdlc:bad\nname"])
    except RuntimeError as exc:
        assert "control character" in str(exc)
    else:
        raise AssertionError("a control character in a label name must be refused")


def test_swap_labels_retries_a_transient_failure_then_succeeds():
    run = _graphql_runner(fail_times=2)
    assert _swap_source(run)._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"]) is True
    assert run.state["mutation_attempts"] == 3


def test_swap_labels_raises_after_exhausting_retries():
    """Unlike every label write in this class today, a failed lifecycle write must SURFACE."""
    import pytest as _pytest
    run = _graphql_runner(fail_times=99)
    with _pytest.raises(Exception):
        _swap_source(run)._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"])


def test_swap_labels_fails_fast_on_a_non_transient_error():
    import pytest as _pytest
    run = _graphql_runner(fail_times=99, fail_with="could not resolve to an Issue")
    with _pytest.raises(Exception):
        _swap_source(run)._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"])
    assert run.state["mutation_attempts"] == 1          # no retries burned on a real error


def test_swap_labels_treats_a_200_with_an_errors_key_as_failure():
    """GitHub returns HTTP 200 with `errors` populated on a PARTIAL application -- root mutations run
    serially and do not roll back, so this is the real partial-write shape and must not read green."""
    import pytest as _pytest
    run = _graphql_runner(errors_on_mutation=[{"path": ["r"], "message": "nope"}])
    with _pytest.raises(Exception):
        _swap_source(run)._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"])


def test_swap_labels_refetches_once_for_a_label_created_after_the_cache_warmed():
    run = _graphql_runner()
    gh = _swap_source(run)
    gh._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"])   # warms the cache
    run.calls.clear()
    gh._swap_labels("43", add=["sdlc:in-progress"], remove=[])          # already known -> no refetch
    assert not [c for c in run.calls if any("labels(first" in str(a) for a in c)]


def test_swap_labels_raises_on_a_label_this_repo_does_not_have():
    import pytest as _pytest
    run = _graphql_runner()
    with _pytest.raises(Exception):
        _swap_source(run)._swap_labels("42", add=["sdlc:nonexistent"], remove=[])


def test_swap_labels_works_with_repo_unset_the_shipped_default():
    """PLAN-REVIEW FIX (blocking): discovery.github.repo ships EMPTY and /sigma-setup supports
    leaving it unset -- gh then infers the repo from the working directory. The first version of
    the primitive raised outright, which would have hard-failed every transition on the shipped
    default config once step 2 routed them through it."""
    run = _graphql_runner()
    base = run
    def run2(args):
        if list(args)[:2] == ["repo", "view"]:
            base.calls.append(list(args))
            return json.dumps({"owner": {"login": "acme"}, "name": "widget"})
        return base(args)
    run2.calls = base.calls
    run2.state = base.state
    gh = _swap_source(run2, repo="")
    assert gh._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"]) is True
    assert gh._owner_name() == ("acme", "widget")


def test_swap_labels_infers_the_repo_only_once_per_process():
    run = _graphql_runner()
    base = run
    def run2(args):
        if list(args)[:2] == ["repo", "view"]:
            base.calls.append(list(args))
            return json.dumps({"owner": {"login": "acme"}, "name": "widget"})
        return base(args)
    run2.calls = base.calls
    run2.state = base.state
    gh = _swap_source(run2, repo="")
    gh._swap_labels("42", add=["sdlc:parked"], remove=["sdlc:goal"])
    gh._swap_labels("43", add=["sdlc:parked"], remove=["sdlc:goal"])
    assert len([c for c in run2.calls if c[:2] == ["repo", "view"]]) == 1


def test_swap_labels_raises_when_no_repo_can_be_identified_at_all():
    import pytest as _pytest
    run = _graphql_runner()
    base = run
    def run2(args):
        if list(args)[:2] == ["repo", "view"]:
            raise RuntimeError("not a git repository")
        return base(args)
    run2.calls = base.calls
    run2.state = base.state
    with _pytest.raises(Exception):
        _swap_source(run2, repo="")._swap_labels("42", add=["sdlc:parked"], remove=[])


def test_swap_labels_refuses_to_inline_a_malformed_node_id():
    """Node ids are inlined into the GraphQL document, so a malformed one must never reach it."""
    import pytest as _pytest
    run = _graphql_runner(issue_node='I_bad" evil')
    with _pytest.raises(Exception):
        _swap_source(run)._swap_labels("42", add=["sdlc:parked"], remove=[])


def test_every_lifecycle_transition_routes_through_the_swap():
    """#1391 step 2 (this replaces step 1's 'nothing calls it yet' guard). All four label-writing
    transitions must go through the ONE primitive -- if a new `gh issue edit --add-label` sneaks
    back into any of them, the 2^n partial-write lattice comes back with it."""
    import pathlib as _pl, re as _re
    src_text = (_pl.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
                / "sources.py").read_text()
    body = src_text[src_text.index("class GitHubSource"):]
    for name in ("mark_in_progress", "complete", "mark_blocked", "_offboard"):
        seg = body[body.index("def %s(" % name):]
        seg = seg[:seg.index("\n    def ", 1)]
        assert "_swap_labels" in seg, f"{name} no longer routes through the swap primitive"
        assert "--add-label" not in seg, f"{name} regained a raw --add-label call"
        assert "--remove-label" not in seg, f"{name} regained a raw --remove-label call"


# --- #2392: mark_in_progress's claim-label write -- retry, then a LOUD, distinct escalation on ----
# genuine exhaustion. Real, live incident: two sessions on different machines picked the SAME issue
# within 45 seconds of each other, root-caused to exactly this write failing silently under a
# GraphQL secondary rate limit ("secondary rate" is already in `_TRANSIENT`) and the pick proceeding
# anyway. The fix is retry-with-backoff (a larger, dedicated budget -- `_CLAIM_LABEL_RETRIES`/
# `_CLAIM_LABEL_RETRY_BASE`, sized to clear GitHub's own "wait at least one minute" guidance for a
# secondary rate limit) plus a loud, distinct stderr escalation once that budget is genuinely spent.
# Explicitly NOT a change to the fail-open "a pick still proceeds regardless" contract -- that is
# the separate, bigger trade-off the issue itself declines to make; see test_loop.py's #2392 section
# for the direct regression test proving the pick still proceeds.


def _claim_source(run, repo="acme/widget"):
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": repo}}}, run=run)
    gh._CLAIM_LABEL_RETRY_BASE = 0     # no real sleeping in tests
    return gh


def test_mark_in_progress_retries_a_transient_claim_label_failure_then_succeeds():
    """Mocks the underlying gh call to fail twice (well within `_CLAIM_LABEL_RETRIES`) then
    succeed -- the write must land, `mark_in_progress` reports success, and nothing escalates."""
    run = _graphql_runner(fail_times=2, fail_with="secondary rate limit exceeded")
    gh = _claim_source(run)
    assert gh.mark_in_progress("42") is True
    assert run.state["mutation_attempts"] == 3


def test_mark_in_progress_retries_use_the_larger_claim_specific_budget():
    """The claim write gets `_CLAIM_LABEL_RETRIES` attempts (7 by default -- sized to clear
    GitHub's own "wait at least one minute" secondary-rate-limit guidance, see that constant's own
    comment in sources.py), not the smaller, generic `_LABEL_SWAP_RETRIES` (4) every other
    lifecycle transition (`complete`, `mark_blocked`, `_offboard`/park) still uses, untouched."""
    run = _graphql_runner(fail_times=99, fail_with="secondary rate limit exceeded")
    gh = _claim_source(run)
    assert gh._CLAIM_LABEL_RETRIES == 7
    assert gh._CLAIM_LABEL_RETRIES != gh._LABEL_SWAP_RETRIES
    assert gh.mark_in_progress("42") is False
    assert run.state["mutation_attempts"] == gh._CLAIM_LABEL_RETRIES


def test_mark_in_progress_escalates_loudly_on_genuine_exhaustion(capsys):
    """Mocks a PERMANENT failure -- every attempt fails on a GraphQL secondary rate limit, the
    exact shape the live incident hit. Once `_CLAIM_LABEL_RETRIES` is genuinely exhausted,
    `mark_in_progress` must emit a second, DISTINCT, greppable stderr line carrying
    `_CLAIM_LABEL_FAILURE_MARKER` -- on top of (not instead of) the generic
    `_swap_labels_best_effort` line every other failed lifecycle write already gets."""
    run = _graphql_runner(fail_times=99, fail_with="secondary rate limit exceeded")
    gh = _claim_source(run)
    assert gh.mark_in_progress("42") is False
    err = capsys.readouterr().err
    assert gh._CLAIM_LABEL_FAILURE_MARKER in err
    assert "#42" in err
    assert "double-pick" in err
    assert "claim label write failed for #42" in err   # the generic line, still present too


def test_mark_in_progress_does_not_escalate_on_a_clean_success(capsys):
    """The escalation is reserved for a genuine, exhausted failure -- a plain, first-attempt
    success writes neither the generic nor the distinct stderr line."""
    run = _graphql_runner()
    gh = _claim_source(run)
    assert gh.mark_in_progress("42") is True
    assert capsys.readouterr().err == ""


def test_mark_in_progress_does_not_escalate_after_a_successful_retry(capsys):
    """Same guarantee, the more interesting case: retries that eventually SUCCEED must not trip the
    exhaustion escalation either -- only a write that never lands after every attempt does."""
    run = _graphql_runner(fail_times=2, fail_with="secondary rate limit exceeded")
    gh = _claim_source(run)
    assert gh.mark_in_progress("42") is True
    assert gh._CLAIM_LABEL_FAILURE_MARKER not in capsys.readouterr().err


def test_mark_in_progress_survives_genuine_exhaustion_without_raising():
    """#2392's explicit scope decision, as a direct regression test: retries + loud escalation, but
    NOT a hard failure. `mark_in_progress` must never raise, whatever happens to the underlying
    write -- see test_loop.py's #2392 section for the caller-level proof that the pick itself still
    proceeds (option (c), "genuinely block the pick", was deliberately NOT implemented)."""
    run = _graphql_runner(fail_times=99, fail_with="secondary rate limit exceeded")
    gh = _claim_source(run)
    gh.mark_in_progress("42")   # must not raise


def test_mark_in_progress_still_fails_fast_on_a_non_transient_claim_error():
    """The larger claim-specific budget must not turn a REAL, non-transient error (a genuinely
    unknown label, a malformed node id) into a long retry loop -- `_is_transient` still gates every
    attempt exactly as it does for the generic swap."""
    run = _graphql_runner(fail_times=99, fail_with="could not resolve to an Issue")
    gh = _claim_source(run)
    assert gh.mark_in_progress("42") is False
    assert run.state["mutation_attempts"] == 1          # no retries burned on a real error


# --- #1391 step 3a: the human-promotion label must actually EXIST ---------------------------------
# `proposed_label` was the one primary lifecycle label `_ensure_labels` never created, despite
# doctor.py (#1354) treating it as primary and SKILL.md's whole AI-filed-awaiting-human-promotion
# gate depending on it. On a fresh repo the label did not exist until someone made it by hand.


def test_ensure_labels_does_not_create_the_retired_proposed_label():
    src = _mod("sources")
    run = _recording_runner()
    src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "acme/widget"}}},
                     run=run)._ensure_labels()
    created = [c[2] for c in run.calls if c[:2] == ["label", "create"]]
    assert "sdlc:needs-confirmation" not in created      # #1140: retired, left inert where it exists


def test_ensure_labels_creates_every_primary_lifecycle_label():
    src = _mod("sources")
    run = _recording_runner()
    src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "acme/widget"}}},
                     run=run)._ensure_labels()
    created = {c[2] for c in run.calls if c[:2] == ["label", "create"]}
    assert {"sdlc:goal", "sdlc:in-progress", "sdlc:parked", "sdlc:blocked"} <= created


def test_proposed_label_defaults_to_needs_confirmation():
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=_recording_runner())
    assert gh.proposed_label == "sdlc:needs-confirmation"


# --------------------------------------------------------- #2263: sdlc:needs-unit registration surface

def test_needs_unit_label_defaults_to_sdlc_needs_unit():
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=_recording_runner())
    assert gh.needs_unit_label == "sdlc:needs-unit"


def test_needs_unit_label_is_configurable():
    src = _mod("sources")
    cfg = {"discovery": {"source": "github",
                         "github": {"needs_unit_label": "team:no-unit"}}}
    gh = src.GitHubSource(cfg, run=_recording_runner())
    assert gh.needs_unit_label == "team:no-unit"


def test_needs_unit_label_shares_needs_label_labels_own_colour():
    """The issue's own instruction: "matching colour/ensure-tuple conventions"."""
    src = _mod("sources")
    colors = dict(src.GitHubSource._LABEL_COLORS)
    assert "needs_unit_label" in colors
    assert colors["needs_unit_label"] == colors["needs_label_label"]


def test_ensure_labels_creates_needs_unit_at_the_pinned_colour():
    src = _mod("sources")
    run = _recording_runner()
    src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "acme/widget"}}},
                     run=run)._ensure_labels()
    creates = {c[2]: c for c in run.calls if c[:2] == ["label", "create"]}
    assert "sdlc:needs-unit" in creates
    call = creates["sdlc:needs-unit"]
    assert call[call.index("--color") + 1] == "fbca04"


def test_needs_unit_label_is_in_the_overlay_and_not_eligible_sets():
    """THE CHOKEPOINT #2263's own issue body names -- registering here is what makes the pick path,
    `_card_is_eligible`, triage and status all see it at once."""
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}},
                          run=_recording_runner())
    assert "sdlc:needs-unit" in gh.overlay_labels()
    assert "sdlc:needs-unit" in gh.not_eligible_labels()
    assert gh._card_is_eligible(42, {"sdlc:goal", "sdlc:needs-unit"}) is False
    assert gh._card_is_eligible(43, {"sdlc:goal"}) is True


def test_mark_needs_unit_adds_the_overlay_and_removes_in_progress():
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    calls = []
    gh._swap_labels_best_effort = lambda goal, add=(), remove=(), what="": (
        calls.append((tuple(add), tuple(remove))) or True)
    gh._set_board_status = lambda goal, status: calls.append(("board", status))
    gh._ensure_labels = lambda: None
    assert gh.mark_needs_unit("42") is True
    assert (("sdlc:needs-unit",), ("sdlc:in-progress",)) in calls
    assert ("board", gh.col["blocked"]) in calls


def test_clear_needs_unit_removes_the_overlay_and_moves_to_ready():
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    calls = []
    gh._swap_labels_best_effort = lambda goal, add=(), remove=(), what="": (
        calls.append((tuple(add), tuple(remove))) or True)
    gh._set_board_status = lambda goal, status: calls.append(("board", status))
    assert gh.clear_needs_unit("42") is True
    assert ((), ("sdlc:needs-unit",)) in calls
    assert ("board", gh.col["ready"]) in calls


def test_the_board_never_claims_a_no_unit_transition_the_labels_did_not_make():
    """Mirrors `mark_needs_label`/`clear_needs_label`'s own load-bearing return-value contract: the
    board move follows the label write actually landing, in both directions."""
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    moves = []
    gh._set_board_status = lambda goal, status: moves.append(status)
    gh._swap_labels_best_effort = lambda *a, **k: False        # neither write lands
    gh._ensure_labels = lambda: None
    assert gh.mark_needs_unit("42") is False
    assert gh.clear_needs_unit("42") is False
    assert moves == [], moves


def test_list_needs_unit_queries_its_own_label():
    src = _mod("sources")
    run = _recording_runner({"list": json.dumps(
        [{"number": 42, "body": "no unit here\n", "labels": [{"name": "sdlc:needs-unit"}]}])})
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    out = gh.list_needs_unit()
    assert [i["number"] for i in out] == [42]
    listed = next(c for c in run.calls if _issues_verb(c) == "list")
    assert "sdlc:needs-unit" in _rest_labels(listed)


def test_list_needs_unit_is_fail_open_on_a_bad_response():
    src = _mod("sources")

    def run(args):
        if _issues_verb(args) == "list":
            raise RuntimeError("simulated gh failure")
        return ""

    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    assert gh.list_needs_unit() == []


def test_proposed_label_honours_the_ledger_handoff_override():
    """It lives under ledger.handoff.proposed_label -- the same place handoff.proposed_label(config)
    reads it -- NOT under discovery.github like the other label keys."""
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github"},
                           "ledger": {"handoff": {"proposed_label": "sdlc:needs-review"}}},
                          run=_recording_runner())
    assert gh.proposed_label == "sdlc:needs-review"


def test_proposed_label_survives_a_malformed_ledger_block():
    src = _mod("sources")
    for bad in ("enabled", None, 7, []):
        gh = src.GitHubSource({"discovery": {"source": "github"}, "ledger": bad},
                              run=_recording_runner())
        assert gh.proposed_label == "sdlc:needs-confirmation"


def test_proposed_label_survives_a_malformed_handoff_block():
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github"}, "ledger": {"handoff": "oops"}},
                          run=_recording_runner())
    assert gh.proposed_label == "sdlc:needs-confirmation"


def test_config_template_ships_the_ready_column_key():
    """`ready` names the single most consequential column: under the default queue_source "status"
    the Ready lane IS the pick queue. It was missing from the shipped template entirely, so an
    adopter whose board names that lane differently had no key to say so."""
    import pathlib as _pl, re as _re
    tmpl = (_pl.Path(__file__).resolve().parent.parent / "skills" / "sigma-init" / "templates"
            / "config.json.tmpl").read_text()
    cols = _re.search(r'"columns"\s*:\s*\{[^}]*\}', tmpl)
    assert cols, "columns block not found in the template"
    assert '"ready"' in cols.group(0)


# --- #1394: the blocker-chain flow is the DEFAULT -------------------------------------------------


def test_the_chain_flow_is_on_with_no_config_at_all():
    """#1394: the user's flow — blockers sort first, a blocked goal resumes when they close — is
    what a fresh install does. Opting OUT is the deliberate gesture now."""
    src = _mod("sources")
    assert src._auto_unpark({}) == "on"
    assert src._blocking_priority_override({}) is True


def test_each_half_can_be_turned_off_explicitly():
    src = _mod("sources")
    assert src._auto_unpark({"discovery": {"auto_unpark": {"mode": "off"}}}) == "off"
    assert src._blocking_priority_override(
        {"discovery": {"blocking_priority_override": False}}) is False


def test_a_config_typo_lands_on_the_default_rather_than_changing_behaviour_silently():
    """The defensive shape INVERTED with the default, and the property that mattered survives: the
    old rule was "only the literal True turns it on, so a typo can never activate a feature nobody
    asked for"; the mirror is "only the literal False turns it off". Either way a mistake leaves you
    the default, never a silent behaviour change."""
    src = _mod("sources")
    for bad in ("yes", "ON", 1, None, [], {}):
        assert src._auto_unpark({"discovery": {"auto_unpark": {"mode": bad}}}) == "on", bad
        assert src._blocking_priority_override(
            {"discovery": {"blocking_priority_override": bad}}) is True, bad


def test_the_shipped_template_states_both_defaults_explicitly():
    """A default nobody can see is a default nobody can turn off. The template ships the keys with
    their real values so the opt-out is discoverable without reading the source."""
    import json as _json
    tmpl = _json.loads((pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-init"
                        / "templates" / "config.json.tmpl").read_text())
    disc = tmpl["discovery"]
    assert disc["auto_unpark"]["mode"] == "on"
    assert disc["blocking_priority_override"] is True
    src = _mod("sources")
    assert src._auto_unpark(tmpl) == "on"                     # template and code agree
    assert src._blocking_priority_override(tmpl) is True


# --- #312: an empty OVERLAY lane is an answer, not a blip -----------------------------------------

def _blocking_empty_runner(calls, fail_blocking_times=0):
    state = {"fails": fail_blocking_times}

    def run(a):
        calls.append(list(a))
        if _issues_verb(a) != "list":
            return ""
        if "sdlc:blocking" in _rest_labels(a):
            if state["fails"]:
                state["fails"] -= 1
                raise RuntimeError("gh: HTTP 502 Bad Gateway")      # transient -> retried
            return "[]"
        return json.dumps([{"number": 5, "labels": [{"name": "sdlc:goal"}]}])
    return run


def test_312_an_empty_blocking_lane_is_read_once_and_never_slept_on(monkeypatch):
    """Before #312 every github `loop next` re-read an empty `sdlc:blocking` lane 3 times with
    1s + 2s backoff (~3s measured per pick). A successful read of a lane that legitimately has no
    members is an answer: one read, no sleep. The backlog's own empty-read retry (#447) is kept."""
    src = _mod("sources")
    slept = []
    monkeypatch.setattr(src.time, "sleep", lambda s: slept.append(s))
    calls = []
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=_blocking_empty_runner(calls))
    assert gh.next_pending() == "5"
    assert sum("sdlc:blocking" in _rest_labels(c) for c in calls) == 1, calls
    assert slept == [], slept


def test_312_a_failing_blocking_lane_read_is_still_retried(monkeypatch):
    """Empty-because-error is not empty-because-none: a transient failure keeps its retry."""
    src = _mod("sources")
    slept = []
    monkeypatch.setattr(src.time, "sleep", lambda s: slept.append(s))
    calls = []
    gh = src.GitHubSource({"discovery": {"source": "github"}},
                          run=_blocking_empty_runner(calls, fail_blocking_times=1))
    assert gh.next_pending() == "5"
    assert sum("sdlc:blocking" in _rest_labels(c) for c in calls) == 2, calls
    assert len(slept) == 1, slept


def test_312_the_backlog_itself_still_retries_an_empty_read(monkeypatch):
    """The #447 insurance on the PRIMARY backlog read is unchanged by the overlay carve-out."""
    src = _mod("sources")
    monkeypatch.setattr(src.time, "sleep", lambda s: None)
    run = _recording_runner({"list": "[]"})
    gh = src.GitHubSource({"discovery": {"source": "github", "blocking_priority_override": False}},
                          run=run)
    assert gh.next_pending() is None
    assert len(run.calls) == gh._BACKLOG_READ_RETRIES


# --- #895 slice 2a: the 7 issue reads go REST first; one `gh issue view` fallback per call ---------

def _rest_429_then_view(view_out):
    """REST issue reads answer a 429 (fallback-class); the ONE fallback `issue view` answers
    `view_out` in the old gh shape. Everything else returns ""."""
    calls = []

    def run(a):
        gql = _gql_swap(a, calls=calls)
        if gql is not None:
            return gql
        if gqlfake.is_issue_write(a):          # #895 slice 3a: REST writes answered by the shim
            return gqlfake.rest_write(a, calls=calls)
        calls.append(list(a))
        if gqlfake.rest_issue_target(a) is not None:
            exc = RuntimeError("gh api failed")
            exc.hint = "gh: API rate limit exceeded for user ID 1. (HTTP 429)"
            raise exc
        if a[:2] == ["issue", "view"]:
            return view_out
        return ""
    run.calls = calls
    return run


def _gh(run):
    return _mod("sources").GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)


def _views(run):
    return [c for c in run.calls if c[:2] == ["issue", "view"]]


def test_release_fallback_branch_reads_old_shape():
    run = _rest_429_then_view(json.dumps({"state": "CLOSED", "labels": []}))
    assert _gh(run).release("42", "x") is False
    assert _views(run) == [["issue", "view", "42", "--repo", "o/r", "--json", "state,labels"]]


def test_complete_fallback_branch_reads_old_shape():
    run = _rest_429_then_view(json.dumps({"state": "CLOSED"}))
    gh = _gh(run)
    gh._NOTE_RETRY_BASE = 0
    gh.complete("42")
    assert not any(len(c) > 1 and c[1] == "close" for c in run.calls)
    assert _views(run) == [["issue", "view", "42", "--repo", "o/r", "--json", "state"]]


def test_fetch_author_fallback_branch_reads_old_shape():
    run = _rest_429_then_view(json.dumps({"author": {"login": "app/sigma-bot"}}))
    assert _gh(run).fetch_author("42") == "app/sigma-bot"
    assert len(_views(run)) == 1


def test_fetch_author_rest_maps_bot_author():
    """#895 step 9 (measured once): a REST Bot `x[bot]` reads `app/x`, as gh shows it."""
    run = _recording_runner({"rest": {"author": {"login": "app/sigma-bot"}}})   # fake converts to x[bot]/Bot
    assert _gh(run).fetch_author("42") == "app/sigma-bot"
    assert not any(c[:2] == ["issue", "view"] for c in run.calls)


def test_fetch_body_labels_fallback_branch_reads_old_shape():
    run = _rest_429_then_view(json.dumps({"body": "B", "labels": [{"name": "feature:x"}]}))
    assert _gh(run).fetch_body_labels("42") == {"body": "B", "labels": [{"name": "feature:x"}]}
    assert len(_views(run)) == 1


def test_append_to_body_fallback_branch_reads_old_shape():
    run = _rest_429_then_view(json.dumps({"body": "Old"}))
    _gh(run).append_to_body("42", "M")
    edit = next(c for c in run.calls if c[:2] == ["issue", "edit"])
    assert edit[edit.index("--body") + 1] == "Old\n\nM\n"


@pytest.mark.parametrize("view_out", ["not json", "[]", "{}"])
def test_append_to_body_never_blanks_the_body_on_a_degraded_read(view_out):
    """The `["body"]` subscript is data-loss protection: a degraded fallback read raises BEFORE
    `issue edit`, so a malformed read can never replace a real body with only the marker."""
    run = _rest_429_then_view(view_out)
    with pytest.raises(KeyError):
        _gh(run).append_to_body("42", "M")
    assert not any(c[:2] == ["issue", "edit"] for c in run.calls)


def test_append_to_body_never_blanks_the_body_on_a_rest_payload_without_body():
    def run(a):
        run.calls.append(list(a))
        if gqlfake.rest_issue_target(a) is not None:
            return json.dumps({"number": 42, "state": "open", "comments": 0})     # no `body` key
        return ""
    run.calls = []
    with pytest.raises(Exception):
        _gh(run).append_to_body("42", "M")
    assert not any(c[:2] == ["issue", "edit"] for c in run.calls)


def test_fetch_comments_fallback_branch_reads_old_shape():
    payload = {"comments": [{"id": "IC_1", "author": {"login": "amy"}, "body": "b",
                             "createdAt": "2026-08-01T00:00:00Z", "authorAssociation": "OWNER"}]}
    run = _rest_429_then_view(json.dumps(payload))
    out = _mod("sources").fetch_comments({}, "5", run=run)
    assert out == [{"id": "IC_1", "author": "amy", "body": "b", "created_at": "2026-08-01T00:00:00Z",
                    "association": "OWNER"}]
    assert len(_views(run)) == 1


def test_fetch_comments_strict_reads_past_100():
    """#895 plan Revision 1: the strict reader reads EVERY comment page (paged by the issue's own
    count), so a marker in comment 120 is never truncated away."""
    comments = [{"id": "IC_%d" % i, "author": {"login": "amy"}, "body": "c%d" % i,
                 "createdAt": "2026-08-01T00:%02d:%02dZ" % (i // 60, i % 60), "authorAssociation": "OWNER"}
                for i in range(150)]
    run = _recording_runner({"rest": {"comments": comments, "labels": [{"name": "area:x"}]}})
    out = _gh(run).fetch_comments_strict("42")
    assert len(out["comments"]) == 150 and out["comments"][-1]["body"] == "c149"
    assert out["labels"] == [{"name": "area:x"}]
    assert not any(c[:2] == ["issue", "view"] for c in run.calls)


def test_fetch_comments_strict_raises_on_malformed_rest_page():
    """#895 plan Revision 1 item 2: a null REST comments page raises (no fallback, no `or []`)."""
    def run(a):
        run.calls.append(list(a))
        t = gqlfake.rest_issue_target(a)
        if t is not None:
            return "null" if t[1] else json.dumps({"number": 42, "state": "open", "body": "", "comments": 3})
        return ""
    run.calls = []
    with pytest.raises(Exception):
        _gh(run).fetch_comments_strict("42")
    assert not any(c[:2] == ["issue", "view"] for c in run.calls)


# ---------------------------------------------------------------- #895 slice 3a: GitHubSource WRITE wrappers

def _write_source(missing=(), fail=None, assignable=None, create_number=None):
    """A real GitHubSource over a recording runner that answers REST writes via `gqlfake.rest_write`.
    `calls` is every argv the runner saw; `legacy` the legacy-shaped `issue ...` records the shim makes."""
    src = _mod("sources")
    calls, legacy = [], []

    def run(args):
        calls.append(list(args))
        if fail is not None and fail(args):
            e = RuntimeError("gh: boom (HTTP 500)")
            e.hint = "gh: boom (HTTP 500)"
            raise e
        out = gqlfake.rest_write(args, calls=legacy, missing_labels=missing, assignable=assignable,
                                 create_number=create_number)
        assert out is not None, "unanswered argv (the shim must not make a write vacuous): %r" % (args,)
        return out

    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    return gh, calls, legacy


def _posts(calls):
    return [c for c in calls if "--method" in c and c[c.index("--method") + 1] in ("POST", "PATCH", "DELETE")]


def test_issue_write_wrappers_go_rest_with_the_sources_repo_and_record_legacy_calls():
    gh, calls, legacy = _write_source()
    gh._issue_comment("#7", "hi")
    gh._issue_edit_body(7, "new body")
    gh._issue_add_labels(7, ["area:x"])
    gh._issue_remove_label(7, "sdlc:in-progress")
    gh._issue_add_assignees(7, "alice")
    gh._issue_close(7, reason="completed")
    assert all(c[0] == "api" and c[1].startswith("repos/o/r/") for c in calls)
    assert legacy == [
        ["issue", "comment", "7", "--repo", "o/r", "--body", "hi"],
        ["issue", "edit", "7", "--repo", "o/r", "--body", "new body"],
        ["issue", "edit", "7", "--repo", "o/r", "--add-label", "area:x"],
        ["issue", "edit", "7", "--repo", "o/r", "--remove-label", "sdlc:in-progress"],
        ["issue", "edit", "7", "--repo", "o/r", "--add-assignee", "alice"],
        ["issue", "close", "7", "--repo", "o/r", "--reason", "completed"]]


def test_issue_create_returns_the_rest_dict_and_non_feature_labels_do_no_lookup():
    gh, calls, legacy = _write_source(create_number=42)
    out = gh._issue_create("T", "B", ["sdlc:goal", "priority:P1"])
    assert out["number"] == 42
    assert legacy == [["issue", "create", "--repo", "o/r", "--title", "T", "--body", "B", "--label", "sdlc:goal",
                       "--label", "priority:P1"]]
    assert len(calls) == 1 and "labels/" not in calls[0][1]          # non-feature -> no GET


def test_issue_create_refuses_absent_feature_label_with_zero_writes():
    gh, calls, legacy = _write_source(missing={"feature:x"})
    with pytest.raises(RuntimeError, match="refusing to create the label 'feature:x'"):
        gh._issue_create("T", "B", ["sdlc:goal", "feature:x"])
    assert _posts(calls) == [] and legacy == []
    assert calls == [["api", "repos/o/r/labels/feature%3Ax", "--method", "GET"]]


def test_issue_add_labels_refuses_absent_feature_label_with_zero_writes():
    """refinement d: layer 2 on the ADD-LABEL path (triage `add-label feature:x`), not only create."""
    gh, calls, legacy = _write_source(missing={"feature:x", "Feature:Voice"})
    with pytest.raises(RuntimeError, match="refusing to create the label"):
        gh._issue_add_labels(7, "feature:x")
    assert _posts(calls) == [] and legacy == []
    with pytest.raises(RuntimeError, match="refusing to create the label"):
        gh._issue_add_labels(7, ["area:x", "Feature:Voice"])        # case-insensitive, mixed list
    assert _posts(calls) == [] and legacy == []


def test_feature_label_lookup_failure_fails_closed():
    gh, calls, legacy = _write_source(fail=lambda a: a[1].startswith("repos/o/r/labels/"))
    for call in (lambda: gh._issue_create("T", "B", ["feature:x"]), lambda: gh._issue_add_labels(7, ["feature:x"])):
        with pytest.raises(RuntimeError, match="refusing to create the label.*could not verify"):
            call()
    assert _posts(calls) == [] and legacy == []


def test_present_feature_label_is_attached_after_one_lookup():
    gh, calls, legacy = _write_source()
    gh._issue_add_labels(7, "feature:x")
    assert calls[0] == ["api", "repos/o/r/labels/feature%3Ax", "--method", "GET"]
    assert len(_posts(calls)) == 1 and legacy == [["issue", "edit", "7", "--repo", "o/r", "--add-label", "feature:x"]]
    gh, calls, legacy = _write_source(create_number=5)
    gh._issue_create("T", "B", ["feature:x"])
    assert calls[0][1] == "repos/o/r/labels/feature%3Ax" and len(_posts(calls)) == 1


def test_assignee_dropped_by_github_raises_through_the_wrapper():
    gh, calls, legacy = _write_source(assignable={"bob"})
    with pytest.raises(Exception, match="did not assign @alice"):
        gh._issue_add_assignees(7, ["alice"])


# ---------------------------------------------------------------- #895 slice 3a: release / append_to_body on REST

def _rest_err(status, text):
    e = RuntimeError("gh api failed")
    e.hint = "gh: %s (HTTP %d)" % (text, status)
    return e


def test_release_label_404_is_a_noop_but_a_vanished_issue_still_surfaces_via_the_comment_404():
    """D4: remove_label swallows a structured 404 (label absent); the following comment 404 is NOT
    swallowed by gh_api, so the vanished issue lands in `release_warnings` (one audit-comment entry)."""
    src = _mod("sources")

    def run(a):
        legacy = gqlfake.legacy_of(a)
        if legacy and (legacy[0][1] == "comment" or "--remove-label" in legacy[0]):
            raise _rest_err(404, "Not Found")
        return gqlfake.rest_write(a) or ""

    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    assert gh.release("42", "x") is True
    (w,) = gh.release_warnings()
    assert w.startswith("audit comment failed")


def test_release_removes_the_label_and_comments_over_rest_not_gh_issue():
    run = _recording_runner()
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}}, run=run)
    raw = []
    inner = gh._run

    def spy(a):
        raw.append(list(a))
        return inner(a)

    gh._run = spy
    assert gh.release("42", "x") is True
    writes = [c for c in raw if gqlfake.is_issue_write(c)]
    assert [(c[1].split("/", 3)[-1], c[3]) for c in writes] == [
        ("issues/42/labels/sdlc%3Ain-progress", "DELETE"), ("issues/42/comments", "POST")]


def test_append_to_body_is_a_rest_patch_that_raises_and_never_blanks_on_a_degraded_read():
    run = _rest_429_then_view(json.dumps({"body": "Old"}))
    _gh(run).append_to_body("42", "M")
    assert [c for c in run.calls if c[:2] == ["issue", "edit"]][0][-1] == "Old\n\nM\n"
    # failure RAISES
    def bad(a):
        if gqlfake.is_issue_read(a):
            return gqlfake.rest_issue(a, lambda n, f: {"body": "Old"})
        raise _rest_err(422, "Validation Failed")
    import pytest
    with pytest.raises(Exception):
        _gh(bad).append_to_body("42", "M")


def test_ensure_label_creates_a_missing_label_once_and_skips_an_existing_one():
    """#1140: on-demand creation of the retired-from-init proposal label."""
    src = _mod("sources")
    run = _recording_runner()
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    assert gh.ensure_label("sdlc:needs-confirmation") is True
    creates = [c for c in run.calls if c[0:2] == ["label", "create"]]
    assert len(creates) == 1 and "--force" not in creates[0]
    names = json.dumps([{"name": "sdlc:needs-confirmation"}])
    calls2 = []

    def run2(args):
        calls2.append(list(args))
        return names if args[0] == "api" else ""
    gh2 = src.GitHubSource({"discovery": {"source": "github"}}, run=run2)
    assert gh2.ensure_label("sdlc:needs-confirmation") is False
    assert not [c for c in calls2 if c[0:2] == ["label", "create"]]
