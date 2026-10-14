import json, pathlib, re, tempfile, time, importlib.util
import gqlfake
import pytest

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _is_issues_list_call(a):
    """#1833: true for the `gh api repos/{owner}/{repo}/issues` REST shape `_list_open_issues` now
    constructs (mirrors test_sources.py's own `_issues_verb`/`_is_issues_list_call` helpers)."""
    return len(a) > 1 and a[0] == "api" and str(a[1]).startswith("repos/") and str(a[1]).endswith("/issues")


def _rest_field(a, key):
    """The value of a `gh api -f key=value` field, or None if `key` was never passed."""
    prefix = key + "="
    return next((v[len(prefix):] for v in a if isinstance(v, str) and v.startswith(prefix)), None)


def _recording_runner(by_verb=None):
    """Fake `gh` runner: records every call, returns canned stdout keyed by the gh verb (args[1]).
    #1833: the issues-listing call is now the REST shape (`_is_issues_list_call`), normalized to
    the same `"list"` bucket key the old `["issue", "list", ...]` shape used, so existing
    `{"list": ...}` canned responses keep working unchanged; every other call (e.g. `gh api user`,
    resolving `"@me"`) still keys on its own second token."""
    calls = []
    by_verb = by_verb or {}
    def run(args):
        calls.append(list(args))
        verb = "list" if _is_issues_list_call(args) else (args[1] if len(args) > 1 else args[0])
        return by_verb.get(verb, "")
    run.calls = calls
    return run


def _gh_sdlc(d, **gh):
    base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True); (base / "state").mkdir()
    (base / "config.json").write_text(json.dumps(
        {"discovery": {"source": "github", "github": {"repo": "acme/widget", **gh}}}))
    return base


# --------------------------------------------------------------------------- config/mode helpers


def test_config_reads_are_tolerant_of_a_missing_file():
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        assert triage._config(pathlib.Path(d) / ".sdlc") == {}


def test_is_github_reads_discovery_source():
    triage = _mod("triage")
    assert triage._is_github({"discovery": {"source": "github"}}) is True
    assert triage._is_github({"discovery": {"source": "local-goals"}}) is False
    assert triage._is_github({}) is False


def test_gh_cfg_reads_the_same_keys_sources_py_does():
    triage = _mod("triage")
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget", "assignee": "dana"}}}
    assert triage._gh_cfg(cfg) == {"repo": "acme/widget", "assignee": "dana"}


# --------------------------------------------------------------------------- board fetch + retry


_ONE_ISSUE = json.dumps([{"number": 1, "title": "x", "body": "", "labels": [], "assignees": []}])


def test_list_open_issues_never_uses_the_graphql_search_field():
    """#1833: `_list_open_issues` was unscoped-by-label but STILL graphql-search-billed by
    construction (`--search` alone routes `gh` through the `search()` field, confirmed live) --
    it must never build that shape again. REST has no field-selection concept at all (the old
    Amendment-7 "createdAt must not be requested" concern is now moot -- every field comes back on
    every fetch regardless, at no extra cost either way, since it was always this one fetch)."""
    triage = _mod("triage")
    run = _recording_runner({"list": _ONE_ISSUE})   # non-empty: skip the empty-result retry path
    triage._list_open_issues({"repo": "acme/widget"}, run)
    assert not any(len(c) > 1 and c[0] == "issue" and c[1] == "list" for c in run.calls)
    assert any(_is_issues_list_call(c) for c in run.calls)
    assert not any("--search" in c or "--json" in c for c in run.calls if _is_issues_list_call(c))


def test_list_open_issues_scopes_by_repo_and_assignee():
    """#1833: repo is embedded in the REST endpoint path itself (no separate `--repo` flag);
    `state`/`sort`/`direction` are REST fields, not a `--search "sort:X"` qualifier; a
    non-"@me" assignee resolves to itself with no extra `gh api user` lookup (see the sibling
    test below for the "@me" case, which does need one)."""
    triage = _mod("triage")
    run = _recording_runner({"list": _ONE_ISSUE})   # non-empty: skip the empty-result retry path
    triage._list_open_issues({"repo": "acme/widget", "assignee": "dana"}, run)
    calls = [c for c in run.calls if _is_issues_list_call(c)]
    assert calls, "no issues-listing call was issued at all"
    for c in calls:
        assert str(c[1]) == "repos/acme/widget/issues"
        assert _rest_field(c, "state") == "open"
        assert _rest_field(c, "sort") == "created"
        assert _rest_field(c, "direction") == "asc"
        assert _rest_field(c, "assignee") == "dana"
    assert not any("api user" in " ".join(c) for c in run.calls)   # no lookup needed for a literal login


def test_list_open_issues_resolves_at_me_to_a_login_not_literally():
    """#1833: REST's `assignee=` has no `"@me"` alias of its own (a hard 422, confirmed live by
    #1829's own research) -- unlike the old `--assignee @me`, which `gh` resolved server-side at
    no extra cost. Must resolve via `gh api user` and pass the RESOLVED login into the query."""
    triage = _mod("triage")
    run = _recording_runner({"list": _ONE_ISSUE, "user": "me-login"})
    triage._list_open_issues({"repo": "acme/widget", "assignee": "@me"}, run)
    calls = [c for c in run.calls if _is_issues_list_call(c)]
    assert calls and all(_rest_field(c, "assignee") == "me-login" for c in calls)


def test_list_open_issues_recovers_from_a_transient_error_on_retry(monkeypatch):
    """Mirrors test_sources.py's test_next_pending_recovers_from_a_transient_error_on_retry."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    issues = [{"number": 9, "title": "x", "body": "", "labels": [], "assignees": []}]
    attempts = {"n": 0}
    def run(a):
        if _is_issues_list_call(a):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("gh: HTTP 502 Bad Gateway")
            return json.dumps(issues)
        return ""
    got, degraded = triage._list_open_issues({"repo": "acme/widget"}, run)
    assert [i["number"] for i in got] == [9]
    assert degraded == []
    assert attempts["n"] == 2


def test_list_open_issues_retries_a_stale_empty_read_before_trusting_it(monkeypatch):
    """Mirrors test_next_pending_recovers_from_a_stale_empty_read_on_retry."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    issues = [{"number": 446, "title": "x", "body": "", "labels": [], "assignees": []}]
    attempts = {"n": 0}
    def run(a):
        if _is_issues_list_call(a):
            attempts["n"] += 1
            return "[]" if attempts["n"] == 1 else json.dumps(issues)
        return ""
    got, degraded = triage._list_open_issues({"repo": "acme/widget"}, run)
    assert [i["number"] for i in got] == [446]
    assert attempts["n"] == 2


def test_list_open_issues_fails_open_when_gh_is_unavailable():
    triage = _mod("triage")
    def run(a):
        raise RuntimeError("gh: HTTP 404 Not Found (repo does not exist)")
    got, degraded = triage._list_open_issues({"repo": "acme/widget"}, run)
    assert got == [] and degraded == ["gh_unavailable"]


def test_list_open_issues_limit_reads_board_item_limit_live(monkeypatch):
    """#706 §2c: the fetch ceiling is read off `sources.GitHubSource._BOARD_ITEM_LIMIT` (the same
    constant #692 raised for the board calls), never a re-declared literal -- proven behaviorally
    (a copy would not see this patch), the same shape as the retry-constants pin above."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BOARD_ITEM_LIMIT", 3)
    run = _recording_runner({"list": _ONE_ISSUE})
    triage._list_open_issues({"repo": "acme/widget"}, run)
    args = run.calls[0]
    assert _rest_field(args, "per_page") == "3"


def test_list_open_issues_saturation_flag_is_renamed_to_truncated(monkeypatch):
    """#706 F3: post-#706 the real ceiling is `_BOARD_ITEM_LIMIT` (5000 in production) -- saturating
    it for a test means patching the constant down, the same thing the live-limit pin above does,
    not fetching thousands of fixture issues. The old literal `"truncated_at_200"` would now lie
    the moment the constant changes again (the exact drift class #692 was about), so it is renamed
    to a ceiling-value-agnostic `"truncated"` -- asserted both ways so a half-done rename is caught."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BOARD_ITEM_LIMIT", 3)
    issues = [{"number": i, "title": "x", "body": "", "labels": [], "assignees": []} for i in range(3)]
    run = _recording_runner({"list": json.dumps(issues)})
    got, degraded = triage._list_open_issues({"repo": "acme/widget"}, run)
    assert len(got) == 3
    assert degraded == ["truncated"]
    assert "truncated_at_200" not in degraded


def test_list_open_issues_under_the_ceiling_is_not_flagged_truncated(monkeypatch):
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BOARD_ITEM_LIMIT", 3)
    issues = [{"number": 1, "title": "x", "body": "", "labels": [], "assignees": []}]
    run = _recording_runner({"list": json.dumps(issues)})
    got, degraded = triage._list_open_issues({"repo": "acme/widget"}, run)
    assert degraded == []


def test_list_open_issues_reads_retry_constants_live_from_sources_not_a_copy():
    """Amendment 5: `sources.GitHubSource._BACKLOG_READ_RETRIES`/`_BACKLOG_READ_RETRY_BASE` must be
    READ, not re-declared -- proven behaviorally (a copy would not see this patch) by shrinking the
    retry count on triage's OWN loaded `sources` reference and counting real attempts, plus a
    source-level check that triage.py declares no such constant of its own."""
    triage = _mod("triage")
    triage.sources.GitHubSource._BACKLOG_READ_RETRIES = 2
    triage.sources.GitHubSource._BACKLOG_READ_RETRY_BASE = 0
    try:
        calls = {"n": 0}
        def run(a):
            calls["n"] += 1
            raise RuntimeError("gh: HTTP 502 Bad Gateway")   # always transient -> exhausts retries
        triage._list_open_issues({"repo": "acme/widget"}, run)
        assert calls["n"] == 2, "did not honor the live (patched) retry count"
    finally:
        del triage.sources.GitHubSource._BACKLOG_READ_RETRIES
        del triage.sources.GitHubSource._BACKLOG_READ_RETRY_BASE
    src = (S / "triage.py").read_text(encoding="utf-8")
    assert not re.search(r"^_BACKLOG_READ_RETR", src, re.MULTILINE), (
        "triage.py must not re-declare its own retry constants")


# --- PR-review finding 1: json.loads sat OUTSIDE the try guarding run(args), and the call site in
# survey() was not wrapped in _safe() -- a gh upgrade-notice line, truncated JSON, or an HTML error
# page on stdout crashed the WHOLE survey with a raw traceback (proven live with a real gh shim).


def test_list_open_issues_fails_open_on_unparseable_stdout(monkeypatch):
    """The same seam the reviewer's shim used: `run(args)` succeeds (no exception) but returns
    text that isn't JSON at all -- must degrade, never raise out of this function."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    issues, degraded = triage._list_open_issues(
        {"repo": "acme/widget"}, lambda a: "gh: a new release is available 2.60.0 -> 2.61.0\n")
    assert issues == []
    assert degraded == ["gh_unparseable"]


def test_survey_does_not_crash_when_the_board_fetch_returns_unparseable_stdout(monkeypatch):
    """Integration-level proof of the same seam: `survey()` itself must not propagate the crash --
    the seven fetch-derived buckets degrade instead of the whole process dying."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    with tempfile.TemporaryDirectory() as d:
        base = _gh_sdlc(d)
        pack = triage.survey(str(base), run=lambda a: "gh: a new release is available\n")
    for name in ("active", "parked", "enqueued", "shadow", "epics", "hygiene", "edges"):
        assert "gh_unparseable" in pack["buckets"][name]["degraded"], name
    assert pack["buckets"]["inbox"]["degraded"] == [] or "ledger_off" in pack["buckets"]["inbox"]["degraded"]


def test_gh_source_returns_none_rather_than_raising_on_construction_failure(monkeypatch):
    """Belt-and-suspenders (#706 §2a): `GitHubSource.__init__` is a pure attribute-resolution pass
    and realistically cannot fail on a dict `.get()` walk, but every bucket function below treats
    `gh_source is None` as "fall back to the pre-#706 default" rather than assuming construction
    always succeeds -- this is what makes that contract real instead of just asserted in a
    docstring. (The other half -- that `_bucket_enqueued` genuinely still works with `gh_source is
    None` -- is already proven by the unmodified `test_enqueued_backlog_matches_next_pending_order`
    below, which never passes a gh_source at all.)"""
    triage = _mod("triage")

    def boom(self, config, run=None):
        raise RuntimeError("synthetic construction failure")

    monkeypatch.setattr(triage.sources.GitHubSource, "__init__", boom)
    got = triage._gh_source({"discovery": {"source": "github", "github": {}}}, lambda a: "")
    assert got is None


# --------------------------------------------------------------------------- buckets 1-5

_GOAL_L, _PROG_L, _PARKED_L = "sdlc:goal", "sdlc:in-progress", "sdlc:parked"
_BLOCKED_L = "sdlc:blocked"   # #1350's goal_blocked_label, default spelling


def _issue(number, labels=(), title="x", body="", assignees=()):
    return {"number": number, "title": title, "body": body,
            "labels": [{"name": n} for n in labels],
            "assignees": [{"login": a} for a in assignees]}


def _ledger_sdlc(d, actor="dana", extra_ledger=None):
    base = pathlib.Path(d) / ".sdlc"
    (base / "goals").mkdir(parents=True); (base / "state").mkdir()
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget"}},
           "ledger": {"enabled": True, "actor": actor}}
    if extra_ledger:
        cfg["ledger"].update(extra_ledger)
    (base / "config.json").write_text(json.dumps(cfg))
    return base


def test_active_is_the_intersection_not_bare_in_progress():
    """The label model (status.py): parking DROPS sdlc:goal but LEAVES sdlc:in-progress -- bare
    in-progress would wrongly count a parked issue as active."""
    triage = _mod("triage")
    issues = [_issue(1, labels=[_GOAL_L, _PROG_L]),           # genuinely active
              _issue(2, labels=[_PROG_L, _PARKED_L])]          # parked, still carries in-progress
    out = triage._bucket_active(issues, [], {"goal_label": _GOAL_L, "in_progress_label": _PROG_L})
    assert [i["number"] for i in out["items"]] == [1]


def test_active_reports_writer_alive_as_the_literal_pid_fact():
    """Fold 8: `resumable` used to read as always-true for essentially every genuinely in-progress
    goal -- the claim's OWN recorded pid is the SHORT-LIVED `loop.py` CLI invocation that wrote it,
    already exited by the time anyone reads it back, REGARDLESS of whether a long-running subagent
    is actively working the goal right now (see loop.py's own next_batch docstring). `resumable`
    alone therefore can't distinguish "genuinely abandoned" from "actively in flight" -- `G2`'s
    compiler must not misread an active goal as abandoned. `writer_alive` is the raw, literal fact
    (pid_alive() on the recorded pid, unconditionally); `resumable` keeps its prior computation but
    is now DOCUMENTED for what it actually is: "the loop's own claim-lease would currently treat
    this as mine to resume" (crash-resume semantics), never "abandoned"."""
    triage = _mod("triage")
    issues = [_issue(5, labels=[_GOAL_L, _PROG_L])]
    cfg = {"goal_label": _GOAL_L, "in_progress_label": _PROG_L}

    # branch 1: a definitively dead pid -> writer_alive False, resumable True (the common case)
    dead = [{"kind": "claimed", "goal": "5", "actor": "dana", "id": "dana:host.999999:1",
            "ts": "2020-01-01T00:00:00Z"}]
    out = triage._bucket_active(issues, dead, cfg)["items"][0]
    assert out["writer_alive"] is False and out["resumable"] is True and out["claim_actor"] == "dana"

    # branch 2: a legacy 2-part id (pre-#337, no pid to check at all) -> writer_alive None, resumable True
    legacy = [{"kind": "claimed", "goal": "5", "actor": "dana", "id": "dana:1",
              "ts": "2020-01-01T00:00:00Z"}]
    out = triage._bucket_active(issues, legacy, cfg)["items"][0]
    assert out["writer_alive"] is None and out["resumable"] is True

    # branch 3: a genuinely LIVE pid (this very test process) -> writer_alive True, resumable False
    import os
    alive = [{"kind": "claimed", "goal": "5", "actor": "dana", "id": f"dana:host.{os.getpid()}:1",
             "ts": "2020-01-01T00:00:00Z"}]
    out = triage._bucket_active(issues, alive, cfg)["items"][0]
    assert out["writer_alive"] is True and out["resumable"] is False


def test_active_respects_ttl_seconds_like_every_sibling_consumer():
    """Fold 10a: `_bucket_active` used to call `open_claims_detailed` with no `ttl_seconds` at
    all, unlike every other consumer (`loop.py`'s `_lease()` included) -- an ancient claim whose
    goal never got a terminal outcome recorded (e.g. `ledger.enabled` was flipped off mid-goal)
    would show as an open claim forever. A short TTL must age it out here too."""
    triage = _mod("triage")
    issues = [_issue(5, labels=[_GOAL_L, _PROG_L])]
    ancient = [{"kind": "claimed", "goal": "5", "actor": "dana", "id": "dana:host.999999:1",
               "ts": "2020-01-01T00:00:00Z"}]
    cfg = {"goal_label": _GOAL_L, "in_progress_label": _PROG_L}
    with_ttl = triage._bucket_active(issues, ancient, cfg, ttl_seconds=3600)["items"][0]
    assert with_ttl["claim_actor"] is None, "an ancient claim past the TTL must not still show as open"
    without_ttl = triage._bucket_active(issues, ancient, cfg, ttl_seconds=None)["items"][0]
    assert without_ttl["claim_actor"] == "dana"   # unbounded (0/None) TTL: never expires, unchanged


def test_parked_issue_lost_the_goal_label():
    """A parked issue drops sdlc:goal -- it must still surface under `parked`."""
    triage = _mod("triage")
    issues = [_issue(3, labels=[_PROG_L, _PARKED_L])]
    out = triage._bucket_parked("/nonexistent", issues, [], {"parked_label": _PARKED_L})
    assert [i["number"] for i in out["items"]] == [3]


def _actionlog_sdlc(tmp_path):
    base = tmp_path / ".sdlc"; (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps({"action_log": {"enabled": True}}))
    return base


def test_bucket_parked_also_surfaces_a_blocked_only_issue():
    """#1358 (against #1350's own gap): a goal `mark_blocked` transitioned to `sdlc:blocked`
    (never `sdlc:parked`) had no discovery path in triage's survey -- invisible to a human running
    /sigma-triage even though it lost `sdlc:goal` exactly like a plain parked issue does. Must
    surface here too, tagged by its own state so a human can tell the two apart."""
    triage = _mod("triage")
    issues = [_issue(3, labels=[_BLOCKED_L])]
    out = triage._bucket_parked("/nonexistent", issues, [],
                                {"parked_label": _PARKED_L, "goal_blocked_label": _BLOCKED_L})
    assert [i["number"] for i in out["items"]] == [3]
    assert out["items"][0]["state"] == "blocked"


def test_bucket_parked_tags_a_plain_parked_issue_state_too():
    triage = _mod("triage")
    issues = [_issue(3, labels=[_PARKED_L])]
    out = triage._bucket_parked("/nonexistent", issues, [],
                                {"parked_label": _PARKED_L, "goal_blocked_label": _BLOCKED_L})
    assert out["items"][0]["state"] == "parked"


def test_bucket_parked_surfaces_a_needs_triage_issue():
    """#2427, a real bug found live and fixed here: #2363's `sdlc:needs-triage` overlay (every tier
    of automatic unit classification tried and failed) had NO discovery path in triage's survey at
    all -- `/sigma-triage`, the tool whose entire job is surfacing things needing a human decision,
    was blind to the one label whose whole purpose is exactly that. Its reason names the absence
    of a self-healing sweep too, unlike its `needs-unit`/`needs-label` siblings."""
    triage = _mod("triage")
    issues = [_issue(3, labels=[_GOAL_L, "sdlc:needs-triage"])]
    out = triage._bucket_parked("/nonexistent", issues, [],
                                {"parked_label": _PARKED_L, "goal_blocked_label": _BLOCKED_L})
    assert [i["number"] for i in out["items"]] == [3]
    assert out["items"][0]["state"] == "needs-triage"
    assert "human must declare" in out["items"][0]["reason"]


def test_bucket_parked_goal_blocked_label_defaults_to_sdlc_blocked_when_unconfigured():
    # `goal_blocked_label` is always-on/code-managed (#1350), unlike `parked_label` -- a gh_cfg
    # that never mentions it (every pre-#1358 config) must still recognize the real default spelling.
    triage = _mod("triage")
    issues = [_issue(3, labels=[_BLOCKED_L])]
    out = triage._bucket_parked("/nonexistent", issues, [], {"parked_label": _PARKED_L})
    assert [i["number"] for i in out["items"]] == [3]


def test_parked_reason_prefers_ledger_why_over_actionlog(tmp_path):
    triage = _mod("triage")
    base = _actionlog_sdlc(tmp_path)
    issues = [_issue(3, labels=[_PARKED_L])]
    entries = [{"kind": "parked", "goal": "3", "why": "from ledger"}]
    triage.actionlog.append(str(base), "3", "recorded", "loop", result="parked", detail="from actionlog")
    out = triage._bucket_parked(str(base), issues, entries, {"parked_label": _PARKED_L})
    assert out["items"][0]["reason"] == "from ledger"
    assert out["items"][0]["reason_source"] == "ledger"


def test_parked_reason_falls_back_to_actionlog_when_ledger_has_none(tmp_path):
    triage = _mod("triage")
    base = _actionlog_sdlc(tmp_path)
    issues = [_issue(3, labels=[_PARKED_L])]
    triage.actionlog.append(str(base), "3", "recorded", "loop", result="parked", detail="from actionlog")
    out = triage._bucket_parked(str(base), issues, [], {"parked_label": _PARKED_L})
    assert out["items"][0]["reason"] == "from actionlog"
    assert out["items"][0]["reason_source"] == "actionlog"


def test_orphaned_worktrees_local_mode_recognizes_a_claimed_goal_by_stem(tmp_path):
    """Fold 7: local-mode ledger claims key on the FULL FILE PATH (`.sdlc/goals/0002.md`, what
    LocalSource passes as `goal`), while a worktree directory is named by `work.stem(goal)`
    ("0002") -- comparing the raw claim key against the bare directory name never matched, so
    every actively-claimed local goal with a live worktree read as a FALSE-POSITIVE orphan. Both
    sides must go through `work.stem()`."""
    triage = _mod("triage")
    work_dir = tmp_path / ".sdlc" / "work" / "0002"
    work_dir.mkdir(parents=True)
    entries = [{"kind": "claimed", "goal": str(tmp_path / ".sdlc" / "goals" / "0002.md"),
               "actor": "dana", "id": "dana:host.123:1", "ts": "2026-01-01T00:00:00Z"}]
    out = triage._orphaned_worktrees(str(tmp_path / ".sdlc"), entries)
    assert out == [], f"a genuinely claimed local goal's worktree read as orphaned: {out}"


def test_enqueued_backlog_matches_next_pending_order():
    triage = _mod("triage")
    issues = [_issue(7, labels=[_GOAL_L]), _issue(3, labels=[_GOAL_L]),
              _issue(9, labels=[_GOAL_L, _PROG_L]),          # active, excluded
              _issue(2, labels=[_GOAL_L, _PARKED_L])]        # parked, excluded
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L, "in_progress_label": _PROG_L,
                                           "parked_label": _PARKED_L})
    assert [i["number"] for i in out["items"]] == [3, 7]


# --- #706: order/prefix/ceiling/Ready-lane on the enqueued bucket --------------------------------
# `gh_source` threads `sources.GitHubSource`'s own order/prefix/tie-break into the bucket that was
# board-release-alignment #706's headline claim: "this is the order next_pending would serve".


def _gh_source_for(triage, run=None, **discovery):
    """A real, hermetic GitHubSource for direct bucket-function tests -- mirrors test_sources.py's
    own `_gh_with(issues, order=..., github={...})` construction shape. `GitHubSource.__init__` is
    a pure attribute-resolution pass (zero gh calls, confirmed by the #706 plan review), so the
    trivial no-op default `run` is safe wherever a test doesn't exercise a live gh read (the
    Ready-lane tests below pass their own explicit fake instead).

    Takes the CALLER's already-`_mod()`-loaded `triage` rather than loading its own: `_mod()`
    creates a brand new module object (and therefore a brand new `sources.GitHubSource` class
    object) on every call, so a helper that reloaded independently would hand back an instance
    whose class is not the one a test's own `monkeypatch.setattr(triage.sources.GitHubSource,
    ...)` actually patches -- the patch would silently no-op against this instance. Sharing the
    caller's `triage` is what makes the class objects (and therefore the patch) the same one."""
    cfg = {"discovery": {"source": "github", **discovery}}
    return triage._gh_source(cfg, run or (lambda a: ""))


def test_bucket_enqueued_sorts_by_priority_by_default():
    triage = _mod("triage")
    gh_source = _gh_source_for(triage)
    issues = [_issue(3, labels=[_GOAL_L, "priority:P4"]), _issue(9, labels=[_GOAL_L, "priority:P0"])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert [i["number"] for i in out["items"]] == [9, 3]


def test_bucket_enqueued_created_order_matches_pre_706_behavior_exactly():
    """Byte-identical to the pre-#706 `sorted(key=lambda x: x["number"])` when `discovery.order` is
    pinned to "created" explicitly -- the "no behavior change when config carries the old defaults
    explicitly" acceptance criterion, not just a plausible-looking order."""
    triage = _mod("triage")
    gh_source = _gh_source_for(triage, order="created")
    issues = [_issue(9, labels=[_GOAL_L, "priority:P0"]), _issue(3, labels=[_GOAL_L, "priority:P4"])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert [i["number"] for i in out["items"]] == [3, 9]


def test_bucket_enqueued_unrecognised_order_falls_back_to_priority_default():
    """F2: a sort-order assertion alone is vacuous here -- `_pick_key` only ever branches on
    `self.order == "created"`, so ANY non-"created" string (a genuine fallback to "priority", or an
    implementation that never validates against `discovery.ORDERS` at all, e.g. reading
    `config["discovery"]["order"]` raw) produces the identical sorted output for "bogus" -- a test
    that only checks the sort cannot tell the two apart. Pin the RESOLUTION directly instead."""
    triage = _mod("triage")
    assert triage.sources._order({"discovery": {"order": "bogus"}}) == "priority"
    gh_source = _gh_source_for(triage, order="bogus")
    assert gh_source.order == "priority"


def test_bucket_enqueued_priority_ties_break_by_issue_number():
    triage = _mod("triage")
    gh_source = _gh_source_for(triage)
    issues = [_issue(9, labels=[_GOAL_L, "priority:P0"]), _issue(3, labels=[_GOAL_L, "priority:P0"])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert [i["number"] for i in out["items"]] == [3, 9]


def test_bucket_enqueued_uses_the_configured_priority_prefix():
    triage = _mod("triage")
    gh_source = _gh_source_for(triage, github={"priority_label_prefix": "sev:"})
    issues = [_issue(3, labels=[_GOAL_L, "sev:P3", "priority:P0"]),   # priority:P0 must be IGNORED
              _issue(9, labels=[_GOAL_L, "sev:P0"])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert [i["number"] for i in out["items"]] == [9, 3]
    by_number = {i["number"]: i for i in out["items"]}
    assert by_number[3]["priority"] == "P3"   # the displayed field also reads the configured prefix


def test_bucket_enqueued_reads_pick_key_live_not_a_copy(monkeypatch):
    """Flagship live-not-copied pin (mirrors test_list_open_issues_limit_reads_board_item_limit_live
    and the #677-review precedent): patch the CLASS attribute AFTER gh_source is already
    constructed, and prove `_bucket_enqueued` still follows it -- passes only if the bound method
    is resolved fresh inside `_bucket_enqueued`'s own call, never cached at construction (F4)."""
    triage = _mod("triage")
    gh_source = _gh_source_for(triage)
    issues = [_issue(3, labels=[_GOAL_L, "priority:P0"]), _issue(9, labels=[_GOAL_L, "priority:P4"])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert [i["number"] for i in out["items"]] == [3, 9]      # sanity: the real _pick_key ranks P0 first
    monkeypatch.setattr(triage.sources.GitHubSource, "_pick_key",
                        lambda self, issue: -issue["number"])
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert [i["number"] for i in out["items"]] == [9, 3], (
        "did not follow the monkeypatched _pick_key -- the bound method was cached instead of "
        "resolved live at call time")


def test_picker_window_stays_lockstep_with_pending_by_labels_own_two_hundred():
    """`_pending_by_label`'s `--limit` used to be a bare string literal ("200"), checked here via a
    source-text pin (same mechanism `test_triage_module_never_redeclares_the_block_vocabulary` uses
    for `_BLOCK_RE`). #815 gave it a real named class attribute instead (`_BACKLOG_FETCH_CAP`, F1's
    own "nothing to read live" complaint no longer applies) -- so this now pins the VALUES directly,
    a stronger check than text-matching ever was: it survives a refactor that changes how the limit
    is spelled in source, and still fails loudly the day the two numbers actually diverge."""
    triage = _mod("triage")
    assert triage.sources.GitHubSource._BACKLOG_FETCH_CAP == triage._PICKER_WINDOW, (
        "sources.GitHubSource._BACKLOG_FETCH_CAP no longer matches triage._PICKER_WINDOW "
        f"({triage._PICKER_WINDOW}) -- update one to match the other")


def test_bucket_enqueued_does_not_flag_at_exactly_the_picker_window():
    triage = _mod("triage")
    gh_source = _gh_source_for(triage)
    issues = [_issue(n, labels=[_GOAL_L]) for n in range(1, 201)]   # exactly 200 eligible
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert len(out["items"]) == 200
    assert "exceeds_picker_window" not in out["degraded"]


def test_bucket_enqueued_flags_exceeding_the_picker_window():
    """F1: the ceiling raise (§2c) makes the enqueued bucket's own headline claim ("this is the
    order next_pending would serve") false past 200 -- `_pending_by_label`'s own fetch is still
    capped there, so a P0 sitting past position 200 in THIS bucket's fetch is invisible to the
    real picker no matter how `_pick_key` ranks it. 201 eligible issues -> the marker must fire."""
    triage = _mod("triage")
    gh_source = _gh_source_for(triage)
    issues = [_issue(n, labels=[_GOAL_L]) for n in range(1, 202)]   # 201 eligible goal-labelled issues
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert len(out["items"]) == 201
    assert "exceeds_picker_window" in out["degraded"]


def test_bucket_enqueued_falls_back_to_number_order_when_gh_source_is_none():
    """The other half of the belt-and-suspenders contract (§2a): `_bucket_enqueued` never assumes
    `gh_source` is present. `test_enqueued_backlog_matches_next_pending_order` above already covers
    this implicitly (it never passes a gh_source at all); this test pins it explicitly against a
    fixture the priority-aware tests above would sort differently, so a regression that silently
    started requiring gh_source cannot hide behind the older, priority-free fixture."""
    triage = _mod("triage")
    issues = [_issue(9, labels=[_GOAL_L, "priority:P0"]), _issue(3, labels=[_GOAL_L, "priority:P4"])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, None)
    assert [i["number"] for i in out["items"]] == [3, 9]   # number order, priority label ignored


def test_bucket_enqueued_excludes_needs_triage_when_gh_source_is_none():
    """#2427, a real bug found live and fixed here: the `gh_source is None` fallback tuple was
    missing `needs_triage_label`, so a `sdlc:needs-triage` issue read as "ready to pick" in the
    degraded (no board scope) path even though the real picker (`not_eligible_labels()`) always
    refused it -- exactly the drift shape every comment on this fallback already warns about for
    its `needs_label`/`needs_unit` siblings."""
    triage = _mod("triage")
    issues = [_issue(3, labels=[_GOAL_L, "sdlc:needs-triage"]), _issue(9, labels=[_GOAL_L])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, None)
    assert [i["number"] for i in out["items"]] == [9]


def test_bucket_enqueued_project_scope_missing_marker_when_ready_lane_errors_on_scope():
    """F5: `_ready_lane()`'s own failure path calls `_note_scope(exc)`, which prints straight to
    stderr and cannot be suppressed without editing sources.py (forbidden, per RAILS). Surfaced
    through `degraded` too, per the review's required disposition, so a JSON consumer of survey()
    (not just a human watching stderr) also learns the board read is blind on this run."""
    triage = _mod("triage")
    def run(a):
        if a[:2] == ["project", "list"]:
            raise RuntimeError("missing the `project` scope — run gh auth refresh -s project")
        return ""
    gh_source = _gh_source_for(
        triage, run=run, github={"project": {"enabled": True, "number": 4, "owner": "acme"}})
    gh_source._RETRY_BASE = 0
    issues = [_issue(3, labels=[_GOAL_L])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert "project_scope_missing" in out["degraded"]
    assert "board_ready_lane_authoritative" not in out["degraded"]


def _board_run(ready_option=True):
    """Minimal `gh project` responder for `_ready_lane()`'s own two bounded, non-paginated calls
    (`project list` + `project field-list`) -- deliberately NOT the full board simulator
    test_github_project.py builds (this bucket never reads item-list at all; proven by
    test_ready_lane_check_never_calls_the_expensive_item_list below), just enough to answer the
    existence check the way `_find_project`/`_list_fields` expect."""
    options = ([{"name": "Backlog"}, {"name": "Ready"}, {"name": "In Progress"}] if ready_option
              else [{"name": "Backlog"}, {"name": "In Progress"}])
    calls = []
    def run(a):
        calls.append(list(a))
        if a[:2] == ["project", "list"]:
            return json.dumps({"projects": [{"number": 4, "id": "PVT_x", "title": "x"}]})
        if a[:2] == ["project", "field-list"]:
            return json.dumps({"fields": [{"id": "F_status", "name": "Status", "options": options}]})
        return ""
    run.calls = calls
    return run


def test_ready_lane_marker_present_when_board_status_queue_is_configured():
    triage = _mod("triage")
    run = _board_run(ready_option=True)
    gh_source = _gh_source_for(
        triage, run=run, github={"project": {"enabled": True, "number": 4, "owner": "acme"}})
    gh_source._RETRY_BASE = 0
    issues = [_issue(3, labels=[_GOAL_L])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert "board_ready_lane_authoritative" in out["degraded"]


def test_ready_lane_marker_absent_when_queue_source_is_label():
    """Mirrors `_ready_lane`'s own structural gate: `queue_source: "label"` is an explicit escape
    hatch back to the historical rule even on a board that DOES carry a Ready option."""
    triage = _mod("triage")
    run = _board_run(ready_option=True)
    gh_source = _gh_source_for(
        triage, run=run,
        github={"project": {"enabled": True, "number": 4, "owner": "acme", "queue_source": "label"}})
    issues = [_issue(3, labels=[_GOAL_L])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert "board_ready_lane_authoritative" not in out["degraded"]
    assert run.calls == [], "queue_source == 'label' must short-circuit _ready_lane before any gh call"


def test_ready_lane_marker_absent_and_zero_extra_gh_calls_when_project_disabled():
    """No `project` block at all -- the overwhelming majority of repos. Proves the "zero extra cost
    for the common case" claim behaviorally, not just by code inspection: the fake run's call log
    stays completely empty."""
    triage = _mod("triage")
    run = _board_run(ready_option=True)
    gh_source = _gh_source_for(triage, run=run)   # no "project" key at all -> project_enabled False
    issues = [_issue(3, labels=[_GOAL_L])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert "board_ready_lane_authoritative" not in out["degraded"]
    assert run.calls == []


def test_ready_lane_check_never_calls_the_expensive_item_list():
    triage = _mod("triage")
    run = _board_run(ready_option=True)
    gh_source = _gh_source_for(
        triage, run=run, github={"project": {"enabled": True, "number": 4, "owner": "acme"}})
    gh_source._RETRY_BASE = 0
    issues = [_issue(3, labels=[_GOAL_L])]
    triage._bucket_enqueued(issues, {"goal_label": _GOAL_L}, gh_source)
    assert not any("item-list" in c for c in run.calls), (
        "_bucket_enqueued must never call the expensive _board_queue() item-list read")


def test_bucket_enqueued_docstring_no_longer_claims_oldest_first():
    """Docs sweep (#706 §2e): the pre-#706 docstring claimed "sorted by number -- the same
    oldest-first order next_pending itself sorts by", which stopped being true the moment #698
    made priority the default order. Source-text check (mirrors
    test_triage_module_never_redeclares_the_block_vocabulary's own anchored-not-substring
    precedent): the stale characterization must not survive in the committed source."""
    src = (S / "triage.py").read_text(encoding="utf-8")
    m = re.search(r'def _bucket_enqueued\(.*?\n(    """.*?""")', src, re.DOTALL)
    assert m, "could not locate _bucket_enqueued's docstring"
    doc = m.group(1)
    assert "oldest-first" not in doc, "_bucket_enqueued's docstring still claims oldest-first order"


def test_shadow_backlog_detects_assigned_but_unlabeled():
    triage = _mod("triage")
    issues = [_issue(4, labels=[], assignees=["dana"]),        # shadow: assigned, no goal label
              _issue(6, labels=[_GOAL_L], assignees=["dana"])] # a real goal -- not shadow
    out = triage._bucket_shadow(issues, {"goal_label": _GOAL_L, "assignee": "dana"}, "dana")
    assert [i["number"] for i in out["items"]] == [4]


def test_shadow_matches_a_login_that_differs_only_in_case():
    """#1442: GitHub logins are case-insensitive for identity but the API returns the CANONICAL
    casing, while `me` comes from the ledger actor. The old exact comparison silently dropped an
    issue from this bucket whenever the two differed only in case -- so work that IS assigned to
    you never surfaced as shadow backlog."""
    triage = _mod("triage")
    issues = [_issue(4, labels=[], assignees=["Dana"])]        # canonical casing from the API
    out = triage._bucket_shadow(issues, {"goal_label": _GOAL_L, "assignee": "dana"}, "dana")
    assert [i["number"] for i in out["items"]] == [4]


def test_shadow_uses_the_one_shared_assignee_normaliser():
    """#1442: triage had a THIRD hand-rolled copy of this. Sharing `sources._logins` is what stops
    the bucket drifting from what the picker actually does -- and it is why the board's bare-string
    shape cannot break this bucket if triage ever reads board items."""
    sources = _mod("sources")
    assert not hasattr(_mod("triage"), "_assignee_logins")     # the third spelling is gone
    assert sources._logins([{"login": "Dana"}]) == {"dana"}


def test_shadow_excludes_a_parked_issue_assigned_to_me():
    """PR-review finding 4: parking DROPS sdlc:goal (the label model) -- a parked, assigned issue
    satisfies shadow's old filter ("no goal label AND assigned") and double-counted into BOTH
    `parked` and `shadow`. Bucket 5 exists ONLY for the #623 never-had-the-goal-label class."""
    triage = _mod("triage")
    issues = [_issue(9, labels=[_PARKED_L, _PROG_L], assignees=["dana"])]
    out = triage._bucket_shadow(
        issues, {"goal_label": _GOAL_L, "parked_label": _PARKED_L, "assignee": "dana"}, "dana")
    assert out["items"] == []


def test_shadow_excludes_an_epic_assigned_to_me():
    """Finding 4, the epic half: every epic (no sdlc:goal by definition) assigned to me also
    double-counted into BOTH `epics` and `shadow` before this fix."""
    triage = _mod("triage")
    issues = [_issue(9, labels=["sdlc:decompose"], assignees=["dana"])]
    out = triage._bucket_shadow(issues, {"goal_label": _GOAL_L, "assignee": "dana"}, "dana")
    assert out["items"] == []


# --- #669 F2: additive priority/model on the pickable bucket items -------------------------------
# Open Decision 4 (669-plan.md) resolved: G1's as-built bucket items carry NO priority/model (branch
# b is today's reality); this is the sanctioned edit to G1's own shipped bucket functions, not new
# scope (669-plan-review.md F2). Only active/enqueued/shadow/parked -- the only buckets that can
# hold a PICKABLE issue -- gain the fields; epics/hygiene/edges/inbox/context are untouched.


def test_label_value_finds_the_first_matching_prefix_deterministically():
    triage = _mod("triage")
    assert triage._label_value({"priority:P0", "model:daily"}, "priority:") == "P0"
    assert triage._label_value({"priority:P0", "model:daily"}, "model:") == "daily"
    assert triage._label_value({"priority:P0"}, "model:") is None
    assert triage._label_value(set(), "priority:") is None


def test_active_item_surfaces_priority_and_model_when_present():
    triage = _mod("triage")
    issues = [_issue(1, labels=[_GOAL_L, _PROG_L, "priority:P0", "model:daily"]),
              _issue(2, labels=[_GOAL_L, _PROG_L])]                     # no priority/model labels
    out = triage._bucket_active(issues, [], {"goal_label": _GOAL_L, "in_progress_label": _PROG_L})
    by_number = {i["number"]: i for i in out["items"]}
    assert by_number[1]["priority"] == "P0" and by_number[1]["model"] == "daily"
    assert by_number[2]["priority"] is None and by_number[2]["model"] is None


def test_bucket_active_uses_the_configured_priority_prefix():
    triage = _mod("triage")
    gh_source = _gh_source_for(triage, github={"priority_label_prefix": "sev:"})
    issues = [_issue(1, labels=[_GOAL_L, _PROG_L, "sev:P1", "priority:P0"])]   # priority:P0 ignored
    out = triage._bucket_active(issues, [], {"goal_label": _GOAL_L, "in_progress_label": _PROG_L},
                                gh_source)
    assert out["items"][0]["priority"] == "P1"


def test_enqueued_item_surfaces_priority_and_model_when_present():
    triage = _mod("triage")
    issues = [_issue(7, labels=[_GOAL_L, "priority:P1", "model:bulk"])]
    out = triage._bucket_enqueued(issues, {"goal_label": _GOAL_L, "in_progress_label": _PROG_L,
                                           "parked_label": _PARKED_L})
    assert out["items"][0]["priority"] == "P1" and out["items"][0]["model"] == "bulk"


def test_parked_item_surfaces_priority_and_model_when_present():
    triage = _mod("triage")
    issues = [_issue(3, labels=[_PARKED_L, "priority:P2", "model:opus"])]
    out = triage._bucket_parked("/nonexistent", issues, [], {"parked_label": _PARKED_L})
    assert out["items"][0]["priority"] == "P2" and out["items"][0]["model"] == "opus"


def test_bucket_parked_uses_the_configured_priority_prefix():
    triage = _mod("triage")
    gh_source = _gh_source_for(triage, github={"priority_label_prefix": "sev:"})
    issues = [_issue(3, labels=[_PARKED_L, "sev:P2", "priority:P0"])]   # priority:P0 ignored
    out = triage._bucket_parked("/nonexistent", issues, [], {"parked_label": _PARKED_L}, gh_source)
    assert out["items"][0]["priority"] == "P2"


def test_shadow_item_surfaces_priority_and_model_when_present():
    triage = _mod("triage")
    issues = [_issue(4, labels=["priority:P0"], assignees=["dana"])]
    out = triage._bucket_shadow(issues, {"goal_label": _GOAL_L, "assignee": "dana"}, "dana")
    assert out["items"][0]["priority"] == "P0" and out["items"][0]["model"] is None


def test_bucket_shadow_uses_the_configured_priority_prefix():
    triage = _mod("triage")
    gh_source = _gh_source_for(triage, github={"priority_label_prefix": "sev:"})
    issues = [_issue(4, labels=["sev:P0", "priority:P4"], assignees=["dana"])]   # priority:P4 ignored
    out = triage._bucket_shadow(issues, {"goal_label": _GOAL_L, "assignee": "dana"}, "dana", gh_source)
    assert out["items"][0]["priority"] == "P0"


# --- inbox --------------------------------------------------------------------------------------


def test_inbox_finds_an_unanswered_handoff_addressed_to_me():
    triage = _mod("triage")
    entries = [{"kind": "handoff", "goal": "10", "actor": "rae", "to": "dana", "issue": 11,
               "priority": "P1", "why": "need the schema"}]
    out = triage._bucket_inbox("/nonexistent", {"ledger": {"enabled": True}}, entries, "dana")
    assert out["items"][0]["from"] == "rae" and out["items"][0]["issue"] == 11


def test_inbox_excludes_a_handoff_i_wrote_myself():
    """Amendment 6: `to == me` alone also matches a hand-off I filed myself (reachable when the
    CODEOWNERS-resolved owner of the target area is me) -- must require actor != me too."""
    triage = _mod("triage")
    entries = [{"kind": "handoff", "goal": "10", "actor": "dana", "to": "dana", "issue": 11,
               "why": "self-addressed"}]
    out = triage._bucket_inbox("/nonexistent", {"ledger": {"enabled": True}}, entries, "dana")
    assert out["items"] == []


def test_inbox_never_clears_the_watch_inbox_file(tmp_path):
    triage = _mod("triage")
    base = tmp_path / ".sdlc"; (base / "state").mkdir(parents=True)
    inbox = base / "state" / "inbox.md"
    inbox.write_text("## something pending\n")
    triage._bucket_inbox(str(base), {"ledger": {"enabled": True}}, [], "dana")
    assert inbox.read_text() == "## something pending\n"


def test_inbox_finds_a_handoff_even_when_the_watch_inbox_file_is_empty(tmp_path):
    """Proves bucket 1 does not rely on watch.read_inbox alone (staleness / the #421 cursor gap)."""
    triage = _mod("triage")
    base = tmp_path / ".sdlc"; (base / "state").mkdir(parents=True)
    (base / "state" / "inbox.md").write_text("")
    entries = [{"kind": "handoff", "goal": "10", "actor": "rae", "to": "dana", "issue": 11, "why": "x"}]
    out = triage._bucket_inbox(str(base), {"ledger": {"enabled": True}}, entries, "dana")
    assert len(out["items"]) == 1


# --------------------------------------------------------------------------- buckets 6-8


def test_epics_matches_the_real_sdlc_decompose_label():
    """`sdlc:decompose` is the REAL, shipped label decompose_check's file-mode stamps on a filed
    "Decompose #N" meta-issue (loop.py / decompose_goal.py) -- the anchor for this bucket, not an
    invented convention."""
    triage = _mod("triage")
    out = triage._bucket_epics([_issue(1, labels=["sdlc:decompose"])])
    assert [i["number"] for i in out["items"]] == [1]
    assert out["items"][0]["matched_label"] == "sdlc:decompose"


def test_epics_heuristic_matches_an_adopter_epic_label():
    """No fixed 'epic'/'needs-decomposition' label convention exists in sigma itself -- an
    adopter's own label naming (documented heuristic, case-insensitive substring) is also caught."""
    triage = _mod("triage")
    out = triage._bucket_epics([_issue(2, labels=["type:Epic"]),
                                _issue(3, labels=["needs-decomposition"]),
                                _issue(4, labels=["sdlc:goal"])])   # not an epic
    assert {i["number"] for i in out["items"]} == {2, 3}


def test_epics_matched_label_is_deterministic():
    """Fold 9: `next()` over a bare `set()` of label names is iteration-order-dependent (a
    12-interpreter probe varied which of several matching labels won) -- sorted first, so the
    alphabetically-first matching label always wins, every run, on every interpreter."""
    triage = _mod("triage")
    issues = [_issue(1, labels=["zzz-epic", "epic-alpha", "sdlc:decompose"])]
    out = triage._bucket_epics(issues)
    assert out["items"][0]["matched_label"] == "epic-alpha", (
        "matched_label must be the alphabetically-first match (sorted before next()), "
        f"not iteration-order-dependent; got {out['items'][0]['matched_label']!r}")


def test_hygiene_flags_missing_priority_and_not_a_missing_model():
    """#1602 inverted the first case. A hygiene item asserts an issue is not enqueue-ready, and a
    missing `model:*` never made one unpickable -- nothing reads the value, the kit seeds no such
    label, and the tier comes from `predict.py` over the goal TEXT. Demanding it only pushed
    adopters into inventing incompatible vocabularies. Missing PRIORITY is still a real gap: this
    module reads it and orders waves by it. Full coverage in tests/test_model_label.py."""
    triage = _mod("triage")
    issues = [_issue(1, labels=[_GOAL_L, "priority:P1"]),                 # no model -> NOT flagged
              _issue(2, labels=[_GOAL_L, "model:daily", "priority:P2"]),  # complete -> not flagged
              _issue(3, labels=[_PARKED_L]),                              # not in scope -> ignored
              _issue(4, labels=[_GOAL_L])]                                # no priority -> flagged
    out = triage._bucket_hygiene(issues, {"goal_label": _GOAL_L, "parked_label": _PARKED_L})
    assert [i["number"] for i in out["items"]] == [4]
    assert out["items"][0]["missing"] == ["priority"]


def test_hygiene_flags_a_goal_and_parked_label_contradiction():
    """#817: `_offboard`'s three label-transition calls are each independently try/excepted -- a
    partial failure (the FIRST call, removing goal_label, raises on a transient gh error while the
    THIRD, adding parked_label, succeeds) leaves an issue carrying BOTH simultaneously. Live
    evidence this actually happens: this repo's own #771 timeline flipped P2->P1->P2->P1->P2 as a
    DIFFERENT partial-failure pattern in the same file's label-editing code. The existing
    missing-model/priority check explicitly EXCLUDES parked issues from its scope (`parked_l in
    names: continue`) -- so a goal+parked contradiction was invisible to hygiene entirely; this
    adds the check the exclusion was blind to, not a replacement for it."""
    triage = _mod("triage")
    issues = [_issue(1, labels=[_GOAL_L, "model:daily", "priority:P1"]),              # clean
              _issue(2, labels=[_GOAL_L, _PARKED_L, "model:daily", "priority:P1"])]   # contradiction
    out = triage._bucket_hygiene(issues, {"goal_label": _GOAL_L, "parked_label": _PARKED_L})
    contradictions = [i for i in out["items"] if i.get("contradiction")]
    assert [i["number"] for i in contradictions] == [2]
    assert contradictions[0]["contradiction"] == "goal+parked"


def test_bucket_hygiene_flags_missing_against_the_configured_prefix():
    """`sev:` configured; an issue carrying `priority:P1` (the OLD default spelling) but no `sev:*`
    label is correctly flagged as missing priority -- proves the hygiene check isn't silently still
    hardcoded to the historical prefix."""
    triage = _mod("triage")
    gh_source = _gh_source_for(triage, github={"priority_label_prefix": "sev:"})
    issues = [_issue(1, labels=[_GOAL_L, "model:daily", "priority:P1"])]   # old-spelling priority label
    out = triage._bucket_hygiene(issues, {"goal_label": _GOAL_L, "parked_label": _PARKED_L}, gh_source)
    assert [i["number"] for i in out["items"]] == [1]
    assert out["items"][0]["missing"] == ["priority"]


# --- #2296: the priority rubric, mechanically applied ---------------------------------------
#
# BR-19/BR-20/BR-25: the "Priority criteria" rubric in sigma-triage's own SKILL.md was prose only
# before this -- read by a human at the question round but applied by nothing. `_priority_hint`
# ports the pattern sigma-scope already uses for its own identical case (a P1 zero-signal default,
# only asking when two adjacent tiers are genuinely ambiguous).


def test_priority_hint_computes_a_clear_tier_from_the_issues_own_text():
    """An issue whose own text unambiguously matches one tier's rubric vocabulary -- the P4 test
    ("speculative, cosmetic, or pure hygiene") -- resolves to that tier with no ambiguity, the same
    "would the answer change the plan's shape" resolution sigma-scope's SKILL.md already performs
    silently once steps 2-3 answer it unambiguously."""
    triage = _mod("triage")
    hint = triage._priority_hint("Remove a stray zero-byte file",
                                 "Purely cosmetic hygiene, zero functional impact.")
    assert hint == {"tier": "P4", "ambiguous": False}


def test_priority_hint_defaults_to_p1_on_zero_signal_mirroring_sdlc_scope():
    """No rubric vocabulary matched at all -- sigma-scope's own zero-signal default (`P1`, per its
    SKILL.md step 6 and `compile_plan.DEFAULT_PRIORITY`) applies here too, silently, not as an
    ambiguous case."""
    triage = _mod("triage")
    hint = triage._priority_hint("Improve the onboarding docs", "Some general polish text.")
    assert hint == {"tier": "P1", "ambiguous": False}


def test_priority_hint_flags_genuine_ambiguity_between_two_adjacent_tiers():
    """Text that plausibly reads as EITHER of two adjacent tiers -- the same "soon -> P1 or P2"
    shape sigma-scope's own SKILL.md names as a real, askable ambiguity -- still surfaces the ask;
    the residual exception is not silently removed by mechanizing the common case."""
    triage = _mod("triage")
    hint = triage._priority_hint(
        "Fix the dashboard refresh",
        "This degrades every run right now, but it's also just planned roadmap work and "
        "nothing is broken yet.")
    assert hint["tier"] is None
    assert hint["ambiguous"] == ["P1", "P2"]


def test_priority_hint_a_non_adjacent_multi_match_resolves_to_the_more_severe_tier():
    """P0 and P3 vocabulary both present is NOT the adjacent-tier ambiguity this module asks
    about -- mirrors predict.py's own "conflicts resolve UPWARD to the more capable tier" rule:
    the more severe (lower-numbered) tier wins outright, silently."""
    triage = _mod("triage")
    hint = triage._priority_hint(
        "Security bypass found during cleanup",
        "This is an unsafe review bypass, also some unrelated follow-up polish tech-debt.")
    assert hint == {"tier": "P0", "ambiguous": False}


def test_bucket_hygiene_wires_the_priority_hint_onto_a_missing_priority_item():
    """The mechanical wiring point #2296 names explicitly: `_bucket_hygiene` (BR-25) gains
    `priority_hint` on every item it already flags `missing: ["priority"]` for -- computed from
    that SAME issue's own title+body, never a second read."""
    triage = _mod("triage")
    issues = [_issue(1, labels=[_GOAL_L], title="Remove a stray zero-byte file",
                     body="Purely cosmetic hygiene, zero functional impact.")]
    out = triage._bucket_hygiene(issues, {"goal_label": _GOAL_L, "parked_label": _PARKED_L})
    assert out["items"][0]["missing"] == ["priority"]
    assert out["items"][0]["priority_hint"] == {"tier": "P4", "ambiguous": False}


def test_bucket_hygiene_never_computes_a_hint_when_priority_is_not_missing():
    """A hygiene item flagged only for the goal+parked contradiction (or not flagged at all)
    carries no `priority_hint` key -- the hint is scoped to the exact gap it fills, not attached
    unconditionally to every item this bucket ever emits."""
    triage = _mod("triage")
    issues = [_issue(1, labels=[_GOAL_L, "priority:P1"])]   # already labelled -- not flagged
    out = triage._bucket_hygiene(issues, {"goal_label": _GOAL_L, "parked_label": _PARKED_L})
    assert out["items"] == []


def test_fmt_row_surfaces_a_confident_priority_hint():
    triage = _mod("triage")
    row = triage._fmt_row({"number": 4, "title": "x", "missing": ["priority"],
                           "priority_hint": {"tier": "P4", "ambiguous": False}})
    assert "missing: priority" in row
    assert "rubric hint: P4" in row


def test_fmt_row_surfaces_a_genuinely_ambiguous_priority_hint():
    triage = _mod("triage")
    row = triage._fmt_row({"number": 4, "title": "x", "missing": ["priority"],
                           "priority_hint": {"tier": None, "ambiguous": ["P1", "P2"]}})
    assert "ambiguous P1/P2" in row
    assert "ask" in row


# --- bucket 8: dependency edges --------------------------------------------------------------


def test_edges_extraction_reads_backlog_check_block_re_live_not_a_copy(monkeypatch):
    """Amendment 1(a): the reviewer PROVED `triage._BLOCK_RE is backlog_check._BLOCK_RE` is
    vacuous -- re.compile caches, so a hand-retyped identical pattern IS the same object. This is
    the replacement: patch triage's OWN loaded `backlog_check` module's `_BLOCK_RE` to a sentinel
    the real vocabulary would never match, and prove extraction follows the sentinel LIVE."""
    triage = _mod("triage")
    sentinel = re.compile(r"(SENTINEL-EDGE)\s+#(\d+)")   # same 2-group shape _BLOCK_RE itself has
    monkeypatch.setattr(triage.backlog_check, "_BLOCK_RE", sentinel)
    issues = [_issue(1, body="SENTINEL-EDGE #2"), _issue(2)]
    out = triage._bucket_edges(issues, [])
    assert out["items"] == [{"blocked": "1", "by": "2", "source": "explicit"}]
    # and the REAL vocabulary, which the sentinel does not match, now produces nothing
    issues2 = [_issue(3, body="blocked by #2"), _issue(2)]
    assert triage._bucket_edges(issues2, [])["items"] == []


def _re_compile_call_bodies(src):
    """The full argument text of every `re.compile(...)` call in `src`, paren-balanced (tracks
    nesting depth character-by-character) rather than a `[^)]*` bracket class -- fold 5: the review
    proved `[^)]*` cannot cross the FIRST `)` it meets, and `_BLOCK_RE`'s own declaration opens
    with a `(?i)` group whose closing `)` comes before "blocked by" ever appears, so the bracket-
    class version could not even match a verbatim copy of the real pattern (proven: a literal copy
    of backlog_check.py's own `_BLOCK_RE = re.compile(...)` line passed the old check clean)."""
    calls = []
    for m in re.finditer(r"re\.compile\(", src):
        depth, i = 1, m.end()
        while i < len(src) and depth:
            depth += (src[i] == "(") - (src[i] == ")")
            i += 1
        calls.append(src[m.end():i - 1] if depth == 0 else src[m.end():i])
    return calls


def test_triage_module_never_redeclares_the_block_vocabulary():
    """Amendment 1(b) / fold 5: source-level belt-and-braces alongside the behavioral pin above.
    Two independent anchors, neither a bare substring (this module's own docstrings legitimately
    discuss "re.compile" and "blocked by" in separate sentences explaining why it does not
    redeclare them -- test_work.py's test_actionlog_module_has_no_ledger_import sets the
    anchored-not-substring precedent): (1) no `_BLOCK_RE`-named assignment (a redeclared constant);
    (2) no `re.compile(...)` call ANYWHERE carries the block vocabulary in its own argument,
    scanned paren-balanced so it cannot be evaded by copying the declaration under a different
    name (proven to catch a verbatim copy below, which the old bracket-class check could not)."""
    src = (S / "triage.py").read_text(encoding="utf-8")
    assert not re.search(r"_BLOCK_RE\w*\s*=", src), "triage.py must not (re)declare a _BLOCK_RE-named constant"
    vocab = re.compile(r"\b(blocked by|depends on|depends upon|requires|waiting on)\b")
    for call in _re_compile_call_bodies(src):
        assert not vocab.search(call), f"a re.compile() call carries the block vocabulary: {call!r}"


def test_block_vocabulary_guard_catches_a_verbatim_copy_of_the_real_declaration():
    """Mutation proof (RAILS rule 8): the OLD `[^)]*`-based guard passed a verbatim copy of
    backlog_check.py's own `_BLOCK_RE` line clean -- proving it here on a planted fixture, against
    the REAL checking function, not a re-description of the bug."""
    copy = ('_MY_OWN_COPY = re.compile(r"(?i)\\b(blocked by|depends on|depends upon|needs|after|'
            'requires|waiting on)\\b[^\\n#]{0,40}?#(\\d+)")')
    calls = _re_compile_call_bodies(copy)
    vocab = re.compile(r"\b(blocked by|depends on|depends upon|requires|waiting on)\b")
    assert any(vocab.search(c) for c in calls), "the paren-balanced scan failed to catch a verbatim copy"


def test_edges_explicit_only_counts_a_real_open_reference():
    triage = _mod("triage")
    issues = [_issue(3, body="blocked by #99")]     # #99 not in the fetched open set
    assert triage._bucket_edges(issues, [])["items"] == []


def test_edges_explicit_excludes_a_self_reference():
    """Amendment 3: the real rule (backlog_check.py _explicit_blockers) is `n != own_ref AND n in
    open_refs` -- an issue whose own body mentions its own number must not edge to itself."""
    triage = _mod("triage")
    issues = [_issue(5, body="blocked by #5")]
    assert triage._bucket_edges(issues, [])["items"] == []


def test_edges_explicit_dedupes_and_caps_per_issue():
    """Amendment 4: bound the noise -- one chatty body repeating the same reference, or citing many
    distinct ones, must not flood the bucket. Fold 10c: strengthened to an EXACT count (the review
    proved the old `<=`/no-duplicates-among-nothing shape passes vacuously on a totally broken
    extraction that returns []) -- 6 unique refs (#2 deduped) capped to _EDGE_CAP_PER_ISSUE=5."""
    triage = _mod("triage")
    body = "needs #2, needs #2 again, depends on #3, requires #4, after #5, waiting on #6, blocked by #7"
    issues = [_issue(1, body=body)] + [_issue(n) for n in range(2, 10)]
    out = triage._bucket_edges(issues, [])["items"]
    pairs = [(e["blocked"], e["by"]) for e in out]
    assert len(pairs) == triage._EDGE_CAP_PER_ISSUE, (
        "expected exactly the capped count of distinct edges, not fewer (extraction may be "
        "broken) or more (the cap did not hold)")
    assert len(pairs) == len(set(pairs)), "duplicate (blocked, by) pair was not deduped"


def test_edges_from_ledger_handoffs():
    triage = _mod("triage")
    entries = [{"kind": "handoff", "goal": "10", "issue": 20, "to": "rae", "state": "open"}]
    out = triage._bucket_edges([], entries)["items"]
    assert {"blocked": "10", "by": "20", "source": "ledger"} in out


def test_ledger_edges_skips_self_edges_when_handoff_key_falls_back_to_goal():
    """PR-review finding 2: `ledger.handoff_key()` falls back to the goal itself when `issue` is
    absent (the default for a source without create_dependency, or a failed create -- handoff.py's
    own create_tracked_issue writes issue=None in exactly that case). Without a guard, that
    produces a SELF-edge {'blocked': '42', 'by': '42'} -- meaningless, and for any source lacking
    create_dependency (e.g. every local-mode hand-off) this fires on EVERY outstanding hand-off."""
    triage = _mod("triage")
    entries = [{"kind": "handoff", "goal": "42", "to": "rae", "state": "open"}]   # no "issue" at all
    assert triage.ledger.handoff_key(entries[0]) == "42"    # confirms the fallback this test targets
    out = triage._bucket_edges([], entries)["items"]
    assert out == []


# --------------------------------------------------------------------------- bucket 9 + survey()


def test_context_reports_the_configured_knobs(tmp_path):
    triage = _mod("triage")
    base = tmp_path / ".sdlc"; (base / "context").mkdir(parents=True)
    (base / "context" / "north-star.md").write_text("# bets\n")
    config = {"parallel": {"goals": {"enabled": True, "max_concurrent": 5}},
              "backlog_check": {"enabled": True}, "goal_decompose": {"enabled": False},
              "ledger": {"enabled": True},
              "gates": {"hard_plan_gate": {"enabled": True}, "stop_gate": {"enabled": False}}}
    out = triage._bucket_context(str(base), config)
    assert out["north_star"] is True
    assert out["parallel_goals"] == {"enabled": True, "max_concurrent": 5}
    assert out["backlog_check_enabled"] is True
    assert out["goal_decompose_enabled"] is False
    assert out["ledger_enabled"] is True
    assert out["gates"] == {"hard_plan_gate": True, "stop_gate": False}


def test_context_defaults_when_nothing_is_configured(tmp_path):
    triage = _mod("triage")
    out = triage._bucket_context(str(tmp_path / ".sdlc"), {})
    assert out["north_star"] is False
    assert out["parallel_goals"]["max_concurrent"] == 3   # loop.goals_parallel's own default


def test_context_gates_read_generously_like_f17s_verify_enforce():
    """#416: `_bucket_context`'s own `gates.hard_plan_gate.enabled` / `gates.stop_gate.enabled`
    reads predate this bucket -- discovered fresh (not in the original issue's own location list,
    filed before this bucket existed) via the same fragile `... is True` pattern F17/#342 fixed for
    verify.enforce and #416 already fixed in hooks/plan_gate.sh, hooks/completion_gate.sh, and
    doctor.py's dashboard row. Fixed here for consistency with those three -- a survey reading
    `gates.hard_plan_gate: false` while the hook it describes is actually ON (enabled: 1/"true")
    would be exactly the doctor.py-vs-hooks disagreement `_gate_enabled`'s own docstring warns
    against, just relocated to a fourth surface. Deliberately NOT extended to the other `is True`
    reads in this same function (`parallel_goals.enabled`, `backlog_check_enabled`,
    `goal_decompose_enabled`) -- those are FEATURE flags, not hard DENY gates, and strict `is True`
    is the CORRECT direction for them (mirrors `ledger.enabled()`'s own documented intentional
    strictness: a stray truthy value must not silently switch a team/automation surface ON)."""
    triage = _mod("triage")
    config = {"gates": {"hard_plan_gate": {"enabled": 1}, "stop_gate": {"enabled": "true"}}}
    out = triage._bucket_context("/nonexistent", config)
    assert out["gates"] == {"hard_plan_gate": True, "stop_gate": True}


def test_survey_json_enqueued_priority_field_reflects_the_configured_prefix(monkeypatch):
    """End-to-end (closes the loop from config to JSON): a `sev:` prefix configured at survey()'s
    own top level round-trips all the way to the enqueued bucket's displayed "priority" field."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    with tempfile.TemporaryDirectory() as d:
        base = _gh_sdlc(d, priority_label_prefix="sev:")
        issues = [{"number": 3, "title": "x", "body": "",
                  "labels": [{"name": "sdlc:goal"}, {"name": "sev:P2"}], "assignees": []}]
        run = _recording_runner({"list": json.dumps(issues)})
        pack = triage.survey(str(base), run=run)
    assert pack["buckets"]["enqueued"]["items"][0]["priority"] == "P2"


def test_full_survey_still_green_with_no_project_block_at_all(monkeypatch):
    """Regression, the other half of "no behavior change when config carries the old defaults":
    a bare discovery.github config (no order, no priority_label_prefix, no project) produces the
    IDENTICAL enqueued content a pre-#706 run would have -- the common case where the defaults are
    simply ABSENT rather than explicitly pinned to the old value (the sibling test above pins the
    latter for github mode; the local-mode equivalent is already covered by
    test_local_mode_active_and_parked_by_status)."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    with tempfile.TemporaryDirectory() as d:
        base = _gh_sdlc(d)   # no order/prefix/project keys at all
        issues = [_issue(7, labels=[_GOAL_L]), _issue(3, labels=[_GOAL_L]),
                  _issue(9, labels=[_GOAL_L, _PROG_L]), _issue(2, labels=[_GOAL_L, _PARKED_L])]
        run = _recording_runner({"list": json.dumps(issues)})
        pack = triage.survey(str(base), run=run)
    assert [i["number"] for i in pack["buckets"]["enqueued"]["items"]] == [3, 7]
    assert pack["buckets"]["enqueued"]["degraded"] == []


def test_saturation_flag_propagates_to_every_fetch_derived_bucket(monkeypatch):
    """Amendment 2 (updated for #706 §2c): a fetch that saturates the live ceiling must flag EVERY
    bucket sourced from it, and render() must surface it. `_BOARD_ITEM_LIMIT` patched small (well
    under `_PICKER_WINDOW` = 200) so this test stays about saturation alone, not F1's separate
    beyond-picker-window marker."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BOARD_ITEM_LIMIT", 5)
    with tempfile.TemporaryDirectory() as d:
        base = _gh_sdlc(d)
        # gh CLI's own shape: labels come back as [{"name": ...}]
        issues = [{"number": i, "title": "x", "body": "", "labels": [{"name": "sdlc:goal"}],
                  "assignees": []} for i in range(5)]
        run = _recording_runner({"list": json.dumps(issues)})
        pack = triage.survey(str(base), run=run)
        for name in ("active", "parked", "enqueued", "shadow", "epics", "hygiene", "edges"):
            assert "truncated" in pack["buckets"][name]["degraded"], name
        assert "truncated" in triage.render(pack)


def test_bucket_membership_matrix_is_mutually_exclusive_for_parked_and_epic_rows(monkeypatch):
    """PR-review finding 4, end-to-end through survey(): a parked-and-assigned issue and an
    epic-and-assigned issue must each land in exactly ONE bucket -- their own -- never ALSO in
    shadow. A genuinely unlabelled-and-assigned issue is the control: it belongs in shadow only."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    issues = [
        _issue(1, labels=[_PARKED_L, _PROG_L], assignees=["dana"]),   # parked row
        _issue(2, labels=["sdlc:decompose"], assignees=["dana"]),      # epic row
        _issue(3, labels=[], assignees=["dana"]),                      # genuine #623 shadow row
    ]
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True); (base / "state").mkdir()
        (base / "config.json").write_text(json.dumps(
            {"discovery": {"source": "github", "github": {"repo": "acme/widget", "assignee": "dana"}},
             "ledger": {"actor": "dana"}}))   # so `me` resolves to "dana" without a real gh call
        run = _recording_runner({"list": json.dumps(issues)})
        pack = triage.survey(str(base), run=run)
    membership = {
        n: sorted(name for name in ("active", "parked", "enqueued", "shadow", "epics", "hygiene")
                  if n in {it["number"] for it in pack["buckets"][name]["items"]})
        for n in (1, 2, 3)
    }
    assert membership[1] == ["parked"], membership[1]
    assert membership[2] == ["epics"], membership[2]
    assert membership[3] == ["shadow"], membership[3]


def test_one_bucket_failing_does_not_blind_the_others(monkeypatch):
    """Amendment 8: force the failure by monkeypatching a bucket function to raise -- a malformed
    ledger line CANNOT produce this (read_all tolerates and skips bad lines, proven by the
    reviewer); the fail-open contract can only be proven by actually breaking a bucket."""
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_sdlc(d)
        def boom(*a, **k):
            raise RuntimeError("synthetic bucket failure")
        monkeypatch.setattr(triage, "_bucket_hygiene", boom)
        run = _recording_runner({"list": "[]"})
        monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
        pack = triage.survey(str(base), run=run)
        assert pack["buckets"]["hygiene"]["degraded"] == ["error: synthetic bucket failure"]
        assert pack["buckets"]["hygiene"]["items"] == []
        for name in ("inbox", "active", "parked", "enqueued", "shadow", "epics", "edges", "context"):
            assert "error" not in " ".join(pack["buckets"][name].get("degraded") or [])


def test_survey_emits_no_ledger_writes(monkeypatch):
    """Amendment 9: proven against a ledger-ENABLED, WRITABLE fixture -- a fixture with the ledger
    off proves nothing (nothing could have been written regardless). Fold 10b: the prior wording
    here claimed `test_vocabulary_coverage.py` "already proves zero ledger.append call SITES exist
    structurally" -- FALSE, disproven directly (a real `ledger.safe_append(...)` ENTRIES-stream
    call, no `stream=ledger.EVENTS`, added to triage.py still passes that guard clean): that guard
    is scoped to the EVENTS-stream kind vocabulary only (Direction A/B over `ledger.EVENT_KINDS`),
    not "does any ledger write exist at all" -- it gives this module ZERO protection against an
    accidental ENTRIES-stream write (e.g. a copy-pasted `loop.py`-style "claimed"/"done"/"parked"
    call, the most plausible accident here). This RUNTIME test is therefore the ONLY thing that
    actually proves the no-write contract, not a "complement" to a static guard that never covered
    it."""
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _ledger_sdlc(d, actor="dana")
        triage.ledger.safe_append(str(base), "claimed", "10", config=triage._config(str(base)))
        before = triage.ledger.read_all(str(base))
        assert before   # the fixture genuinely has a writable, non-empty ledger
        before_files = sorted(p.name for p in (base / "ledger" / "entries").glob("*.jsonl"))
        monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
        run = _recording_runner({"list": "[]"})
        triage.survey(str(base), run=run)
        after = triage.ledger.read_all(str(base))
        after_files = sorted(p.name for p in (base / "ledger" / "entries").glob("*.jsonl"))
        assert after == before
        assert after_files == before_files


def test_survey_returns_all_nine_bucket_keys_even_when_gh_is_unavailable():
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_sdlc(d)
        def run(a):
            raise RuntimeError("gh: HTTP 404 Not Found")
        pack = triage.survey(str(base), run=run)
        assert set(pack["buckets"]) == {"inbox", "active", "parked", "enqueued", "shadow",
                                        "epics", "hygiene", "edges", "context"}
        assert pack["buckets"]["active"]["degraded"] == ["gh_unavailable"]


# --------------------------------------------------------------------------- local mode


def _local_sdlc(d, goals):
    """goals: [(id, status, title, body), ...] or [(id, status, title, body, priority), ...] --
    the 5-tuple form is #706's addition (local-mode enqueued ordering); every pre-existing 4-tuple
    call site is unaffected, priority frontmatter simply absent as before."""
    base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True); (base / "state").mkdir()
    (base / "config.json").write_text(json.dumps({"discovery": {"source": "local-goals"}}))
    for g in goals:
        gid, status, title, body = g[0], g[1], g[2], g[3]
        priority_line = f"priority: {g[4]}\n" if len(g) > 4 and g[4] else ""
        (base / "goals" / f"{gid}.md").write_text(
            f"---\nid: {gid}\nstatus: {status}\ntitle: {title}\n{priority_line}---\n{body}\n")
    return base


def test_local_mode_active_and_parked_by_status():
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "in_progress", "A", ""), ("0002", "parked", "B", ""),
                               ("0003", "pending", "C", "")])
        pack = triage.survey(str(base), run=lambda a: "")   # hermetic: never shell out to real gh
        assert pack["mode"] == "local"
        assert len(pack["buckets"]["active"]["items"]) == 1
        assert len(pack["buckets"]["parked"]["items"]) == 1
        assert len(pack["buckets"]["enqueued"]["items"]) == 1   # only the pending one


def test_local_mode_enqueued_sorts_by_priority_by_default():
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "pending", "A", "", "P4"), ("0002", "pending", "B", "", "P0")])
        pack = triage.survey(str(base), run=lambda a: "")   # hermetic: never shell out to real gh
    refs = [i["ref"] for i in pack["buckets"]["enqueued"]["items"]]
    assert refs[0].endswith("0002.md"), "P0 (0002) must sort before P4 (0001) despite filename order"


def test_local_mode_enqueued_created_order_matches_pre_706_behavior_exactly():
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "pending", "A", "", "P4"), ("0002", "pending", "B", "", "P0")])
        cfg = json.loads((base / "config.json").read_text())
        cfg["discovery"]["order"] = "created"
        (base / "config.json").write_text(json.dumps(cfg))
        pack = triage.survey(str(base), run=lambda a: "")
    refs = [i["ref"] for i in pack["buckets"]["enqueued"]["items"]]
    assert refs[0].endswith("0001.md"), "created order must ignore priority and use filename order"


def test_local_mode_enqueued_resolves_configured_priority_aliases():
    """Independent-review finding on #854: `_local_bucket` never received `priority_aliases`, so a
    local goal filed `priority: Critical` sorted UNPRIORITISED in survey's own enqueued bucket even
    though `next_pending` (LocalSource, already alias-aware) would have picked it first -- survey
    and the live picker disagreeing about the SAME goal's urgency."""
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "pending", "A", "", "P1"), ("0002", "pending", "B", "", "Critical")])
        cfg = {"discovery": {"source": "local-goals", "priority_aliases": {"critical": "P0"}}}
        pack = triage.survey(str(base), config=cfg, run=lambda a: "")
    refs = [i["ref"] for i in pack["buckets"]["enqueued"]["items"]]
    assert refs[0].endswith("0002.md"), "Critical (aliased to P0) must sort before P1"


def test_local_mode_enqueued_unset_aliases_leaves_english_priorities_unranked():
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "pending", "A", "", "P1"), ("0002", "pending", "B", "", "Critical")])
        pack = triage.survey(str(base), run=lambda a: "")
    refs = [i["ref"] for i in pack["buckets"]["enqueued"]["items"]]
    assert refs[0].endswith("0001.md"), "unaliased 'Critical' must sort last, unchanged"


def test_local_mode_enqueued_priority_rank_is_lockstep_with_discovery(monkeypatch):
    """F4: the copied `(priority_rank, ref)` tuple shape is the one genuinely-duplicated piece in
    this design (no bound method to call the way `_pick_key` gives github mode) -- an
    identity/equality check on the imported names alone would only prove the names were imported,
    not that the local sort actually reads them at call time. Mutation-style instead (mirrors the
    github-mode flagship pin, test_bucket_enqueued_reads_pick_key_live_not_a_copy): monkeypatch
    discovery's OWN priority_rank to invert the ranking and prove the local enqueued order follows
    it live."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.discovery, "priority_rank",
                        lambda v, aliases=None: {"p0": 9, "p4": 0}.get(
                            str(v).strip().lower(), triage.discovery.UNPRIORITISED))
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "pending", "A", "", "P0"), ("0002", "pending", "B", "", "P4")])
        pack = triage.survey(str(base), run=lambda a: "")
    refs = [i["ref"] for i in pack["buckets"]["enqueued"]["items"]]
    assert refs[0].endswith("0002.md"), (
        "P4 (sentinel rank 0) must sort BEFORE P0 (sentinel rank 9) once discovery.priority_rank is "
        "monkeypatched -- if this still passed under the REAL priority_rank's own ordering, the "
        "copied tuple shape is not actually reading the live function")


def test_local_mode_shadow_and_epics_are_not_applicable():
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "pending", "A", "")])
        pack = triage.survey(str(base), run=lambda a: "")   # hermetic: never shell out to real gh
        assert pack["buckets"]["shadow"] == {"items": [], "count": 0,
                                             "degraded": ["not_applicable_local_mode"]}
        assert pack["buckets"]["hygiene"] == {"items": [], "count": 0,
                                              "degraded": ["not_applicable_local_mode"]}
        assert pack["buckets"]["epics"]["degraded"] == ["not_applicable_local_mode"]


def test_local_mode_edges_is_not_applicable(monkeypatch):
    """PR-review finding 3: `_local_goal_docs` used a full FILE PATH as `ref` while `_BLOCK_RE`
    only ever captures a bare digit string -- `n not in open_refs` could never pass, so the bucket
    was structurally incapable of ever finding an edge while still reporting `degraded: []`
    (falsely healthy: the earlier vacuous test only asserted the scanner was CALLED, not that it
    found anything -- its own fixture, "blocked by #0002" against a path-shaped ref, could never
    match, proven below by calling the real scanner directly on that exact shape). Honest fix:
    local-mode edges reports `not_applicable_local_mode`, the same pattern shadow/hygiene/epics
    already use, and never calls the shared scanner at all -- proven with a spy."""
    triage = _mod("triage")
    # the old fixture's own claim, disproven directly: a path-shaped ref can never match _BLOCK_RE's
    # bare-digit capture, so calling the real scanner on it was always going to find nothing.
    dead_docs = [{"ref": "/tmp/x/.sdlc/goals/0001.md", "title": "A", "body": "blocked by #0002"},
                 {"ref": "/tmp/x/.sdlc/goals/0002.md", "title": "B", "body": ""}]
    assert triage._scan_block_edges(dead_docs) == []

    calls = []
    monkeypatch.setattr(triage, "_scan_block_edges", lambda docs: calls.append(docs) or [])
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "pending", "A", "blocked by #0002"),
                               ("0002", "pending", "B", "")])
        pack = triage.survey(str(base), run=lambda a: "")   # hermetic: never shell out to real gh
    assert pack["buckets"]["edges"] == {"items": [], "count": 0, "degraded": ["not_applicable_local_mode"]}
    assert calls == [], "_local_bucket('edges', ...) must not call the structurally-dead scan path"


def test_local_mode_active_item_carries_priority_from_frontmatter():
    """#839: found during independent review of #714/PR #835 -- `_local_bucket` built every
    active/parked/enqueued item from `ref`/`title`/etc alone, never the goal's own `priority:`
    frontmatter key, even though `_local_goal_docs` (this bucket's own upstream reader) already
    reads it into `docs[]["priority"]` per-goal. Whatever consumes a bucket item's "priority" field
    -- survey's own JSON output today, `plan`'s wave-ordering sort once fed one (see the
    `compile_plan` test below) -- saw `None` for every local-mode goal regardless of its real tier."""
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "in_progress", "A", "", "P1")])
        pack = triage.survey(str(base), run=lambda a: "")
    assert pack["buckets"]["active"]["items"][0]["priority"] == "P1"


def test_local_mode_parked_item_carries_priority_from_frontmatter():
    """#839, parked bucket's own half of the same gap."""
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "parked", "A", "", "P2")])
        pack = triage.survey(str(base), run=lambda a: "")
    assert pack["buckets"]["parked"]["items"][0]["priority"] == "P2"


def test_local_mode_enqueued_item_carries_priority_from_frontmatter():
    """#839, enqueued bucket's own half -- distinct from the pre-existing priority-ORDER tests above
    (test_local_mode_enqueued_sorts_by_priority_by_default etc.): those only ever proved the
    bucket's own internal SORT reads priority correctly, never that the emitted ITEM carries the
    value a downstream consumer (like `plan`, see below) could actually read back out."""
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "pending", "A", "", "P0")])
        pack = triage.survey(str(base), run=lambda a: "")
    assert pack["buckets"]["enqueued"]["items"][0]["priority"] == "P0"


def test_local_mode_bucket_items_priority_is_none_when_frontmatter_omits_it():
    """A goal with no `priority:` frontmatter line at all -- the pre-#698 default shape most local
    repos still have -- must carry an explicit `None`, not a missing key a naive `.get("priority")`
    downstream could confuse with "field doesn't exist on this item shape"."""
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "in_progress", "A", ""), ("0002", "parked", "B", ""),
                               ("0003", "pending", "C", "")])
        pack = triage.survey(str(base), run=lambda a: "")
    assert pack["buckets"]["active"]["items"][0]["priority"] is None
    assert pack["buckets"]["parked"]["items"][0]["priority"] is None
    assert pack["buckets"]["enqueued"]["items"][0]["priority"] is None


def test_local_mode_high_priority_goal_wave_orders_ahead_in_plan_real_output():
    """#839's own acceptance bar: not just that a bucket item carries "priority", but that the value
    actually changes `triage.py plan`'s real wave order for a local-mode goal.

    Drives `compile_plan` directly -- the exact function `plan_cmd` (the `plan` CLI verb) calls, and
    this suite's own established way to test `plan`'s compiler without going through argv parsing
    (see `_wave_fixture` above) -- because `plan`'s `--pick`/`_survey_index` are GitHub-issue-number-
    only by design (`_parse_pick` rejects any non-digit token; `_survey_index` only ever keys on a
    bucket item's "number", which local items never have -- see `_survey_edges`'s own docstring,
    "plan's own domain is GitHub issue numbers only"). `extra_index` is `compile_plan`'s own
    documented caller-supplied-dict extension point (#713) -- exactly the shape a caller who already
    ran `survey` and wants to feed its bucket items straight into a plan would build, keyed by
    whatever `picked` uses; local mode's natural id is the goal's own `ref`, and both `_build_nodes`
    and `schedule_waves` are id-type-agnostic (never assume an int), so a `ref` string works exactly
    like a github issue number does everywhere else in this suite.

    Also directly reuses `discovery.priority_of` (#839's own explicit ask, over reimplementing
    frontmatter-priority parsing a second time) as an independent cross-check: proves the raw value
    survey now attaches to a local item ranks IDENTICALLY to what the canonical frontmatter-priority
    reader computes for that same goal file -- the same two-readings-must-agree shape this file's
    other lockstep tests already use (e.g. test_local_mode_enqueued_priority_rank_is_lockstep_with_
    discovery). Note `priority_of` itself returns an already-RANKED int (0-4, or UNPRIORITISED),
    not the raw label -- so the fix stores the raw frontmatter value (what every other bucket's own
    "priority" field already holds, and what `discovery.priority_rank`/`_priority_rank` downstream
    expect to rank themselves), and this test proves the two readings agree rather than assigning
    `priority_of`'s own return value into the item (which would hand `schedule_waves` an int it
    would then re-rank as a STRING -- and, for a P0 goal specifically, `str(0 or "")` is `""` via
    Python's falsy-zero, silently colliding with "no priority at all")."""
    triage = _mod("triage")
    with tempfile.TemporaryDirectory() as d:
        base = _local_sdlc(d, [("0001", "pending", "Low", "", "P4"),
                               ("0002", "pending", "High", "", "P0")])
        pack = triage.survey(str(base), run=lambda a: "")
        items = {i["ref"]: i for i in pack["buckets"]["enqueued"]["items"]}
        low_ref = next(r for r in items if r.endswith("0001.md"))
        high_ref = next(r for r in items if r.endswith("0002.md"))

        # cross-check against the canonical reader -- see docstring above.
        assert (triage.discovery.priority_rank(items[low_ref]["priority"])
                == triage.discovery.priority_of(low_ref))
        assert (triage.discovery.priority_rank(items[high_ref]["priority"])
                == triage.discovery.priority_of(high_ref))

        extra_index = {ref: {"title": item["title"], "priority": item["priority"], "model": None}
                       for ref, item in items.items()}
        result = triage.compile_plan([low_ref, high_ref], [], [], 1, extra_index=extra_index)

    assert result["cycle_error"] is None
    order = [wave[0]["id"] for wave in result["waves"]]
    assert order.index(high_ref) < order.index(low_ref), (
        "a P0 local goal must wave-order ahead of a P4 local goal in plan's real output")


# --------------------------------------------------------------------------- render() + CLI + --json


def test_human_table_renders_all_nine_bucket_headers(monkeypatch):
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    with tempfile.TemporaryDirectory() as d:
        base = _gh_sdlc(d)
        pack = triage.survey(str(base), run=_recording_runner({"list": "[]"}))
        out = triage.render(pack)
    for title in ("Inbox", "Active", "Parked/blocked", "Enqueued backlog", "Shadow backlog",
                  "Epics / needs-decomposition", "Hygiene gaps", "Dependency edges", "Context"):
        assert f"## {title}" in out


def test_render_shows_orphaned_worktrees_and_review_queue_for_parked():
    """Fold 6: the issue spec names three parked-bucket surfaces (park reasons, review-queue.md
    entries, orphaned worktrees) -- render() only ever showed the first, so the printed count could
    contradict what the bucket actually knows (e.g. "(0)" while an orphaned worktree sits there).
    All three must reach the human table."""
    triage = _mod("triage")
    pack = {"schema": triage.SCHEMA, "generated_at": "2026-01-01T00:00:00Z", "mode": "github",
            "buckets": {"inbox": {"items": [], "count": 0, "degraded": []},
                        "active": {"items": [], "count": 0, "degraded": []},
                        "parked": {"items": [], "count": 0, "degraded": [],
                                   "orphaned_worktrees": ["77"], "review_queue": ["0099-old-goal.md"]},
                        "enqueued": {"items": [], "count": 0, "degraded": []},
                        "shadow": {"items": [], "count": 0, "degraded": []},
                        "epics": {"items": [], "count": 0, "degraded": []},
                        "hygiene": {"items": [], "count": 0, "degraded": []},
                        "edges": {"items": [], "count": 0, "degraded": []},
                        "context": {"degraded": []}}}
    out = triage.render(pack)
    assert "77" in out
    assert "0099-old-goal.md" in out


def test_json_output_has_stable_bucket_keys_and_generated_at(monkeypatch):
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    with tempfile.TemporaryDirectory() as d:
        base = _gh_sdlc(d)
        pack = triage.survey(str(base), run=_recording_runner({"list": "[]"}), now=1700000000)
    assert pack["schema"] == triage.SCHEMA
    assert pack["generated_at"] == "2023-11-14T22:13:20Z"
    assert set(pack["buckets"]) == {"inbox", "active", "parked", "enqueued", "shadow",
                                    "epics", "hygiene", "edges", "context"}


def test_cli_survey_json_round_trips_and_is_valid_json(capsys, monkeypatch):
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    with tempfile.TemporaryDirectory() as d:
        base = _gh_sdlc(d)
        monkeypatch.setattr(triage.sources, "_run_gh", lambda a: "[]")
        rc = triage.main(["triage.py", "survey", str(base), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["schema"] == triage.SCHEMA


def test_cli_survey_default_is_the_human_table(capsys, monkeypatch):
    triage = _mod("triage")
    monkeypatch.setattr(triage.sources.GitHubSource, "_BACKLOG_READ_RETRY_BASE", 0)
    with tempfile.TemporaryDirectory() as d:
        base = _gh_sdlc(d)
        monkeypatch.setattr(triage.sources, "_run_gh", lambda a: "[]")
        rc = triage.main(["triage.py", "survey", str(base)])
    assert rc == 0
    assert "## Inbox" in capsys.readouterr().out


def test_cli_usage_on_an_unknown_verb(capsys):
    triage = _mod("triage")
    rc = triage.main(["triage.py", "bogus"])
    assert rc == 2
    assert "usage" in capsys.readouterr().err


# ===================================================================================================
# #669: the `plan` compiler (sigma-triage 2/4) -- pure compile, zero network, zero ledger writes BY
# DEFAULT. #713 added the one opt-in exception (--resolve-missing); its own tests, further down,
# use a dedicated per-issue-number-keyed fake (_view_run/_issue_view_json), not _recording_runner
# (whose per-VERB-only keying cannot distinguish different `gh issue view <N>` calls from each
# other -- same reasoning as `enact`'s own `_enact_runner` below, independently arrived at).
# Fixture reuse note (669-plan.md §9): _mod/_recording_runner/_gh_sdlc above are irrelevant to
# every OTHER `plan` test -- new local helpers only.
# ===================================================================================================


def _plan_sdlc(d):
    """A bare .sdlc/plans/ tree -- `plan` is a pure verb, no discovery.source config assumed."""
    base = pathlib.Path(d) / ".sdlc"
    (base / "plans").mkdir(parents=True)
    return base


def _survey_fixture(buckets):
    """A --from-survey-shaped dict matching PLAN_SCHEMA's sibling schema `triage-survey/v1`.
    `buckets`: {name: [item, ...]} -- only the buckets a given test cares about need be present."""
    return {"schema": "triage-survey/v1", "generated_at": "2026-01-01T00:00:00Z", "mode": "github",
           "buckets": {name: {"items": items, "count": len(items), "degraded": []}
                       for name, items in buckets.items()}}


def _write_survey(path, buckets):
    path.write_text(json.dumps(_survey_fixture(buckets)))
    return str(path)


def _wave_fixture(triage, picked, edges=(), defer=(), cap=3, survey=None, now=0):
    """Shared builder feeding the algorithmic-core tests (#669 review F8 lever 2 / plan §9 items
    8-19) -- drives `compile_plan` directly with tuple-shaped args (not raw CLI strings), since
    these tests exercise the compiler, not the CLI-parsing layer (already covered above)."""
    return triage.compile_plan(picked, list(edges), list(defer), cap, survey=survey, now=now)


# --- CLI parsing (plan §9 tests 1-7) --------------------------------------------------------------


def test_parse_pick_splits_dedupes_and_preserves_order():
    triage = _mod("triage")
    assert triage._parse_pick("20,12,20,15") == [20, 12, 15]


def test_parse_pick_rejects_a_non_numeric_token_naming_it():
    triage = _mod("triage")
    with pytest.raises(ValueError, match="abc"):
        triage._parse_pick("12,abc")


@pytest.mark.parametrize("raw", ["1512", "15:12:99", "abc:12", "15:abc", ""])
def test_parse_edge_rejects_malformed_shapes(raw):
    """#669 review F8: parametrized over the four malformed shapes (no colon, two colons, non-digit
    BLOCKED side, non-digit BY side, empty string) -- house style already does this exact shape
    (test_hook.py, test_decompose_check.py). Each must raise; a non-empty raw input must be named
    in the message (an empty string has nothing to name)."""
    triage = _mod("triage")
    with pytest.raises(ValueError) as exc_info:
        triage._parse_edge(raw)
    if raw:
        assert raw in str(exc_info.value)


def test_parse_defer_splits_on_first_colon_only():
    triage = _mod("triage")
    assert triage._parse_defer("40:needs design: ask rae") == (40, "needs design: ask rae")


def test_parse_defer_requires_a_non_empty_reason():
    triage = _mod("triage")
    with pytest.raises(ValueError, match="40"):
        triage._parse_defer("40:")


def test_plan_argv_edge_and_defer_accumulate_repeats():
    """Proves the repeat-collection gap is actually CLOSED here, not just described. The real,
    demonstrated defect (#669 review F1 -- verified live against both actual modules, correcting
    the plan's own inverted claim about which copy is "the fixed one"): BOTH existing `_flags()`
    copies (`ledger.py`, `slices.py`) return a flat `dict`, so a second `--edge` silently overwrites
    the first regardless of which copy you pick -- `ledger._flags(['--edge','15:12','--edge',
    '20:15'])` also collapses to `{'edge': '20:15'}`, the identical loss `slices.py`'s copy shows."""
    triage = _mod("triage")
    parsed = triage._parse_plan_argv(["--edge", "15:12", "--edge", "20:15"])
    assert parsed["edge"] == ["15:12", "20:15"]


def test_plan_argv_value_starting_with_dashes_is_never_misparsed():
    """A slug that itself looks like a flag is consumed as ITS OWN value, never silently dropped.
    `_parse_plan_argv` treats EVERY one of `plan`'s own flags as a known value-flag, consumed
    unconditionally -- there is no bare/boolean flag on this verb at all, so the "guess from
    whether the next token starts with --" branch that causes the value-list-breadth bug in BOTH
    existing `_flags()` copies (#669 review F1 -- corrected provenance, see the prior test's own
    docstring) never needs to exist here in the first place."""
    triage = _mod("triage")
    parsed = triage._parse_plan_argv(["--slug", "--weird"])
    assert parsed["slug"] == "--weird"


def test_plan_argv_reads_resolve_missing():
    """#713: --resolve-missing is a VALUE flag like every other one on this verb (module-level
    comment above _PLAN_VALUE_FLAGS: no bare/boolean flag exists here at all) -- the hyphenated CLI
    name renames to the underscored dict key, same as --from-survey."""
    triage = _mod("triage")
    parsed = triage._parse_plan_argv(["--pick", "10", "--resolve-missing", "5"])
    assert parsed["resolve_missing"] == "5"


def test_plan_argv_resolve_missing_defaults_to_none_when_absent():
    triage = _mod("triage")
    parsed = triage._parse_plan_argv(["--pick", "10"])
    assert parsed["resolve_missing"] is None


# --- the algorithmic core: _cycles / _priority_rank / schedule_waves, hand-built node lists,
# no CLI, no survey, no file I/O (plan §9 items 8[cycle half]/11/12/13/14 + review F3/F6) -----------


def _node(nid, needs=(), priority=None, model=None):
    return {"id": nid, "needs": list(needs), "priority": priority, "model": model, "title": f"#{nid}"}


def test_cycles_names_every_member_excluding_independent_nodes():
    """#669 review F6: with picks {10,11,12} ALL in the cycle, an implementation that simply lists
    every picked issue would pass vacuously. Issue 13 is independent (unblocked, no edges at all)
    and must be ABSENT from the reported cycle."""
    triage = _mod("triage")
    nodes = [_node(10, needs=[12]), _node(11, needs=[10]), _node(12, needs=[11]), _node(13)]
    cycles = triage._cycles(nodes)
    assert len(cycles) == 1
    assert set(cycles[0]) == {10, 11, 12}
    assert 13 not in cycles[0]


def test_priority_rank_p0_through_p4_unchanged_by_the_delegation_to_discovery():
    """#714's own backward-compatibility invariant (the #698 hinge `discovery.priority_rank`'s own
    docstring documents): P0-P4 must rank EXACTLY 0-4, byte-identical to `_priority_rank`'s old,
    self-parsed regex result -- true both before and after `_priority_rank` starts delegating to
    `discovery.priority_rank` instead of maintaining its own second, wider-range parser. If this
    ever regresses, `plan`'s own wave order for the overwhelming common case (a P0-P4 label) would
    silently change alongside the P5-P9 fix, not just the divergent tail #714 is actually about."""
    triage = _mod("triage")
    for tier, expected in (("P0", 0), ("P1", 1), ("P2", 2), ("P3", 3), ("P4", 4)):
        assert triage._priority_rank(tier) == expected


def test_priority_rank_p5_through_p9_collapse_to_unprioritised():
    """#714: `triage._priority_rank` used to parse "P0".."P9" itself (regex `^P([0-9])$`, own
    fallback 999) -- a WIDER range than `discovery.priority_rank`'s P0-P4 (`UNPRIORITISED = 5`), the
    one the live picker and `survey`'s own enqueued-bucket sort both use. The mismatch started at
    P5: a `priority:P7`-labelled issue ranked 7th in `plan`'s own wave order -- ahead of every
    genuinely unprioritised goal -- while the live picker ranked that same label dead last, behind
    every P0-P4 goal. One board, two disagreeing answers to "how urgent is this".

    Bug repro, pinned as a permanent regression guard: before this fix, `triage._priority_rank("P7")`
    returned `7` (proved by running this exact assertion against unmodified code, #714's own
    RED-first step). Now `_priority_rank` delegates to `discovery.priority_rank` instead of keeping
    a second, independently-drifting implementation -- so P5-P9 fall through discovery's own
    `PRIORITIES` tuple just like a typo or an absent label would, and collapse to the SAME rank as a
    truly-unprioritised (None/unparseable) goal, matching the live picker/survey exactly."""
    triage = _mod("triage")
    # P5-P9 all collapse to the SAME rank as a truly-unprioritised goal (discovery.UNPRIORITISED,
    # 5) -- they must NOT keep ranking as their own digit (P7 -> 7, etc.), which is exactly the
    # #714 bug: that wider self-parsed range let a P5-P9 label jump ahead of unprioritised goals in
    # `plan`'s wave order while the live picker/survey ranked it dead last.
    for tier in ("P5", "P6", "P7", "P8", "P9"):
        assert triage._priority_rank(tier) == triage.discovery.UNPRIORITISED == 5


def test_priority_rank_resolves_a_configured_alias():
    """Independent-review finding: _priority_rank had no aliases parameter at all, so a
    priority_aliases-labelled issue (e.g. `priority:critical`) ranked UNPRIORITISED in `plan`'s own
    wave order while the live picker and survey's enqueued bucket (both already alias-aware via
    sources.py) would have ranked it correctly -- the exact "two places compute the same rank, one
    drifts" bug class #714 already fixed once for the P5-P9 case."""
    triage = _mod("triage")
    aliases = {"critical": "P0", "low": "P3"}
    assert triage._priority_rank("critical", aliases) == 0
    assert triage._priority_rank("Low", aliases) == 3
    assert triage._priority_rank("critical") == triage.discovery.UNPRIORITISED   # unset stays unset


def test_schedule_waves_resolves_configured_aliases():
    triage = _mod("triage")
    nodes = [_node(30, priority="critical"), _node(10, priority="P1")]
    aliases = {"critical": "P0"}
    waves = triage.schedule_waves(nodes, cap=3, aliases=aliases)
    assert [n["id"] for n in waves[0]] == [30, 10]     # the alias'd P0 goes first


def test_schedule_waves_unset_aliases_leaves_english_priorities_unranked():
    triage = _mod("triage")
    nodes = [_node(30, priority="critical"), _node(10, priority="P1")]
    waves = triage.schedule_waves(nodes, cap=3)
    assert [n["id"] for n in waves[0]] == [10, 30]     # "critical" unrecognised -> sorts last


def test_cap_width_respected_including_cap_one_forces_a_strict_chain():
    triage = _mod("triage")
    nodes = [_node(30, priority="P1"), _node(10, priority="P0"),
             _node(20, priority="P1"), _node(40, priority="P2")]   # all independent
    waves1 = [[n["id"] for n in w] for w in triage.schedule_waves(nodes, cap=1)]
    assert waves1 == [[10], [20], [30], [40]]
    waves3 = [[n["id"] for n in w] for w in triage.schedule_waves(nodes, cap=3)]
    assert waves3 == [[10, 20, 30], [40]]


def test_priority_then_number_tiebreak_within_a_wave():
    triage = _mod("triage")
    nodes = [_node(30, priority="P1"), _node(10, priority="P0"), _node(20, priority="P1")]
    waves = triage.schedule_waves(nodes, cap=3)
    assert [n["id"] for n in waves[0]] == [10, 20, 30]


def test_unresolvable_priority_sorts_last_not_erroring():
    """#669 review F6: vacuous with a single pick -- fixed to require >= 2 picks, at least one
    carrying a real priority, so "sorts last" is exercised relative to something, not trivially
    true of a wave with one member."""
    triage = _mod("triage")
    nodes = [_node(20, priority=None), _node(10, priority="P0")]
    waves = triage.schedule_waves(nodes, cap=3)
    assert [n["id"] for n in waves[0]] == [10, 20]


def test_edge_respected_across_waves():
    """Property-style: for every edge (a node's own "needs" entry), the blocker's wave index must
    be strictly less than the blocked's, across several generated (nodes, cap) combinations
    including a wide fan-in and a wide fan-out."""
    triage = _mod("triage")
    cases = [
        [_node(1), _node(2, needs=[1]), _node(3, needs=[2])],                      # chain
        [_node(1), _node(2), _node(3), _node(4, needs=[1, 2, 3])],                 # wide fan-in
        [_node(1), _node(2, needs=[1]), _node(3, needs=[1]), _node(4, needs=[1])], # wide fan-out
    ]
    for nodes in cases:
        for cap in (1, 2, 3, 10):
            waves = triage.schedule_waves(nodes, cap)
            wave_of = {n["id"]: i for i, w in enumerate(waves) for n in w}
            for n in nodes:
                for need in n["needs"]:
                    assert wave_of[need] < wave_of[n["id"]], (nodes, cap, n["id"], need)


# --- #900: blocker priority promotion -- schedule_waves' own EFFECTIVE-rank sort key, hand-built
# node lists, same "algorithmic core" scope as the aliases group above ----------------------------


def test_schedule_waves_explicit_off_matches_pre_900_order_and_default_is_the_full_rule():
    """(#1406: the default is now the full rule; explicit off is the pre-900 order.) Regression pin -- the single most important test in this slice. #10 (P2) blocks #30 (P0);
    #20 (P1) is unrelated and ready from the start. If blocker promotion were wired in and left ON
    by default, #10 would jump ahead of #20 (see test_blocker_promotion_always_mode_promotes_a_
    blocker_ahead_of_its_own_tier below, identical graph). It must NOT: `schedule_waves` called
    with `blocker_promotion_mode` omitted entirely (simulating every caller written before this
    parameter existed) produces the exact same wave order as calling it with "off" explicit --
    #10 stays at its own raw P2 rank, sorted strictly by (priority_rank, id), byte-identical to
    every wave order this function produced before #900."""
    triage = _mod("triage")
    nodes = [_node(10, priority="P2"), _node(20, priority="P1"), _node(30, needs=[10], priority="P0")]
    omitted = [n["id"] for w in triage.schedule_waves(nodes, cap=1) for n in w]
    explicit_off = [n["id"] for w in triage.schedule_waves(nodes, cap=1, blocker_promotion_mode="off")
                    for n in w]
    assert explicit_off == [20, 10, 30]  # #10 (P2) NOT promoted by #30's P0 -- natural priority order
    assert omitted == [10, 30, 20]       # #1406: the default is the full rule, #10 rides #30's P0


def test_schedule_waves_unrecognised_blocker_promotion_mode_behaves_like_off():
    """Same fail-safe-not-fail-loud posture `blocker_promotion_rank` itself documents, and every
    other config-adjacent helper in this codebase (`_order`, `_priority_aliases`) already keeps: a
    typo'd mode string must not silently activate promotion nobody asked for. In practice
    `sources._blocker_promotion` already normalises this before `plan_cmd` ever calls
    `schedule_waves` -- this pins the defensive fallback here too, for any other/future caller."""
    triage = _mod("triage")
    nodes = [_node(10, priority="P2"), _node(20, priority="P1"), _node(30, needs=[10], priority="P0")]
    waves = triage.schedule_waves(nodes, cap=1, blocker_promotion_mode="urgent-ish")
    assert [n["id"] for w in waves for n in w] == [20, 10, 30]


def test_blocker_promotion_always_mode_promotes_a_blocker_ahead_of_its_own_tier():
    """Same graph as the regression pin above, `blocker_promotion_mode="always"` instead: #10 (P2)
    directly blocks #30 (P0), so its EFFECTIVE rank becomes #30's P0 (0) -- more urgent than #20's
    own natural P1 (1) -- and #10 is scheduled first, ahead of #20, which "off" mode never does."""
    triage = _mod("triage")
    nodes = [_node(10, priority="P2"), _node(20, priority="P1"), _node(30, needs=[10], priority="P0")]
    waves = triage.schedule_waves(nodes, cap=1, blocker_promotion_mode="always")
    assert [n["id"] for w in waves for n in w] == [10, 30, 20]


def test_blocker_promotion_smart_mode_promotes_when_dependent_has_no_other_ready_work():
    """Same graph, `mode="smart"` instead of "always": #30 has no other ready P0 work sitting
    alongside it in wave 1 (only #10/#20 are ready then), so #30 is fully eligible and "smart"
    promotes #10 exactly like "always" does here -- the two modes only diverge once an eligible
    dependent gains other ready work at its own tier (next test)."""
    triage = _mod("triage")
    nodes = [_node(10, priority="P2"), _node(20, priority="P1"), _node(30, needs=[10], priority="P0")]
    waves = triage.schedule_waves(nodes, cap=1, blocker_promotion_mode="smart")
    assert [n["id"] for w in waves for n in w] == [10, 30, 20]


def test_blocker_promotion_smart_mode_withholds_when_dependent_has_other_ready_work_at_its_tier():
    """#30 (P0) is blocked by #10 (P2), but #40 is ALSO ready at P0 in the SAME wave-1 computation
    -- promoting #10 would not get #30 worked any sooner (#40 is already queued ahead at that tier
    regardless), so "smart" mode must not count #30 as a promotion reason at all: #10 stays at its
    own raw P2 rank, unlike "always" mode on the identical graph (next test), which promotes
    regardless of #30's pickability."""
    triage = _mod("triage")
    nodes = [_node(10, priority="P2"), _node(30, needs=[10], priority="P0"), _node(40, priority="P0")]
    waves = triage.schedule_waves(nodes, cap=1, blocker_promotion_mode="smart")
    assert [n["id"] for w in waves for n in w] == [40, 10, 30]


def test_blocker_promotion_always_mode_ignores_pickability_unlike_smart():
    """Identical graph to the "smart"-withholds test directly above, `mode="always"` instead: #10
    IS promoted here despite #30 having other ready P0 work (#40) -- proving the withholding just
    above is specific to "smart" mode, not a general failure to promote. #10 promotes to rank 0,
    ties #40 on rank, and wins the (rank, id) tie-break (10 < 40); #30 then ties #40 once it's
    unblocked and wins that same tie-break (30 < 40)."""
    triage = _mod("triage")
    nodes = [_node(10, priority="P2"), _node(30, needs=[10], priority="P0"), _node(40, priority="P0")]
    waves = triage.schedule_waves(nodes, cap=1, blocker_promotion_mode="always")
    assert [n["id"] for w in waves for n in w] == [10, 30, 40]


def test_blocker_promotion_multi_dependent_promotes_to_the_highest_tier_among_what_it_blocks():
    """#10 (P3) directly blocks TWO issues -- #20 (P1) and #30 (P0) -- and competes against an
    unrelated #40 (P2) for wave 1. "always" promotes #10 to the MINIMUM (most urgent) rank among
    its dependents, #30's P0, not #20's merely-better-than-#10's-own P1 -- so #10 schedules first,
    ahead of #40, and the two former dependents drain in their own priority order right after."""
    triage = _mod("triage")
    nodes = [_node(10, priority="P3"), _node(20, needs=[10], priority="P1"),
             _node(30, needs=[10], priority="P0"), _node(40, priority="P2")]
    waves = triage.schedule_waves(nodes, cap=1, blocker_promotion_mode="always")
    assert [n["id"] for w in waves for n in w] == [10, 30, 20, 40]


# --- independent-review BLOCKING fix: `_effective_rank` must promote TRANSITIVELY (a chain, not
# just one hop), matching `discovery.blocker_promotion_rank`'s own "ONE LEVEL ONLY, BY DESIGN"
# section, which puts the walk-the-rest-of-the-chain job on the caller -- `_effective_rank` was not
# actually doing it ------------------------------------------------------------------------------


def test_blocker_promotion_transitive_chain_promotes_the_root_blocker_through_every_hop():
    """Independent-review BLOCKING finding on the #900 landing: `_effective_rank` fed each direct
    dependent's RAW priority into `blocker_promotion_rank`, never a promoted one, so a chain three or
    more issues deep only ever promoted the innermost link -- exactly the one hop a single
    `blocker_promotion_rank` call covers, per that function's own "ONE LEVEL ONLY, BY DESIGN"
    docstring section; walking the rest of the chain is explicitly the CALLER's job, which
    `_effective_rank` was not actually doing. Reviewer's exact repro, through the real
    `compile_plan`/`schedule_waves` entrypoints (never a hand-rolled shortcut): #503 (P4) directly
    blocks #502 (P3), which directly blocks #501 (P0) -- two hops further out. #999 (P2) is unrelated
    and ready from wave 1.

    Bug repro, pinned as a permanent regression guard: before this fix, running this exact
    `compile_plan` call with `blocker_promotion_mode="always"` (confirmed by running it against
    unmodified code) scheduled #999 BEFORE #503 -- wave order `[999, 503, 502, 501]`, identical to
    "smart" and even "off" -- because #503's own dependent-scan only ever read #502's raw P3
    (`_priority_rank(d["priority"], aliases)`, never a promoted value), so #503 promoted only to P3
    at best, still behind #999's P2. After the fix, #503's effective rank resolves all the way
    through #502 to #501's P0, two hops out, and schedules first.

    "off" mode is asserted here too, from the SAME fixture: unaffected by any of this, still
    byte-identical to every wave order this function produced before #900 existed."""
    triage = _mod("triage")
    survey = _survey_fixture({"active": [
        {"number": 503, "title": "Root blocker, two hops from the urgent end", "priority": "P4"},
        {"number": 502, "title": "Middle link", "priority": "P3"},
        {"number": 501, "title": "Most urgent, deepest blocked", "priority": "P0"},
        {"number": 999, "title": "Unrelated control, ready immediately", "priority": "P2"},
    ]})
    edges = [(502, 503), (501, 502)]    # cli_edges are (blocked, by): #502 blocked-by #503, #501 blocked-by #502
    off = triage.compile_plan([503, 502, 501, 999], edges, [], 1, survey=survey, now=0,
                              blocker_promotion_mode="off")
    always = triage.compile_plan([503, 502, 501, 999], edges, [], 1, survey=survey, now=0,
                                 blocker_promotion_mode="always")
    assert [w[0]["id"] for w in off["waves"]] == [999, 503, 502, 501]
    assert [w[0]["id"] for w in always["waves"]] == [503, 502, 501, 999]


def test_blocker_promotion_transitive_chain_also_resolves_under_smart_mode():
    """Same three-level chain as the "always" reproduction directly above, "smart" mode instead:
    nothing in this graph has other ready work at any tier along the chain (#999 is P2, matching
    none of #503/#502/#501's own tiers, raw or effective), so "smart" has nothing to withhold and
    reaches the identical fully-promoted result -- the two modes only diverge once an eligible
    dependent gains other ready work at its own tier (see the has-other-tier test below, and the
    single-hop "withholds" test elsewhere in this file)."""
    triage = _mod("triage")
    survey = _survey_fixture({"active": [
        {"number": 503, "title": "Root blocker, two hops from the urgent end", "priority": "P4"},
        {"number": 502, "title": "Middle link", "priority": "P3"},
        {"number": 501, "title": "Most urgent, deepest blocked", "priority": "P0"},
        {"number": 999, "title": "Unrelated control, ready immediately", "priority": "P2"},
    ]})
    result = triage.compile_plan([503, 502, 501, 999], [(502, 503), (501, 502)], [], 1,
                                 survey=survey, now=0, blocker_promotion_mode="smart")
    assert [w[0]["id"] for w in result["waves"]] == [503, 502, 501, 999]


def test_blocker_promotion_has_other_work_check_uses_the_dependents_effective_tier_not_raw():
    """Pins the independent-review's OTHER open decision (`_effective_rank`'s own docstring, "RAW VS
    EFFECTIVE"): a dependent's "own tier", for the "has other unblocked work" check, is its EFFECTIVE
    (recursively resolved) rank, not its raw priority -- these diverge here on purpose. #73 (P0) is
    directly blocked by #72 (P3), so #72's OWN effective rank promotes all the way to P0. #71 (P4)
    directly blocks #72. #74 (P3) is unrelated and ready immediately, sitting at #72's RAW tier (P3)
    but nowhere near #72's EFFECTIVE tier (P0).

    Evaluating #71's "smart"-mode promotion via #72: if the has-other check compared against #72's
    STALE raw P3, #74 would look like "other work already at #72's tier" and "smart" mode would
    withhold #71's promotion entirely -- wrongly, since #74 sits nowhere near the P0 tier #72
    actually resolves to. Comparing against #72's true EFFECTIVE tier (P0) instead, #74 (raw P3, no
    promotion chain of its own) does not match, so nothing withholds #72 from #71's pool and #71
    promotes all the way to P0, exactly like "always" would on this same graph -- proving the fix
    does not merely feed a promoted rank into `blocker_promotion_rank` while silently gating
    eligibility on the dependent's stale raw number."""
    triage = _mod("triage")
    nodes = [_node(71, priority="P4"), _node(72, needs=[71], priority="P3"),
             _node(73, needs=[72], priority="P0"), _node(74, priority="P3")]
    waves = triage.schedule_waves(nodes, cap=1, blocker_promotion_mode="smart")
    assert [n["id"] for w in waves for n in w] == [71, 72, 73, 74]


def test_schedule_waves_cycle_reached_via_effective_rank_recursion_does_not_hang_or_recurse_forever():
    """Independent-review requirement: making `_effective_rank` recurse into its direct dependents'
    OWN effective ranks (the fix above) needs its own cycle guard, defensively, even though
    `compile_plan` already runs `_cycles()` before ever calling `schedule_waves` -- so a genuine
    "needs" cycle should never reach this function that way in practice. `schedule_waves` is also
    callable directly, bypassing that pre-check, exactly as this test (and the stranded-nodes test
    below) does.

    #891 is ready immediately and directly blocks #892; #892 and #893 block each other in a genuine
    2-cycle (#892 needs #893, #893 needs #892) -- never reaches `_cycles()` here since
    `schedule_waves` is called directly. Resolving #891's effective rank walks into #892, into #893,
    and back into #892 -- already mid-resolution -- which the cycle guard must catch by falling back
    to #892's raw rank rather than re-entering. Without the guard this recurses forever (a Python
    `RecursionError`, not a plain `ValueError`) -- `pytest.raises(ValueError)` below does NOT catch
    that, so this test fails loudly, not silently, if the guard ever regresses. #891 schedules fine
    in wave 1; #892/#893 can never become ready (their cycle is permanent, by construction), so wave
    2 correctly raises the SAME already-tested stranded-node `ValueError` -- proving the cycle guard
    resolved wave 1 cleanly rather than hanging, not that this graph was somehow fully schedulable."""
    triage = _mod("triage")
    nodes = [_node(891, priority="P1"),
             _node(892, needs=[891, 893], priority="P2"),
             _node(893, needs=[892], priority="P3")]
    with pytest.raises(ValueError, match=r"892, 893"):
        triage.schedule_waves(nodes, cap=3, blocker_promotion_mode="always")


def test_schedule_waves_raises_on_stranded_nodes_naming_them():
    """#669 review F3 (required change): `slices.schedule()`'s identical `if not ready: break`
    branch silently drops any nodes still `remaining` -- safe THERE because `validate()` names the
    reason on a separate path before `schedule()` is ever reached. `plan` has no second reporter,
    so the same silent drop here would print a cheerful summary and write three artifacts while
    quietly omitting issues the operator explicitly asked to plan. Forced by construction: a node
    whose "needs" points at an id absent from `nodes` entirely -- through validated `compile_plan`
    inputs this is unreachable (`_build_nodes` only ever puts an ALREADY-picked id into a node's
    own "needs"), so driving `schedule_waves` directly here is the only way to exercise it, exactly
    matching this TDD group's own "hand-built node lists, no CLI" scope."""
    triage = _mod("triage")
    nodes = [_node(10, needs=[999])]   # 999 is not, and never will be, in `nodes`
    with pytest.raises(ValueError, match="10"):
        triage.schedule_waves(nodes, cap=3)


# --- survey consumption: _load_survey / _survey_index / _survey_edges (plan §9 items 26-28) -------


@pytest.mark.parametrize("kind", ["missing", "malformed", "wrong_schema"])
def test_missing_or_malformed_from_survey_degrades_not_raises(tmp_path, capsys, kind):
    """#669 review F8: parametrized over the three degrade inputs (missing file, malformed JSON,
    wrong schema) -- all three must still produce a valid (title-less) plan, never a traceback,
    with exactly one stderr note."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    if kind == "missing":
        path = str(tmp_path / "does-not-exist.json")
    elif kind == "malformed":
        path = str(tmp_path / "bad.json")
        pathlib.Path(path).write_text("not json")
    else:
        path = str(tmp_path / "wrong-schema.json")
        pathlib.Path(path).write_text(json.dumps({"schema": "something-else/v1"}))
    rc = triage.plan_cmd(str(base), {}, ["--pick", "10", "--from-survey", path], now=0)
    assert rc == 0
    assert "no survey data" in capsys.readouterr().err


def test_survey_supplies_titles_priority_and_model_when_present():
    triage = _mod("triage")
    survey = _survey_fixture({"active": [{"number": 12, "title": "Fix the thing",
                                          "priority": "P0", "model": "daily"}]})
    result = triage.compile_plan([12], [], [], 3, survey=survey, now=0)
    node = result["waves"][0][0]
    assert node["title"] == "Fix the thing" and node["priority"] == "P0" and node["model"] == "daily"


def test_compile_plan_resolves_configured_aliases():
    triage = _mod("triage")
    survey = _survey_fixture({"active": [{"number": 12, "title": "Fix the thing", "priority": "critical"},
                                         {"number": 13, "title": "Also fix", "priority": "P1"}]})
    result = triage.compile_plan([12, 13], [], [], 3, survey=survey, now=0,
                                 aliases={"critical": "P0"})
    assert [n["id"] for n in result["waves"][0]] == [12, 13]   # the alias'd P0 goes first


def test_compile_plan_unset_aliases_leaves_english_priorities_unranked():
    triage = _mod("triage")
    survey = _survey_fixture({"active": [{"number": 12, "title": "Fix the thing", "priority": "critical"},
                                         {"number": 13, "title": "Also fix", "priority": "P1"}]})
    result = triage.compile_plan([12, 13], [], [], 3, survey=survey, now=0)
    assert [n["id"] for n in result["waves"][0]] == [13, 12]   # "critical" unrecognised, sorts last


def test_plan_cmd_threads_priority_aliases_from_config(tmp_path):
    """End-to-end: #854's independent-review finding was that `plan` disagreed with the live picker
    on an alias'd priority because `priority_aliases` never reached `compile_plan` at all -- this
    pins the full path (config -> plan_cmd -> compile_plan -> schedule_waves -> _priority_rank)."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    survey_path = _write_survey(tmp_path / "survey.json",
                                {"active": [{"number": 12, "title": "Fix the thing", "priority": "critical"},
                                           {"number": 13, "title": "Also fix", "priority": "P1"}]})
    config = {"discovery": {"priority_aliases": {"critical": "P0"}}}
    rc = triage.plan_cmd(str(base), config,
                         ["--pick", "12,13", "--from-survey", survey_path, "--slug", "aliastest"],
                         now=0)
    assert rc == 0
    active = json.loads((base / "plans" / "triage" / "active.json").read_text())
    plan = json.loads((base / "plans" / "triage" / active["plan_json"]).read_text())
    assert plan["waves"][0] == [12, 13]   # the alias'd P0 goes first


# --- #900: blocker_promotion_mode threading through compile_plan/plan_cmd -- mirrors the aliases
# threading group directly above, same shared graph as the schedule_waves-level promotion tests
# (#10 P2 blocks #30 P0 via an --edge; #20 P1 is unrelated, ready from the start) ------------------


def test_compile_plan_resolves_configured_blocker_promotion_mode():
    triage = _mod("triage")
    survey = _survey_fixture({"active": [{"number": 10, "title": "Low-tier blocker", "priority": "P2"},
                                         {"number": 20, "title": "Unrelated P1", "priority": "P1"},
                                         {"number": 30, "title": "Urgent, blocked", "priority": "P0"}]})
    result = triage.compile_plan([10, 20, 30], [(30, 10)], [], 1, survey=survey, now=0,
                                 blocker_promotion_mode="always")
    assert [w[0]["id"] for w in result["waves"]] == [10, 30, 20]   # #10 promoted ahead of #20


def test_compile_plan_unset_blocker_promotion_mode_is_the_full_rule_and_off_is_unpromoted():
    triage = _mod("triage")
    survey = _survey_fixture({"active": [{"number": 10, "title": "Low-tier blocker", "priority": "P2"},
                                         {"number": 20, "title": "Unrelated P1", "priority": "P1"},
                                         {"number": 30, "title": "Urgent, blocked", "priority": "P0"}]})
    result = triage.compile_plan([10, 20, 30], [(30, 10)], [], 1, survey=survey, now=0)
    assert [w[0]["id"] for w in result["waves"]] == [10, 30, 20]   # #1406: default promotes #10
    off = triage.compile_plan([10, 20, 30], [(30, 10)], [], 1, survey=survey, now=0,
                              blocker_promotion_mode="off")
    assert [w[0]["id"] for w in off["waves"]] == [20, 10, 30]      # explicit off: natural order


def test_plan_cmd_threads_blocker_promotion_mode_from_config(tmp_path):
    """End-to-end: mirrors test_plan_cmd_threads_priority_aliases_from_config's own #854 shape for
    #900 -- pins the full path (config -> plan_cmd -> compile_plan -> schedule_waves ->
    discovery.blocker_promotion_rank) so a repeat of the #854 regression (schedule_waves alone
    getting the new parameter while compile_plan/plan_cmd never threaded it through, so the live
    picker and plan's wave order silently disagreed) cannot land silently here too."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    survey_path = _write_survey(tmp_path / "survey.json",
                                {"active": [{"number": 10, "title": "Low-tier blocker", "priority": "P2"},
                                           {"number": 20, "title": "Unrelated P1", "priority": "P1"},
                                           {"number": 30, "title": "Urgent, blocked", "priority": "P0"}]})
    config = {"discovery": {"blocker_promotion": {"mode": "always"}}}
    rc = triage.plan_cmd(str(base), config,
                         ["--pick", "10,20,30", "--edge", "30:10", "--from-survey", survey_path,
                          "--cap", "1", "--slug", "promotiontest"],
                         now=0)
    assert rc == 0
    active = json.loads((base / "plans" / "triage" / "active.json").read_text())
    plan = json.loads((base / "plans" / "triage" / active["plan_json"]).read_text())
    assert [w[0] for w in plan["waves"]] == [10, 30, 20]   # #10 promoted ahead of #20


def test_pick_missing_from_survey_degrades_to_a_bare_number():
    """#713's own reproduction of the reported bug, pinned as a permanent regression guard: a
    picked issue absent from the survey snapshot (the index-lag miss -- GitHub's search-backed
    `gh issue list` had not yet indexed a just-filed/just-labelled issue at the time `survey` ran)
    degrades to a bare "#N" title with no priority/model. This is, and after #713 REMAINS, the
    DEFAULT behavior with no --resolve-missing supplied -- `plan`'s zero-network guarantee holds
    for every input that does not explicitly opt into the fallback below."""
    triage = _mod("triage")
    survey = _survey_fixture({"active": [{"number": 12, "title": "Fix the thing", "priority": "P0"}]})
    result = triage.compile_plan([12, 99], [], [], 3, survey=survey, now=0)   # 99: not in the survey
    by_id = {n["id"]: n for wave in result["waves"] for n in wave}
    assert by_id[99]["title"] == "#99"
    assert by_id[99]["priority"] is None and by_id[99]["model"] is None


def test_compile_plan_extra_index_fills_a_pick_missing_from_survey():
    """#713: `extra_index` is the opt-in live-read fallback's resolved output (computed by
    plan_cmd, a plain caller-supplied dict) -- `compile_plan` itself performs no I/O regardless of
    whether it is populated, so this is still "pure given its args": the network read, if any,
    already happened by the time this function is called, exactly like `survey=` above."""
    triage = _mod("triage")
    result = triage.compile_plan([99], [], [], 3, survey=None,
                                 extra_index={99: {"title": "Resolved live", "priority": "P1",
                                                    "model": "bulk"}}, now=0)
    node = result["waves"][0][0]
    assert node["title"] == "Resolved live" and node["priority"] == "P1" and node["model"] == "bulk"


def test_compile_plan_survey_index_wins_over_extra_index_on_overlap():
    """Defensive precedence rule for a case plan_cmd's own construction never actually produces
    (extra_index is only ever built from picks ALREADY confirmed missing from the survey index) --
    pinned anyway so the merge direction is a tested decision, not an accident of dict-literal
    order that a future refactor could silently flip."""
    triage = _mod("triage")
    survey = _survey_fixture({"active": [{"number": 99, "title": "From survey", "priority": "P0"}]})
    result = triage.compile_plan([99], [], [], 3, survey=survey,
                                 extra_index={99: {"title": "From live read", "priority": "P4",
                                                    "model": None}}, now=0)
    node = result["waves"][0][0]
    assert node["title"] == "From survey" and node["priority"] == "P0"


def test_survey_edges_are_relayed_with_renamed_source_and_self_references_excluded():
    triage = _mod("triage")
    survey = _survey_fixture({
        "edges": [{"blocked": 15, "by": 12, "source": "explicit"},
                  {"blocked": 30, "by": 20, "source": "ledger"},
                  {"blocked": 40, "by": 40, "source": "explicit"}],   # self-ref -- must be dropped
    })
    edges = triage._survey_edges(survey)
    assert (15, 12, "survey-body") in edges
    assert (30, 20, "survey-ledger") in edges
    assert all(b != y for b, y, s in edges)
    assert len(edges) == 2


# --- #713: _resolve_missing_picks -- the opt-in, bounded, best-effort index-lag fallback ----------
# `plan` itself stays pure (compile_plan/_build_nodes above are untouched in kind); this is the ONE
# I/O-performing helper #713 adds, called by plan_cmd BEFORE compile_plan, never by compile_plan
# itself. `_recording_runner` (keyed by gh verb alone) cannot return different data per issue
# number for a batch of `gh issue view N` calls sharing the verb "view" -- a dedicated fake here.


def _view_run(by_number):
    """Fake `run` for the REST issue GET (`api repos/<o>/<r>/issues/N --method GET`, #895), keyed by
    issue number rather than verb; any other argv is recorded in `run.unexpected` and raised. A number absent from `by_number` raises, matching a real `gh issue view` on an issue
    that does not exist (or any other single-call failure) -- proves the per-pick fail-open path."""
    calls, unexpected = [], []
    def run(args):
        calls.append(list(args))
        target = gqlfake.rest_issue_target(args)       # #895: the read is REST first
        if target is None:
            unexpected.append(list(args))              # incl. the `issue view` fallback
            raise RuntimeError("unexpected argv: %r" % (args,))
        n = int(target[0])
        if n not in by_number:
            raise RuntimeError(f"gh: issue #{n} not found")
        return gqlfake.rest_issue(args, lambda _n, _f: by_number[n])
    run.calls = calls
    run.unexpected = unexpected
    return run


def _issue_view_json(number, title, labels=()):
    return json.dumps({"number": number, "title": title,
                       "labels": [{"name": l} for l in labels]})


def test_resolve_missing_picks_only_fetches_picks_absent_from_survey_index():
    triage = _mod("triage")
    run = _view_run({99: _issue_view_json(99, "Live title", ["priority:P1"])})
    survey_index = {12: {"title": "Already known", "priority": "P0", "model": None}}
    resolved = triage._resolve_missing_picks([12, 99], survey_index, None, run, limit=5)
    assert list(resolved) == [99]
    assert len(run.calls) == 1   # 12 was already covered -- no call for it at all


def test_resolve_missing_picks_is_bounded_by_limit_in_pick_order():
    """More missing picks than the cap: only the FIRST `limit` of them, in --pick's own order, get
    a live read -- deterministic, not a set-iteration accident (mirrors #706's own picker-window
    boundary-test discipline)."""
    triage = _mod("triage")
    run = _view_run({10: _issue_view_json(10, "Ten"), 20: _issue_view_json(20, "Twenty"),
                     30: _issue_view_json(30, "Thirty")})
    resolved = triage._resolve_missing_picks([10, 20, 30], {}, None, run, limit=2)
    assert set(resolved) == {10, 20}
    assert len(run.calls) == 2


def test_resolve_missing_picks_one_failure_does_not_block_the_rest():
    triage = _mod("triage")
    run = _view_run({20: _issue_view_json(20, "Twenty")})   # 10 has no canned response -> run() raises
    resolved = triage._resolve_missing_picks([10, 20], {}, None, run, limit=5)
    assert 10 not in resolved
    assert resolved[20]["title"] == "Twenty"


def test_resolve_missing_picks_reads_rest_first_with_and_without_a_repo():
    """#895: repo set -> `repos/<owner>/<repo>/issues/N`; repo unset -> `repos/{owner}/{repo}/...`
    (gh fills the placeholders). Nothing but that one GET is requested."""
    triage = _mod("triage"); sources = _mod("sources")
    gh_source = sources.GitHubSource(
        {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}}, run=lambda a: "")
    run = _view_run({7: _issue_view_json(7, "X", ["priority:P3"])})
    triage._resolve_missing_picks([7], {}, gh_source, run, limit=5)
    assert run.calls == [["api", "repos/acme/widget/issues/7", "--method", "GET"]]
    run = _view_run({7: _issue_view_json(7, "X", ["priority:P3"])})
    triage._resolve_missing_picks([7], {}, None, run, limit=5)
    assert run.calls == [["api", "repos/{owner}/{repo}/issues/7", "--method", "GET"]]
    assert run.unexpected == []


def test_resolve_missing_picks_passes_the_injected_run_as_the_fallback_too(monkeypatch):
    """No source here: the injected `run` is both the REST runner and `gql_run`, so a REST 5xx reaches
    the SAME `run` as one `issue view` (without `gql_run=run` the fallback would shell out to real gh)."""
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)
    triage = _mod("triage")
    calls = []

    def run(args):
        calls.append(list(args))
        if args[0] == "api":
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return _issue_view_json(7, "X", ["priority:P3"])
    resolved = triage._resolve_missing_picks([7], {}, None, run, limit=5)
    assert resolved[7]["priority"] == "P3"
    assert [c[:3] for c in calls if c[0] == "issue"] == [["issue", "view", "7"]]


def test_resolve_missing_picks_never_raises_when_everything_fails():
    triage = _mod("triage")
    def boom(args):
        raise RuntimeError("gh: not authenticated")
    resolved = triage._resolve_missing_picks([10, 20], {}, None, boom, limit=5)
    assert resolved == {}


def test_resolve_missing_picks_uses_the_configured_priority_prefix():
    """Reuses the SAME configurable-prefix contract #706 wired into every survey bucket
    (`gh_source.priority_prefix`, `gh_source is None` falling back to the historical "priority:")
    rather than re-hardcoding the literal a third time."""
    triage = _mod("triage")
    sources = _mod("sources")
    gh_source = sources.GitHubSource(
        {"discovery": {"source": "github", "github": {"priority_label_prefix": "sev:"}}},
        run=lambda a: "")
    run = _view_run({7: _issue_view_json(7, "X", ["sev:P2", "priority:P0", "model:opus"])})
    resolved = triage._resolve_missing_picks([7], {}, gh_source, run, limit=5)
    assert resolved[7]["priority"] == "P2"   # priority:P0 ignored under the configured prefix
    assert resolved[7]["model"] == "opus"


def test_resolve_missing_picks_falls_back_to_the_historical_prefix_when_gh_source_is_none():
    triage = _mod("triage")
    run = _view_run({7: _issue_view_json(7, "X", ["priority:P3"])})
    resolved = triage._resolve_missing_picks([7], {}, None, run, limit=5)
    assert resolved[7]["priority"] == "P3"


# --- compile_plan orchestration: validation ordering + external edges (plan §9 items 9, 10, 15, 16;
# review F3/F5/F6) -----------------------------------------------------------------------------


def test_compile_plan_cycle_error_names_every_member_and_writes_nothing(tmp_path, capsys):
    """#669 review F6: a 4th, independent issue (#13) must be ABSENT from cycle_error -- otherwise
    an implementation that simply lists every picked issue passes vacuously. Also confirms the
    no-partial-publish contract through the FULL plan_cmd path (the base plan's own requirement)."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    rc = triage.plan_cmd(str(base), {},
                         ["--pick", "10,11,12,13",
                          "--edge", "10:11", "--edge", "11:12", "--edge", "12:10"])
    assert rc == 1
    err = capsys.readouterr().err
    for member in ("#10", "#11", "#12"):
        assert member in err
    assert "#13" not in err
    assert not (base / "plans" / "triage").exists()


def test_compile_plan_rejects_self_edge_before_even_reaching_cycle_detection():
    """Proves the explicit self-reference guard fires, not an accidental catch by the cycle
    detector -- a self-edge would ALSO look like a trivial 1-node cycle to a naive DFS, but this
    must be a ValueError, never a cycle_error."""
    triage = _mod("triage")
    with pytest.raises(ValueError, match="12"):
        triage.compile_plan([12], [(12, 12)], [], 3)


def test_compile_plan_rejects_an_edge_whose_blocked_side_is_not_picked():
    triage = _mod("triage")
    with pytest.raises(ValueError, match="15"):
        triage.compile_plan([12], [(15, 12)], [], 3)


def test_external_by_does_not_block_scheduling():
    triage = _mod("triage")
    result = triage.compile_plan([15], [(15, 99)], [], 3, now=0)   # #99 never picked
    assert [n["id"] for n in result["waves"][0]] == [15]
    assert {"blocked": 15, "by": 99, "source": "cli", "external": True} in result["edges"]


def test_md_renders_the_external_dependency_bullet():
    """The `.md` skeleton's "also depends on #N (external...)" line, per §5's own worked example
    -- not exercised by the schema/wave-header pin tests above (neither of those cases has an
    external edge)."""
    triage = _mod("triage")
    result = triage.compile_plan([15], [(15, 99)], [], 3, now=0)   # #99 never picked
    md = triage.render_md(result, "s", "2026-08-10")
    assert "#15 also depends on #99 (external" in md


def test_deferred_and_picked_overlap_is_a_hard_error():
    triage = _mod("triage")
    with pytest.raises(ValueError, match="12"):
        triage.compile_plan([12], [], [(12, "reason")], 3)


def test_deferred_and_picked_overlap_writes_no_files_through_plan_cmd(tmp_path):
    """#669 review F5: extends the no-partial-publish assertion beyond the cycle path -- defer/pick
    overlap is the cheapest ValueError path to prove it on."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    rc = triage.plan_cmd(str(base), {}, ["--pick", "12", "--defer", "12:reason"])
    assert rc == 2
    assert not (base / "plans" / "triage").exists()


def test_stranded_node_error_writes_no_files_through_plan_cmd(tmp_path, monkeypatch):
    """#669 review F5, extended to F3's new error: monkeypatch schedule_waves to force the
    stranded-node ValueError inside a REAL plan_cmd call -- through validated CLI inputs this path
    is unreachable (see schedule_waves' own docstring), so this is the only way to prove plan_cmd's
    error handling covers it too, not just the self-edge/blocked-not-picked/defer-overlap shapes."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)

    def boom(nodes, cap, aliases=None, blocker_promotion_mode=None):
        raise ValueError("stranded node(s) -- unresolvable dependency, never in nodes: [999]")
    monkeypatch.setattr(triage, "schedule_waves", boom)

    rc = triage.plan_cmd(str(base), {}, ["--pick", "10"])
    assert rc == 2
    assert not (base / "plans" / "triage").exists()


# --- atomic write + artifact rendering (plan §9 items 17-21; review F6/F9) ------------------------


def test_same_slug_rerun_overwrites_cleanly(tmp_path):
    """#669 review F6: the original construction was self-admittedly vacuous (a superset second
    run can never prove the first pick's absence). Fixed: run 2 is a strict SUBSET with a
    DIFFERENT --defer reason than run 1 -- the dropped content (the extra pick, the first --defer
    reason) must be genuinely gone from both artifacts after the overwrite."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    now = 1700000000
    date_str = time.strftime("%Y-%m-%d", time.gmtime(now))
    json_path = base / "plans" / "triage" / f"{date_str}-s.json"
    md_path = base / "plans" / "triage" / f"{date_str}-s.md"

    rc1 = triage.plan_cmd(str(base), {},
                          ["--pick", "10,11", "--defer", "40:first reason", "--slug", "s"], now=now)
    assert rc1 == 0
    text1 = json_path.read_text()
    assert '"issue": 11' in text1 and "first reason" in text1

    rc2 = triage.plan_cmd(str(base), {},
                          ["--pick", "10", "--defer", "40:second reason", "--slug", "s"], now=now)
    assert rc2 == 0
    text2 = json_path.read_text()
    assert '"issue": 11' not in text2, "the dropped pick must be genuinely gone, not just unmentioned"
    assert "first reason" not in text2
    assert "second reason" in text2
    md_text2 = md_path.read_text()
    assert "first reason" not in md_text2 and "second reason" in md_text2


def test_active_json_updated_and_points_at_the_new_files(tmp_path):
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    now = 1700000000
    date_str = time.strftime("%Y-%m-%d", time.gmtime(now))
    active_path = base / "plans" / "triage" / "active.json"

    rc1 = triage.plan_cmd(str(base), {}, ["--pick", "10", "--slug", "one"], now=now)
    assert rc1 == 0
    active1 = json.loads(active_path.read_text())
    assert active1["plan_json"] == f"{date_str}-one.json"
    assert (base / "plans" / "triage" / active1["plan_json"]).exists()
    assert (base / "plans" / "triage" / active1["plan_md"]).exists()

    rc2 = triage.plan_cmd(str(base), {}, ["--pick", "11", "--slug", "two"], now=now)
    assert rc2 == 0
    active2 = json.loads(active_path.read_text())
    assert active2["plan_json"] == f"{date_str}-two.json"
    assert active2["plan_json"] != active1["plan_json"]


def test_active_json_untouched_when_the_compile_hard_errors(tmp_path):
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    now = 1700000000
    active_path = base / "plans" / "triage" / "active.json"

    rc1 = triage.plan_cmd(str(base), {}, ["--pick", "10", "--slug", "one"], now=now)
    assert rc1 == 0
    before = active_path.read_bytes()

    rc2 = triage.plan_cmd(str(base), {},
                          ["--pick", "10,11,12",
                           "--edge", "10:11", "--edge", "11:12", "--edge", "12:10"], now=now)
    assert rc2 == 1
    assert active_path.read_bytes() == before


def test_md_content_pins_wave_headers_priority_and_how_to_run_footer(tmp_path):
    """Pins the caveat text survives refactors -- including #669 review F9's added clause: the
    footer must say `next-batch` only returns more than one goal when `parallel.goals.enabled` is
    on, alongside the pre-existing `next-batch` phrase."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    survey_path = base / "survey.json"
    survey_path.write_text(json.dumps(_survey_fixture(
        {"active": [{"number": 12, "title": "Fix the thing", "priority": "P0", "model": "daily"}]})))
    now = 1700000000
    date_str = time.strftime("%Y-%m-%d", time.gmtime(now))
    rc = triage.plan_cmd(str(base), {},
                         ["--pick", "12", "--slug", "s", "--from-survey", str(survey_path)], now=now)
    assert rc == 0
    md_text = (base / "plans" / "triage" / f"{date_str}-s.md").read_text()
    assert "## Wave 1" in md_text
    assert "P0" in md_text
    assert "next-batch" in md_text
    assert "parallel.goals.enabled" in md_text


def test_json_content_matches_the_documented_schema_fields(tmp_path):
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    now = 1700000000
    date_str = time.strftime("%Y-%m-%d", time.gmtime(now))
    rc = triage.plan_cmd(str(base), {},
                         ["--pick", "12,15", "--edge", "15:12", "--slug", "s"], now=now)
    assert rc == 0
    obj = json.loads((base / "plans" / "triage" / f"{date_str}-s.json").read_text())
    assert set(obj) == {"version", "schema", "generated_at", "slug", "cap", "picked", "edges",
                        "deferred", "waves"}
    assert obj["schema"] == triage.PLAN_SCHEMA
    for item in obj["picked"]:
        assert set(item) == {"issue", "priority", "model", "wave"}


def test_plan_filename_date_matches_generated_at_across_a_utc_midnight_straddle(tmp_path, monkeypatch):
    """#680 (PR #679 independent review finding 1): compile_plan stamps generated_at from ONE
    clock read (_stamp(), fed compile_plan's own `now`), but plan_cmd used to compute date_str
    from a SEPARATE, independent time.time() read of its own — two calls, not one, whenever
    plan_cmd's own `now` is left at its real production default (None; per its own docstring,
    main() never passes it explicitly). A plan compiled across the UTC-midnight second got a
    filename dated the day AFTER its own generated_at.

    Every OTHER plan_cmd test in this file passes a fixed `now=` — by construction, a single
    shared value can never disagree with itself, so none of them can expose this. Reproduced here
    instead by making time.time() itself return two different, midnight-straddling values on
    successive calls — the only way to force the disagreement deterministically without a real
    Linux clock race."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)

    # 2026-08-11T23:59:59Z, then +2s -> 2026-08-12T00:00:01Z. compile_plan's _stamp() call
    # consumes the FIRST read (becomes generated_at); the pre-fix code's second, independent read
    # (for date_str) consumes the second. The fix reads the clock only once, so only the first
    # value is ever asked for -- the second is simply never consumed, which is fine.
    clock = iter([1786492799, 1786492801])
    monkeypatch.setattr(time, "time", lambda: next(clock))

    rc = triage.plan_cmd(str(base), {}, ["--pick", "10", "--slug", "s"], now=None)
    assert rc == 0

    plans_dir = base / "plans" / "triage"
    obj = json.loads(next(plans_dir.glob("*-s.json")).read_text())
    assert obj["generated_at"] == "2026-08-11T23:59:59Z"      # the FIRST clock read, unambiguous
    generated_date = obj["generated_at"][:10]
    written = [p.name for p in plans_dir.iterdir()]
    assert any(name.startswith(generated_date) for name in written), (
        f"generated_at is {obj['generated_at']!r} (date {generated_date!r}) but the written "
        f"filenames are {written!r} — the plan artifact's own filename disagrees with the "
        "timestamp embedded inside it")


# --- _cap_from_config (plan §9 items 22-24; review F10) --------------------------------------------


def test_config_cap_default_reads_parallel_goals_max_concurrent_with_fallback_three():
    triage = _mod("triage")
    assert triage._cap_from_config({"parallel": {"goals": {"max_concurrent": 5}}}) == 5
    assert triage._cap_from_config({}) == 3
    assert triage._cap_from_config({"parallel": {"goals": {"max_concurrent": "nope"}}}) == 3


def test_cap_from_config_clamps_zero_and_negative_at_one():
    """#669 review F10: `loop.goals_parallel()`'s own `max(1, cap)` clamp must apply here too -- a
    configured 0 or negative `max_concurrent` is a VALID parsed int, not a non-numeric-fallback
    case, and would otherwise reach `schedule_waves` as a zero/negative cap and starve the plan of
    any waves at all."""
    triage = _mod("triage")
    assert triage._cap_from_config({"parallel": {"goals": {"max_concurrent": 0}}}) == 1
    assert triage._cap_from_config({"parallel": {"goals": {"max_concurrent": -5}}}) == 1


def test_default_cap_constant_stays_in_lockstep_with_loop_py():
    """Open Decision 2 (669-plan.md): a copy, not a live read -- only this test keeps that honest."""
    triage = _mod("triage")
    loop = _mod("loop")
    assert triage._DEFAULT_CAP == loop.DEFAULT_GOALS_MAX_CONCURRENT


def test_cli_cap_flag_falls_back_softly_on_a_bad_value(capsys, tmp_path):
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    rc = triage.plan_cmd(str(base), {}, ["--pick", "10", "--cap", "notanumber"], now=0)
    assert rc == 0
    assert "not a positive integer" in capsys.readouterr().err


def test_cli_cap_flag_uses_the_given_value_directly_when_valid(tmp_path):
    """The happy path of the same branch -- a positive integer --cap is used AS GIVEN, never
    silently replaced by the config default."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    now = 1700000000
    date_str = time.strftime("%Y-%m-%d", time.gmtime(now))
    rc = triage.plan_cmd(str(base), {}, ["--pick", "10,11,12", "--cap", "1", "--slug", "s"], now=now)
    assert rc == 0
    obj = json.loads((base / "plans" / "triage" / f"{date_str}-s.json").read_text())
    assert obj["cap"] == 1
    assert len(obj["waves"]) == 3   # cap 1 forces three separate waves for three independent picks


def test_plan_cmd_pick_resolving_to_no_valid_issues_is_a_usage_error(capsys, tmp_path):
    """`--pick` given but resolving to zero issue numbers (e.g. bare commas) is the SAME usage
    error as omitting --pick entirely -- distinct code path from the "flag absent" check, since
    `_parse_plan_argv` already sees a non-empty raw string here."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    rc = triage.plan_cmd(str(base), {}, ["--pick", ",,,"])
    assert rc == 2
    assert "usage" in capsys.readouterr().err
    assert not (base / "plans" / "triage").exists()


# --- slug safety (plan §9 item 25; review F7 ruling) ------------------------------------------------


@pytest.mark.parametrize("bad_slug", ["../../etc", "", ".", "  ", "a/b", "a:b", ".."])
def test_slug_rejects_unsafe_or_empty_values(tmp_path, bad_slug):
    """#669 review F7 (binding ruling): `state.unsafe_goal_reason(slug)` is REUSED (an import and a
    call, not a copied character class), PLUS an additional guard the shared function alone does
    NOT enforce -- the reviewer's own probe found `''`, `'.'`, and `'  '` all pass
    `unsafe_goal_reason` unchanged (it exists to block path TRAVERSAL, not to reject a merely
    degenerate slug) and would still produce a useless filename like `2026-08-10-.json`. No file is
    written anywhere for any of these."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    rc = triage.plan_cmd(str(base), {}, ["--pick", "10", "--slug", bad_slug])
    assert rc == 2
    assert not (base / "plans" / "triage").exists()


def test_slug_reuses_the_shared_validator_live_not_a_copy(monkeypatch):
    """Behavioral proof of REUSE (not a re-typed character class): patching triage's own loaded
    `state` module's `unsafe_goal_reason` must change what `_validate_slug` accepts."""
    triage = _mod("triage")
    monkeypatch.setattr(triage.state, "unsafe_goal_reason", lambda s: "sentinel reason" if s == "ok" else None)
    with pytest.raises(ValueError, match="sentinel reason"):
        triage._validate_slug("ok")


# --- plan_cmd / main CLI wiring + the end-to-end no-gh proof (plan §9 item 29) ---------------------


def test_plan_never_shells_out_to_gh(monkeypatch, tmp_path):
    """Proves the "zero network under any input" claim behaviorally, not by code inspection alone
    -- monkeypatch subprocess.run to raise if called at all; run a full plan_cmd compile with and
    without --from-survey. #713: this is now specifically the DEFAULT-path guarantee -- no input
    here passes --resolve-missing, #713's own opt-in, deliberately-the-only exception (see the
    dedicated tests below, which prove the opposite: that flag DOES call out, on purpose, only when
    given)."""
    triage = _mod("triage")
    import subprocess

    def boom(*a, **k):
        raise AssertionError("plan must never shell out")
    monkeypatch.setattr(subprocess, "run", boom)

    base = _plan_sdlc(tmp_path)
    rc = triage.plan_cmd(str(base), {}, ["--pick", "10,15", "--edge", "15:10", "--slug", "a"], now=0)
    assert rc == 0

    survey_path = base / "survey.json"
    survey_path.write_text(json.dumps(_survey_fixture({"active": [{"number": 10}]})))
    rc2 = triage.plan_cmd(str(base), {},
                          ["--pick", "10", "--slug", "b", "--from-survey", str(survey_path)], now=0)
    assert rc2 == 0


# --- #713: plan_cmd's --resolve-missing wiring (the CLI-level end of the opt-in fallback) ---------


def _plan_gh_sdlc(d):
    """Like _plan_sdlc, but with a real discovery.github config -- --resolve-missing needs one to
    construct a gh_source (repo scoping, configurable priority prefix) the way survey() already does."""
    base = _plan_sdlc(d)
    return base, {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}}


def test_plan_never_shells_out_to_gh_in_github_mode_either(monkeypatch, tmp_path):
    """PR #725 review finding 1: all pre-#713 `plan_cmd` call sites (including
    `test_plan_never_shells_out_to_gh` above) use `config={}` via `_plan_sdlc`, which
    short-circuits `_is_github()` to False before the --resolve-missing gate is ever reached -- so
    a sabotage that dropped the opt-in check entirely (attempting the fallback whenever github mode
    is on, regardless of whether --resolve-missing was ever passed) left every one of those tests
    green. This is the missing combination: github-mode config, the flag OMITTED, network forbidden
    at the subprocess boundary -- proven live (mutation-proof, RAILS rule 8) by temporarily
    reintroducing exactly that sabotage and confirming this specific test, and only this one, fails.

    Records calls rather than relying on the raise propagating: `_resolve_missing_picks`'s own
    `except Exception: continue` (correct, desired fail-open behavior for a real read failure) would
    otherwise silently swallow a bare `raise`-only sentinel too -- proven the hard way while writing
    this test, a raise-only version of `boom` passed even under the sabotage below. Recording the
    call BEFORE raising sidesteps that: the attempt is observable regardless of what catches the
    exception afterward."""
    triage = _mod("triage")
    import subprocess

    calls = []
    def boom(*a, **k):
        calls.append((a, k))
        raise AssertionError("plan must never shell out")
    monkeypatch.setattr(subprocess, "run", boom)

    base, config = _plan_gh_sdlc(tmp_path)
    rc = triage.plan_cmd(str(base), config,
                         ["--pick", "10,15", "--edge", "15:10", "--slug", "a"], now=0)
    assert rc == 0
    assert calls == [], "subprocess.run was invoked -- plan attempted a live read it must not have"


def test_plan_cmd_resolve_missing_fills_in_a_degraded_pick(tmp_path):
    """The end-to-end fix: a pick with no survey data at all (--from-survey omitted entirely, the
    simplest reproduction of "I don't already have this issue's data from anywhere") gets a real
    title/priority via --resolve-missing instead of the bare "#N" degrade."""
    triage = _mod("triage")
    base, config = _plan_gh_sdlc(tmp_path)
    run = _view_run({99: _issue_view_json(99, "Fix the thing", ["priority:P1"])})
    rc = triage.plan_cmd(str(base), config,
                         ["--pick", "99", "--slug", "s", "--resolve-missing", "5"], now=0, run=run)
    assert rc == 0
    # title/priority surface in the .md artifact (render_json's own "waves" carries bare issue ids
    # only, and its "picked" list carries no title at all -- render_md is the human-readable one).
    md = (base / "plans" / "triage" / "1970-01-01-s.md").read_text()
    assert "#99 · P1 · - · Fix the thing" in md
    assert len(run.calls) == 1


def test_plan_cmd_resolve_missing_respects_the_cap(tmp_path):
    triage = _mod("triage")
    base, config = _plan_gh_sdlc(tmp_path)
    run = _view_run({10: _issue_view_json(10, "Ten"), 20: _issue_view_json(20, "Twenty"),
                     30: _issue_view_json(30, "Thirty")})
    rc = triage.plan_cmd(str(base), config,
                         ["--pick", "10,20,30", "--slug", "s", "--resolve-missing", "1"],
                         now=0, run=run)
    assert rc == 0
    assert len(run.calls) == 1
    md = (base / "plans" / "triage" / "1970-01-01-s.md").read_text()
    resolved = [n for n, title in ((10, "Ten"), (20, "Twenty"), (30, "Thirty"))
               if f"· {title}" in md]
    assert len(resolved) == 1   # exactly one of the three actually got resolved
    still_bare = [n for n in (10, 20, 30) if f"#{n} · - · - · #{n}" in md]
    assert len(still_bare) == 2


def test_plan_cmd_resolve_missing_skips_already_present_survey_picks(tmp_path):
    """The cap is spent only on GENUINE misses -- a pick the survey snapshot already covers must
    never consume budget or trigger a call, even though it would technically fit under the cap."""
    triage = _mod("triage")
    base, config = _plan_gh_sdlc(tmp_path)
    survey_path = base / "survey.json"
    survey_path.write_text(json.dumps(_survey_fixture(
        {"active": [{"number": 10, "title": "Already known", "priority": "P0"}]})))
    run = _view_run({12: _issue_view_json(12, "Twelve", ["priority:P2"])})
    rc = triage.plan_cmd(str(base), config,
                         ["--pick", "10,12", "--slug", "s", "--from-survey", str(survey_path),
                          "--resolve-missing", "5"], now=0, run=run)
    assert rc == 0
    assert len(run.calls) == 1   # only #12 -- #10 was already covered by the survey
    assert run.calls[0] == ["api", "repos/acme/widget/issues/12", "--method", "GET"]
    assert run.unexpected == []


def test_plan_cmd_resolve_missing_rejects_a_non_positive_value(capsys, tmp_path):
    """Mirrors --cap's own established precedent right above this flag's parsing: a malformed
    value is a NOTE + safe fallback (here: the fallback is simply "attempt nothing"), never the
    hard --pick/--slug-style usage error -- an optional enrichment flag must not block an otherwise
    valid compile."""
    triage = _mod("triage")
    base, config = _plan_gh_sdlc(tmp_path)
    def boom(args):
        raise AssertionError("must not attempt a live read on a rejected --resolve-missing value")
    rc = triage.plan_cmd(str(base), config,
                         ["--pick", "99", "--slug", "s", "--resolve-missing", "0"], now=0, run=boom)
    assert rc == 0
    err = capsys.readouterr().err
    assert "--resolve-missing" in err
    md = (base / "plans" / "triage" / "1970-01-01-s.md").read_text()
    assert "#99 · - · - · #99" in md   # unresolved, exactly like today


def test_plan_cmd_resolve_missing_is_a_no_op_outside_github_mode(capsys, tmp_path):
    """A gh issue view call is meaningless outside github discovery mode -- must not even attempt
    one (versus attempting and letting every call fail open, which would work but waste a read on
    a call that could never succeed)."""
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    def boom(args):
        raise AssertionError("must not attempt a live read outside github mode")
    rc = triage.plan_cmd(str(base), {},   # no discovery.source at all
                         ["--pick", "99", "--slug", "s", "--resolve-missing", "5"], now=0, run=boom)
    assert rc == 0
    md = (base / "plans" / "triage" / "1970-01-01-s.md").read_text()
    assert "#99 · - · - · #99" in md


def test_cli_plan_usage_mentions_resolve_missing():
    triage = _mod("triage")
    assert "--resolve-missing" in triage._PLAN_USAGE


def test_plan_cmd_requires_pick(capsys, tmp_path):
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    rc = triage.plan_cmd(str(base), {}, [])
    assert rc == 2
    assert "usage" in capsys.readouterr().err


def test_cli_plan_verb_is_wired_through_main(tmp_path):
    triage = _mod("triage")
    base = _plan_sdlc(tmp_path)
    rc = triage.main(["triage.py", "plan", str(base), "--pick", "10"])
    assert rc == 0


def test_cli_usage_mentions_plan_verb(capsys):
    triage = _mod("triage")
    rc = triage.main(["triage.py", "bogus"])
    assert rc == 2
    assert "plan <sdlc_dir>" in capsys.readouterr().err


# ===================================================================================================
# #670: `enact` (sigma-triage 3/4) -- compiles an already-written plan.json into native primitives.
# Fixture note: `_gh_sdlc` above is reused for a real discovery.github config; `_recording_runner`'s
# per-VERB-only keying (args[1]) cannot express different `gh issue view` responses for different
# issue numbers, which every enact test needs -- `_enact_runner` below is the per-issue-number-aware
# sibling this goal needs, a new small local builder added exactly where one is needed (the same
# precedent `_wave_fixture`/`_survey_fixture` above already set, never generalizing an existing one).
# ===================================================================================================


def _view_json(labels=(), assignees=(), body=""):
    return json.dumps({"labels": [{"name": n} for n in labels],
                       "assignees": [{"login": a} for a in assignees], "body": body})


def _enact_runner(views=None, fail_on=()):
    """Fake `gh` runner for enact: `gh issue view <N> ...` responses come from `views={"<N>":
    <_view_json(...) string>}`, keyed by ISSUE NUMBER (unlike `_recording_runner`'s per-VERB-only
    keying). Every other call is recorded and returns "" (success) unless its full argument list
    contains one of `fail_on`'s substrings, in which case it raises -- the mechanism the
    partial-failure tests need. `run.calls` is the full call log, same convention as
    `_recording_runner`."""
    calls, fallbacks, rest_writes = [], [], []
    views = views or {}
    labels = set()
    def run(args):
        joined = " ".join(str(a) for a in args)
        for needle in fail_on:
            if needle in joined:
                raise RuntimeError(f"simulated gh failure: {needle}")
        # #1392: an unpark is now ONE `swap-label` action, executed through
        # `sources._swap_labels` -- i.e. `gh api graphql`, not `gh issue edit`. `gqlfake.swap`
        # answers the three request shapes that transport needs and translates the mutation back
        # into the synthetic `issue edit ... --add-label/--remove-label` call records these tests'
        # existing call-log assertions are written against.
        gql = gqlfake.swap(args, labels=labels, calls=calls, repo_args=("--repo", "o/r"))
        if gql is not None:
            return gql
        if gqlfake.is_issue_write(args):                # #895 slice 3a: REST writes, legacy-recorded
            rest_writes.append(list(args))
            return gqlfake.rest_write(args, calls=calls, repo_args=("--repo", "acme/widget"), labels=labels)
        calls.append(list(args))
        # #895: the per-issue state read is REST first; the gh-shape `views` are answered REST-shaped.
        rest = gqlfake.rest_issue(args, lambda n, f: views.get(n, _view_json()))
        if rest is not None:
            return rest
        if len(args) >= 3 and args[0] == "issue" and args[1] == "view":
            fallbacks.append(list(args))      # the one fallback; REST-first tests assert this empty
            return views.get(args[2], _view_json())
        return ""
    run.calls = calls
    run.fallbacks = fallbacks
    run.labels = labels
    run.rest_writes = rest_writes
    return run


def _picked_item(issue, priority=None, model=None, wave=1):
    return {"issue": issue, "priority": priority, "model": model, "wave": wave}


def _edge(blocked, by, source="cli", external=False):
    return {"blocked": blocked, "by": by, "source": source, "external": external}


def _plan(picked=(), edges=(), deferred=(), slug="s", cap=3):
    return {"version": 1, "schema": "triage-plan/v1", "generated_at": "2026-01-01T00:00:00Z",
           "slug": slug, "cap": cap, "picked": list(picked), "edges": list(edges),
           "deferred": list(deferred), "waves": []}


def _enact_config(**gh):
    return {"discovery": {"source": "github", "github": {"repo": "acme/widget", **gh}}}


# --- _load_plan -------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["missing", "bad_json", "wrong_schema", "not_a_dict",
                                  "picked_not_list"])
def test_load_plan_rejects_bad_input_naming_the_problem(tmp_path, kind):
    triage = _mod("triage")
    path = tmp_path / "plan.json"
    if kind == "missing":
        target = str(tmp_path / "nope.json")
    elif kind == "bad_json":
        path.write_text("not json")
        target = str(path)
    elif kind == "wrong_schema":
        path.write_text(json.dumps({"schema": "triage-survey/v1"}))
        target = str(path)
    elif kind == "not_a_dict":
        path.write_text(json.dumps([1, 2, 3]))
        target = str(path)
    else:
        path.write_text(json.dumps({"schema": "triage-plan/v1", "picked": "nope"}))
        target = str(path)
    with pytest.raises(ValueError):
        triage._load_plan(target)


def test_load_plan_accepts_a_well_formed_plan(tmp_path):
    triage = _mod("triage")
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_plan(picked=[_picked_item(10)])))
    loaded = triage._load_plan(str(path))
    assert loaded["schema"] == "triage-plan/v1"
    assert loaded["picked"][0]["issue"] == 10


# --- _fetch_issue_state ------------------------------------------------------------------------------


def test_fetch_issue_state_parses_labels_assignees_body():
    triage = _mod("triage")
    source = triage.sources.GitHubSource(_enact_config(),
        run=_enact_runner(views={"10": _view_json(labels=["sdlc:goal"], assignees=["dana"],
                                                   body="hello")}))
    state = triage._fetch_issue_state(source, "10")
    assert state == {"labels": {"sdlc:goal"}, "assignees": {"dana"}, "body": "hello"}


def test_fetch_issue_state_returns_none_on_failure():
    triage = _mod("triage")
    source = triage.sources.GitHubSource(_enact_config(),
                                         run=_enact_runner(fail_on=["issues/10", "issue view 10"]))
    assert triage._fetch_issue_state(source, "10") is None     # GhApiError reaches the site's own arm


def test_fetch_issue_state_reads_rest_first_with_no_fallback():
    triage = _mod("triage")
    run = _enact_runner(views={"10": _view_json(labels=["sdlc:goal"])})
    triage._fetch_issue_state(triage.sources.GitHubSource(_enact_config(), run=run), "10")
    assert [c for c in run.calls if c[0] == "api"] == [["api", "repos/acme/widget/issues/10", "--method", "GET"]]
    assert run.fallbacks == []


# --- _already_marked (marker-dedup precision; F2) --------------------------------------------------


def test_already_marked_detects_the_established_marker_string():
    triage = _mod("triage")
    assert triage._already_marked("some text\n\n**Blocked by:** #42\n", "42") is True
    assert triage._already_marked("some text\n\n**Blocked by:** #42\n", "15") is False


def test_already_marked_distinguishes_two_separate_blocker_lines():
    triage = _mod("triage")
    body = "**Blocked by:** #12\n\n**Blocked by:** #15\n"
    assert triage._already_marked(body, "12") is True
    assert triage._already_marked(body, "15") is True


def test_already_marked_known_accepted_prose_false_positive():
    """F2 (670-plan-review.md, binding): ordinary prose ('after #12', 'needs #12') reads as an
    existing marker for #12 -- pinned as KNOWN, ACCEPTED behaviour per the issue's own literal
    'anything _BLOCK_RE already detects... counts as existing' rule, not tightened here."""
    triage = _mod("triage")
    assert triage._already_marked("This work needs #12 landed first, then we continue.", "12") is True
    assert triage._already_marked("Do this after #12 ships.", "12") is True


def test_edge_actions_prose_mention_suppresses_the_real_marker_known_accepted():
    """F2's consequence one level up: a picked issue whose body merely MENTIONS its blocker in
    ordinary prose gets NO append-marker action -- the real marker is silently skipped, per the
    spec's own literal, accepted rule (see `_already_marked`'s docstring), not a bug."""
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": set(), "assignees": set(),
                             "body": "This work needs #12 landed first."}}
    plan = _plan(picked=[_picked_item(42)], edges=[_edge(42, 12)])
    assert triage._edge_actions(plan, state_by_issue) == []


# --- _picked_actions (assign / labels / unpark) ------------------------------------------------------


def test_picked_actions_unpark_readds_goal_label_and_drops_parked_label():
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": {"sdlc:parked", "sdlc:in-progress"}, "assignees": set(),
                             "body": ""}}
    plan = _plan(picked=[_picked_item(42)])
    actions = triage._picked_actions(plan, state_by_issue, None, "sdlc:goal", "sdlc:parked")
    # #1392: the unpark is ONE atomic `swap-label`, not an add/remove PAIR -- a half-applied pair
    # left `sdlc:goal` added with `sdlc:parked` still on, which `_fetch_pending` excludes, so the
    # goal stayed exactly as unpickable as before while now carrying two lifecycle labels.
    assert [(a["action"], a["detail"]) for a in actions] == [
        ("swap-label", "+sdlc:goal -sdlc:parked")]
    assert actions[0]["add"] == ["sdlc:goal"] and actions[0]["remove"] == ["sdlc:parked"]
    assert not any("sdlc:in-progress" in (a["detail"] or "") for a in actions)


# --- #1358: `_bucket_parked` (survey) now surfaces a `sdlc:blocked`-only issue in the pickable
# 'parked' bucket too -- `enact` must be able to clear THAT label as well, or picking one would add
# `sdlc:goal` back without ever removing `sdlc:blocked`, leaving both present at once (the exact
# double-primary-label violation #1350's own `_offboard` fix exists to avoid). `blocked_label` is
# an optional, KEYWORD-only addition defaulting to `None` so every existing positional call above
# (none of which pass it) stays byte-for-byte unchanged.


def test_picked_actions_unpark_also_drops_blocked_label_when_configured():
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": {"sdlc:blocked"}, "assignees": set(), "body": ""}}
    plan = _plan(picked=[_picked_item(42)])
    actions = triage._picked_actions(plan, state_by_issue, None, "sdlc:goal", "sdlc:parked",
                                     blocked_label="sdlc:blocked")
    assert [(a["action"], a["detail"]) for a in actions] == [
        ("swap-label", "+sdlc:goal -sdlc:blocked")]
    assert actions[0]["remove"] == ["sdlc:blocked"]


def test_picked_actions_blocked_label_omitted_is_byte_identical_to_before():
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": {"sdlc:blocked"}, "assignees": set(), "body": ""}}
    plan = _plan(picked=[_picked_item(42)])
    actions = triage._picked_actions(plan, state_by_issue, None, "sdlc:goal", "sdlc:parked")
    assert not any("sdlc:blocked" in (a["detail"] or "") for a in actions)


def test_compute_actions_threads_blocked_label_through_to_picked_actions():
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": {"sdlc:blocked"}, "assignees": set(), "body": ""}}
    plan = _plan(picked=[_picked_item(42)])
    actions = triage.compute_actions(plan, state_by_issue, None, "sdlc:goal", "sdlc:parked",
                                     blocked_label="sdlc:blocked")
    assert any(a["action"] == "swap-label" and a["remove"] == ["sdlc:blocked"] for a in actions)


def test_picked_actions_assignee_positive_and_negative_control():
    """F3 amendment on test 13 (670-plan-review.md, binding): the negative case alone (no assignee
    configured -> zero assign actions) would pass against an implementation that never emits
    `assign` at all. This test also proves the positive control: assignee configured + issue
    unassigned -> exactly one assign action IS emitted."""
    triage = _mod("triage")
    state = {"labels": {"sdlc:goal"}, "assignees": set(), "body": ""}
    plan = _plan(picked=[_picked_item(42)])

    negative = triage._picked_actions(plan, {"42": state}, None, "sdlc:goal", "sdlc:parked")
    assert not any(a["action"] == "assign" for a in negative)

    positive = triage._picked_actions(plan, {"42": state}, "dana", "sdlc:goal", "sdlc:parked")
    assign_actions = [a for a in positive if a["action"] == "assign"]
    assert len(assign_actions) == 1
    assert assign_actions[0]["detail"] == "dana"


def test_picked_actions_no_action_when_already_fully_compliant():
    triage = _mod("triage")
    state = {"labels": {"sdlc:goal", "priority:P1"}, "assignees": {"dana"}, "body": ""}
    plan = _plan(picked=[_picked_item(42, priority="P1")])
    actions = triage._picked_actions(plan, {"42": state}, "dana", "sdlc:goal", "sdlc:parked")
    assert actions == []


def test_picked_actions_null_priority_and_model_produce_no_label_action_for_that_field():
    triage = _mod("triage")
    state = {"labels": {"sdlc:goal", "model:daily"}, "assignees": set(), "body": ""}
    plan = _plan(picked=[_picked_item(42, priority=None, model="daily")])
    actions = triage._picked_actions(plan, {"42": state}, None, "sdlc:goal", "sdlc:parked")
    assert not any(a["detail"].startswith("priority:") for a in actions)
    assert not any(a["detail"].startswith("model:") for a in actions)


def test_picked_actions_state_fetch_failure_reports_exactly_once():
    triage = _mod("triage")
    plan = _plan(picked=[_picked_item(42, priority="P1", model="daily")])
    actions = triage._picked_actions(plan, {"42": None}, "dana", "sdlc:goal", "sdlc:parked")
    assert len(actions) == 1
    assert actions[0]["result"] == "failed"


# --- _edge_actions (markers; self-ref + duplicate-edge guards, F1) ---------------------------------


def test_edge_actions_emits_a_marker_when_missing():
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": set(), "assignees": set(), "body": ""}}
    plan = _plan(picked=[_picked_item(42)], edges=[_edge(42, 12)])
    actions = triage._edge_actions(plan, state_by_issue)
    assert actions == [{"action": "append-marker", "issue": "42", "detail": "12",
                        "result": None, "error": None}]


def test_edge_actions_skips_an_already_marked_blocker():
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": set(), "assignees": set(), "body": "**Blocked by:** #12\n"}}
    plan = _plan(picked=[_picked_item(42)], edges=[_edge(42, 12)])
    assert triage._edge_actions(plan, state_by_issue) == []


def test_edge_actions_different_blockers_both_get_their_own_marker():
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": set(), "assignees": set(), "body": "**Blocked by:** #12\n"}}
    plan = _plan(picked=[_picked_item(42)], edges=[_edge(42, 12), _edge(42, 15)])
    actions = triage._edge_actions(plan, state_by_issue)
    assert [a["detail"] for a in actions] == ["15"]


def test_edge_actions_duplicate_edge_produces_exactly_one_marker_action():
    """F1 (670-plan-review.md, binding): `--edge 15:12 --edge 15:12` (a repeatable flag, sanctioned
    by `plan`) writes the identical edge object twice into `plan.json`. Without the `seen`-set guard
    both would independently emit `append-marker` against the same pre-write body snapshot."""
    triage = _mod("triage")
    state_by_issue = {"15": {"labels": set(), "assignees": set(), "body": ""}}
    plan = _plan(picked=[_picked_item(15)], edges=[_edge(15, 12), _edge(15, 12)])
    actions = triage._edge_actions(plan, state_by_issue)
    assert len(actions) == 1


def test_edge_actions_self_reference_is_skipped_defensively():
    triage = _mod("triage")
    state_by_issue = {"12": {"labels": set(), "assignees": set(), "body": ""}}
    plan = _plan(picked=[_picked_item(12)], edges=[_edge(12, 12)])
    assert triage._edge_actions(plan, state_by_issue) == []


def test_edge_actions_external_by_still_gets_a_marker_on_the_blocked_picked_issue():
    triage = _mod("triage")
    state_by_issue = {"15": {"labels": set(), "assignees": set(), "body": ""}}
    plan = _plan(picked=[_picked_item(15)], edges=[_edge(15, 99, external=True)])
    actions = triage._edge_actions(plan, state_by_issue)
    assert len(actions) == 1 and actions[0]["detail"] == "99"


def test_edge_actions_blocked_side_outside_picked_is_never_touched():
    """An edge whose `blocked` isn't in `state_by_issue` at all (never fetched -- only picked
    issues are) is silently skipped, the same posture as never touching a deferred issue."""
    triage = _mod("triage")
    plan = _plan(picked=[_picked_item(15)], edges=[_edge(99, 12)])
    assert triage._edge_actions(plan, {}) == []


# --- report-only notices: F4 (label conflict), F5 (stale marker) -----------------------------------


def test_label_conflict_notice_names_both_values_while_the_add_only_action_still_fires():
    """F4 (670-plan-review.md, binding, ruling on Open Decision 3): no removal -- the plan's OWN
    value is still added (that IS 'add only what's missing'); the notice is additional, surfacing
    the resulting double-tier residual, never a substitute for the action."""
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": {"priority:P3"}, "assignees": set(), "body": ""}}
    plan = _plan(picked=[_picked_item(42, priority="P1")])

    notices = triage._label_conflict_notices(plan, state_by_issue)
    assert len(notices) == 1
    assert "priority:P1" in notices[0] and "priority:P3" in notices[0] and "#42" in notices[0]

    actions = triage._picked_actions(plan, state_by_issue, None, "sdlc:goal", "sdlc:parked")
    assert any(a["detail"] == "priority:P1" for a in actions)


def test_label_conflict_notice_silent_when_values_match_or_absent():
    triage = _mod("triage")
    plan = _plan(picked=[_picked_item(42, priority="P1")])
    matching = {"42": {"labels": {"priority:P1"}, "assignees": set(), "body": ""}}
    assert triage._label_conflict_notices(plan, matching) == []
    absent = {"42": {"labels": set(), "assignees": set(), "body": ""}}
    assert triage._label_conflict_notices(plan, absent) == []


def test_stale_marker_notice_for_a_blocker_not_in_this_plans_edges():
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": set(), "assignees": set(), "body": "**Blocked by:** #99\n"}}
    plan = _plan(picked=[_picked_item(42)], edges=[])
    notices = triage._stale_marker_notices(plan, state_by_issue)
    assert len(notices) == 1
    assert "#99" in notices[0] and "#42" in notices[0]


def test_stale_marker_notice_silent_when_the_blocker_is_in_this_plans_edges():
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": set(), "assignees": set(), "body": "**Blocked by:** #12\n"}}
    plan = _plan(picked=[_picked_item(42)], edges=[_edge(42, 12)])
    assert triage._stale_marker_notices(plan, state_by_issue) == []


def test_stale_marker_notice_excludes_a_self_mention_and_dedupes_repeats():
    triage = _mod("triage")
    state_by_issue = {"42": {"labels": set(), "assignees": set(),
                             "body": "see also #42. needs #99 first. after #99 too."}}
    plan = _plan(picked=[_picked_item(42)], edges=[])
    notices = triage._stale_marker_notices(plan, state_by_issue)
    assert len(notices) == 1
    assert "#99" in notices[0]


# --- compute_actions / compute_notices (ordering; F3 on test 18) -----------------------------------


def test_compute_actions_is_picked_then_edges_and_matches_a_pinned_sequence():
    """F3 amendment on test 18 (670-plan-review.md, binding): pins the ACTUAL expected
    (action, issue, detail) sequence for a known fixture, not merely self-consistency across
    reruns (which any deterministic implementation, including a wrong or empty one, would satisfy)."""
    triage = _mod("triage")
    state_by_issue = {
        "10": {"labels": set(), "assignees": set(), "body": ""},
        "20": {"labels": {"sdlc:goal"}, "assignees": set(), "body": ""},
    }
    plan = _plan(picked=[_picked_item(10, priority="P1"), _picked_item(20)], edges=[_edge(20, 10)])
    actions = triage.compute_actions(plan, state_by_issue, None, "sdlc:goal", "sdlc:parked")
    sequence = [(a["action"], a["issue"], a["detail"]) for a in actions]
    assert sequence == [
        # #1392: the lifecycle label is one `swap-label`; `priority:*`/`model:*` stay plain
        # `add-label` adds -- they are annotations, not lifecycle state, and nothing is traded away
        # by writing them separately.
        ("swap-label", "10", "+sdlc:goal"),
        ("add-label", "10", "priority:P1"),
        ("append-marker", "20", "10"),
    ]


def test_compute_actions_deterministic_across_reruns():
    triage = _mod("triage")
    state_by_issue = {"10": {"labels": set(), "assignees": set(), "body": ""}}
    plan = _plan(picked=[_picked_item(10, priority="P1", model="daily")])
    first = triage.compute_actions(plan, state_by_issue, "dana", "sdlc:goal", "sdlc:parked")
    for _ in range(3):
        assert triage.compute_actions(plan, state_by_issue, "dana", "sdlc:goal", "sdlc:parked") == first


# --- _label_create_calls -----------------------------------------------------------------------------


def test_label_create_calls_deduped_in_first_seen_order():
    triage = _mod("triage")
    plan = _plan(picked=[_picked_item(10, priority="P1", model="daily"),
                        _picked_item(20, priority="P1", model="bulk")])
    assert triage._label_create_calls(plan) == ["priority:P1", "model:daily", "model:bulk"]


# --- apply_actions (execution; dry-run purity; partial failure) ------------------------------------


def test_apply_actions_dry_run_never_calls_gh_for_writes():
    triage = _mod("triage")
    run = _enact_runner()
    source = triage.sources.GitHubSource(_enact_config(), run=run)
    actions = [{"action": "add-label", "issue": "10", "detail": "sdlc:goal", "result": None, "error": None}]
    out = triage.apply_actions(source, actions, False)
    assert out[0]["result"] == "would"
    assert run.calls == []


def test_apply_actions_apply_executes_and_marks_done():
    triage = _mod("triage")
    run = _enact_runner()
    source = triage.sources.GitHubSource(_enact_config(), run=run)
    actions = [{"action": "add-label", "issue": "10", "detail": "sdlc:goal", "result": None, "error": None}]
    out = triage.apply_actions(source, actions, True)
    assert out[0]["result"] == "done"
    assert ["issue", "edit", "10", "--repo", "acme/widget", "--add-label", "sdlc:goal"] in run.calls


def test_apply_actions_partial_failure_isolates_and_continues():
    triage = _mod("triage")
    run = _enact_runner(fail_on=["labels[]=priority:P1"])
    source = triage.sources.GitHubSource(_enact_config(), run=run)
    actions = [
        {"action": "add-label", "issue": "12", "detail": "sdlc:goal", "result": None, "error": None},
        {"action": "add-label", "issue": "12", "detail": "priority:P1", "result": None, "error": None},
        {"action": "assign", "issue": "18", "detail": "dana", "result": None, "error": None},
    ]
    out = triage.apply_actions(source, actions, True)
    results = {(a["issue"], a["detail"]): a["result"] for a in out}
    assert results[("12", "sdlc:goal")] == "done"
    assert results[("12", "priority:P1")] == "failed"
    assert results[("18", "dana")] == "done"
    assert next(a for a in out if a["result"] == "failed")["error"]


def test_apply_actions_state_fetch_failure_passed_through_unchanged_in_both_modes():
    triage = _mod("triage")
    source = triage.sources.GitHubSource(_enact_config(), run=_enact_runner())
    sentinel = {"action": "add-label", "issue": "10", "detail": "(current state unknown)",
               "result": "failed", "error": "boom"}
    assert triage.apply_actions(source, [sentinel], False) == [sentinel]
    assert triage.apply_actions(source, [sentinel], True) == [sentinel]


def test_ensure_arbitrary_labels_failure_does_not_block_the_add_label_call():
    triage = _mod("triage")
    run = _enact_runner(fail_on=["label create priority:P1"])
    source = triage.sources.GitHubSource(_enact_config(), run=run)
    triage._ensure_arbitrary_labels(source, ["priority:P1"])
    actions = [{"action": "add-label", "issue": "10", "detail": "priority:P1", "result": None, "error": None}]
    out = triage.apply_actions(source, actions, True)
    assert out[0]["result"] == "done"


# --- enact() orchestrator ----------------------------------------------------------------------------


def test_enact_dry_run_and_apply_compute_the_identical_action_list():
    triage = _mod("triage")
    views = {"10": _view_json(body="")}
    plan = _plan(picked=[_picked_item(10, priority="P1", model="daily")])
    config = _enact_config(assignee="dana")

    dry = triage.enact(".sdlc", config, plan, apply=False, run=_enact_runner(views=views))
    wet = triage.enact(".sdlc", config, plan, apply=True, run=_enact_runner(views=views))

    dry_triples = [(a["action"], a["issue"], a["detail"]) for a in dry["actions"]]
    wet_triples = [(a["action"], a["issue"], a["detail"]) for a in wet["actions"]]
    assert dry_triples == wet_triples
    assert dry_triples   # non-empty -- proves parity over something real, not two empty lists
    assert all(a["result"] == "would" for a in dry["actions"])
    assert all(a["result"] == "done" for a in wet["actions"])


def test_enact_dry_run_issues_zero_write_shaped_gh_calls():
    """PR #683 review (BLOCKING, cycle 1/3): `enact()` called `source._ensure_labels()`
    unconditionally, before the `if apply:` gate -- three `gh label create --force` calls (real
    repo-level writes: create-or-recolor `sdlc:goal`/`sdlc:in-progress`/`sdlc:parked`) fired on
    EVERY dry-run, not just under --apply, violating "DRY-RUN by default: ... change nothing."
    `test_apply_actions_dry_run_never_calls_gh_for_writes` alone could never catch this: it drives
    `apply_actions` directly with a hand-built action list, one level below `enact()`, and never
    exercises `enact()`'s own `_ensure_labels()` call at all -- this is the enact()-level sibling
    that does. Checks the actual call log (any `label create`/`issue edit` — the write-shaped gh
    subcommands), not just the returned action list, the same non-vacuous-witness discipline
    `test_enact_never_touches_a_deferred_issue` already established."""
    triage = _mod("triage")
    plan = _plan(picked=[_picked_item(10, priority="P1", model="daily")], edges=[_edge(10, 5)])
    config = _enact_config(assignee="dana")
    run = _enact_runner(views={"10": _view_json()})

    triage.enact(".sdlc", config, plan, apply=False, run=run)

    write_calls = [c for c in run.calls if len(c) > 1 and c[1] in ("create", "edit")]
    assert write_calls == []


def test_enact_second_apply_produces_zero_actions():
    """F3 amendment on test 3 (670-plan-review.md, binding): asserts the FIRST apply produced a
    non-empty action list, so the second run's emptiness proves convergence, not an
    always-empty implementation."""
    triage = _mod("triage")
    plan = _plan(picked=[_picked_item(10, priority="P1", model="daily")])
    config = _enact_config()

    before = {"10": _view_json(body="")}
    first = triage.enact(".sdlc", config, plan, apply=True, run=_enact_runner(views=before))
    assert first["actions"]
    assert all(a["result"] == "done" for a in first["actions"])

    after = {"10": _view_json(labels=["sdlc:goal", "priority:P1", "model:daily"], body="")}
    second = triage.enact(".sdlc", config, plan, apply=True, run=_enact_runner(views=after))
    assert second["actions"] == []


def test_enact_no_assignee_configured_emits_zero_assign_actions():
    triage = _mod("triage")
    plan = _plan(picked=[_picked_item(10)])
    config = _enact_config()
    result = triage.enact(".sdlc", config, plan, apply=False,
                          run=_enact_runner(views={"10": _view_json(labels=["sdlc:goal"])}))
    assert not any(a["action"] == "assign" for a in result["actions"])
    assert result["actions"] == []


def test_enact_never_touches_a_deferred_issue():
    """Proves the claim against the actual `gh` CALL LOG, not just the action list -- a raising
    guard would be caught by `_fetch_issue_state`'s own broad `except Exception: return None` and
    silently prove nothing (confirmed live: a deliberately-introduced leak that fetched a deferred
    issue's state passed a raising-guard version of this test without detection, because issue 40
    can never produce an ACTION either way -- it is in neither `picked` nor `edges` -- so only the
    call log is a non-vacuous witness)."""
    triage = _mod("triage")
    plan = _plan(picked=[_picked_item(10)], deferred=[{"issue": 40, "why": "later"}])
    config = _enact_config(assignee="dana")
    run = _enact_runner(views={"10": _view_json()})

    result = triage.enact(".sdlc", config, plan, apply=True, run=run)

    assert not any(a["issue"] == "40" for a in result["actions"])
    assert not any("40" in str(x) for call in run.calls for x in call)


def test_enact_makes_zero_ledger_calls(monkeypatch):
    triage = _mod("triage")
    def boom(*a, **k):
        raise AssertionError("enact must never write to the ledger")
    monkeypatch.setattr(triage.ledger, "append", boom)
    monkeypatch.setattr(triage.ledger, "safe_append", boom)
    plan = _plan(picked=[_picked_item(10, priority="P1")])
    config = _enact_config(assignee="dana")
    triage.enact(".sdlc", config, plan, apply=True, run=_enact_runner(views={"10": _view_json()}))


def test_enact_result_carries_degraded_notices_alongside_the_add_only_action():
    """F4 + F5 together: a conflicting label produces BOTH the notice (never suppressing the
    normal add-only action for the plan's OWN value) and a stale-marker notice; notices never
    themselves become action-list entries."""
    triage = _mod("triage")
    plan = _plan(picked=[_picked_item(42, priority="P1")], edges=[])
    config = _enact_config()
    body = "**Blocked by:** #99\n"
    result = triage.enact(".sdlc", config, plan, apply=False,
                          run=_enact_runner(views={"42": _view_json(
                              labels=["sdlc:goal", "priority:P3"], body=body)}))
    assert len(result["degraded"]) == 2
    assert any("priority:P1" in n and "priority:P3" in n for n in result["degraded"])
    assert any("#99" in n for n in result["degraded"])
    assert any(a["action"] == "add-label" and a["detail"] == "priority:P1" for a in result["actions"])
    assert all(a["action"] in triage.ENACT_ACTIONS for a in result["actions"])


def test_action_dict_shape_is_stable():
    triage = _mod("triage")
    plan = _plan(picked=[_picked_item(10, priority="P1")])
    result = triage.enact(".sdlc", _enact_config(), plan, apply=False,
                          run=_enact_runner(views={"10": _view_json()}))
    for a in result["actions"]:
        # #1392: `swap-label` carries two ADDITIVE keys beyond the shared five -- `detail` stays a
        # human-readable string (`render_actions` formats it directly), and the machine-readable
        # label lists ride alongside it rather than inside it.
        expected = {"action", "issue", "detail", "result", "error"}
        if a["action"] == "swap-label":
            expected |= {"add", "remove"}
        assert set(a) == expected
        assert a["action"] in triage.ENACT_ACTIONS


# --- render_actions ------------------------------------------------------------------------------


def test_render_actions_smoke_empty_and_nonempty():
    triage = _mod("triage")
    empty = {"slug": "s", "cap": 3, "apply": False, "actions": [], "degraded": []}
    assert "no actions needed" in triage.render_actions(empty)

    nonempty = {"slug": "s", "cap": 3, "apply": True, "actions": [
        {"action": "add-label", "issue": "10", "detail": "sdlc:goal", "result": "done", "error": None},
        {"action": "assign", "issue": "12", "detail": "dana", "result": "failed", "error": "boom"},
    ], "degraded": ["#42: a notice"]}
    out = triage.render_actions(nonempty)
    assert "DRY-RUN" not in out and "APPLIED" in out
    assert "#10" in out and "#12" in out and "boom" in out
    assert "Notices" in out and "#42: a notice" in out


def test_render_actions_marker_line_has_no_stray_space_before_the_number():
    """PR #683 review (FOLD, cycle 1/3): `_ACTION_VERBS["append-marker"] = "mark blocked by #"`
    plus the line format `f"{verb} {a['detail']}"` rendered "mark blocked by # 671" (a stray space
    between the '#' and the number) -- pins the corrected line exactly, not just "contains 671"."""
    triage = _mod("triage")
    result = {"slug": "s", "cap": 3, "apply": False, "actions": [
        {"action": "append-marker", "issue": "42", "detail": "671", "result": "would", "error": None},
    ], "degraded": []}
    out = triage.render_actions(result)
    assert "mark blocked by #671" in out
    assert "# 671" not in out


# --- CLI wiring -----------------------------------------------------------------------------------


def test_plan_flag_value_extracts_the_path():
    triage = _mod("triage")
    assert triage._plan_flag_value(["--plan", "/tmp/x.json", "--apply"]) == "/tmp/x.json"
    assert triage._plan_flag_value(["--apply"]) is None


def test_enact_cmd_requires_plan_flag(capsys, tmp_path):
    triage = _mod("triage")
    rc = triage.enact_cmd(str(tmp_path), {}, [])
    assert rc == 2
    assert "usage" in capsys.readouterr().err


def test_enact_cmd_bad_plan_file_is_exit_2(capsys, tmp_path):
    triage = _mod("triage")
    rc = triage.enact_cmd(str(tmp_path), {}, ["--plan", str(tmp_path / "nope.json")])
    assert rc == 2
    assert "triage enact:" in capsys.readouterr().err


def test_enact_cmd_dry_run_by_default_apply_flag_flips_it(capsys, tmp_path):
    triage = _mod("triage")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(picked=[_picked_item(10)])))
    config = _enact_config()

    rc1 = triage.enact_cmd(str(tmp_path), config, ["--plan", str(plan_path)],
                           run=_enact_runner(views={"10": _view_json()}))
    assert rc1 == 0
    assert "DRY-RUN" in capsys.readouterr().out

    rc2 = triage.enact_cmd(str(tmp_path), config, ["--plan", str(plan_path), "--apply"],
                           run=_enact_runner(views={"10": _view_json()}))
    assert rc2 == 0
    assert "APPLIED" in capsys.readouterr().out


def test_enact_cmd_nonzero_exit_when_an_action_fails(tmp_path):
    triage = _mod("triage")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(picked=[_picked_item(10, priority="P1")])))
    config = _enact_config()
    run = _enact_runner(views={"10": _view_json()}, fail_on=["labels[]=priority:P1"])
    rc = triage.enact_cmd(str(tmp_path), config, ["--plan", str(plan_path), "--apply"], run=run)
    assert rc == 1


def test_cli_enact_verb_is_wired_through_main(tmp_path, monkeypatch):
    triage = _mod("triage")
    # main()'s dispatch (like its own "survey" branch) doesn't thread a `run=` through -- fake the
    # module-level default `sources._run_gh` instead, exactly `test_cli_survey_json_round_trips...`
    # already does, so this never risks a real gh call against the real repo.
    monkeypatch.setattr(triage.sources, "_run_gh", lambda a: "")
    base = _gh_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(picked=[])))
    rc = triage.main(["triage.py", "enact", str(base), "--plan", str(plan_path)])
    assert rc == 0


def test_cli_usage_mentions_enact_verb(capsys):
    triage = _mod("triage")
    rc = triage.main(["triage.py", "bogus"])
    assert rc == 2
    assert "enact <sdlc_dir>" in capsys.readouterr().err


# --- #1391 step 4: the reconciler must be able to move a board card ------------------------------
# Until this existed `_execute_action` had four kinds and none could touch the board, so every
# unpark fixed the LABEL and left the CARD in Blocked -- on a board-authoritative repo that IS a
# permanently-unpickable goal, manufactured by the recovery machinery itself.


class _FakeBoardSource:
    """Minimal source double exposing just the surface `set-status` uses."""
    def __init__(self, lands=True):
        self.lands = lands
        self.moves = []

    def _set_board_status(self, goal, status_name):
        self.moves.append((goal, status_name))
        return self.lands

    def _repo_args(self):
        return []


def test_execute_action_supports_set_status():
    tr = _mod("triage")
    src = _FakeBoardSource()
    tr._execute_action(src, {"action": "set-status", "issue": "42", "detail": "Ready"})
    assert src.moves == [("42", "Ready")]


def test_set_status_raises_when_the_board_write_does_not_land():
    """The honest-bool return exists precisely so a card move that silently failed is recorded as
    `failed`, never reported as `done`."""
    import pytest as _pytest
    tr = _mod("triage")
    src = _FakeBoardSource(lands=False)
    with _pytest.raises(Exception):
        tr._execute_action(src, {"action": "set-status", "issue": "42", "detail": "Ready"})


def test_apply_actions_records_a_failed_card_move_as_failed():
    tr = _mod("triage")
    src = _FakeBoardSource(lands=False)
    out = tr.apply_actions(src, [{"action": "set-status", "issue": "42", "detail": "Ready",
                                  "result": None, "error": None}], True)
    assert out[0]["result"] == "failed" and out[0]["error"]


def test_apply_actions_records_a_landed_card_move_as_done():
    tr = _mod("triage")
    src = _FakeBoardSource(lands=True)
    out = tr.apply_actions(src, [{"action": "set-status", "issue": "42", "detail": "Ready",
                                  "result": None, "error": None}], True)
    assert out[0]["result"] == "done" and out[0]["error"] is None


def test_apply_actions_dry_run_never_moves_a_card():
    tr = _mod("triage")
    src = _FakeBoardSource()
    out = tr.apply_actions(src, [{"action": "set-status", "issue": "42", "detail": "Ready",
                                  "result": None, "error": None}], False)
    assert out[0]["result"] == "would"
    assert src.moves == []


def test_enqueued_bucket_never_lists_a_blocked_goal_as_ready(tmp_path):
    """#1393 plan-review finding (BLOCKING). `mark_blocked` used to REMOVE `sdlc:goal`, so a blocked
    goal failed `_bucket_enqueued`'s first test and could never reach the rest of the filter. Now
    that `sdlc:blocked` is an overlay riding alongside membership, it does reach it -- and without
    the added exclusion every blocked goal on the board is reported as ready to pick, and a
    compiled drain plan schedules it."""
    triage = _mod("triage")
    issues = [
        {"number": 42, "title": "blocked", "body": "",
         "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:blocked"}], "assignees": []},
        {"number": 43, "title": "ready", "body": "",
         "labels": [{"name": "sdlc:goal"}], "assignees": []},
    ]
    bucket = triage._bucket_enqueued(issues, {"repo": "acme/widget"})
    assert [i["number"] for i in bucket["items"]] == [43]


def test_enqueued_bucket_still_lists_a_plain_goal_and_still_excludes_parked_and_in_progress():
    """The three pre-existing exclusions, re-asserted beside the new one so a future edit that drops
    one is caught here rather than in production."""
    triage = _mod("triage")
    issues = [
        {"number": 41, "title": "parked", "body": "",
         "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:parked"}], "assignees": []},
        {"number": 42, "title": "active", "body": "",
         "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}], "assignees": []},
        {"number": 43, "title": "ready", "body": "",
         "labels": [{"name": "sdlc:goal"}], "assignees": []},
    ]
    bucket = triage._bucket_enqueued(issues, {"repo": "acme/widget"})
    assert [i["number"] for i in bucket["items"]] == [43]


def test_picking_a_proposal_promotes_it_instead_of_stacking_labels():
    """#1393: picking an issue that carries `sdlc:needs-confirmation` used to ADD `sdlc:goal` on top
    of it, producing exactly the half-promoted state `_fetch_pending`, `_card_is_eligible` and
    `/sigma-promote`'s `drift` bucket all call drift -- so `enact` manufactured the corruption
    promote.py exists to repair, and the picked issue stayed unpickable afterwards. A human choosing
    an issue in a triage plan IS the approval, so picking is now the atomic promote transition."""
    triage = _mod("triage")
    state = {"50": {"labels": {"sdlc:needs-confirmation", "sdlc:followup"}, "assignees": set(),
                    "body": ""}}
    actions = triage.compute_actions(_plan(picked=[_picked_item(50)]), state, None, "sdlc:goal",
                                     "sdlc:parked", proposed_label="sdlc:needs-confirmation")
    swap = next(a for a in actions if a["action"] == "swap-label")
    assert swap["add"] == ["sdlc:goal"] and swap["remove"] == ["sdlc:needs-confirmation"]


def test_the_ready_bucket_uses_the_pickers_own_eligibility_rule():
    """The survey's "ready to pick" filter must BE the picker's rule, not an approximation. Five
    hand-written copies had drifted; this one was missing `proposed_label`, so a survey listed
    issues awaiting human approval as ready and a compiled plan scheduled them."""
    triage = _mod("triage")
    src = triage.sources.GitHubSource(
        {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}}, run=lambda a: "")
    issues = [{"number": n, "title": "t", "body": "", "assignees": [],
               "labels": [{"name": "sdlc:goal"}] + ([{"name": lbl}] if lbl else [])}
              for n, lbl in ((41, "sdlc:needs-confirmation"), (42, "sdlc:parked"),
                             (43, "sdlc:blocked"), (44, "sdlc:in-progress"), (45, None))]
    bucket = triage._bucket_enqueued(issues, {"repo": "acme/widget"}, gh_source=src)
    assert [i["number"] for i in bucket["items"]] == [45]


def test_a_scalar_gate_block_does_not_take_the_whole_context_bucket_down(tmp_path):
    """#2116. `_gate_enabled` used to RAISE on a non-dict block -- and the raise did not merely make
    this survey line wrong, it made `_safe` replace the ENTIRE `context` bucket with an error stub,
    losing north_star / parallel_goals / ledger_enabled / stop_gate along with it.

    The input is not exotic: `ledger.LOCKABLE_KEYS` names the BLOCK path `gates.hard_plan_gate`, so
    `{"gates": {"hard_plan_gate": true}}` is a legal Org policy that `work.py` reads as ON."""
    triage = _mod("triage")
    out = triage._bucket_context("/nonexistent", {"gates": {"hard_plan_gate": True,
                                                            "stop_gate": "true"}})
    assert out["gates"] == {"hard_plan_gate": True, "stop_gate": True}
    assert out["degraded"] == []
    assert "north_star" in out and "parallel_goals" in out       # the bucket survived intact


def test_a_scalar_gates_parent_does_not_crash_the_context_bucket(tmp_path):
    triage = _mod("triage")
    for value in (True, "on", []):
        out = triage._bucket_context("/nonexistent", {"gates": value})
        assert out["gates"] == {"hard_plan_gate": False, "stop_gate": False}, value


# ----------------------------------------------------------------- #895 slice 3a: the three issue writes are REST

def _act(kind, issue, detail):
    return {"action": kind, "issue": issue, "detail": detail, "result": None, "error": None}


def test_assign_add_label_remove_label_go_rest_not_gh_issue_edit():
    triage = _mod("triage")
    run = _enact_runner()
    source = triage.sources.GitHubSource(_enact_config(), run=run)
    out = triage.apply_actions(source, [_act("assign", "10", "dana"), _act("add-label", "10", "sdlc:goal"),
                                        _act("remove-label", "10", "sdlc:parked")], True)
    assert [a["result"] for a in out] == ["done"] * 3
    assert [(c[1], c[3]) for c in run.rest_writes] == [
        ("repos/acme/widget/issues/10/assignees", "POST"), ("repos/acme/widget/issues/10/labels", "POST"),
        ("repos/acme/widget/issues/10/labels/sdlc%3Aparked", "DELETE")]


def test_a_failed_rest_write_is_still_a_failed_result_carrying_the_rest_error():
    """The RAISE contract: `_execute_action` raises, `apply_actions` turns it into result=failed and
    carries the error text, then continues with the next action."""
    triage = _mod("triage")
    run = _enact_runner(fail_on=["issues/10/assignees"])
    source = triage.sources.GitHubSource(_enact_config(), run=run)
    out = triage.apply_actions(source, [_act("assign", "10", "dana"), _act("add-label", "10", "sdlc:goal")], True)
    assert [a["result"] for a in out] == ["failed", "done"]
    assert "issues/10/assignees" in out[0]["error"]


def test_an_arbitrary_label_is_minted_by_rest_a_documented_divergence_from_gh():
    """D3: `gh issue edit --add-label X` FAILS on an absent label; REST POST /labels creates it. The
    triage add-label of a non-feature label does no existence lookup (accepted, documented, UNMEASURED
    on a live repo); a feature label still goes through the layer-2 lookup."""
    triage = _mod("triage")
    run = _enact_runner()
    source = triage.sources.GitHubSource(_enact_config(), run=run)
    out = triage.apply_actions(source, [_act("add-label", "10", "made-up-label")], True)
    assert out[0]["result"] == "done"
    assert [c[1] for c in run.rest_writes] == ["repos/acme/widget/issues/10/labels"]
    assert not any("/labels/" in c[1] and "--method" in c and c[c.index("--method") + 1] == "GET"
                   for c in run.rest_writes)
