"""#801 slice 1 + #895 slices 2a-4a-1: skills/sigma-loop/scripts/gh_api.py -- GraphQL capability check, REST
helper ops, and the REST-first `read_issue` policy (classification, normaliser, breaker, fallback log),
the issue list/write helpers, and the PR read helpers `view_pr_gh` / `pr_for_branch_gh` (#895 4a-1:
closed field whitelist, converter table, capped comment paging, shared breaker).

No claim is made that /sigma-loop works in a Claude
Code cloud session (unmeasured). Every test injects a fake `run` that
follows the sources convention: args WITHOUT a leading "gh", returns stdout str, RAISES on failure.
"""
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


GQL_TEXT = "HTTP 403: GitHub GraphQL is not available from Claude Code sessions"
PROBE_ARGS = ["api", "graphql", "-f", "query={viewer{login}}"]


class Fake:
    """Recording fake `run`: replies is a list of str (returned) or Exception (raised), reused last."""

    def __init__(self, *replies):
        self.replies = list(replies) or ["{}"]
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        r = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


# ---------------------------------------------------------------- capability check

def test_override_off_and_on_beat_cloud_env():
    g = _mod("gh_api")
    off = g.graphql_available(env={"SIGMA_GH_GRAPHQL": "off"})
    assert off["available"] is False and off["source"] == "override"
    for v in ("0", "false", "OFF"):
        assert g.graphql_available(env={"SIGMA_GH_GRAPHQL": v})["available"] is False
    on = g.graphql_available(env={"SIGMA_GH_GRAPHQL": "on", "CLAUDE_CODE_REMOTE": "true"})
    assert on["available"] is True and on["source"] == "override"


def test_unknown_override_ignored_and_noted():
    g = _mod("gh_api")
    r = g.graphql_available(env={"SIGMA_GH_GRAPHQL": "banana"})
    assert r["available"] is True and r["source"] == "default"
    assert "banana" in r["reason"]
    r = g.graphql_available(env={"SIGMA_GH_GRAPHQL": "banana", "CLAUDE_CODE_REMOTE": "1"})
    assert r["available"] is False and r["source"] == "env"


def test_cloud_env_means_unavailable():
    g = _mod("gh_api")
    for v in ("true", "1", "TRUE"):
        r = g.graphql_available(env={"CLAUDE_CODE_REMOTE": v})
        assert r["available"] is False and r["source"] == "env"
        assert "cloud" in r["reason"].lower()


def test_empty_or_false_cloud_env_is_default_available():
    g = _mod("gh_api")
    for env in ({}, {"CLAUDE_CODE_REMOTE": ""}, {"CLAUDE_CODE_REMOTE": "false"}):
        r = g.graphql_available(env=env)
        assert r == {"available": True, "reason": r["reason"], "source": "default"}


def test_probe_false_never_calls_run(tmp_path):
    g = _mod("gh_api")
    run = Fake("{}")
    g.graphql_available(env={}, cache_dir=tmp_path, run=run, probe=False)
    assert run.calls == []
    assert not (tmp_path / "gh-capability.json").exists()


def test_probe_true_without_run_raises_value_error():
    g = _mod("gh_api")
    with pytest.raises(ValueError):
        g.graphql_available(env={}, probe=True)


def test_probe_success_available_and_cache_written(tmp_path):
    g = _mod("gh_api")
    run = Fake('{"data":{}}')
    r = g.graphql_available(env={"CLAUDE_CODE_REMOTE": "", "UNRELATED_VAR": "canary-value"}, cache_dir=tmp_path,
                            run=run, now=1000.0, probe=True)
    assert run.calls == [PROBE_ARGS]
    assert r["available"] is True and r["source"] == "probe"
    data = json.loads((tmp_path / "gh-capability.json").read_text())
    assert set(data) == {"env_key", "available", "reason", "at"}
    assert data["env_key"] == "" and data["available"] is True and data["at"] == 1000.0
    assert "canary-value" not in json.dumps(data)
    assert list(tmp_path.glob("*.tmp")) == [] and [p.name for p in tmp_path.iterdir()] == ["gh-capability.json"]


def test_probe_third_shape_is_unavailable(tmp_path):
    g = _mod("gh_api")
    run = Fake(RuntimeError(GQL_TEXT))
    r = g.graphql_available(env={}, cache_dir=tmp_path, run=run, now=5.0, probe=True)
    assert r["available"] is False and r["source"] == "probe"
    assert json.loads((tmp_path / "gh-capability.json").read_text())["available"] is False


def test_probe_inconclusive_fails_open_and_does_not_cache_unavailable(tmp_path):
    g = _mod("gh_api")
    run = Fake(RuntimeError("dial tcp: i/o timeout"))
    r = g.graphql_available(env={}, cache_dir=tmp_path, run=run, now=5.0, probe=True)
    assert r["available"] is True and "inconclusive" in r["reason"]
    p = tmp_path / "gh-capability.json"
    assert not p.exists() or json.loads(p.read_text())["available"] is not False


def test_second_call_within_ttl_uses_cache_then_reprobes_after_ttl(tmp_path):
    g = _mod("gh_api")
    run = Fake(RuntimeError(GQL_TEXT), '{"data":{}}')
    first = g.graphql_available(env={}, cache_dir=tmp_path, run=run, now=1000.0, probe=True)
    assert first["available"] is False
    second = g.graphql_available(env={}, cache_dir=tmp_path, run=run, now=1000.0 + g.CACHE_TTL_SECONDS - 1,
                                 probe=True)
    assert second["source"] == "cache" and second["available"] is False and len(run.calls) == 1
    third = g.graphql_available(env={}, cache_dir=tmp_path, run=run, now=1000.0 + g.CACHE_TTL_SECONDS + 1,
                                probe=True)
    assert third["available"] is True and third["source"] == "probe" and len(run.calls) == 2


def test_cache_with_different_env_key_is_a_miss(tmp_path):
    g = _mod("gh_api")
    (tmp_path / "gh-capability.json").write_text(json.dumps(
        {"env_key": "yes", "available": False, "reason": "x", "at": 1000.0}))
    run = Fake('{"data":{}}')
    r = g.graphql_available(env={"CLAUDE_CODE_REMOTE": "false"}, cache_dir=tmp_path, run=run,
                            now=1001.0, probe=True)
    assert r["source"] == "probe" and len(run.calls) == 1


def test_corrupt_cache_is_a_miss(tmp_path):
    g = _mod("gh_api")
    (tmp_path / "gh-capability.json").write_text("{not json")
    run = Fake('{"data":{}}')
    r = g.graphql_available(env={}, cache_dir=tmp_path, run=run, now=1.0, probe=True)
    assert r["available"] is True and r["source"] == "probe"


def test_exactly_one_probe_call_no_retry(tmp_path):
    g = _mod("gh_api")
    run = Fake(RuntimeError("boom"))
    g.graphql_available(env={}, cache_dir=tmp_path, run=run, now=1.0, probe=True)
    assert len(run.calls) == 1


def test_missing_cache_dir_is_created(tmp_path):
    g = _mod("gh_api")
    d = tmp_path / "a" / "b"
    g.graphql_available(env={}, cache_dir=d, run=Fake("{}"), now=1.0, probe=True)
    assert (d / "gh-capability.json").exists()


def test_unwritable_cache_dir_is_swallowed_and_noted(tmp_path):
    g = _mod("gh_api")
    blocker = tmp_path / "file"
    blocker.write_text("x")          # a file where a directory is needed -> mkdir/open fails
    r = g.graphql_available(env={}, cache_dir=blocker / "sub", run=Fake("{}"), now=1.0, probe=True)
    assert r["available"] is True and "cache" in r["reason"].lower()


def test_importing_gh_api_does_not_load_sources():
    """R6: gh_api must not pull `sources` (which reconfigures stdout and loads a dozen siblings)."""
    code = (
        "import importlib.util,sys\n"
        "p=%r\n"
        "s=importlib.util.spec_from_file_location('gh_api',p);m=importlib.util.module_from_spec(s)\n"
        "s.loader.exec_module(m)\n"
        "print(sorted(n for n in sys.modules if n.split('.')[0]=='sources'))\n"
    ) % str(S / "gh_api.py")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_importing_gh_api_loads_neither_sources_nor_state():
    """R6 + #895: `sources` and `state` are loaded lazily (state only when a read has an sdlc_dir).
    `_load` never registers in sys.modules, so record what `spec_from_file_location` is asked for."""
    code = (
        "import importlib.util,pathlib\n"
        "seen=[];orig=importlib.util.spec_from_file_location\n"
        "def spy(name,*a,**k):\n    seen.append(name);return orig(name,*a,**k)\n"
        "importlib.util.spec_from_file_location=spy\n"
        "s=orig('gh_api',%r);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
        "print(sorted(set(seen)))\n"
    ) % str(S / "gh_api.py")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    loaded = out.stdout.strip()
    assert "'sources'" not in loaded and "'state'" not in loaded, loaded


def test_sources_reads_issues_through_gh_api():
    """#895 slice 2a: sources.py's issue reads go through gh_api.read_issue; no `issue view` argv left."""
    src = (S / "sources.py").read_text()
    assert 'gh_api = _load("gh_api")' in src
    assert '"issue", "view"' not in src
    assert src.count("gh_api.read_issue(") == 2      # the shared `_read_issue` + `fetch_comments`


# ---------------------------------------------------------------- GhApiError

def test_ghapierror_wraps_text_and_hint():
    g = _mod("gh_api")
    inner = RuntimeError(GQL_TEXT)
    inner.hint = "short hint"
    with pytest.raises(g.GhApiError) as ei:
        g.comment_issue(Fake(inner), 1, "x")
    assert str(ei.value) == GQL_TEXT and ei.value.hint == "short hint"
    gs = _mod("gh_session")
    assert gs.graphql_unavailable(str(ei.value)) is True
    with pytest.raises(g.GhApiError) as ei:
        g.comment_issue(Fake(RuntimeError("plain")), 1, "x")
    assert ei.value.hint is None


def test_proxy_block_text_survives_the_wrapper():
    g, gs = _mod("gh_api"), _mod("gh_session")
    with pytest.raises(g.GhApiError) as ei:
        g.view_pr(Fake(RuntimeError("GitHub access is not enabled for this session.")), 3)
    assert gs.proxy_session_block(str(ei.value)) == gs.REMEDIATION


# ---------------------------------------------------------------- REST ops

def _assert_clean(run):
    for call in run.calls:
        assert "graphql" not in " ".join(call)
        assert call[0] == "api" and "--method" in call


def test_list_issues_delegates_to_injected_fetch():
    g = _mod("gh_api")
    seen = {}

    def fetch(run, repo, labels, cap, **kw):
        seen.update(run=run, repo=repo, labels=labels, cap=cap, kw=kw)
        return [{"number": 1}]

    run = Fake()
    assert g.list_issues(run, repo="o/r", labels=["sdlc:goal"], fetch=fetch) == [{"number": 1}]
    assert seen["repo"] == "o/r" and seen["labels"] == ["sdlc:goal"] and seen["cap"] == 200
    assert seen["run"] is run


def test_list_issues_agrees_with_real_sources_fetch_issues_rest():
    g, sources = _mod("gh_api"), _mod("sources")
    run = Fake(json.dumps([{"number": 2}, {"number": 3, "pull_request": {}}]))
    out = g.list_issues(run, repo="o/r", labels=["a"], cap=5, fetch=sources.fetch_issues_rest)
    assert out == [{"number": 2}]
    assert run.calls[0][:3] == ["api", "repos/o/r/issues", "--method"]
    _assert_clean(run)


def _rest_issue(n=7, comments=0, **kw):
    d = {"number": n, "state": "open", "state_reason": None, "user": {"login": "alice", "type": "User"},
         "body": "B", "labels": [{"name": "sdlc:goal"}], "assignees": [{"login": "bob"}],
         "closed_at": None, "comments": comments}
    d.update(kw)
    return d


def _rest_comment(i, login=None, utype="User"):
    return {"id": 1000 + i, "node_id": "IC_%d" % i, "user": {"login": login or "u%d" % i, "type": utype},
            "body": "c%d" % i, "created_at": "2026-01-01T00:%02d:%02dZ" % (i // 60, i % 60),
            "author_association": "OWNER"}


class RestFake:
    """Routes REST argv: the issue GET, `/comments` pages by `page=`, and fallback `issue view`."""

    def __init__(self, issue=None, comments=(), fail=None, gql=None, issue_raw=None, pages=None):
        self.issue = issue if issue is not None else _rest_issue(comments=len(comments))
        self.comments, self.fail, self.gql = list(comments), fail, gql
        self.issue_raw, self.pages_raw = issue_raw, pages or {}
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "issue":
            if isinstance(self.gql, Exception):
                raise self.gql
            return self.gql if self.gql is not None else '{"state": "OPEN", "labels": []}'
        if self.fail is not None:
            raise self.fail
        if args[1].endswith("/comments"):
            page = int([a for a in args if a.startswith("page=")][0][5:])
            if page in self.pages_raw:
                return self.pages_raw[page]
            return json.dumps(self.comments[(page - 1) * 100:page * 100])
        return self.issue_raw if self.issue_raw is not None else json.dumps(self.issue)

    def page_calls(self):
        return [c for c in self.calls if c[0] == "api" and c[1].endswith("/comments")]

    def run_calls(self):
        return [c for c in self.calls if c[0] == "api" and c[1].endswith("/check-runs")]

    def status_calls(self):
        return [c for c in self.calls if c[0] == "api" and c[1].endswith("/status")]

    def gql_calls(self):
        return [c for c in self.calls if c[0] == "issue"]

    def rest_calls(self):
        return [c for c in self.calls if c[0] == "api"]


def test_view_issue_fetches_issue_and_bounded_comments():
    g = _mod("gh_api")
    run = RestFake(comments=[_rest_comment(i) for i in range(130)])
    out = g.view_issue(run, 7, repo="o/r", comments="all")
    assert out["number"] == 7 and len(out["comments"]) == 130
    assert run.calls[0] == ["api", "repos/o/r/issues/7", "--method", "GET"]
    assert run.calls[1][:3] == ["api", "repos/o/r/issues/7/comments", "--method"]
    assert "per_page=100" in run.calls[1] and "page=2" in run.calls[2] and len(run.calls) == 3
    _assert_clean(run)


def test_view_issue_tail_reads_only_needed_pages():
    g = _mod("gh_api")
    run = RestFake(comments=[_rest_comment(i) for i in range(150)])
    out = g.view_issue(run, 7, repo="o/r", comments=100)
    assert [c["body"] for c in out["comments"]] == ["c%d" % i for i in range(50, 150)]
    assert len(run.page_calls()) == 2


def test_view_issue_comment_paging():
    g = _mod("gh_api")
    many = [_rest_comment(i) for i in range(150)]
    run = RestFake(comments=many)
    assert len(g.view_issue(run, 7, comments="all")["comments"]) == 150 and len(run.calls) == 3
    run = RestFake(comments=many)
    out = g.view_issue(run, 7, comments=20)["comments"]
    assert [c["body"] for c in out] == ["c%d" % i for i in range(130, 150)]
    assert len(run.page_calls()) <= 2
    run = RestFake(comments=many)
    g.view_issue(run, 7, comments="none")
    assert len(run.calls) == 1
    run = RestFake(comments=[])
    assert g.view_issue(run, 7, comments="all")["comments"] == [] and len(run.calls) == 1
    run = RestFake(comments=[_rest_comment(i) for i in range(30)])
    assert len(g.view_issue(run, 7, comments=50)["comments"]) == 30


@pytest.mark.parametrize("fake", [
    lambda: RestFake(issue_raw="null"),
    lambda: RestFake(issue=_rest_issue(comments=5), pages={1: "null"}),
    lambda: RestFake(issue=_rest_issue(comments=5), pages={1: "{}"}),
    lambda: RestFake(issue=_rest_issue(comments="5")),
], ids=["null-issue", "null-page", "dict-page", "str-count"])
def test_view_issue_rejects_malformed_payloads(fake):
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError) as ei:
        g.view_issue(fake(), 7, comments="all")
    assert ei.value.kind == "other"


# ---------------------------------------------------------------- classification (#895)

RATE = "gh: API rate limit exceeded for user ID 1. (HTTP 429)"
PROXY = "GitHub access is not enabled for this session."


def _err(hint=None, text=None, cause=None):
    exc = RuntimeError(text if text is not None else "gh api repos/o/r/issues/7 --method GET failed: %s" % hint)
    if hint is not None:
        exc.hint = hint
    if cause is not None:
        exc.__cause__ = cause
    return exc


CLASSIFY_ROWS = [
    ("429", _err(RATE), (429, "rate_limit")),
    ("403-rate", _err("gh: You have exceeded a secondary rate limit. (HTTP 403)"), (403, "rate_limit")),
    ("403-permission", _err("gh: Resource not accessible by integration (HTTP 403)"), (403, "permission")),
    ("401", _err("gh: Bad credentials (HTTP 401)"), (401, "auth")),
    ("404", _err("gh: Not Found (HTTP 404)"), (404, "not_found")),
    ("410", _err("gh: This issue was deleted (HTTP 410)"), (410, "other")),
    ("422", _err("gh: Validation Failed (HTTP 422)"), (422, "invalid")),
    ("502", _err("gh: Server Error (HTTP 502)"), (502, "server")),
    ("dial-tcp", _err('Get "https://api.github.com/x": dial tcp: lookup api.github.com: no such host'),
     (None, "transport")),
    ("tls-timeout", _err('Get "https://api.github.com/x": net/http: TLS handshake timeout'), (None, "transport")),
    ("deadline", _err("context deadline exceeded"), (None, "transport")),
    ("eof", _err('Get "https://api.github.com/x":\nEOF'), (None, "transport")),
    ("timeout-expired-cause", _err(text="boom", cause=subprocess.TimeoutExpired(["gh"], 120)),
     (None, "transport")),
    ("proxy", _err(PROXY), (None, "proxy")),
    ("proxy-gql", _err(GQL_TEXT), (None, "proxy")),
    ("proxy-429", _err(PROXY + " (HTTP 429)"), (None, "proxy")),
    ("proxy-502", _err(PROXY + " (HTTP 502)"), (None, "proxy")),
    ("proxy-timed-out", _err(PROXY + " request timed out"), (None, "proxy")),
    ("404-timeout-svc", _err("gh: Not Found (HTTP 404)",
                             text="gh api repos/acme/timeout-svc/issues/7 --method GET failed: gh: Not Found (HTTP 404)"),
     (404, "not_found")),
    ("404-timeout-svc-no-hint", _err(text="gh api repos/acme/timeout-svc/issues/7 failed: gh: Not Found (HTTP 404)"),
     (404, "not_found")),
    ("404-timeout-in-hint", _err("gh: Not Found (HTTP 404)\nupstream request timeout"), (404, "not_found")),
    ("403-rate-limiter-repo", _err("gh: Resource not accessible by integration (HTTP 403)",
                                   text="gh api repos/acme/rate-limiter/issues/7 failed: gh: Resource not "
                                        "accessible by integration (HTTP 403)"), (403, "permission")),
]


@pytest.mark.parametrize("exc,expected", [r[1:] for r in CLASSIFY_ROWS], ids=[r[0] for r in CLASSIFY_ROWS])
def test_classify_table(exc, expected):
    g = _mod("gh_api")
    assert g.classify(exc) == expected


def test_call_fills_status_and_kind():
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError) as ei:
        g.view_pr(Fake(_err(RATE)), 3)
    assert (ei.value.status, ei.value.kind, ei.value.hint) == (429, "rate_limit", RATE)


# ---------------------------------------------------------------- normaliser (#895)

def test_to_gh_shape_fields():
    g = _mod("gh_api")
    issue = _rest_issue(state="closed", state_reason="not_planned", closed_at="2026-01-02T00:00:00Z",
                        comments=[_rest_comment(1)])
    out = g.to_gh_shape(issue, ["state", "stateReason", "author", "closedAt", "body", "labels",
                                "assignees", "comments"])
    assert out == {"state": "CLOSED", "stateReason": "NOT_PLANNED", "author": {"login": "alice"},
                   "closedAt": "2026-01-02T00:00:00Z", "body": "B", "labels": [{"name": "sdlc:goal"}],
                   "assignees": [{"login": "bob"}],
                   "comments": [{"id": "IC_1", "author": {"login": "u1"}, "body": "c1",
                                 "createdAt": "2026-01-01T00:00:01Z", "authorAssociation": "OWNER"}]}
    assert g.to_gh_shape(_rest_issue(), ["state"]) == {"state": "OPEN"}
    assert g.to_gh_shape(_rest_issue(), ["stateReason"]) == {"stateReason": None}
    assert g.to_gh_shape(_rest_issue(body=None), ["body"]) == {"body": ""}
    with pytest.raises(ValueError):
        g.to_gh_shape(_rest_issue(), ["bogus"])


def test_to_gh_shape_number_and_title():
    g = _mod("gh_api")
    assert g.to_gh_shape(_rest_issue(n=12, title="T"), ["number", "title"]) == {"number": 12, "title": "T"}
    assert g.to_gh_shape(_rest_issue(title=None), ["title"]) == {"title": ""}


def test_to_gh_shape_merged_pr_reads_merged():
    """REST reports a merged PR as `closed` + pull_request.merged_at; gh says MERGED (#895 2b)."""
    g = _mod("gh_api")
    merged = _rest_issue(state="closed", pull_request={"merged_at": "2026-10-09T00:00:00Z"})
    assert g.to_gh_shape(merged, ["state"]) == {"state": "MERGED"}
    unmerged = _rest_issue(state="closed", pull_request={"merged_at": None})
    assert g.to_gh_shape(unmerged, ["state", "stateReason"]) == {"state": "CLOSED", "stateReason": None}
    assert g.to_gh_shape(_rest_issue(state="closed"), ["state"]) == {"state": "CLOSED"}


def test_to_gh_shape_refuses_missing_body():
    """A REST issue with NO body key must raise, never read as "" -- append_to_body would then
    overwrite the real body with only its marker."""
    g = _mod("gh_api")
    issue = _rest_issue()
    del issue["body"]
    with pytest.raises(g.GhApiError):
        g.to_gh_shape(issue, ["body"])


def test_to_gh_shape_bot_author():
    """Issue author `x[bot]`/Bot -> `app/x`, gh's spelling (MEASURED once, #895 step 9). Comment
    authors stay unmapped (plan B3; gh shows a bare `x`, mapping is a follow-up)."""
    g = _mod("gh_api")
    issue = _rest_issue(user={"login": "sigma-bot[bot]", "type": "Bot"},
                        comments=[_rest_comment(1, login="sigma-bot[bot]", utype="Bot")])
    out = g.to_gh_shape(issue, ["author", "comments"])
    assert out["author"] == {"login": "app/sigma-bot"}
    assert out["comments"][0]["author"] == {"login": "sigma-bot[bot]"}
    assert g.to_gh_shape(_rest_issue(), ["author"])["author"] == {"login": "alice"}


# ---------------------------------------------------------------- read_issue policy (#895)

def test_read_issue_rest_first_success():
    g = _mod("gh_api")
    run = RestFake()
    out = g.read_issue(run, 7, ["state", "labels"], "o/r", env={})
    assert out == {"state": "OPEN", "labels": [{"name": "sdlc:goal"}]}
    assert run.gql_calls() == [] and len(run.calls) == 1


TRANSIENT = [("429", RATE), ("403-rate", "gh: You have exceeded a secondary rate limit. (HTTP 403)"),
             ("502", "gh: Server Error (HTTP 502)"), ("dial-tcp", "dial tcp: i/o timeout")]


@pytest.mark.parametrize("hint", [t[1] for t in TRANSIENT], ids=[t[0] for t in TRANSIENT])
def test_read_issue_falls_back_on_transient(hint):
    g = _mod("gh_api")
    run = RestFake(fail=_err(hint), gql='{"state": "OPEN", "labels": [], "extra": 1}')
    out = g.read_issue(run, 7, ["state", "labels"], "o/r", env={})
    assert out == {"state": "OPEN", "labels": []}
    assert run.gql_calls() == [["issue", "view", "7", "--repo", "o/r", "--json", "state,labels"]]


CLIENT = [("401", "gh: Bad credentials (HTTP 401)"),
          ("403-permission", "gh: Resource not accessible by integration (HTTP 403)"),
          ("404", "gh: Not Found (HTTP 404)"), ("422", "gh: Validation Failed (HTTP 422)")]


@pytest.mark.parametrize("hint", [c[1] for c in CLIENT], ids=[c[0] for c in CLIENT])
def test_read_issue_never_falls_back_on_client_errors(hint):
    g = _mod("gh_api")
    run = RestFake(fail=_err(hint))
    with pytest.raises(g.GhApiError):
        g.read_issue(run, 7, ["state"], "o/r", env={})
    assert run.gql_calls() == []


@pytest.mark.parametrize("hint", [PROXY + " (HTTP 429)", PROXY + " (HTTP 502)", PROXY + " request timed out"],
                         ids=["proxy-429", "proxy-502", "proxy-timed-out"])
def test_read_issue_never_falls_back_on_proxy_block(hint):
    g = _mod("gh_api")
    run = RestFake(fail=_err(hint))
    with pytest.raises(g.GhApiError) as ei:
        g.read_issue(run, 7, ["state"], "o/r", env={})
    assert run.gql_calls() == [] and ei.value.kind == "proxy"


@pytest.mark.parametrize("env", [{"CLAUDE_CODE_REMOTE": "true"}, {"SIGMA_GH_GRAPHQL": "off"}],
                         ids=["cloud", "override-off"])
def test_read_issue_skips_fallback_in_cloud_session(env):
    g = _mod("gh_api")
    run = RestFake(fail=_err(RATE))
    with pytest.raises(g.GhApiError) as ei:
        g.read_issue(run, 7, ["state"], "o/r", env=env)
    assert run.gql_calls() == [] and ei.value.kind == "rate_limit" and ei.value.hint == RATE


def test_read_issue_at_most_one_fallback():
    g = _mod("gh_api")
    fb = _err("gh: Server Error (HTTP 503)", text="gh issue view failed")
    run = RestFake(fail=_err("gh: Server Error (HTTP 502)"), gql=fb)
    with pytest.raises(g.GhApiError) as ei:
        g.read_issue(run, 7, ["state"], "o/r", env={})
    assert len(run.rest_calls()) == 1 and len(run.gql_calls()) == 1
    assert ei.value.kind == "server" and ei.value.hint == "gh: Server Error (HTTP 503)"
    assert "502" in str(ei.value) and "503" in str(ei.value)


def test_read_issue_fallback_degrades_malformed_json_like_todays_parsers():
    g = _mod("gh_api")
    run = RestFake(fail=_err(RATE), gql="<html>")
    assert g.read_issue(run, 7, ["body"], None, env={}) == {}


def test_read_issue_comment_limit_reads_the_tail():
    g = _mod("gh_api")
    run = RestFake(comments=[_rest_comment(i) for i in range(150)])
    out = g.read_issue(run, 7, ["comments"], "o/r", env={}, comment_limit=10)
    assert [c["body"] for c in out["comments"]] == ["c%d" % i for i in range(140, 150)]


# ---------------------------------------------------------------- breaker + log (#895)

def _breaker(sdlc):
    return json.loads((sdlc / "state" / "gh-rest-breaker.json").read_text())


def _fail_reads(g, sdlc, times, start=1000.0, env=None, hint="gh: Server Error (HTTP 502)"):
    runs = []
    for i in range(times):
        run = RestFake(fail=_err(hint))
        g.read_issue(run, 7, ["state"], "o/r", env=env or {}, sdlc_dir=sdlc, now=start + i)
        runs.append(run)
    return runs


def test_breaker_opens_after_three_failures(tmp_path):
    g = _mod("gh_api")
    _fail_reads(g, tmp_path, 3)
    b = _breaker(tmp_path)
    assert b["consecutive"] == 3 and b["opened_at"] == 1002.0 and b["last_kind"] == "server"
    run = RestFake()
    assert g.read_issue(run, 7, ["state"], "o/r", env={}, sdlc_dir=tmp_path, now=1003.0) == {"state": "OPEN"}
    assert run.rest_calls() == [] and len(run.gql_calls()) == 1


def test_breaker_half_opens_after_cooldown(tmp_path):
    g = _mod("gh_api")
    _fail_reads(g, tmp_path, 3)
    run = RestFake()
    g.read_issue(run, 7, ["state"], "o/r", env={}, sdlc_dir=tmp_path, now=1002.0 + 301)
    assert len(run.rest_calls()) == 1 and run.gql_calls() == []
    assert _breaker(tmp_path)["consecutive"] == 0
    # the cool-down is the operator's lever; unparseable/negative falls back to the default
    assert g._cooldown({"SIGMA_GH_BREAKER_COOLDOWN": "10"}) == 10
    assert g._cooldown({"SIGMA_GH_BREAKER_COOLDOWN": "x"}) == 300 == g._cooldown({"SIGMA_GH_BREAKER_COOLDOWN": "-1"})


def test_breaker_reopens_after_failed_half_open_probe(tmp_path):
    g = _mod("gh_api")
    _fail_reads(g, tmp_path, 3)
    _fail_reads(g, tmp_path, 1, start=1002.0 + 301)
    assert _breaker(tmp_path)["opened_at"] == 1303.0
    run = RestFake()
    g.read_issue(run, 7, ["state"], "o/r", env={}, sdlc_dir=tmp_path, now=1304.0)
    assert run.rest_calls() == []


@pytest.mark.parametrize("content", ["{not json", "[]", '{"consecutive": "x", "opened_at": "y"}'])
def test_corrupt_breaker_file_is_closed(tmp_path, content):
    g = _mod("gh_api")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "gh-rest-breaker.json").write_text(content)
    run = RestFake()
    assert g.read_issue(run, 7, ["state"], "o/r", env={}, sdlc_dir=tmp_path, now=5.0) == {"state": "OPEN"}
    assert len(run.rest_calls()) == 1


def test_breaker_open_without_fallback_still_tries_rest(tmp_path):
    g = _mod("gh_api")
    _fail_reads(g, tmp_path, 3)
    run = RestFake()
    out = g.read_issue(run, 7, ["state"], "o/r", env={"SIGMA_GH_GRAPHQL": "off"}, sdlc_dir=tmp_path, now=1003.0)
    assert out == {"state": "OPEN"} and len(run.rest_calls()) == 1 and run.gql_calls() == []


def test_no_sdlc_dir_never_writes(tmp_path, monkeypatch):
    g = _mod("gh_api")
    monkeypatch.chdir(tmp_path)
    for _ in range(4):
        g.read_issue(RestFake(fail=_err(RATE)), 7, ["state"], "o/r", env={}, now=1.0)
    assert list(tmp_path.rglob("*")) == []


def test_fallback_log_is_capped_and_secret_free(tmp_path):
    g = _mod("gh_api")
    secret = "gh: rate limit ghp_SECRETTOKEN body=PRIVATE (HTTP 429)"
    _fail_reads(g, tmp_path, g.FALLBACK_LOG_CAP + 5, env={"SIGMA_GH_BREAKER_COOLDOWN": "0"}, hint=secret)
    text = (tmp_path / "state" / "gh-fallback.json").read_text()
    entries = json.loads(text)
    assert len(entries) == g.FALLBACK_LOG_CAP
    assert "SECRET" not in text and "PRIVATE" not in text and "repos/" not in text
    assert set(entries[-1]) == {"ts", "op", "number", "kind", "status", "fell_back", "why"}
    assert entries[-1]["kind"] == "rate_limit" and entries[-1]["status"] == 429 and entries[-1]["fell_back"]


def test_stderr_once_per_breaker_open(tmp_path, capsys):
    g = _mod("gh_api")
    _fail_reads(g, tmp_path, 3)
    first = [l for l in capsys.readouterr().err.splitlines() if l.startswith("sigma: gh REST")]
    assert len(first) == 3 and "breaker open" in first[-1]
    for i in range(5):
        g.read_issue(RestFake(), 7, ["state"], "o/r", env={}, sdlc_dir=tmp_path, now=1003.0 + i)
    assert [l for l in capsys.readouterr().err.splitlines() if l.startswith("sigma: gh REST")] == []


def test_comment_issue_argv():
    g = _mod("gh_api")
    run = Fake('{"id":1}')
    assert g.comment_issue(run, 9, "hello", repo="o/r") == {"id": 1}
    assert run.calls == [["api", "repos/o/r/issues/9/comments", "--method", "POST", "-f", "body=hello"]]


def test_add_labels_argv_and_placeholder_repo():
    g = _mod("gh_api")
    run = Fake("[]")
    g.add_labels(run, 4, ["sdlc:goal", "area:x"])
    assert run.calls == [["api", "repos/{owner}/{repo}/issues/4/labels", "--method", "POST",
                          "-f", "labels[]=sdlc:goal", "-f", "labels[]=area:x"]]


def test_remove_label_quotes_name_and_404_is_noop():
    g = _mod("gh_api")
    run = Fake("[]")
    g.remove_label(run, 4, "sdlc:goal", repo="o/r")
    assert run.calls == [["api", "repos/o/r/issues/4/labels/sdlc%3Agoal", "--method", "DELETE"]]
    e404 = RuntimeError("gh: Label does not exist (HTTP 404)")
    e404.hint = "gh: Label does not exist (HTTP 404)"
    assert g.remove_label(Fake(e404), 4, "x") is None
    e500 = RuntimeError("gh: server error (HTTP 500)")
    e500.hint = "gh: server error (HTTP 500)"
    with pytest.raises(g.GhApiError):
        g.remove_label(Fake(e500), 4, "x")


def test_create_issue_argv_with_non_feature_labels():
    g = _mod("gh_api")
    run = Fake('{"number": 10}')
    out = g.create_issue(run, "T", "B", labels=["area:x", "sdlc:goal"], repo="o/r")
    assert out == {"number": 10}
    assert run.calls == [["api", "repos/o/r/issues", "--method", "POST", "-f", "title=T", "-f", "body=B",
                          "-f", "labels[]=area:x", "-f", "labels[]=sdlc:goal"]]


def test_create_issue_refuses_feature_label_before_any_call():
    """R5 closed (layer 1): was `..._pins_feature_label_bypass`. A feature:* label is never minted by
    REST either; the refusal happens BEFORE the fake sees a call, case-insensitively."""
    g = _mod("gh_api")
    for name in ("feature:x", "Feature:Voice"):
        run = Fake('{"number": 10}')
        with pytest.raises(g.GhApiError) as ei:
            g.create_issue(run, "T", "B", labels=["sdlc:goal", name], repo="o/r")
        assert ei.value.kind == "refused" and run.calls == []
        assert g._write_fallback_ok(ei.value, True) is False
    run = Fake('{"number": 10}')
    g.create_issue(run, "T", "B", labels=["feature:x"], repo="o/r", feature_labels_exist=True)
    assert run.calls[0][-2:] == ["-f", "labels[]=feature:x"]


def test_add_labels_refuses_feature_label_before_any_call():
    g = _mod("gh_api")
    run = Fake("[]")
    with pytest.raises(g.GhApiError) as ei:
        g.add_labels(run, 4, ["sdlc:goal", "feature:x"], repo="o/r")
    assert ei.value.kind == "refused" and run.calls == []
    g.add_labels(run, 4, ["feature:x"], repo="o/r", feature_labels_exist=True)
    assert len(run.calls) == 1
    run = Fake("[]")
    g.add_labels(run, 4, ["featureflag", "sdlc:goal"], repo="o/r")          # no colon: an ordinary label
    assert len(run.calls) == 1


def test_label_exists_404_is_false_other_failures_raise():
    g = _mod("gh_api")
    run = Fake('{"name": "feature:x"}')
    assert g.label_exists(run, "feature:x", repo="o/r") is True
    assert run.calls == [["api", "repos/o/r/labels/feature%3Ax", "--method", "GET"]]
    e = RuntimeError("gh: Not Found (HTTP 404)")
    e.hint = "gh: Not Found (HTTP 404)"
    assert g.label_exists(Fake(e), "feature:x", repo="o/r") is False
    e500 = RuntimeError("gh: Server Error (HTTP 500)")
    e500.hint = "gh: Server Error (HTTP 500)"
    with pytest.raises(g.GhApiError):
        g.label_exists(Fake(e500), "feature:x", repo="o/r")
    with pytest.raises(g.GhApiError):
        g.label_exists(Fake(RuntimeError("HTTP 404 in argv only")), "feature:x", repo="o/r")


def test_close_issue_argv():
    g = _mod("gh_api")
    run = Fake("{}")
    g.close_issue(run, 5, repo="o/r")
    assert run.calls == [["api", "repos/o/r/issues/5", "--method", "PATCH", "-f", "state=closed"]]
    run = Fake("{}")
    g.close_issue(run, 5, repo="o/r", reason="not_planned")
    assert run.calls[0][-2:] == ["-f", "state_reason=not_planned"]


def test_create_view_pr_argv():
    g = _mod("gh_api")
    run = Fake('{"number": 11}')
    assert g.create_pr(run, "T", "B", head="sdlc/1", base="main", repo="o/r") == {"number": 11}
    assert run.calls == [["api", "repos/o/r/pulls", "--method", "POST", "-f", "title=T", "-f", "body=B",
                          "-f", "head=sdlc/1", "-f", "base=main"]]
    run = Fake('{"number": 11, "state": "open"}')
    assert g.view_pr(run, 11, repo="o/r")["state"] == "open"
    assert run.calls == [["api", "repos/o/r/pulls/11", "--method", "GET"]]


def test_merge_pr_argv_sha_guard_and_no_auto():
    g = _mod("gh_api")
    run = Fake('{"merged": true}')
    g.merge_pr(run, 11, repo="o/r")
    assert run.calls == [["api", "repos/o/r/pulls/11/merge", "--method", "PUT", "-f", "merge_method=squash"]]
    run = Fake('{"merged": true}')
    g.merge_pr(run, 11, merge_method="rebase", sha="abc123", repo="o/r")
    assert run.calls[0][-4:] == ["-f", "merge_method=rebase", "-f", "sha=abc123"]
    assert not any("auto" in a for a in run.calls[0])


def test_failures_wrap_without_retry_and_unparseable_json_raises():
    g = _mod("gh_api")
    calls = [
        lambda r: g.comment_issue(r, 1, "x"), lambda r: g.add_labels(r, 1, ["a"]),
        lambda r: g.create_issue(r, "t", "b"), lambda r: g.close_issue(r, 1),
        lambda r: g.create_pr(r, "t", "b", "h", "b"), lambda r: g.view_pr(r, 1),
        lambda r: g.merge_pr(r, 1), lambda r: g.view_issue(r, 1),
        lambda r: g.edit_issue(r, 1, body="b"), lambda r: g.add_assignees(r, 1, ["a"]),
    ]
    for call in calls:
        run = Fake(RuntimeError("transport"))
        with pytest.raises(g.GhApiError):
            call(run)
        assert len(run.calls) == 1
        run = Fake("<html>not json")
        with pytest.raises(g.GhApiError):
            call(run)


def test_only_the_probe_mentions_graphql():
    src = (S / "gh_api.py").read_text()
    assert src.count('"graphql"') == 1


# ---------------------------------------------------------------- list_issues_gh (#895 slice 2c)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gqlfake  # noqa: E402


class ListFake:
    """REST issues-list GET via gqlfake.rest_list + the one `issue list` fallback. `fail` is raised on
    every REST call (or a list of replies, one per REST call); `gql` is the fallback reply."""

    def __init__(self, items=(), fail=None, gql=None, rest_raw=None):
        self.items, self.fail, self.gql, self.rest_raw = list(items), fail, gql, rest_raw
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "issue":
            if isinstance(self.gql, Exception):
                raise self.gql
            return self.gql if self.gql is not None else "[]"
        if self.fail is not None:
            raise self.fail
        if self.rest_raw is not None:
            return self.rest_raw
        out = gqlfake.rest_list(args, self.items)
        assert out is not None, args
        return out

    def rest_calls(self):
        return [c for c in self.calls if c[0] == "api"]

    def gql_calls(self):
        return [c for c in self.calls if c[0] == "issue"]


def _params(call):
    return dict(a.split("=", 1) for a in call if "=" in a and not a.startswith("repos/"))


def test_list_issues_gh_rest_success_in_gh_shape():
    g = _mod("gh_api")
    run = ListFake([gqlfake.rest_item(3, ["sdlc:goal"], body="b3"), gqlfake.rest_item(2, ["x"]),
                    {"number": 9, "pull_request": {}, "state": "open", "labels": []}])
    out = g.list_issues_gh(run, ["number", "labels", "state", "body"], repo="o/r", labels=["sdlc:goal"], env={})
    assert out == [{"number": 3, "labels": [{"name": "sdlc:goal"}], "state": "OPEN", "body": "b3"}]
    assert run.gql_calls() == [] and len(run.rest_calls()) == 1
    c = run.rest_calls()[0]
    assert c[:4] == ["api", "repos/o/r/issues", "--method", "GET"]
    assert _params(c)["labels"] == "sdlc:goal" and _params(c)["state"] == "open"


def test_list_issues_gh_labels_and_and_no_label_omitted():
    g = _mod("gh_api")
    run = ListFake([gqlfake.rest_item(1, ["a", "b"]), gqlfake.rest_item(2, ["a"])])
    assert [i["number"] for i in g.list_issues_gh(run, ["number"], labels=["a", "b"], env={})] == [1]
    assert _params(run.calls[0])["labels"] == "a,b"
    run = ListFake([gqlfake.rest_item(1)])
    g.list_issues_gh(run, ["number"], env={})
    assert "labels" not in _params(run.calls[0]) and run.calls[0][1] == "repos/{owner}/{repo}/issues"


def test_list_issues_gh_pages_stop_at_short_page_and_cap_is_honoured():
    g = _mod("gh_api")
    run = ListFake([gqlfake.rest_item(i) for i in range(250, 0, -1)])
    out = g.list_issues_gh(run, ["number"], cap=1000, env={})
    assert len(out) == 250 and len(run.rest_calls()) == 3
    run = ListFake([gqlfake.rest_item(i) for i in range(250, 0, -1)])
    out = g.list_issues_gh(run, ["number"], cap=120, env={})
    assert len(out) == 120 and out[0]["number"] == 250      # newest first, and the cap keeps the NEWEST


def test_list_issues_gh_order_is_requested_and_unexpressible_orders_refused():
    g = _mod("gh_api")
    run = ListFake([gqlfake.rest_item(1)])
    g.list_issues_gh(run, ["number"], env={})
    assert _params(run.calls[0])["sort"] == "created" and _params(run.calls[0])["direction"] == "desc"
    run = ListFake([gqlfake.rest_item(1)])
    g.list_issues_gh(run, ["number"], sort="updated", env={})
    assert _params(run.calls[0])["sort"] == "updated" and _params(run.calls[0])["direction"] == "desc"
    for sort in ("created", "updated"):
        run = ListFake()
        with pytest.raises(ValueError):
            g.list_issues_gh(run, ["number"], sort=sort, direction="asc", env={})
        assert run.calls == []
    with pytest.raises(ValueError):
        g.list_issues_gh(ListFake(), ["number"], sort="comments", env={})


def test_list_issues_gh_refuses_unmappable_fields_before_any_call():
    g = _mod("gh_api")
    for bad in (["bogus"], ["comments"]):
        run = ListFake()
        with pytest.raises(ValueError):
            g.list_issues_gh(run, bad, env={})
        assert run.calls == []


@pytest.mark.parametrize("raw", ['{"not": "a list"}', "", "   ", "not json", "null", '"x"'],
                         ids=["dict", "empty", "blank", "malformed", "null", "string"])
def test_list_issues_gh_bad_page_is_kind_other_with_no_fallback(raw):
    g = _mod("gh_api")
    run = ListFake(rest_raw=raw)
    with pytest.raises(g.GhApiError) as ei:
        g.list_issues_gh(run, ["number"], env={})
    assert ei.value.kind == "other" and run.gql_calls() == [] and len(run.rest_calls()) == 1


def test_list_issues_gh_bad_second_page_discards_and_never_falls_back():
    g = _mod("gh_api")
    seen = []

    def run(args):
        seen.append(args)
        if args[0] == "issue":
            return "[]"
        return json.dumps([gqlfake.rest_item(i) for i in range(100)]) if _params(args)["page"] == "1" else "{}"

    with pytest.raises(g.GhApiError):
        g.list_issues_gh(run, ["number"], cap=1000, env={})
    assert [c[0] for c in seen] == ["api", "api"]


def _fallback_argv(labels=("sdlc:goal",), state="open", fields="number,labels", cap=200, updated=False, repo="o/r"):
    argv = ["issue", "list", "--repo", repo]
    for l in labels:
        argv += ["--label", l]
    argv += ["--state", state, "--json", fields, "--limit", str(cap)]
    return argv + (["--search", "sort:updated-desc"] if updated else [])


@pytest.mark.parametrize("hint", [t[1] for t in TRANSIENT], ids=[t[0] for t in TRANSIENT])
def test_list_issues_gh_falls_back_exactly_once_on_transient(hint):
    g = _mod("gh_api")
    run = ListFake(fail=_err(hint), gql='[{"number": 5, "labels": [], "extra": 1}]')
    out = g.list_issues_gh(run, ["number", "labels"], repo="o/r", labels=["sdlc:goal"], env={})
    assert out == [{"number": 5, "labels": []}]
    assert run.gql_calls() == [_fallback_argv()] and len(run.rest_calls()) == 1


def test_list_issues_gh_fallback_argv_shapes():
    g = _mod("gh_api")
    run = ListFake(fail=_err(RATE), gql="[]")
    g.list_issues_gh(run, ["number"], labels=["a", "b"], state="all", cap=5000, env={})
    assert run.gql_calls() == [["issue", "list", "--label", "a", "--label", "b", "--state", "all",
                                "--json", "number", "--limit", "5000"]]
    run = ListFake(fail=_err(RATE), gql="[]")
    g.list_issues_gh(run, ["number", "body"], repo="o/r", labels=["g"], sort="updated", env={})
    assert run.gql_calls() == [_fallback_argv(("g",), fields="number,body", updated=True)]


@pytest.mark.parametrize("hint", [c[1] for c in CLIENT], ids=[c[0] for c in CLIENT])
def test_list_issues_gh_never_falls_back_on_client_errors(hint):
    g = _mod("gh_api")
    run = ListFake(fail=_err(hint))
    with pytest.raises(g.GhApiError):
        g.list_issues_gh(run, ["number"], env={})
    assert run.gql_calls() == []


@pytest.mark.parametrize("hint", [PROXY + " (HTTP 429)", PROXY + " (HTTP 502)"], ids=["proxy-429", "proxy-502"])
def test_list_issues_gh_never_falls_back_on_proxy_block(hint):
    g = _mod("gh_api")
    run = ListFake(fail=_err(hint))
    with pytest.raises(g.GhApiError) as ei:
        g.list_issues_gh(run, ["number"], env={})
    assert run.gql_calls() == [] and ei.value.kind == "proxy"


@pytest.mark.parametrize("env", [{"CLAUDE_CODE_REMOTE": "true"}, {"SIGMA_GH_GRAPHQL": "off"}],
                         ids=["cloud", "override-off"])
def test_list_issues_gh_skips_fallback_when_graphql_unavailable(env):
    g = _mod("gh_api")
    run = ListFake(fail=_err(RATE))
    with pytest.raises(g.GhApiError) as ei:
        g.list_issues_gh(run, ["number"], env=env)
    assert run.gql_calls() == [] and ei.value.kind == "rate_limit"


def test_list_issues_gh_fallback_failure_or_non_list_is_an_error_with_no_second_call():
    g = _mod("gh_api")
    run = ListFake(fail=_err("gh: Server Error (HTTP 502)"), gql=_err("gh: Server Error (HTTP 503)", text="x failed"))
    with pytest.raises(g.GhApiError) as ei:
        g.list_issues_gh(run, ["number"], env={})
    assert len(run.rest_calls()) == 1 and len(run.gql_calls()) == 1 and ei.value.kind == "server"
    for raw in ('{"a": 1}', "", "<html>"):
        run = ListFake(fail=_err(RATE), gql=raw)
        with pytest.raises(g.GhApiError) as ei:
            g.list_issues_gh(run, ["number"], env={})
        assert len(run.gql_calls()) == 1 and len(run.rest_calls()) == 1 and ei.value.kind == "other"


def test_list_issues_gh_fallback_goes_through_the_gate_with_the_given_gql_run():
    g = _mod("gh_api")
    rest = ListFake(fail=_err(RATE))
    gql = ListFake(gql='[{"number": 1}]')
    assert g.list_issues_gh(rest, ["number"], gql_run=gql, env={}) == [{"number": 1}]
    assert rest.gql_calls() == [] and len(gql.gql_calls()) == 1


def test_list_issues_gh_breaker_is_shared_with_read_issue_and_log_is_bounded(tmp_path):
    g = _mod("gh_api")
    for i in range(2):
        g.list_issues_gh(ListFake(fail=_err("gh: Server Error (HTTP 502)")), ["number"], env={}, sdlc_dir=tmp_path,
                         now=1000.0 + i)
    g.read_issue(RestFake(fail=_err("gh: Server Error (HTTP 502)")), 7, ["state"], "o/r", env={}, sdlc_dir=tmp_path,
                 now=1002.0)
    b = _breaker(tmp_path)
    assert b["consecutive"] == 3 and b["opened_at"] == 1002.0
    run = ListFake(gql="[]")
    g.list_issues_gh(run, ["number"], env={}, sdlc_dir=tmp_path, now=1003.0)
    assert run.rest_calls() == [] and len(run.gql_calls()) == 1          # breaker open: list skips REST too
    entries = json.loads((tmp_path / "state" / "gh-fallback.json").read_text())
    assert [e["op"] for e in entries[:2]] == ["issue_list", "issue_list"] and entries[2]["op"] == "issue_read"
    assert entries[0]["number"] is None and set(entries[0]) == {"ts", "op", "number", "kind", "status", "fell_back", "why"}


def test_list_issues_gh_stderr_lines_for_a_list_op_do_not_crash(tmp_path, capsys):
    """number is None for a list: the shared stderr text must not %d it."""
    g = _mod("gh_api")
    g.list_issues_gh(ListFake(fail=_err("gh: Server Error (HTTP 502)")), ["number"], env={}, sdlc_dir=tmp_path, now=1.0)
    err = capsys.readouterr().err
    assert "fell back to gh issue list once" in err and "issue list failed (server, HTTP 502)" in err
    for i in range(2):
        g.list_issues_gh(ListFake(fail=_err("gh: Server Error (HTTP 502)")), ["number"], env={}, sdlc_dir=tmp_path,
                         now=2.0 + i)
    err = capsys.readouterr().err
    assert "breaker open for 300s" in err and "reads use gh issue list" in err


def test_list_issues_gh_rest_success_resets_breaker(tmp_path):
    g = _mod("gh_api")
    g.list_issues_gh(ListFake(fail=_err(RATE)), ["number"], env={}, sdlc_dir=tmp_path, now=1.0)
    g.list_issues_gh(ListFake([gqlfake.rest_item(1)]), ["number"], env={}, sdlc_dir=tmp_path, now=2.0)
    assert _breaker(tmp_path)["consecutive"] == 0


def test_list_issues_gh_no_sdlc_dir_never_writes(tmp_path, monkeypatch):
    g = _mod("gh_api")
    monkeypatch.chdir(tmp_path)
    for _ in range(4):
        g.list_issues_gh(ListFake(fail=_err(RATE)), ["number"], env={}, now=1.0)
    assert list(tmp_path.rglob("*")) == []


def test_list_fallback_argv_is_built_only_in_gh_api():
    assert '"issue", "list"' in (S / "gh_api.py").read_text()


def test_github_source_list_issues_rest_first_with_shared_breaker(tmp_path, monkeypatch):
    """#895 2c step 4: `GitHubSource._list_issues` mirrors `_read_issue`: its own `_run`, its repo, its
    sdlc_dir (breaker + log written there), newest first."""
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)
    sources = _mod("sources")
    run = ListFake([gqlfake.rest_item(2, ["sdlc:goal"]), gqlfake.rest_item(1, ["sdlc:goal"])])
    gh = sources.GitHubSource({"discovery": {"source": "github", "github": {"repo": "acme/widget"}}}, run=run,
                              sdlc_dir=str(tmp_path))
    assert [i["number"] for i in gh._list_issues(["number"], labels=["sdlc:goal"])] == [2, 1]
    c = run.rest_calls()[0]
    assert c[1] == "repos/acme/widget/issues" and _params(c)["direction"] == "desc"
    run.items, run.fail = [], _err("gh: Server Error (HTTP 502)")
    run.gql = "[]"
    assert gh._list_issues(["number"], labels=["sdlc:goal"]) == []
    assert run.gql_calls() == [["issue", "list", "--repo", "acme/widget", "--label", "sdlc:goal", "--state", "open",
                                "--json", "number", "--limit", "5000"]]
    assert _breaker(tmp_path)["consecutive"] == 1
    assert json.loads((tmp_path / "state" / "gh-fallback.json").read_text())[0]["op"] == "issue_list"


# ---------------------------------------------------------------- WRITE policy (#895 slice 3a)

def _gerr(g, hint=None, text="gh api x failed", cause=None):
    """A GhApiError built the way `_call` builds one: a RuntimeError (optionally with a gh-stderr
    `.hint` and a `__cause__`) wrapped, so `.kind`/`.status` come from `classify` on `str(exc)` when there
    is no hint -- exactly the shape the write layer must NOT trust."""
    e = RuntimeError(text if hint is None else "%s: %s" % (text, hint))
    if hint is not None:
        e.hint = hint
    if cause is not None:
        e.__cause__ = cause
    try:
        g._call(Fake(e), ["api", "x"])
    except g.GhApiError as out:
        return out


WRITE_SAMPLES = {
    "primary_rate_limit": "gh: API rate limit exceeded for user ID 1. (HTTP 429)",
    "secondary_rate_limit": "gh: You have exceeded a secondary rate limit. (HTTP 403)",
    "server_5xx": "gh: Server Error (HTTP 502)",
    "transport_ambiguous": "Post https://api.github.com/x: dial tcp: i/o timeout",
    "auth_401": "gh: Bad credentials (HTTP 401)",
    "not_found_404": "gh: Not Found (HTTP 404)",
    "invalid_422": "gh: Validation Failed (HTTP 422)",
    "permission_403": "gh: Resource not accessible by integration (HTTP 403)",
    "proxy_block": GQL_TEXT,
    "other_unparsed": "gh: something odd happened",
}


def _sample(g, case):
    if case == "refused":
        return g.GhApiError("refused", kind="refused")
    return _gerr(g, WRITE_SAMPLES[case])


def test_write_policy_table():
    g = _mod("gh_api")
    ids = [r[0] for r in g.WRITE_POLICY]
    assert len(ids) == len(set(ids)) and set(ids) == set(WRITE_SAMPLES) | {"refused"}
    for case, _desc, idem_ok, non_ok in g.WRITE_POLICY:
        exc = _sample(g, case)
        assert g._write_case(exc) == case
        assert g._write_fallback_ok(exc, True) is idem_ok, case
        assert g._write_fallback_ok(exc, False) is non_ok, case
    flags = {r[0]: (r[2], r[3]) for r in g.WRITE_POLICY}
    assert flags["primary_rate_limit"] == (True, True)
    assert flags["server_5xx"] == (True, False)
    for case in ("secondary_rate_limit", "transport_ambiguous", "auth_401", "not_found_404", "invalid_422",
                 "permission_403", "proxy_block", "refused", "other_unparsed"):
        assert flags[case] == (False, False), case


def test_cloud_sessions_doc_lists_every_write_policy_case():
    """docs/cloud-sessions.md carries the policy table: every WRITE_POLICY case id is a backticked row id
    there, and the yes/no columns equal the table's flags (so the prose cannot drift from the code)."""
    g = _mod("gh_api")
    doc = (S.parent.parent.parent / "docs" / "cloud-sessions.md").read_text(encoding="utf-8")
    rows = {}
    for line in doc.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 6 and cells[1].startswith("`") and cells[1].endswith("`"):
            rows[cells[1].strip("`")] = (cells[-3], cells[-2])
    for case, _desc, idem_ok, non_ok in g.WRITE_POLICY:
        assert case in rows, "docs/cloud-sessions.md lacks the write-policy row `%s`" % case
        assert rows[case] == ("yes" if idem_ok else "no", "yes" if non_ok else "no"), case


def test_every_classify_kind_and_refused_maps_to_exactly_one_row():
    g = _mod("gh_api")
    ids = {r[0] for r in g.WRITE_POLICY}
    kinds = {"proxy", "rate_limit", "permission", "auth", "not_found", "invalid", "server", "transport",
             "other", "refused"}
    assert set(g.WRITE_CASE_FOR_KIND) == kinds
    assert set(g.WRITE_CASE_FOR_KIND.values()) <= ids


def test_secondary_rate_limit_never_falls_back():
    g = _mod("gh_api")
    for hint in ("gh: You have exceeded a secondary rate limit (HTTP 403)",
                 "gh: abuse detection mechanism (HTTP 403)",
                 "gh: secondary limit, slow down (HTTP 429)"):
        exc = _gerr(g, hint)
        assert g._write_case(exc) == "secondary_rate_limit", hint
        assert g._write_fallback_ok(exc, True) is False and g._write_fallback_ok(exc, False) is False


# ---------------------------------------------------------------- _rest_write (#895 slice 3a)

def _rw(g, rest_exc, *, idempotent, fb=None, env=None, sdlc_dir=None, now=100.0):
    """Drive `_rest_write` with a REST closure that raises `rest_exc` and a counting fallback."""
    calls = {"rest": 0, "fb": 0}

    def rest():
        calls["rest"] += 1
        raise rest_exc

    def fallback():
        calls["fb"] += 1
        if isinstance(fb, Exception):
            raise fb
        return "fell-back"

    try:
        out = g._rest_write("issue_comment", 7, "issue #7 comment", "gh issue comment", rest, fallback,
                            idempotent=idempotent, env={} if env is None else env, sdlc_dir=sdlc_dir, now=now)
    except Exception as exc:                          # noqa: BLE001
        return exc, calls
    return out, calls


def test_rest_write_success_returns_rest_result_without_fallback():
    g = _mod("gh_api")
    out = g._rest_write("o", 1, "w", "c", lambda: "ok", lambda: pytest.fail("fallback"), idempotent=True, env={})
    assert out == "ok"


def test_exactly_one_fallback_then_propagate():
    g = _mod("gh_api")
    rest_exc = _gerr(g, WRITE_SAMPLES["server_5xx"])
    out, calls = _rw(g, rest_exc, idempotent=True)
    assert out == "fell-back" and calls == {"rest": 1, "fb": 1}
    boom = RuntimeError("fallback also failed")
    out, calls = _rw(g, rest_exc, idempotent=True, fb=boom)
    assert out is boom and calls == {"rest": 1, "fb": 1}             # propagates, no second try


def test_cloud_session_never_falls_back():
    g = _mod("gh_api")
    for env in ({"CLAUDE_CODE_REMOTE": "true"}, {"SIGMA_GH_GRAPHQL": "off"}):
        for case, idem in (("server_5xx", True), ("primary_rate_limit", True), ("primary_rate_limit", False)):
            rest_exc = _gerr(g, WRITE_SAMPLES[case])
            out, calls = _rw(g, rest_exc, idempotent=idem, env=env)
            assert out is rest_exc and calls == {"rest": 1, "fb": 0}, (env, case)


def test_write_never_touches_read_breaker(tmp_path):
    g = _mod("gh_api")
    state = tmp_path / "state"
    rest_exc = _gerr(g, WRITE_SAMPLES["server_5xx"])
    for _ in range(5):
        out, _c = _rw(g, rest_exc, idempotent=True, sdlc_dir=str(tmp_path))
        assert out == "fell-back"
    assert not (state / "gh-rest-breaker.json").exists()
    (state / "gh-rest-breaker.json").write_text('{"consecutive": 0, "opened_at": null, "last_kind": null}')
    before = (state / "gh-rest-breaker.json").stat().st_mtime_ns, (state / "gh-rest-breaker.json").read_text()
    _rw(g, rest_exc, idempotent=True, sdlc_dir=str(tmp_path))
    after = (state / "gh-rest-breaker.json").stat().st_mtime_ns, (state / "gh-rest-breaker.json").read_text()
    assert before == after


def test_write_fallback_log_has_no_text_argv_or_body(tmp_path):
    g = _mod("gh_api")
    secret = "gh: Server Error (HTTP 502) tok_SECRET body=top-secret-body"
    rest_exc = _gerr(g, secret, text="gh api -f body=top-secret-body failed")
    _rw(g, rest_exc, idempotent=True, sdlc_dir=str(tmp_path))
    raw = (tmp_path / "state" / "gh-fallback.json").read_text()
    entries = json.loads(raw)
    assert len(entries) == 1 and entries[0]["op"] == "issue_comment" and entries[0]["fell_back"] is True
    assert entries[0]["kind"] == "server_5xx" and entries[0]["status"] == 502
    assert "SECRET" not in raw and "top-secret-body" not in raw


def test_write_fallback_log_is_capped(tmp_path):
    g = _mod("gh_api")
    rest_exc = _gerr(g, WRITE_SAMPLES["server_5xx"])
    for _ in range(g.FALLBACK_LOG_CAP + 5):
        _rw(g, rest_exc, idempotent=True, sdlc_dir=str(tmp_path))
    assert len(json.loads((tmp_path / "state" / "gh-fallback.json").read_text())) == g.FALLBACK_LOG_CAP


def test_write_fallback_prints_one_stderr_line(capsys):
    g = _mod("gh_api")
    _rw(g, _gerr(g, WRITE_SAMPLES["server_5xx"]), idempotent=True)
    err = capsys.readouterr().err
    assert err.count("fell back to gh issue comment once") == 1 and err.count("\n") == 1


BODY_WORDING = ("rate limit", "abuse", "secondary rate limit", "timeout", "timed out", "HTTP 502", "HTTP 429",
                "dial tcp", "HTTP 503")


def test_body_wording_does_not_drive_write_classification():
    """refinement a: `exc.kind`/`exc.status` come from classify(str(exc)) when there is no hint, and
    str(exc) embeds the write BODY. With hint=None (and with an unrelated hint) none of this may fall back."""
    g = _mod("gh_api")
    for word in BODY_WORDING:
        for hint in (None, "gh: something odd happened"):
            exc = _gerr(g, hint, text="gh api repos/o/r/issues/1/comments -f body=%s failed" % word)
            assert g._write_case(exc) == "other_unparsed", (word, hint)
            for idem in (True, False):
                out, calls = _rw(g, exc, idempotent=idem)
                assert out is exc and calls == {"rest": 1, "fb": 0}, (word, hint, idem)
    # the same wording IN THE HINT behaves per the table
    assert g._write_case(_gerr(g, "gh: Server Error (HTTP 502)")) == "server_5xx"
    assert g._write_case(_gerr(g, "gh: API rate limit exceeded (HTTP 429)")) == "primary_rate_limit"


def test_hint_none_with_rate_limit_text_does_not_fall_back_even_though_kind_says_rate_limit():
    g = _mod("gh_api")
    exc = _gerr(g, None, text="gh api x -f body=hit the rate limit failed")
    assert exc.kind == "rate_limit"                       # what classify() concluded from the body...
    assert g._write_fallback_ok(exc, True) is False        # ...which the write layer must ignore
    forged = g.GhApiError("x", hint=None, status=502, kind="server")
    assert g._write_fallback_ok(forged, True) is False


def test_real_transport_shapes_are_no_fallback_for_every_write():
    """refinement b: a real gh transport failure is a RuntimeError with a hint and NO `HTTP nnn` and no
    TimeoutExpired cause (-> other_unparsed), or a TimeoutExpired-caused one (-> transport_ambiguous);
    neither falls back, for idempotent or not."""
    g = _mod("gh_api")
    shapes = [
        (_gerr(g, "error connecting to api.github.com"), "other_unparsed"),
        (_gerr(g, "gh: request failed"), "other_unparsed"),
        (_gerr(g, "gh: request failed", cause=subprocess.TimeoutExpired(["gh"], 120)), "transport_ambiguous"),
        (_gerr(g, None, text="gh api x failed", cause=subprocess.TimeoutExpired(["gh"], 120)), "transport_ambiguous"),
        (_gerr(g, "Post https://api.github.com/x: dial tcp 1.2.3.4:443: i/o timeout"), "transport_ambiguous"),
    ]
    for exc, case in shapes:
        assert g._write_case(exc) == case
        for idem in (True, False):
            out, calls = _rw(g, exc, idempotent=idem)
            assert out is exc and calls["fb"] == 0


# ---------------------------------------------------------------- per-op wiring (#895 slice 3a)

def _he(hint, cause=None):
    e = RuntimeError("gh api -f body=X failed: " + hint)
    e.hint = hint
    if cause is not None:
        e.__cause__ = cause
    return e


class WFake:
    """REST (`api ...`) calls raise `rest_exc` (or answer `rest_ok`); `issue ...` calls are the fallback."""

    def __init__(self, rest_exc=None, rest_ok="{}", fb_exc=None):
        self.rest_exc, self.rest_ok, self.fb_exc, self.calls = rest_exc, rest_ok, fb_exc, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "api":
            if self.rest_exc is not None:
                raise self.rest_exc
            return self.rest_ok
        if self.fb_exc is not None:
            raise self.fb_exc
        return "https://github.com/o/r/issues/9\n"

    def fb(self):
        return [c for c in self.calls if c[0] == "issue"]

    def rest(self):
        return [c for c in self.calls if c[0] == "api"]


#: op name -> (callable(g, run, **kw), idempotent, the exact `gh issue ...` fallback argv)
WRITE_OPS = {
    "comment": (lambda g, r, **kw: g.comment_issue(r, 7, "hello", repo="o/r", **kw), False,
                ["issue", "comment", "7", "--repo", "o/r", "--body", "hello"]),
    "create": (lambda g, r, **kw: g.create_issue(r, "T", "B", labels=["area:x", "sdlc:goal"], repo="o/r", **kw),
               False, ["issue", "create", "--repo", "o/r", "--title", "T", "--body", "B", "--label", "area:x",
                       "--label", "sdlc:goal"]),
    "add_labels": (lambda g, r, **kw: g.add_labels(r, 7, ["a", "b"], repo="o/r", **kw), True,
                   ["issue", "edit", "7", "--repo", "o/r", "--add-label", "a", "--add-label", "b"]),
    "remove_label": (lambda g, r, **kw: g.remove_label(r, 7, "a", repo="o/r", **kw), True,
                     ["issue", "edit", "7", "--repo", "o/r", "--remove-label", "a"]),
    "close": (lambda g, r, **kw: g.close_issue(r, 7, repo="o/r", reason="completed", **kw), True,
              ["issue", "close", "7", "--repo", "o/r", "--reason", "completed"]),
    "edit": (lambda g, r, **kw: g.edit_issue(r, 7, body="new", repo="o/r", **kw), True,
             ["issue", "edit", "7", "--repo", "o/r", "--body", "new"]),
    "assign": (lambda g, r, **kw: g.add_assignees(r, 7, ["alice"], repo="o/r", **kw), True,
               ["issue", "edit", "7", "--repo", "o/r", "--add-assignee", "alice"]),
}
IDEMPOTENT = [k for k, v in WRITE_OPS.items() if v[1]]
NON_IDEMPOTENT = [k for k, v in WRITE_OPS.items() if not v[1]]


def test_write_op_table_matches_the_documented_idempotent_set():
    assert sorted(IDEMPOTENT) == ["add_labels", "assign", "close", "edit", "remove_label"]
    assert sorted(NON_IDEMPOTENT) == ["comment", "create"]


def _drive(op, exc, env=None, **kw):
    g = _mod("gh_api")
    run = WFake(rest_exc=exc)
    try:
        out = WRITE_OPS[op][0](g, run, env={} if env is None else env, **kw)
    except Exception as e:                            # noqa: BLE001
        out = e
    return out, run


def test_comment_5xx_never_falls_back():
    out, run = _drive("comment", _he("gh: Server Error (HTTP 502)"))
    assert isinstance(out, Exception) and len(run.calls) == 1 and run.fb() == []


def test_create_5xx_never_falls_back():
    out, run = _drive("create", _he("gh: Server Error (HTTP 503)"))
    assert isinstance(out, Exception) and len(run.calls) == 1 and run.fb() == []


@pytest.mark.parametrize("op", IDEMPOTENT)
def test_idempotent_5xx_falls_back_exactly_once_with_todays_argv(op):
    out, run = _drive(op, _he("gh: Server Error (HTTP 502)"))
    assert not isinstance(out, Exception), out
    assert len(run.rest()) == 1 and run.fb() == [WRITE_OPS[op][2]]


@pytest.mark.parametrize("op", sorted(WRITE_OPS))
def test_primary_rate_limit_falls_back_for_every_write(op):
    out, run = _drive(op, _he("gh: API rate limit exceeded (HTTP 403)"))
    assert not isinstance(out, Exception), out
    assert run.fb() == [WRITE_OPS[op][2]] and len(run.rest()) == 1


def test_primary_429_create_falls_back_once():
    out, run = _drive("create", _he("gh: API rate limit exceeded (HTTP 429)"))
    assert run.fb() == [WRITE_OPS["create"][2]] and len(run.calls) == 2


@pytest.mark.parametrize("op", sorted(WRITE_OPS))
def test_transport_timeout_never_falls_back_for_any_write(op):
    """Ambiguous: the request may have landed, so even the idempotent writes do not re-issue it."""
    for exc in (_he("gh: request failed", cause=subprocess.TimeoutExpired(["gh"], 120)),
                _he("Post https://api.github.com/x: dial tcp: i/o timeout"),
                _he("error connecting to api.github.com")):
        out, run = _drive(op, exc)
        assert isinstance(out, Exception) and run.fb() == [] and len(run.calls) == 1, (op, exc)


def _never(op, hint, name_ok_none=False):
    out, run = _drive(op, _he(hint))
    if name_ok_none and op == "remove_label":
        assert out is None                              # a 404 on remove_label is the documented no-op
    else:
        assert isinstance(out, Exception), (op, out)
    assert run.fb() == [] and len(run.calls) == 1, op


@pytest.mark.parametrize("op", sorted(WRITE_OPS))
def test_no_fallback_on_422(op):
    _never(op, "gh: Validation Failed (HTTP 422)")


@pytest.mark.parametrize("op", sorted(WRITE_OPS))
def test_no_fallback_on_404(op):
    _never(op, "gh: Not Found (HTTP 404)", name_ok_none=True)


@pytest.mark.parametrize("op", sorted(WRITE_OPS))
def test_no_fallback_on_permission_403(op):
    _never(op, "gh: Resource not accessible by integration (HTTP 403)")


@pytest.mark.parametrize("op", sorted(WRITE_OPS))
def test_no_fallback_on_proxy_block(op):
    _never(op, GQL_TEXT)


@pytest.mark.parametrize("op", sorted(WRITE_OPS))
def test_no_fallback_on_401(op):
    _never(op, "gh: Bad credentials (HTTP 401)")


@pytest.mark.parametrize("op", sorted(WRITE_OPS))
def test_secondary_rate_limit_never_falls_back_per_op(op):
    _never(op, "gh: You have exceeded a secondary rate limit (HTTP 403)")


@pytest.mark.parametrize("op", sorted(WRITE_OPS))
def test_cloud_session_never_falls_back_per_op(op):
    for env in ({"CLAUDE_CODE_REMOTE": "true"}, {"SIGMA_GH_GRAPHQL": "off"}):
        out, run = _drive(op, _he("gh: API rate limit exceeded (HTTP 429)"), env=env)
        assert isinstance(out, Exception) and run.fb() == [] and len(run.calls) == 1


def test_fallback_runs_through_fallback_run_defaulting_to_run():
    g = _mod("gh_api")
    rest = WFake(rest_exc=_he("gh: Server Error (HTTP 502)"))
    fb = WFake()
    g.close_issue(rest, 7, repo="o/r", fallback_run=fb, env={})
    assert fb.calls == [["issue", "close", "7", "--repo", "o/r"]] and rest.fb() == []
    rest = WFake(rest_exc=_he("gh: Server Error (HTTP 502)"))
    g.close_issue(rest, 7, repo="o/r", env={})
    assert rest.fb() == [["issue", "close", "7", "--repo", "o/r"]]


def test_fallback_failure_propagates_as_gh_api_error_without_retry():
    g = _mod("gh_api")
    run = WFake(rest_exc=_he("gh: Server Error (HTTP 502)"), fb_exc=RuntimeError("fb down"))
    with pytest.raises(g.GhApiError):
        g.close_issue(run, 7, repo="o/r", env={})
    assert len(run.rest()) == 1 and len(run.fb()) == 1


def test_remove_label_matches_structured_404_only():
    g = _mod("gh_api")
    ok = lambda hint, text=None: (lambda e: (setattr(e, "hint", hint) if hint else None, e)[1])(RuntimeError(text or hint))
    assert g.remove_label(Fake(ok("gh: Not Found (HTTP 404)")), 4, "x", env={}) is None
    # a body/argv mentioning 404, with a non-404 (or absent) hint, raises
    e = RuntimeError("gh api repos/o/r/issues/404/labels/x DELETE failed: gh: Server Error (HTTP 500)")
    e.hint = "gh: Server Error (HTTP 500)"
    with pytest.raises(g.GhApiError):
        g.remove_label(WFake(rest_exc=e), 404, "x", env={"SIGMA_GH_GRAPHQL": "off"})
    with pytest.raises(g.GhApiError):
        g.remove_label(Fake(RuntimeError("HTTP 404: Label does not exist")), 4, "x", env={})      # no hint
    with pytest.raises(g.GhApiError):
        g.remove_label(Fake(ok("gh: boom", "gh api x -f body=error 404 failed: gh: boom")), 4, "x", env={})


def test_remove_label_404_swallow_cannot_tell_a_vanished_issue_from_an_absent_label():
    """DOCUMENTED blind spot (plan D4): exact GitHub 404 bodies are UNMEASURED, so both 404s are a no-op.
    A caller that must notice a vanished issue relies on its NEXT write (e.g. the comment) failing."""
    g = _mod("gh_api")
    for hint in ("gh: Label does not exist (HTTP 404)", "gh: Not Found (HTTP 404)"):
        assert g.remove_label(Fake(_he(hint)), 4, "x", env={}) is None


# ---------------------------------------------------------------- edit_issue / add_assignees (Task B)

def test_edit_issue_patches_body_with_raw_field_never_dash_F():
    g = _mod("gh_api")
    run = Fake("{}")
    g.edit_issue(run, 5, body="@file.txt and key=val", repo="o/r")
    assert run.calls == [["api", "repos/o/r/issues/5", "--method", "PATCH", "-f", "body=@file.txt and key=val"]]
    assert "-F" not in run.calls[0]
    with pytest.raises(ValueError):
        g.edit_issue(run, 5, repo="o/r")


def _assign_fake(user="bob", post=None, user_exc=None, post_exc=None):
    def f(args):
        f.calls.append(list(args))
        if args[:2] == ["api", "user"]:
            if user_exc:
                raise user_exc
            return json.dumps({"login": user})
        if args[0] == "api":
            if post_exc:
                raise post_exc
            return json.dumps(post if post is not None else {"assignees": []})
        return "ok"
    f.calls = []
    return f


def test_add_assignees_argv_strips_at_and_matches_login_case_insensitively():
    g = _mod("gh_api")
    run = _assign_fake(post={"assignees": [{"login": "Alice"}]})
    g.add_assignees(run, 7, ["@alice"], repo="o/r")
    assert run.calls == [["api", "repos/o/r/issues/7/assignees", "--method", "POST", "-f", "assignees[]=alice"]]


def test_add_assignees_resolves_me():
    g = _mod("gh_api")
    for me in ("@me", "me"):
        run = _assign_fake(user="bob", post={"assignees": [{"login": "bob"}]})
        g.add_assignees(run, 7, [me], repo="o/r")
        assert run.calls == [["api", "user", "--method", "GET"],
                             ["api", "repos/o/r/issues/7/assignees", "--method", "POST", "-f", "assignees[]=bob"]]


def test_add_assignees_refuses_team_slug():
    g = _mod("gh_api")
    run = _assign_fake()
    with pytest.raises(g.GhApiError) as ei:
        g.add_assignees(run, 7, ["org/team"], repo="o/r")
    assert ei.value.kind == "invalid" and run.calls == []


def test_add_assignees_dropped_login_is_invalid():
    g = _mod("gh_api")
    run = _assign_fake(post={"assignees": [{"login": "someone-else"}]})
    with pytest.raises(g.GhApiError) as ei:
        g.add_assignees(run, 7, ["alice"], repo="o/r", env={})
    assert ei.value.kind == "invalid" and "alice" in str(ei.value)
    assert [c for c in run.calls if c[0] == "issue"] == []                 # invalid never falls back
    with pytest.raises(g.GhApiError):
        g.add_assignees(_assign_fake(post={}), 7, ["alice"], repo="o/r", env={})
    with pytest.raises(g.GhApiError):
        g.add_assignees(_assign_fake(post=[1]), 7, ["alice"], repo="o/r", env={})   # malformed 2xx body


def test_add_assignees_fallback_receives_original_me():
    g = _mod("gh_api")
    run = _assign_fake(user="bob", post_exc=_he("gh: Server Error (HTTP 503)"))
    g.add_assignees(run, 7, ["@me"], repo="o/r", env={})
    assert [c for c in run.calls if c[0] == "issue"] == [
        ["issue", "edit", "7", "--repo", "o/r", "--add-assignee", "@me"]]


def test_add_assignees_get_user_403_raises_without_post_or_fallback():
    g = _mod("gh_api")
    run = _assign_fake(user_exc=_he("gh: Resource not accessible by integration (HTTP 403)"))
    with pytest.raises(g.GhApiError) as ei:
        g.add_assignees(run, 7, ["@me"], repo="o/r", env={})
    assert ei.value.kind == "permission" and "403" in str(ei.value)
    assert run.calls == [["api", "user", "--method", "GET"]]


def test_add_assignees_get_user_5xx_follows_the_idempotent_table():
    g = _mod("gh_api")
    run = _assign_fake(user_exc=_he("gh: Server Error (HTTP 502)"))
    g.add_assignees(run, 7, ["@me"], repo="o/r", env={})
    assert [c for c in run.calls if c[0] == "issue"] == [
        ["issue", "edit", "7", "--repo", "o/r", "--add-assignee", "@me"]]


# ---------------------------------------------------------------- #931: landing write plumbing (upkeep part C, slice 2)

SHA = "a" * 40
SHA2 = "b" * 40


def test_merge_pr_pinned_argv_is_explicit_and_pin_last():
    g = _mod("gh_api")
    run = Fake(json.dumps({"merged": True, "sha": SHA2}))
    out = g.merge_pr_pinned(run, "o/r", 11, SHA, merge_method="merge")
    assert run.calls == [["api", "repos/o/r/pulls/11/merge", "--method", "PUT", "-f", "merge_method=merge",
                          "-f", "sha=" + SHA]]
    assert g.merge_reply_sha(out) == SHA2
    assert not any("auto" in a or "delete" in a for a in run.calls[0])


@pytest.mark.parametrize("kw", [
    dict(sha=""), dict(sha=None), dict(sha="abc123"), dict(sha="A" * 40), dict(sha="g" * 40), dict(sha=SHA + "0"),
    dict(merge_method="squash-ish"), dict(merge_method=""), dict(repo=""), dict(repo=None), dict(repo="{owner}/{repo}"),
    dict(repo="norepo"), dict(number=0), dict(number=True), dict(number="11"),
], ids=lambda kw: "-".join("%s=%r" % kv for kv in kw.items()))
def test_merge_pr_pinned_refuses_before_any_call(kw):
    g = _mod("gh_api")
    args = dict(repo="o/r", number=11, sha=SHA, merge_method="merge")
    args.update(kw)
    run = Fake("{}")
    with pytest.raises(g.GhApiError) as ei:
        g.merge_pr_pinned(run, args["repo"], args["number"], args["sha"], merge_method=args["merge_method"])
    assert ei.value.kind == "invalid" and run.calls == []


def test_merge_pr_pinned_method_is_keyword_only_and_required():
    g = _mod("gh_api")
    with pytest.raises(TypeError):
        g.merge_pr_pinned(Fake("{}"), "o/r", 11, SHA)
    with pytest.raises(TypeError):
        g.merge_pr_pinned(Fake("{}"), "o/r", 11, SHA, "merge")


@pytest.mark.parametrize("reply", ["[]", "null", json.dumps({"merged": True, "sha": "xyz"}),
                                   json.dumps({"merged": True, "sha": 5})], ids=["list", "null", "bad-sha", "int-sha"])
def test_merge_pr_pinned_rejects_malformed_replies(reply):
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError):
        g.merge_pr_pinned(Fake(reply), "o/r", 11, SHA, merge_method="merge")


def test_merge_reply_without_sha_reads_none_and_failure_keeps_class():
    g = _mod("gh_api")
    assert g.merge_reply_sha({"merged": False, "message": "x"}) is None
    exc = RuntimeError("gh failed")
    exc.hint = "gh: Head branch was modified. Review and try the merge again. (HTTP 409)"
    with pytest.raises(g.GhApiError) as ei:
        g.merge_pr_pinned(Fake(exc), "o/r", 11, SHA, merge_method="merge")
    assert ei.value.status == 409


@pytest.mark.parametrize("repo", ["./r", "../r", "o/.", "o/..", ".../r", "o/..."])
def test_repo_pattern_rejects_dot_only_segments(repo):
    g = _mod("gh_api")
    run = Fake("{}")
    with pytest.raises(g.GhApiError) as ei:
        g.merge_pr_pinned(run, repo, 11, SHA, merge_method="merge")
    assert ei.value.kind == "invalid" and run.calls == []


@pytest.mark.parametrize("repo", ["o/r", "a.b/c-d_e", "o/.github", "o/r.."])
def test_repo_pattern_keeps_ordinary_names(repo):
    g = _mod("gh_api")
    run = Fake(json.dumps({"merged": True, "sha": SHA2}))
    g.merge_pr_pinned(run, repo, 11, SHA, merge_method="merge")
    assert len(run.calls) == 1


@pytest.mark.parametrize("reply", [{"merged": True}, {"merged": False, "message": "x"}, {"merged": True, "sha": None}])
def test_merge_pr_pinned_rejects_reply_without_sha(reply):
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError):
        g.merge_pr_pinned(Fake(json.dumps(reply)), "o/r", 11, SHA, merge_method="merge")


def test_commit_parents_argv_and_validation():
    g = _mod("gh_api")
    run = Fake(json.dumps({"sha": SHA, "parents": [{"sha": SHA2}, {"sha": "c" * 40}]}))
    assert g.commit_parents(run, "o/r", SHA) == [SHA2, "c" * 40]
    assert run.calls == [["api", "repos/o/r/commits/" + SHA, "--method", "GET"]]
    for bad in ('{"parents": null}', '{"parents": [{"sha": "x"}]}', "[]", '{"parents": ["x"]}'):
        with pytest.raises(g.GhApiError):
            g.commit_parents(Fake(bad), "o/r", SHA)
    run = Fake("{}")
    with pytest.raises(g.GhApiError):
        g.commit_parents(run, "o/r", "main")
    with pytest.raises(g.GhApiError):
        g.commit_parents(run, None, SHA)
    assert run.calls == []


def test_branch_rules_and_queue_detection():
    g = _mod("gh_api")
    run = Fake(json.dumps([{"type": "pull_request"}, {"type": "merge_queue"}]))
    rules = g.branch_rules(run, "o/r", "main")
    assert run.calls == [["api", "repos/o/r/rules/branches/main", "--method", "GET"]]
    assert g.rules_have_merge_queue(rules) is True
    assert g.rules_have_merge_queue([{"type": "pull_request"}]) is False
    assert g.rules_have_merge_queue([]) is False
    run = Fake("[]")
    g.branch_rules(run, "o/r", "feature/a b")
    assert run.calls[0][1] == "repos/o/r/rules/branches/feature%2Fa%20b"
    with pytest.raises(g.GhApiError):
        g.branch_rules(Fake("{}"), "o/r", "main")
    with pytest.raises(g.GhApiError):
        g.branch_rules(Fake("[]"), "", "main")
    with pytest.raises(g.GhApiError):
        g.rules_have_merge_queue({"type": "merge_queue"})


@pytest.mark.parametrize("payload,want", [({"delete_branch_on_merge": True}, True),
                                          ({"delete_branch_on_merge": False}, False),
                                          ({}, None), ({"delete_branch_on_merge": "true"}, None)])
def test_repo_settings_and_delete_branch_on_merge(payload, want):
    g = _mod("gh_api")
    run = Fake(json.dumps(payload))
    got = g.repo_settings(run, "o/r")
    assert run.calls == [["api", "repos/o/r", "--method", "GET"]]
    assert g.delete_branch_on_merge(got) is want
    with pytest.raises(g.GhApiError):
        g.repo_settings(Fake("[]"), "o/r")


def test_create_pr_nondraft_sends_typed_false_and_requires_repo():
    g = _mod("gh_api")
    run = Fake('{"number": 12}')
    assert g.create_pr_nondraft(run, "T", "B", "feature/u", "main", "o/r") == {"number": 12}
    assert run.calls == [["api", "repos/o/r/pulls", "--method", "POST", "-f", "title=T", "-f", "body=B",
                          "-f", "head=feature/u", "-f", "base=main", "-F", "draft=false"]]
    run = Fake("{}")
    with pytest.raises(g.GhApiError):
        g.create_pr_nondraft(run, "T", "B", "h", "main", None)
    assert run.calls == []


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _runner(g, outcome, **kw):
    seen = []

    def fake(argv, **k):
        seen.append((argv, k))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return g.bounded_runner(popen=fake, **kw), seen


def test_bounded_runner_binds_cwd_timeout_and_returns_stdout():
    g = _mod("gh_api")
    run, seen = _runner(g, _Proc(0, '{"a": 1}'), cwd="/some/dir", timeout=7)
    assert run(["api", "user"]) == '{"a": 1}'
    argv, k = seen[0]
    assert argv == ["gh", "api", "user"] and k["cwd"] == "/some/dir" and k["timeout"] == 7
    assert k["capture_output"] is True and k["text"] is True


def test_bounded_runner_keeps_exit_code_status_and_kind():
    g = _mod("gh_api")
    run, _ = _runner(g, _Proc(1, "", "gh: Not Found (HTTP 404)"))
    with pytest.raises(g.GhApiError) as ei:
        run(["api", "x"])
    assert (ei.value.returncode, ei.value.status, ei.value.kind) == (1, 404, "not_found")
    assert "Not Found" in ei.value.hint
    run, _ = _runner(g, _Proc(1, "", "gh: boom (HTTP 502)"))
    with pytest.raises(g.GhApiError) as ei:
        run(["api", "x"])
    assert ei.value.kind == "server" and ei.value.returncode == 1


def test_bounded_runner_timeout_is_transport_and_missing_binary_is_other():
    g = _mod("gh_api")
    run, _ = _runner(g, subprocess.TimeoutExpired(["gh"], 5))
    with pytest.raises(g.GhApiError) as ei:
        run(["api", "x"])
    assert ei.value.kind == "transport" and ei.value.returncode is None and ei.value.status is None
    run, _ = _runner(g, FileNotFoundError("gh"))
    with pytest.raises(g.GhApiError) as ei:
        run(["api", "x"])
    assert ei.value.kind == "other" and ei.value.returncode is None and "gh" in ei.value.hint


def test_bounded_runner_rejects_bad_timeout():
    g = _mod("gh_api")
    for bad in (0, -1, True, "5", None):
        with pytest.raises(ValueError):
            g.bounded_runner(timeout=bad)


def test_call_carries_returncode_from_the_wrapped_failure():
    g = _mod("gh_api")
    exc = RuntimeError("x")
    exc.returncode = 4
    exc.hint = "gh: nope (HTTP 422)"
    with pytest.raises(g.GhApiError) as ei:
        g.view_pr(Fake(exc), 1, repo="o/r")
    assert ei.value.returncode == 4 and ei.value.kind == "invalid"
    with pytest.raises(g.GhApiError) as ei:
        g.view_pr(Fake(RuntimeError("y")), 1, repo="o/r")
    assert ei.value.returncode is None


def test_gate_closed_existing_pr_ops_are_byte_identical():
    g = _mod("gh_api")
    run = Fake("{}")
    g.create_pr(run, "T", "B", head="h", base="b")
    g.view_pr(run, 3)
    g.merge_pr(run, 3)
    g.merge_pr(run, 3, sha="")
    assert run.calls == [
        ["api", "repos/{owner}/{repo}/pulls", "--method", "POST", "-f", "title=T", "-f", "body=B", "-f", "head=h",
         "-f", "base=b"],
        ["api", "repos/{owner}/{repo}/pulls/3", "--method", "GET"],
        ["api", "repos/{owner}/{repo}/pulls/3/merge", "--method", "PUT", "-f", "merge_method=squash"],
        ["api", "repos/{owner}/{repo}/pulls/3/merge", "--method", "PUT", "-f", "merge_method=squash"],
    ]
    assert _mod("gh_api").GhApiError("t").returncode is None


def test_gh_api_adds_no_literal_gh_pr_list_and_no_graphql_string():
    src = (S / "gh_api.py").read_text()
    assert '"gh", "pr"' not in src and '"gh", "issue"' not in src
    assert src.count('"graphql"') == 1


# ---------------------------------------------------------------- PR reads, REST first (#895 slice 4a-1)

def _rest_pull(n=7, state="open", merged=False, merged_at=None, head_full="o/r", base_full="o/r", **kw):
    d = {"number": n, "title": "T", "body": "B", "state": state, "merged": merged, "merged_at": merged_at,
         "closed_at": None, "auto_merge": None, "user": {"login": "alice", "type": "User"},
         "head": {"sha": "a" * 40, "ref": "sdlc/7", "repo": {"full_name": head_full}},
         "base": {"ref": "main", "repo": {"full_name": base_full}}}
    d.update(kw)
    return d


def _drop(d, *path):
    """A deep copy of `d` with the key at `path` removed (absent, not null)."""
    d = json.loads(json.dumps(d))
    cur = d
    for k in path[:-1]:
        cur = cur[k]
    del cur[path[-1]]
    return d


PR_SHAPE_ROWS = [
    ("state-open", _rest_pull(), ["state"], {"state": "OPEN"}),
    ("state-closed", _rest_pull(state="closed"), ["state"], {"state": "CLOSED"}),
    ("state-closed-merged-at", _rest_pull(state="closed", merged_at="2026-01-02T00:00:00Z"), ["state"],
     {"state": "MERGED"}),
    ("state-merged-true", _rest_pull(state="closed", merged=True), ["state"], {"state": "MERGED"}),
    ("head-sha", _rest_pull(), ["headRefOid", "headRefName"], {"headRefOid": "a" * 40, "headRefName": "sdlc/7"}),
    ("head-sha-missing", _drop(_rest_pull(), "head", "sha"), ["headRefOid"], {"headRefOid": ""}),
    ("auto-merge-null", _rest_pull(), ["autoMergeRequest"], {"autoMergeRequest": None}),
    ("auto-merge-object", _rest_pull(auto_merge={"merge_method": "squash"}), ["autoMergeRequest"],
     {"autoMergeRequest": {"merge_method": "squash"}}),
    ("cross-same-repo", _rest_pull(), ["isCrossRepository"], {"isCrossRepository": False}),
    ("cross-fork", _rest_pull(head_full="fork/r"), ["isCrossRepository"], {"isCrossRepository": True}),
    ("cross-case-same-repo", _rest_pull(head_full="O/R"), ["isCrossRepository"], {"isCrossRepository": False}),
    ("cross-deleted-fork", _rest_pull(head={"sha": "a" * 40, "ref": "x", "repo": None}), ["isCrossRepository"],
     {"isCrossRepository": True}),
    ("author-bot-stays-bracketed", _rest_pull(user={"login": "sigma[bot]", "type": "Bot"}), ["author"],
     {"author": {"login": "sigma[bot]"}}),
    ("author-missing", _drop(_rest_pull(), "user"), ["author"], {"author": {"login": ""}}),
    ("body-null", _rest_pull(body=None), ["body", "title"], {"body": "", "title": "T"}),
    ("dates", _rest_pull(merged_at="m", closed_at="c"), ["mergedAt", "closedAt", "number"],
     {"mergedAt": "m", "closedAt": "c", "number": 7}),
]


@pytest.mark.parametrize("pull,fields,expected", [r[1:] for r in PR_SHAPE_ROWS], ids=[r[0] for r in PR_SHAPE_ROWS])
def test_to_gh_pr_shape_table(pull, fields, expected):
    g = _mod("gh_api")
    assert g.to_gh_pr_shape(pull, fields) == expected


PR_SHAPE_ERRORS = [
    ("state-missing", _drop(_rest_pull(), "state"), ["state"]),
    ("state-weird", _rest_pull(state="weird"), ["state"]),
    ("auto-merge-absent", _drop(_rest_pull(), "auto_merge"), ["autoMergeRequest"]),
    ("base-repo-missing", _drop(_rest_pull(), "base", "repo"), ["isCrossRepository"]),
    ("head-missing", _drop(_rest_pull(), "head"), ["isCrossRepository"]),
    ("body-absent", _drop(_rest_pull(), "body"), ["body"]),
    ("number-not-int", _rest_pull(n="7"), ["number"]),
    ("number-bool", _rest_pull(n=True), ["number"]),
    ("non-dict", [1], ["number"]),
]


@pytest.mark.parametrize("pull,fields", [r[1:] for r in PR_SHAPE_ERRORS], ids=[r[0] for r in PR_SHAPE_ERRORS])
def test_to_gh_pr_shape_malformed_raises(pull, fields):
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError):
        g.to_gh_pr_shape(pull, fields)


@pytest.mark.parametrize("field", ["statusCheckRollup", "reviewDecision", "latestReviews", "files", "bogus"])
def test_pr_fields_whitelist_is_closed(field):
    g = _mod("gh_api")
    assert field not in g.PR_FIELDS
    with pytest.raises(ValueError):
        g.to_gh_pr_shape(_rest_pull(), [field])
    run = Fake("{}")
    with pytest.raises(ValueError):
        g.view_pr_gh(run, 7, ["state", field], env={})
    assert run.calls == []                                   # refused before any call


def test_pr_fields_is_exactly_what_the_migrated_sites_request():
    """4a-1's seven sites plus the merge gate (#895 4a-2): `mergeable`, `mergeStateStatus`. The rollup is
    NOT a PR field: only `pr_check_rollup_gh` serves it, keyed by an explicit head sha."""
    g = _mod("gh_api")
    assert g.PR_FIELDS == ("number", "title", "body", "state", "headRefOid", "headRefName", "mergedAt",
                           "closedAt", "autoMergeRequest", "isCrossRepository", "author", "comments",
                           "mergeable", "mergeStateStatus")


class PrFake:
    """Routes the PR REST argv: `pulls/<n>` (the pull), `issues/<n>/comments` pages, `pulls?head=` (list),
    and the `pr view` fallback."""

    def __init__(self, pull=None, comments=(), fail=None, gql=None, pages=None, rows=None, pull_raw=None,
                 runs=(), runs_total=None, runs_raw=None, statuses=(), statuses_total=None, status_raw=None):
        self.pull = pull if pull is not None else _rest_pull()
        self.comments, self.fail, self.gql = list(comments), fail, gql
        self.pages_raw, self.rows, self.pull_raw = pages or {}, rows, pull_raw
        # #895 4a-2: `commits/<sha>/check-runs` and `commits/<sha>/status` pages. `*_raw` maps a page
        # number to a raw body or an Exception (raised), so one page can fail after a good one.
        self.runs, self.runs_total, self.runs_raw = list(runs), runs_total, runs_raw or {}
        self.statuses, self.statuses_total, self.status_raw = list(statuses), statuses_total, status_raw or {}
        self.calls = []

    @staticmethod
    def _page(args):
        return int([a for a in args if a.startswith("page=")][0][5:])

    def _rollup_page(self, args, raw, rows, total, key):
        page = self._page(args)
        if page in raw:
            if isinstance(raw[page], Exception):
                raise raw[page]
            return raw[page]
        return json.dumps({"total_count": len(rows) if total is None else total,
                           key: rows[(page - 1) * 100:page * 100]})

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "pr":
            if isinstance(self.gql, Exception):
                raise self.gql
            return self.gql if self.gql is not None else '{"state": "OPEN"}'
        if self.fail is not None:
            raise self.fail
        if args[1].endswith("/check-runs"):
            return self._rollup_page(args, self.runs_raw, self.runs, self.runs_total, "check_runs")
        if args[1].endswith("/status"):
            return self._rollup_page(args, self.status_raw, self.statuses, self.statuses_total, "statuses")
        if args[1].endswith("/comments"):
            page = int([a for a in args if a.startswith("page=")][0][5:])
            if page in self.pages_raw:
                return self.pages_raw[page]
            return json.dumps(self.comments[(page - 1) * 100:page * 100])
        if "pulls?" in args[1]:
            return self.rows if isinstance(self.rows, str) else json.dumps(self.rows or [])
        return self.pull_raw if self.pull_raw is not None else json.dumps(self.pull)

    def gql_calls(self):
        return [c for c in self.calls if c[0] == "pr"]

    def rest_calls(self):
        return [c for c in self.calls if c[0] == "api"]

    def page_calls(self):
        return [c for c in self.calls if c[0] == "api" and c[1].endswith("/comments")]

    def run_calls(self):
        return [c for c in self.calls if c[0] == "api" and c[1].endswith("/check-runs")]

    def status_calls(self):
        return [c for c in self.calls if c[0] == "api" and c[1].endswith("/status")]


def _pr_comment(i, login=None, assoc="OWNER"):
    c = {"id": 2000 + i, "node_id": "IC_%d" % i, "user": {"login": login or "u%d" % i, "type": "User"},
         "body": "c%d" % i, "created_at": "2026-01-01T00:%02d:%02dZ" % (i // 60 % 60, i % 60)}
    if assoc is not None:
        c["author_association"] = assoc
    return c


def test_pr_comments_single_short_page():
    g = _mod("gh_api")
    run = PrFake(comments=[_pr_comment(i) for i in range(3)])
    rows = g.pr_comments(run, 7, "o/r")
    assert [r["body"] for r in rows] == ["c0", "c1", "c2"]
    assert run.calls == [["api", "repos/o/r/issues/7/comments", "--method", "GET", "-f", "per_page=100",
                          "-f", "page=1"]]


def test_pr_comments_full_page_then_short_page_is_two_calls_in_order():
    g = _mod("gh_api")
    run = PrFake(comments=[_pr_comment(i) for i in range(130)])
    rows = g.pr_comments(run, 7)
    assert len(rows) == 130 and [r["body"] for r in rows] == ["c%d" % i for i in range(130)]
    assert len(run.page_calls()) == 2 and run.calls[0][1] == "repos/{owner}/{repo}/issues/7/comments"


def test_pr_comments_cap_raises_kind_other_never_truncates():
    g = _mod("gh_api")
    run = PrFake(comments=[_pr_comment(i) for i in range(300)])
    with pytest.raises(g.GhApiError) as ei:
        g.pr_comments(run, 7, page_cap=3)
    assert ei.value.kind == "other" and "cap" in str(ei.value) and len(run.page_calls()) == 3
    assert g.PR_COMMENT_PAGE_CAP == 30


@pytest.mark.parametrize("raw", ['{"a": 1}', "null", '[1]', '["x"]'], ids=["dict", "null", "int-row", "str-row"])
def test_pr_comments_malformed_page_or_row_raises(raw):
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError):
        g.pr_comments(PrFake(pages={1: raw}), 7)


def test_pr_comment_without_association_has_no_authorassociation_key():
    g = _mod("gh_api")
    run = PrFake(comments=[_pr_comment(0, assoc=None), _pr_comment(1)])
    out = g.view_pr_gh(run, 7, ["comments", "author"], "o/r", env={})
    assert "authorAssociation" not in out["comments"][0]
    assert out["comments"][1]["authorAssociation"] == "OWNER"
    assert out["author"] == {"login": "alice"}
    assert [c[1] for c in run.rest_calls()] == ["repos/o/r/pulls/7", "repos/o/r/issues/7/comments"]


def test_view_pr_gh_rest_first_success_and_no_comment_read_unless_asked():
    g = _mod("gh_api")
    run = PrFake()
    assert g.view_pr_gh(run, "7", ["state", "autoMergeRequest"], env={}) == {"state": "OPEN", "autoMergeRequest": None}
    assert run.calls == [["api", "repos/{owner}/{repo}/pulls/7", "--method", "GET"]]


def test_view_pr_gh_non_numeric_number_is_value_error():
    g = _mod("gh_api")
    run = PrFake()
    with pytest.raises(ValueError):
        g.view_pr_gh(run, "seven", ["state"], env={})
    assert run.calls == []


@pytest.mark.parametrize("hint", [t[1] for t in TRANSIENT], ids=[t[0] for t in TRANSIENT])
def test_view_pr_gh_falls_back_once_on_transient(hint):
    g = _mod("gh_api")
    run = PrFake(fail=_err(hint), gql='{"state": "MERGED", "number": 7, "extra": 1}')
    assert g.view_pr_gh(run, 7, ["state", "number"], "o/r", env={}) == {"state": "MERGED", "number": 7}
    assert run.gql_calls() == [["pr", "view", "7", "--repo", "o/r", "--json", "state,number"]]


def test_view_pr_gh_fallback_argv_without_repo():
    g = _mod("gh_api")
    run = PrFake(fail=_err(RATE))
    g.view_pr_gh(run, 7, ["state"], env={})
    assert run.gql_calls() == [["pr", "view", "7", "--json", "state"]]


@pytest.mark.parametrize("hint", [c[1] for c in CLIENT] + [PROXY + " (HTTP 502)"],
                         ids=[c[0] for c in CLIENT] + ["proxy"])
def test_view_pr_gh_never_falls_back_on_client_errors_or_proxy(hint):
    g = _mod("gh_api")
    run = PrFake(fail=_err(hint))
    with pytest.raises(g.GhApiError):
        g.view_pr_gh(run, 7, ["state"], "o/r", env={})
    assert run.gql_calls() == []


@pytest.mark.parametrize("env", [{"CLAUDE_CODE_REMOTE": "true"}, {"SIGMA_GH_GRAPHQL": "off"}],
                         ids=["cloud", "override-off"])
def test_view_pr_gh_no_fallback_when_graphql_unavailable(env):
    g = _mod("gh_api")
    run = PrFake(fail=_err(RATE))
    with pytest.raises(g.GhApiError) as ei:
        g.view_pr_gh(run, 7, ["state"], "o/r", env=env)
    assert run.gql_calls() == [] and ei.value.kind == "rate_limit"


def test_view_pr_gh_exactly_one_fallback_and_its_failure_propagates():
    g = _mod("gh_api")
    run = PrFake(fail=_err("gh: Server Error (HTTP 502)"), gql=_err("gh: Server Error (HTTP 503)", text="gh pr view failed"))
    with pytest.raises(g.GhApiError) as ei:
        g.view_pr_gh(run, 7, ["state"], "o/r", env={})
    assert len(run.rest_calls()) == 1 and len(run.gql_calls()) == 1
    assert "502" in str(ei.value) and "503" in str(ei.value)


_GARBAGE_FALLBACK = ["", "   ", "<html>", "[1]", "null", "{}", '{"number": 7}']


@pytest.mark.parametrize("gql", _GARBAGE_FALLBACK,
                         ids=["empty", "blank", "non-json", "list", "null", "empty-object", "field-missing"])
def test_view_pr_gh_fallback_malformed_output_raises_never_degrades(gql):
    """#895 B1: a fallback that exits 0 with nothing usable is "could not read", never `{}` -- `{}` reads
    as "not a fork" (merge_rights would merge) and "no comments" (the review gate would skip a block).
    Kind is `other`, outside FALLBACK_KINDS, so nothing retries it."""
    g = _mod("gh_api")
    run = PrFake(fail=_err(RATE), gql=gql)
    with pytest.raises(g.GhApiError) as ei:
        g.view_pr_gh(run, 7, ["state", "number"], env={})
    assert ei.value.kind not in g.FALLBACK_KINDS and len(run.gql_calls()) == 1


def test_view_pr_gh_breaker_open_fallback_only_path_also_raises_on_garbage(tmp_path):
    """REST is skipped while the breaker is open, so the fallback is the ONLY read: still no `{}`."""
    g = _mod("gh_api")
    for i in range(3):
        g.view_pr_gh(PrFake(fail=_err("gh: Server Error (HTTP 502)")), 7, ["state"], "o/r", env={},
                     sdlc_dir=tmp_path, now=1000.0 + i)
    run = PrFake(gql="")
    with pytest.raises(g.GhApiError):
        g.view_pr_gh(run, 7, ["state"], "o/r", env={}, sdlc_dir=tmp_path, now=1003.0)
    assert run.rest_calls() == [] and len(run.gql_calls()) == 1


def test_pr_for_branch_gh_fallback_malformed_output_raises():
    g = _mod("gh_api")
    for gql in ("", "<html>", "[1]", '{"state": "MERGED"}'):
        with pytest.raises(g.GhApiError):
            g.pr_for_branch_gh(PrFake(fail=_err(RATE), gql=gql), "feat/x", ["state", "number"], "o/r", env={})


def test_view_pr_gh_classifies_on_the_hint_only():
    g = _mod("gh_api")
    # no hint, argv in str(exc) names a repo `timeout-svc`, real detail is a 404: no fallback
    exc = RuntimeError("gh api repos/acme/timeout-svc/pulls/7 --method GET failed: gh: Not Found (HTTP 404)")
    run = PrFake(fail=exc)
    with pytest.raises(g.GhApiError) as ei:
        g.view_pr_gh(run, 7, ["state"], env={})
    assert ei.value.kind == "not_found" and run.gql_calls() == []
    run = PrFake(fail=_err("gh: Validation Failed (HTTP 422)",
                           text="gh api repos/acme/timeout-svc/pulls/7 failed: timeout"))
    with pytest.raises(g.GhApiError):
        g.view_pr_gh(run, 7, ["state"], env={})
    assert run.gql_calls() == []


def test_view_pr_gh_malformed_pull_is_kind_other_no_fallback():
    g = _mod("gh_api")
    for raw in ("[1]", "null", "<html>"):
        run = PrFake(pull_raw=raw)
        with pytest.raises(g.GhApiError) as ei:
            g.view_pr_gh(run, 7, ["state"], env={})
        assert ei.value.kind == "other" and run.gql_calls() == []


def test_pr_and_issue_reads_share_one_breaker(tmp_path):
    g = _mod("gh_api")
    for i in range(3):
        g.view_pr_gh(PrFake(fail=_err("gh: Server Error (HTTP 502)")), 7, ["state"], "o/r", env={},
                     sdlc_dir=tmp_path, now=1000.0 + i)
    b = _breaker(tmp_path)
    assert b["consecutive"] == 3 and b["last_kind"] == "server"
    log = json.loads((tmp_path / "state" / "gh-fallback.json").read_text())
    assert {e["op"] for e in log} == {"pr_read"}
    issue_run = RestFake()
    g.read_issue(issue_run, 7, ["state"], "o/r", env={}, sdlc_dir=tmp_path, now=1003.0)
    assert issue_run.rest_calls() == [] and len(issue_run.gql_calls()) == 1      # the PR failures opened it


def test_view_pr_gh_no_sdlc_dir_never_writes(tmp_path, monkeypatch):
    g = _mod("gh_api")
    monkeypatch.chdir(tmp_path)
    for _ in range(4):
        g.view_pr_gh(PrFake(fail=_err(RATE)), 7, ["state"], "o/r", env={}, now=1.0)
    assert list(tmp_path.rglob("*")) == []


def _row(n, state="closed", merged_at=None, created="2026-01-01T00:00:00Z", sha=None):
    return {"number": n, "state": state, "merged_at": merged_at, "closed_at": "c%d" % n, "created_at": created,
            "title": "t", "body": "b", "user": {"login": "alice"},
            "head": {"sha": sha or ("%d" % n) * 40, "ref": "feat/x", "repo": {"full_name": "o/r"}},
            "base": {"ref": "main", "repo": {"full_name": "o/r"}}}


BRANCH_FIELDS = ["state", "mergedAt", "closedAt", "headRefOid", "headRefName", "number"]


def test_pr_for_branch_gh_argv_and_requires_a_slash_repo():
    g = _mod("gh_api")
    run = PrFake(rows=[_row(5, merged_at="m5")])
    g.pr_for_branch_gh(run, "feat/x y", BRANCH_FIELDS, "acme/r", env={})
    assert run.calls == [["api", "repos/acme/r/pulls?head=acme:feat/x%20y&state=all&sort=created"
                                 "&direction=desc&per_page=30", "--method", "GET"]]
    for bad in (None, "", "noslash"):
        with pytest.raises(ValueError):
            g.pr_for_branch_gh(PrFake(), "b", BRANCH_FIELDS, bad, env={})


def test_pr_for_branch_gh_open_row_beats_a_newer_merged_row():
    g = _mod("gh_api")
    run = PrFake(rows=[_row(9, merged_at="m9"), _row(4, state="open")])
    assert g.pr_for_branch_gh(run, "feat/x", BRANCH_FIELDS, "o/r", env={})["number"] == 4


def test_pr_for_branch_gh_no_open_row_takes_the_newest_and_shapes_it():
    g = _mod("gh_api")
    run = PrFake(rows=[_row(9, merged_at="m9"), _row(4)])
    assert g.pr_for_branch_gh(run, "feat/x", BRANCH_FIELDS, "o/r", env={}) == {
        "state": "MERGED", "mergedAt": "m9", "closedAt": "c9", "headRefOid": "9" * 40,
        "headRefName": "feat/x", "number": 9}
    run = PrFake(rows=[_row(4)])
    assert g.pr_for_branch_gh(run, "feat/x", ["state"], "o/r", env={}) == {"state": "CLOSED"}


def test_pr_for_branch_gh_empty_list_is_none():
    g = _mod("gh_api")
    assert g.pr_for_branch_gh(PrFake(rows=[]), "feat/x", BRANCH_FIELDS, "o/r", env={}) is None


@pytest.mark.parametrize("raw", ["", "null", '{"message": "x"}', "[1]", "<html>"],
                         ids=["empty", "null", "dict", "non-dict-row", "garbage"])
def test_pr_for_branch_gh_non_list_body_raises(raw):
    g = _mod("gh_api")
    run = PrFake(rows=raw)
    with pytest.raises(g.GhApiError) as ei:
        g.pr_for_branch_gh(run, "feat/x", BRANCH_FIELDS, "o/r", env={})
    assert ei.value.kind == "other" and run.gql_calls() == []


@pytest.mark.parametrize("field", ["isCrossRepository", "autoMergeRequest", "comments"])
def test_pr_for_branch_gh_refuses_fields_the_list_cannot_answer(field):
    g = _mod("gh_api")
    run = PrFake()
    with pytest.raises(ValueError):
        g.pr_for_branch_gh(run, "feat/x", ["state", field], "o/r", env={})
    assert run.calls == []


def test_pr_for_branch_gh_fallback_argv_and_rules():
    g = _mod("gh_api")
    run = PrFake(fail=_err(RATE), gql='{"state": "MERGED", "number": 3}')
    assert g.pr_for_branch_gh(run, "feat/x", ["state", "number"], "o/r", env={}) == {"state": "MERGED", "number": 3}
    assert run.gql_calls() == [["pr", "view", "feat/x", "--repo", "o/r", "--json", "state,number"]]
    run = PrFake(fail=_err("gh: Not Found (HTTP 404)"))
    with pytest.raises(g.GhApiError):
        g.pr_for_branch_gh(run, "feat/x", ["state"], "o/r", env={})
    assert run.gql_calls() == []
    run = PrFake(fail=_err(RATE))
    with pytest.raises(g.GhApiError):
        g.pr_for_branch_gh(run, "feat/x", ["state"], "o/r", env={"CLAUDE_CODE_REMOTE": "1"})
    assert run.gql_calls() == []


def test_pr_fallback_argv_is_built_only_in_gh_api():
    assert '"pr", "view"' in (S / "gh_api.py").read_text()


def test_cloud_sessions_doc_names_every_pr_field():
    g = _mod("gh_api")
    doc = (S.parent.parent.parent / "docs" / "cloud-sessions.md").read_text(encoding="utf-8")
    start = doc.index("## PR reads (#895 slice 4a-1)")
    end = doc.find("\n## ", start + 1)
    section = doc[start:end if end != -1 else len(doc)]
    missing = [f for f in g.PR_FIELDS if "`%s`" % f not in section]
    assert not missing, "docs/cloud-sessions.md PR-reads section does not name: %r" % missing


# ---------------------------------------------------------------- merge-gate reads, REST first (#895 slice 4a-2)

SHA = "a" * 40
_STATES = ["clean", "dirty", "unstable", "blocked", "behind", "draft", "has_hooks", "unknown"]


def _gate_pull(mergeable=True, state="clean", **kw):
    """A REST pull carrying the two mergeability keys; `ABSENT_KEY` drops one."""
    d = _rest_pull(**kw)
    for k, v in (("mergeable", mergeable), ("mergeable_state", state)):
        if v is not ABSENT_KEY:
            d[k] = v
    return d


ABSENT_KEY = object()

MERGE_ROWS = [
    ("true-clean", _gate_pull(True, "clean"), {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}),
    ("false-dirty", _gate_pull(False, "dirty"), {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}),
    ("null-unknown", _gate_pull(None, "unknown"), {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}),
    # GitHub still computing: the gate's UNKNOWN retry must run, so a stale `true` never wins
    ("true-but-state-unknown", _gate_pull(True, "unknown"), {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}),
    # both fields agree: a null mergeable never sits beside a CLEAN state
    ("null-but-state-clean", _gate_pull(None, "clean"), {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}),
] + [("state-%s" % s, _gate_pull(True, s),
      {"mergeable": "UNKNOWN" if s == "unknown" else "MERGEABLE", "mergeStateStatus": s.upper()})
     for s in _STATES]


@pytest.mark.parametrize("pull,expected", [r[1:] for r in MERGE_ROWS], ids=[r[0] for r in MERGE_ROWS])
def test_to_gh_pr_shape_mergeability_table(pull, expected):
    g = _mod("gh_api")
    assert g.to_gh_pr_shape(pull, ["mergeable", "mergeStateStatus"]) == expected
    assert g.to_gh_pr_shape(pull, ["mergeable"]) == {"mergeable": expected["mergeable"]}
    assert g.to_gh_pr_shape(pull, ["mergeStateStatus"]) == {"mergeStateStatus": expected["mergeStateStatus"]}


MERGE_ERRORS = [
    ("mergeable-absent", _gate_pull(ABSENT_KEY, "clean")),
    ("mergeable-string-true", _gate_pull("true", "clean")),
    ("mergeable-int", _gate_pull(1, "clean")),
    ("state-absent", _gate_pull(True, ABSENT_KEY)),
    ("state-null", _gate_pull(True, None)),
    ("state-int", _gate_pull(True, 3)),
    ("state-upper-CLEAN", _gate_pull(True, "CLEAN")),
    ("state-ok", _gate_pull(True, "ok")),
    ("state-queued", _gate_pull(True, "queued")),
    ("state-upper-BEHIND", _gate_pull(True, "BEHIND")),
    ("state-absent-mergeable-null", _gate_pull(None, ABSENT_KEY)),
]


@pytest.mark.parametrize("pull", [r[1] for r in MERGE_ERRORS], ids=[r[0] for r in MERGE_ERRORS])
@pytest.mark.parametrize("fields", [["mergeable", "mergeStateStatus"], ["mergeable"], ["mergeStateStatus"]],
                         ids=["both", "mergeable-only", "state-only"])
def test_to_gh_pr_shape_malformed_mergeability_raises_kind_other(pull, fields):
    """Fail closed: an absent or unrecognised value is an error, never a default. Either field validates
    BOTH keys (the state can force mergeable UNKNOWN, a null mergeable forces the state UNKNOWN)."""
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError) as ei:
        g.to_gh_pr_shape(pull, fields)
    assert ei.value.kind == "other"


@pytest.mark.parametrize("pull", [r[1] for r in MERGE_ERRORS], ids=[r[0] for r in MERGE_ERRORS])
def test_malformed_mergeability_never_reads_as_clean_or_behind(pull):
    """The property behind the table: BEHIND triggers a force-push rebase and CLEAN a merge, so no invalid
    input may produce either, whatever the converter does with it."""
    g = _mod("gh_api")
    try:
        out = g.to_gh_pr_shape(pull, ["mergeable", "mergeStateStatus"])
    except g.GhApiError:
        return
    assert out.get("mergeStateStatus") not in ("CLEAN", "BEHIND") and out.get("mergeable") != "MERGEABLE"


def test_view_pr_gh_serves_the_gate_fields_from_one_pull_get():
    g = _mod("gh_api")
    run = PrFake(pull=_gate_pull(True, "blocked"))
    assert g.view_pr_gh(run, 7, ["mergeable", "mergeStateStatus", "headRefOid"], env={}) == {
        "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED", "headRefOid": SHA}
    assert run.calls == [["api", "repos/{owner}/{repo}/pulls/7", "--method", "GET"]]


def test_view_pr_gh_still_refuses_status_check_rollup_before_any_call():
    g = _mod("gh_api")
    run = PrFake()
    with pytest.raises(ValueError):
        g.view_pr_gh(run, 7, ["mergeable", "statusCheckRollup"], env={})
    assert run.calls == []


def _run(name, status="completed", conclusion="success", url=None, **kw):
    d = {"id": 1, "name": name, "status": status, "conclusion": conclusion,
         "details_url": url or "https://github.com/o/r/actions/runs/42/job/9", "html_url": "h"}
    d.update(kw)
    return d


def _status(context, state="success", url="https://ci.example/1"):
    return {"id": 1, "context": context, "state": state, "target_url": url, "description": "d"}


def test_pr_check_rollup_gh_shape_order_and_urls():
    g = _mod("gh_api")
    run = PrFake(runs=[_run("lint"), _run("slow", status="in_progress", conclusion=None),
                       _run("tests", conclusion="failure")],
                 statuses=[_status("ci/legacy", "failure")])
    out = g.pr_check_rollup_gh(run, 7, SHA, "o/r", env={})
    assert out == [
        {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS",
         "detailsUrl": "https://github.com/o/r/actions/runs/42/job/9"},
        {"__typename": "CheckRun", "name": "slow", "status": "IN_PROGRESS", "conclusion": "",
         "detailsUrl": "https://github.com/o/r/actions/runs/42/job/9"},
        {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "FAILURE",
         "detailsUrl": "https://github.com/o/r/actions/runs/42/job/9"},
        {"__typename": "StatusContext", "context": "ci/legacy", "state": "FAILURE",
         "targetUrl": "https://ci.example/1"},
    ]
    assert run.calls == [
        ["api", "repos/o/r/commits/%s/check-runs" % SHA, "--method", "GET", "-f", "per_page=100", "-f", "page=1"],
        ["api", "repos/o/r/commits/%s/status" % SHA, "--method", "GET", "-f", "per_page=100", "-f", "page=1"]]


def test_pr_check_rollup_gh_queries_exactly_the_sha_it_was_given():
    g = _mod("gh_api")
    run = PrFake()
    other = "b" * 40
    assert g.pr_check_rollup_gh(run, 7, other, env={}) == []
    assert [c[1] for c in run.rest_calls()] == ["repos/{owner}/{repo}/commits/%s/check-runs" % other,
                                                "repos/{owner}/{repo}/commits/%s/status" % other]


ROLLUP_ROW_ERRORS = [
    ("completed-null-conclusion", _run("x", conclusion=None)),
    ("completed-empty-conclusion", _run("x", conclusion="")),
    ("conclusion-not-str", _run("x", conclusion=1)),
    ("in-progress-conclusion-not-str", _run("x", status="in_progress", conclusion=["failure"])),
    ("name-missing", {k: v for k, v in _run("x").items() if k != "name"}),
    ("status-not-str", _run("x", status=None)),
    ("row-not-object", "x"),
]


@pytest.mark.parametrize("row", [r[1] for r in ROLLUP_ROW_ERRORS], ids=[r[0] for r in ROLLUP_ROW_ERRORS])
def test_pr_check_rollup_gh_malformed_check_run_raises(row):
    """completed + null conclusion would read as ok in `work._check_verdict` (empty conclusion + COMPLETED),
    turning "unknown" into a pass: it RAISES instead (kind other, no fallback)."""
    g = _mod("gh_api")
    run = PrFake(runs=[row])
    with pytest.raises(g.GhApiError) as ei:
        g.pr_check_rollup_gh(run, 7, SHA, env={})
    assert ei.value.kind == "other" and run.gql_calls() == []


def test_pr_check_rollup_gh_in_progress_empty_conclusion_reads_as_null():
    g = _mod("gh_api")
    run = PrFake(runs=[_run("x", status="queued", conclusion="")])
    assert g.pr_check_rollup_gh(run, 7, SHA, env={})[0]["conclusion"] == ""


@pytest.mark.parametrize("row", ["x", {"state": "success"}, {"context": "c", "state": None},
                                 {"context": 3, "state": "success"}],
                         ids=["not-object", "context-missing", "state-null", "context-int"])
def test_pr_check_rollup_gh_malformed_status_raises(row):
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError):
        g.pr_check_rollup_gh(PrFake(statuses=[row]), 7, SHA, env={})


def test_pr_check_rollup_gh_pages_by_total_count():
    g = _mod("gh_api")
    run = PrFake(runs=[_run("c%d" % i) for i in range(103)])
    out = g.pr_check_rollup_gh(run, 7, SHA, env={})
    assert len(out) == 103 and len(run.run_calls()) == 2
    assert [c[-1] for c in run.run_calls()] == ["page=1", "page=2"]


def test_pr_check_rollup_gh_exact_multiple_of_100_is_two_pages_not_three():
    g = _mod("gh_api")
    run = PrFake(runs=[_run("c%d" % i) for i in range(200)])
    assert len(g.pr_check_rollup_gh(run, 7, SHA, env={})) == 200 and len(run.run_calls()) == 2


@pytest.mark.parametrize("kw", [dict(runs=[_run("a")], runs_total=2),
                                dict(statuses=[_status("a")], statuses_total=5)],
                         ids=["check-runs", "statuses"])
def test_pr_check_rollup_gh_short_page_below_total_count_raises(kw):
    """A missing failing check must never read as green: a short page while fetched < total_count raises."""
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError) as ei:
        g.pr_check_rollup_gh(PrFake(**kw), 7, SHA, env={})
    assert ei.value.kind == "other"


def test_pr_check_rollup_gh_cap_reached_below_total_count_raises():
    g = _mod("gh_api")
    run = PrFake(runs=[_run("c%d" % i) for i in range(1000)], runs_total=1001)
    with pytest.raises(g.GhApiError) as ei:
        g.pr_check_rollup_gh(run, 7, SHA, env={})
    assert "cap" in str(ei.value) and len(run.run_calls()) == g.CHECK_RUN_PAGE_CAP == 10
    run = PrFake(statuses=[_status("s%d" % i) for i in range(500)], statuses_total=501)
    with pytest.raises(g.GhApiError):
        g.pr_check_rollup_gh(run, 7, SHA, env={})
    assert len(run.status_calls()) == g.STATUS_PAGE_CAP == 5


@pytest.mark.parametrize("raw", ['{"total_count": 1, "check_runs": {"a": 1}}', '{"check_runs": []}',
                                 '{"total_count": true, "check_runs": []}', '{"total_count": "1", "check_runs": []}',
                                 "[]", "null", "<html>"],
                         ids=["runs-not-list", "total-missing", "total-bool", "total-str", "list", "null", "garbage"])
def test_pr_check_rollup_gh_malformed_check_runs_page_raises(raw):
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError) as ei:
        g.pr_check_rollup_gh(PrFake(runs_raw={1: raw}), 7, SHA, env={})
    assert ei.value.kind == "other"


@pytest.mark.parametrize("raw", ['{"total_count": 0, "state": "pending"}',
                                 '{"total_count": 0, "state": "success", "statuses": null}',
                                 '{"state": "success", "statuses": []}'],
                         ids=["statuses-absent", "statuses-null", "total-missing"])
def test_pr_check_rollup_gh_malformed_status_page_raises(raw):
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError):
        g.pr_check_rollup_gh(PrFake(status_raw={1: raw}), 7, SHA, env={})


def test_pr_check_rollup_gh_ignores_the_combined_state():
    """An empty status set reports combined `pending` (MEASURED): never read; no rows -> []."""
    g = _mod("gh_api")
    run = PrFake(status_raw={1: '{"total_count": 0, "state": "pending", "statuses": []}'})
    assert g.pr_check_rollup_gh(run, 7, SHA, env={}) == []
    run = PrFake(status_raw={1: '{"total_count": 0, "state": "failure", "statuses": []}'})
    assert g.pr_check_rollup_gh(run, 7, SHA, env={}) == []


@pytest.mark.parametrize("sha", ["", "a" * 39, "A" * 40, "g" * 40, "sdlc/7", "a" * 64, None],
                         ids=["empty", "39", "upper", "non-hex", "branch", "sha256", "none"])
def test_pr_check_rollup_gh_refuses_a_non_sha_before_any_call(sha):
    g = _mod("gh_api")
    run = PrFake()
    with pytest.raises(ValueError):
        g.pr_check_rollup_gh(run, 7, sha, env={})
    assert run.calls == []


_FB_ROLLUP = json.dumps({"statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}], "headRefOid": SHA})


@pytest.mark.parametrize("hint", [t[1] for t in TRANSIENT], ids=[t[0] for t in TRANSIENT])
def test_pr_check_rollup_gh_falls_back_once_on_transient(hint):
    g = _mod("gh_api")
    run = PrFake(fail=_err(hint), gql=_FB_ROLLUP)
    assert g.pr_check_rollup_gh(run, 7, SHA, "o/r", env={}) == [{"name": "ci", "conclusion": "SUCCESS"}]
    assert run.gql_calls() == [["pr", "view", "7", "--repo", "o/r", "--json", "statusCheckRollup,headRefOid"]]


@pytest.mark.parametrize("hint", [c[1] for c in CLIENT] + [PROXY + " (HTTP 502)"],
                         ids=[c[0] for c in CLIENT] + ["proxy"])
def test_pr_check_rollup_gh_never_falls_back_on_client_errors_or_proxy(hint):
    g = _mod("gh_api")
    run = PrFake(runs_raw={1: _err(hint)}, gql=_FB_ROLLUP)
    with pytest.raises(g.GhApiError):
        g.pr_check_rollup_gh(run, 7, SHA, "o/r", env={})
    assert run.gql_calls() == []


@pytest.mark.parametrize("env", [{"CLAUDE_CODE_REMOTE": "true"}, {"SIGMA_GH_GRAPHQL": "off"}],
                         ids=["cloud", "override-off"])
def test_pr_check_rollup_gh_no_fallback_when_graphql_unavailable(env):
    g = _mod("gh_api")
    run = PrFake(fail=_err(RATE), gql=_FB_ROLLUP)
    with pytest.raises(g.GhApiError) as ei:
        g.pr_check_rollup_gh(run, 7, SHA, "o/r", env=env)
    assert run.gql_calls() == [] and ei.value.kind == "rate_limit"


def test_pr_check_rollup_gh_exactly_one_fallback_and_its_failure_propagates():
    g = _mod("gh_api")
    run = PrFake(fail=_err("gh: Server Error (HTTP 502)"), gql=_err("gh: Server Error (HTTP 503)", text="x"))
    with pytest.raises(g.GhApiError):
        g.pr_check_rollup_gh(run, 7, SHA, env={})
    assert len(run.gql_calls()) == 1


def test_pr_check_rollup_gh_second_page_429_is_one_fallback_never_half_rest():
    g = _mod("gh_api")
    run = PrFake(runs=[_run("c%d" % i) for i in range(150)], runs_raw={2: _err(RATE)}, gql=_FB_ROLLUP)
    assert g.pr_check_rollup_gh(run, 7, SHA, env={}) == [{"name": "ci", "conclusion": "SUCCESS"}]
    assert len(run.run_calls()) == 2 and len(run.gql_calls()) == 1 and run.status_calls() == []


@pytest.mark.parametrize("gql", [json.dumps({"statusCheckRollup": [], "headRefOid": "b" * 40}),
                                 json.dumps({"headRefOid": SHA}),
                                 json.dumps({"statusCheckRollup": None, "headRefOid": SHA}),
                                 json.dumps({"statusCheckRollup": {}, "headRefOid": SHA})],
                         ids=["other-head", "rollup-missing", "rollup-null", "rollup-not-list"])
def test_pr_check_rollup_gh_fallback_for_another_head_or_without_a_list_raises(gql):
    """A rollup for a different head is never attached: the gate judges `sha`, nothing else."""
    g = _mod("gh_api")
    with pytest.raises(g.GhApiError) as ei:
        g.pr_check_rollup_gh(PrFake(fail=_err(RATE), gql=gql), 7, SHA, env={})
    assert ei.value.kind not in g.FALLBACK_KINDS


def _list_row(n=7, **kw):
    d = {"number": n, "state": "open", "mergeable": None, "head": {"sha": SHA, "ref": "feature/x"}}
    d.update(kw)
    return d


OPEN_FIELDS = ["number", "mergeable", "headRefOid"]


def test_open_pr_for_branch_gh_lists_then_reads_the_pull():
    """List rows carry `mergeable: null` and no `mergeable_state` (MEASURED), so the per-PR GET answers."""
    g = _mod("gh_api")
    run = PrFake(rows=[_list_row()], pull=_gate_pull(False, "dirty"))
    assert g.open_pr_for_branch_gh(run, "feature/x", OPEN_FIELDS, "acme/app", env={}) == {
        "number": 7, "mergeable": "CONFLICTING", "headRefOid": SHA}
    assert run.calls == [["api", "repos/acme/app/pulls?head=acme:feature/x&state=open&per_page=1", "--method", "GET"],
                         ["api", "repos/acme/app/pulls/7", "--method", "GET"]]


def test_open_pr_for_branch_gh_empty_list_is_none_in_one_call():
    g = _mod("gh_api")
    run = PrFake(rows=[])
    assert g.open_pr_for_branch_gh(run, "feature/x", OPEN_FIELDS, "acme/app", env={}) is None
    assert len(run.calls) == 1


@pytest.mark.parametrize("raw", ["", "null", '{"message": "x"}', "[1]", "<html>"],
                         ids=["empty", "null", "dict", "non-dict-row", "garbage"])
def test_open_pr_for_branch_gh_non_list_body_raises(raw):
    g = _mod("gh_api")
    run = PrFake(rows=raw)
    with pytest.raises(g.GhApiError) as ei:
        g.open_pr_for_branch_gh(run, "feature/x", OPEN_FIELDS, "acme/app", env={})
    assert ei.value.kind == "other" and run.gql_calls() == []


@pytest.mark.parametrize("number", ["7", True, None, 7.0], ids=["str", "bool", "none", "float"])
def test_open_pr_for_branch_gh_list_row_number_must_be_an_int(number):
    g = _mod("gh_api")
    run = PrFake(rows=[_list_row(n=number)])
    with pytest.raises(g.GhApiError) as ei:
        g.open_pr_for_branch_gh(run, "feature/x", OPEN_FIELDS, "acme/app", env={})
    assert ei.value.kind == "other" and run.gql_calls() == []
    assert not any(c[1].startswith("repos/acme/app/pulls/") for c in run.rest_calls())


def test_open_pr_for_branch_gh_argument_refusals_before_any_call():
    g = _mod("gh_api")
    for repo in (None, "", "noslash"):
        run = PrFake()
        with pytest.raises(ValueError):
            g.open_pr_for_branch_gh(run, "feature/x", OPEN_FIELDS, repo, env={})
        assert run.calls == []
    for fields in (["number", "comments"], ["number", "statusCheckRollup"]):
        run = PrFake()
        with pytest.raises(ValueError):
            g.open_pr_for_branch_gh(run, "feature/x", fields, "acme/app", env={})
        assert run.calls == []


def test_open_pr_for_branch_gh_fallback_argv_and_rules():
    g = _mod("gh_api")
    gql = json.dumps([{"number": 3, "mergeable": "CONFLICTING", "headRefOid": SHA}])
    run = PrFake(fail=_err(RATE), gql=gql)
    assert g.open_pr_for_branch_gh(run, "feature/x", OPEN_FIELDS, "acme/app", env={}) == {
        "number": 3, "mergeable": "CONFLICTING", "headRefOid": SHA}
    assert run.gql_calls() == [["pr", "list", "--repo", "acme/app", "--head", "feature/x", "--state", "open",
                                "--limit", "1", "--json", "number,mergeable,headRefOid"]]
    assert g.open_pr_for_branch_gh(PrFake(fail=_err(RATE), gql="[]"), "feature/x", OPEN_FIELDS, "acme/app",
                                   env={}) is None
    for bad in (json.dumps([{"number": 3, "headRefOid": SHA}]), "", "<html>", "{}", "[1]"):
        with pytest.raises(g.GhApiError):
            g.open_pr_for_branch_gh(PrFake(fail=_err(RATE), gql=bad), "feature/x", OPEN_FIELDS, "acme/app", env={})
    run = PrFake(fail=_err("gh: Not Found (HTTP 404)"), gql=gql)
    with pytest.raises(g.GhApiError):
        g.open_pr_for_branch_gh(run, "feature/x", OPEN_FIELDS, "acme/app", env={})
    assert run.gql_calls() == []
    run = PrFake(fail=_err(RATE), gql=gql)
    with pytest.raises(g.GhApiError):
        g.open_pr_for_branch_gh(run, "feature/x", OPEN_FIELDS, "acme/app", env={"CLAUDE_CODE_REMOTE": "true"})
    assert run.gql_calls() == []


def test_pr_list_fallback_argv_is_built_only_in_gh_api():
    assert '["pr", "list", "--repo", repo,' in (S / "gh_api.py").read_text()


def test_cloud_sessions_doc_has_a_row_for_every_4a2_field():
    """The 4a-1 test above reads only the 4a-1 section, whose prose already names `mergeable` and
    `mergeStateStatus`, so it cannot go red for them. This one requires a TABLE ROW per new field in the
    4a-2 section."""
    doc = (S.parent.parent.parent / "docs" / "cloud-sessions.md").read_text(encoding="utf-8")
    start = doc.index("## Merge-gate and landing-PR reads (#895 slice 4a-2, PR A)")
    end = doc.find("\n## ", start + 1)
    section = doc[start:end if end != -1 else len(doc)]
    rows = [ln for ln in section.splitlines() if ln.startswith("| `")]
    for prefix in ("| `mergeable` |", "| `mergeStateStatus` |", "| `statusCheckRollup` CheckRun |",
                   "| `statusCheckRollup` StatusContext |"):
        assert any(r.startswith(prefix) for r in rows), "4a-2 section has no table row %r" % prefix


# ================================================================ #895 4a-2 PR B: reviews and the design-PR list

import prfake                                              # noqa: E402 - the shared REST body builders

_GM = []


def _gm():
    """ONE gh_api module for this section: `_prs`/`_cr` and the test body must share GhApiError's class."""
    if not _GM:
        _GM.append(_mod("gh_api"))
    return _GM[0]


class RvFake:
    """Routes `pulls/<n>/reviews` pages (`pages`: page number -> raw body or Exception; else `rows`
    sliced 100 at a time), the failure to raise on every REST call (`fail`), and the `pr view` fallback."""

    def __init__(self, rows=(), pages=None, fail=None, gql=None):
        self.rows, self.pages, self.fail, self.gql, self.calls = list(rows), pages or {}, fail, gql, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "pr":
            if isinstance(self.gql, Exception):
                raise self.gql
            return self.gql if self.gql is not None else "{}"
        if self.fail is not None:
            raise self.fail
        page = int([a for a in args if a.startswith("page=")][0][5:])
        if page in self.pages:
            if isinstance(self.pages[page], Exception):
                raise self.pages[page]
            return self.pages[page]
        return json.dumps(self.rows[(page - 1) * 100:page * 100])

    def rest_calls(self):
        return [c for c in self.calls if c[0] == "api"]

    def gql_calls(self):
        return [c for c in self.calls if c[0] == "pr"]


def _rv(rows):
    """`(login, STATE)` tuples -> REST review rows (ids and submitted_at ascend)."""
    return json.loads(prfake.rest_reviews(rows))


def _cr(*rows, **kw):
    g = _gm()
    return g.pr_changes_requested(RvFake(rows=_rv(rows)), 7, "o/r", env=kw.pop("env", {}), **kw)


@pytest.mark.parametrize("history,expected", [
    ([("bo", "CHANGES_REQUESTED")], ["bo"]),
    ([("bo", "CHANGES_REQUESTED"), ("bo", "COMMENTED")], ["bo"]),            # C1: a later comment never clears
    ([("bo", "CHANGES_REQUESTED"), ("bo", "APPROVED")], []),
    ([("bo", "APPROVED"), ("bo", "CHANGES_REQUESTED")], ["bo"]),
    ([("bo", "DISMISSED")], []),                                              # a lone dismissed CR reads DISMISSED
    ([("bo", "CHANGES_REQUESTED"), ("bo", "CHANGES_REQUESTED"), ("bo", "DISMISSED")], ["bo"]),   # earlier CR stands
    ([("bo", "PENDING")], []),
    ([("bo", "CHANGES_REQUESTED"), ("bo", "PENDING")], ["bo"]),
    ([("Bo", "CHANGES_REQUESTED"), ("bo", "APPROVED")], []),                  # the key is the lower-cased login
    ([("bo", "CHANGES_REQUESTED"), ("al", "APPROVED"), ("cy", "CHANGES_REQUESTED")], ["bo", "cy"]),
    ([("x[bot]", "CHANGES_REQUESTED")], ["x[bot]"]),                          # bot logins are raw REST
    ([], []),
], ids=["cr", "cr-comment", "cr-approve", "approve-cr", "lone-dismissed", "cr-cr-dismissed", "pending",
        "cr-pending", "case", "two", "bot", "none"])
def test_pr_changes_requested_reducer_semantics(history, expected):
    assert _cr(*history) == expected


def test_pr_changes_requested_one_ghost_can_never_clear_anothers_request():
    """C6: a null `user` (deleted account) is keyed by the review's own id, never by None."""
    out = _cr((None, "CHANGES_REQUESTED"), (None, "APPROVED"))
    assert out == ["(deleted user)"]
    assert _cr((None, "CHANGES_REQUESTED"), (None, "CHANGES_REQUESTED")) == ["(deleted user)"] * 2
    g = _gm()
    rows = _rv([("x", "CHANGES_REQUESTED"), ("x", "APPROVED")])
    rows[0]["user"] = {"login": ""}; rows[1]["user"] = {}
    assert g.pr_changes_requested(RvFake(rows=rows), 7, "o/r", env={}) == ["(unknown user)"]
    rows[0]["user"] = {"login": 5}; rows[1]["user"] = "x"
    assert g.pr_changes_requested(RvFake(rows=rows), 7, "o/r", env={}) == ["(unknown user)"]


def test_pr_changes_requested_orders_by_submitted_at_then_id_not_by_page_order():
    g = _gm()
    rows = _rv([("bo", "CHANGES_REQUESTED"), ("bo", "APPROVED")])
    assert g.pr_changes_requested(RvFake(rows=list(reversed(rows))), 7, "o/r", env={}) == []
    same_second = _rv([("bo", "CHANGES_REQUESTED"), ("bo", "APPROVED")])
    for r in same_second:
        r["submitted_at"] = "2026-01-01T00:00:00Z"
    assert g.pr_changes_requested(RvFake(rows=list(reversed(same_second))), 7, "o/r", env={}) == []


@pytest.mark.parametrize("deviation", [{"submitted_at": None}, {"submitted_at": ""}, {"submitted_at": 5},
                                       {"id": None}, {"id": True}, {"id": "9"}, {"id": 1.5}],
                         ids=["at-null", "at-empty", "at-int", "id-null", "id-bool", "id-str", "id-float"])
def test_pr_changes_requested_a_decisive_row_without_order_keys_raises(deviation):
    g = _gm()
    rows = _rv([("bo", "CHANGES_REQUESTED")])
    rows[0].update(deviation)
    with pytest.raises(g.GhApiError) as ei:
        g.pr_changes_requested(RvFake(rows=rows), 7, "o/r", env={})
    assert ei.value.kind == "other"


def test_pr_changes_requested_an_ignored_state_needs_no_order_keys():
    g = _gm()
    rows = _rv([("bo", "PENDING"), ("bo", "COMMENTED")])
    for r in rows:
        r["submitted_at"] = None
    assert g.pr_changes_requested(RvFake(rows=rows), 7, "o/r", env={}) == []


@pytest.mark.parametrize("state", ["APPROVE", "approved", "Changes_Requested", "", None, 3, ["APPROVED"], "STALE"])
def test_pr_changes_requested_an_unrecognised_state_raises(state):
    """C3: a new GitHub state must fail loudly, never be read as 'not a request for changes'."""
    g = _gm()
    rows = _rv([("bo", "APPROVED")])
    rows[0]["state"] = state
    with pytest.raises(g.GhApiError) as ei:
        g.pr_changes_requested(RvFake(rows=rows), 7, "o/r", env={})
    assert ei.value.kind == "other"


def test_pr_changes_requested_argv_and_paging():
    g = _gm()
    run = RvFake(rows=_rv([("bo", "COMMENTED")] * 150))
    assert g.pr_changes_requested(run, 7, "o/r", env={}) == []
    assert run.rest_calls() == [
        ["api", "repos/o/r/pulls/7/reviews", "--method", "GET", "-f", "per_page=100", "-f", "page=1"],
        ["api", "repos/o/r/pulls/7/reviews", "--method", "GET", "-f", "per_page=100", "-f", "page=2"]]
    run = RvFake(rows=_rv([("bo", "COMMENTED")] * 100) * 1)
    run.rows = run.rows[:100]
    g.pr_changes_requested(run, 7, env={})                 # exactly 100: page 2 is asked and comes back empty
    assert len(run.rest_calls()) == 2 and run.rest_calls()[0][1] == "repos/{owner}/{repo}/pulls/7/reviews"


def test_pr_changes_requested_a_full_page_at_the_cap_raises_never_truncates():
    """C2: 1000 reviews fill all 10 pages; the CR hiding on page 11 must not read as 'none'."""
    g = _gm()
    run = RvFake(rows=_rv([("bo", "COMMENTED")] * 1000))
    with pytest.raises(g.GhApiError) as ei:
        g.pr_changes_requested(run, 7, "o/r", env={})
    assert "cap" in str(ei.value) and ei.value.kind == "other" and len(run.rest_calls()) == g.REVIEW_PAGE_CAP == 10
    assert run.gql_calls() == []


@pytest.mark.parametrize("raw", ["", "null", "{}", '{"message": "x"}', "[1]", "<html>", '["x"]'],
                         ids=["blank", "null", "dict", "message", "non-dict-row", "garbage", "str-row"])
def test_pr_changes_requested_a_malformed_page_raises(raw):
    g = _gm()
    run = RvFake(pages={1: raw})
    with pytest.raises(g.GhApiError) as ei:
        g.pr_changes_requested(run, 7, "o/r", env={})
    assert ei.value.kind == "other" and run.gql_calls() == []


GOOD_HISTORY = [("bo", "CHANGES_REQUESTED"), ("al", "APPROVED"), ("bo", "COMMENTED"), (None, "CHANGES_REQUESTED"),
                ("Cy", "CHANGES_REQUESTED"), ("cy", "DISMISSED"), ("di", "PENDING"), (None, "APPROVED")]


@pytest.mark.parametrize("hint", [t[1] for t in TRANSIENT], ids=[t[0] for t in TRANSIENT])
def test_pr_changes_requested_falls_back_once_and_the_two_paths_agree(hint):
    """PARITY: the SAME history through the REST reducer and the `gh pr view --json reviews` reducer."""
    g = _gm()
    rest = g.pr_changes_requested(RvFake(rows=_rv(GOOD_HISTORY)), 7, "o/r", env={})
    run = RvFake(fail=_err(hint), gql=prfake.gh_reviews(GOOD_HISTORY))
    fb = g.pr_changes_requested(run, 7, "o/r", env={})
    assert fb == rest == sorted(["(deleted user)", "bo", "Cy"])
    assert run.gql_calls() == [["pr", "view", "7", "--repo", "o/r", "--json", "reviews"]]


@pytest.mark.parametrize("gql", [
    json.dumps({"reviews": [{"author": {"login": "bo"}, "state": "CHANGES_REQUESTED", "submittedAt": "t"}] * 100}),
    json.dumps({"reviews": {}}), json.dumps({"reviews": None}), json.dumps({}), "", "<html>", "[]",
    json.dumps({"reviews": [{"state": "CHANGES_REQUESTED", "submittedAt": "t"}]}),
    json.dumps({"reviews": [{"author": None, "state": "CHANGES_REQUESTED"}]}),
    json.dumps({"reviews": [{"author": None, "submittedAt": None}]}),
    json.dumps({"reviews": [{"author": None, "state": "WEIRD", "submittedAt": None}]}),
    json.dumps({"reviews": ["x"]})],
    ids=["100-rows", "dict", "null", "no-key", "blank", "garbage", "list", "no-author", "no-submittedAt",
         "no-state", "unknown-state", "str-row"])
def test_pr_changes_requested_a_malformed_fallback_raises(gql):
    g = _gm()
    run = RvFake(fail=_err(RATE), gql=gql)
    with pytest.raises(g.GhApiError) as ei:
        g.pr_changes_requested(run, 7, "o/r", env={})
    assert ei.value.kind not in g.FALLBACK_KINDS and len(run.gql_calls()) == 1


def test_pr_changes_requested_fallback_ghosts_are_keyed_per_row():
    g = _gm()
    gql = json.dumps({"reviews": [{"author": None, "state": "CHANGES_REQUESTED", "submittedAt": None},
                                  {"author": None, "state": "APPROVED", "submittedAt": None},
                                  {"author": {"login": ""}, "state": "CHANGES_REQUESTED", "submittedAt": None}]})
    assert g.pr_changes_requested(RvFake(fail=_err(RATE), gql=gql), 7, "o/r", env={}) == [
        "(deleted user)", "(unknown user)"]
    assert g.pr_changes_requested(RvFake(fail=_err(RATE), gql=json.dumps({"reviews": []})), 7, env={}) == []


@pytest.mark.parametrize("hint", [c[1] for c in CLIENT] + [PROXY + " (HTTP 502)"],
                         ids=[c[0] for c in CLIENT] + ["proxy"])
def test_pr_changes_requested_never_falls_back_on_client_errors_or_proxy(hint):
    g = _gm()
    run = RvFake(fail=_err(hint), gql=prfake.gh_reviews([]))
    with pytest.raises(g.GhApiError):
        g.pr_changes_requested(run, 7, "o/r", env={})
    assert run.gql_calls() == []


@pytest.mark.parametrize("env", [{"CLAUDE_CODE_REMOTE": "true"}, {"SIGMA_GH_GRAPHQL": "off"}], ids=["cloud", "off"])
def test_pr_changes_requested_no_fallback_when_graphql_is_unavailable(env):
    g = _gm()
    run = RvFake(fail=_err(RATE), gql=prfake.gh_reviews([]))
    with pytest.raises(g.GhApiError) as ei:
        g.pr_changes_requested(run, 7, "o/r", env=env)
    assert run.gql_calls() == [] and ei.value.kind == "rate_limit"


def test_pr_changes_requested_second_page_429_is_one_fallback_never_half_rest():
    g = _gm()
    run = RvFake(rows=_rv([("bo", "COMMENTED")] * 150), pages={2: _err(RATE)}, gql=prfake.gh_reviews([("bo", "CHANGES_REQUESTED")]))
    assert g.pr_changes_requested(run, 7, "o/r", env={}) == ["bo"]
    assert len(run.rest_calls()) == 2 and len(run.gql_calls()) == 1


def test_pr_changes_requested_breaker_open_skips_rest_and_logs_the_op(tmp_path):
    g = _gm()
    d = str(tmp_path / ".sdlc")
    for _ in range(g.BREAKER_THRESHOLD):
        g.pr_changes_requested(RvFake(fail=_err(RATE), gql=prfake.gh_reviews([])), 7, "o/r", env={}, sdlc_dir=d, now=1000.0)
    run = RvFake(rows=[], gql=prfake.gh_reviews([("bo", "CHANGES_REQUESTED")]))
    assert g.pr_changes_requested(run, 7, "o/r", env={}, sdlc_dir=d, now=1001.0) == ["bo"]
    assert run.rest_calls() == []
    log = json.loads((tmp_path / ".sdlc" / "state" / "gh-fallback.json").read_text())
    assert {e["op"] for e in log} == {"pr_reviews_read"}


# ---- open_prs_for_head_gh ------------------------------------------------------------------------------

class ListRestFake:
    """The REST design read: the list, per-PR `pulls/<n>` and `pulls/<n>/files`, `fail` on EVERY REST call,
    `gql` for the `pr list` fallback. Bodies are raw strings (or Exceptions to raise) keyed by route."""

    def __init__(self, prs=({"number": 42},), list_raw=None, pull_raw=None, files_raw=None, fail=None, gql=None):
        handlers = prfake.design_handlers(list(prs))
        self.list_raw = list_raw if list_raw is not None else handlers[0][1]
        self.pull_raw, self.files_raw = pull_raw or {}, files_raw or {}
        self.pulls = {t: r for t, r in handlers[1:]}
        self.fail, self.gql, self.calls = fail, gql, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "pr":
            if isinstance(self.gql, Exception):
                raise self.gql
            return self.gql if self.gql is not None else "[]"
        if self.fail is not None:
            raise self.fail
        line = " ".join(args)
        if "pulls?head=" in line:
            body = self.list_raw
        else:
            body = next(r for t, r in self.pulls.items() if t in line)
        if isinstance(body, Exception):
            raise body
        return body

    def rest_calls(self):
        return [c for c in self.calls if c[0] == "api"]

    def gql_calls(self):
        return [c for c in self.calls if c[0] == "pr"]


FIELDS = "number,url,mergeable,mergeStateStatus,isCrossRepository,headRefName,files,changedFiles"


def _prs(run, **kw):
    g = _gm()
    return g.open_prs_for_head_gh(run, kw.pop("branch", "sdlc/9"), kw.pop("limit", 30), kw.pop("repo", "o/r"),
                                  env=kw.pop("env", {}), **kw)


def test_open_prs_for_head_gh_lists_then_reads_each_pull_and_its_files():
    run = ListRestFake()
    assert _prs(run) == [{"number": 42, "url": "https://x/42", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                          "isCrossRepository": False, "headRefName": "sdlc/9",
                          "files": [{"path": ".sdlc/design/9.md"}, {"path": ".sdlc/design/9-in-brief.md"}],
                          "changedFiles": 2}]
    assert run.calls == [
        ["api", "repos/o/r/pulls?head=o:sdlc%2F9&state=open&per_page=30", "--method", "GET"],
        ["api", "repos/o/r/pulls/42", "--method", "GET"],
        ["api", "repos/o/r/pulls/42/files", "--method", "GET", "-f", "per_page=100", "-f", "page=1"]]


def test_open_prs_for_head_gh_with_and_without_repo_the_owner_placeholder_is_only_the_no_repo_form():
    """`{owner}` expands INSIDE the query string from the cwd (MEASURED); with a repo it is never sent."""
    with_repo, without = ListRestFake(), ListRestFake()
    _prs(with_repo)
    _prs(without, repo=None)
    assert not any("{owner}" in a for c in with_repo.calls for a in c)
    assert without.calls[0][1] == "repos/{owner}/{repo}/pulls?head={owner}:sdlc%2F9&state=open&per_page=30"
    assert without.calls[1][1] == "repos/{owner}/{repo}/pulls/42"


def test_open_prs_for_head_gh_quotes_a_hostile_branch_into_one_query_value():
    run = ListRestFake(list_raw="[]")
    _prs(run, branch="a&b#c d/e?f=g")
    path = run.calls[0][1]
    assert path.endswith("?head=o:a%26b%23c%20d%2Fe%3Ff%3Dg&state=open&per_page=30") and path.count("&") == 2


def test_open_pr_for_branch_gh_quotes_special_characters_in_the_branch():
    g = _gm()
    run = PrFake(rows=[])
    g.open_pr_for_branch_gh(run, "x&y#z w/v", OPEN_FIELDS, "acme/app", env={})
    assert run.calls[0][1] == "repos/acme/app/pulls?head=acme:x%26y%23z%20w/v&state=open&per_page=1"


def test_check_run_and_status_error_messages_truncate_the_row_not_the_tuple():
    g = _gm()
    for fn in (g._check_run_row, g._status_row):
        with pytest.raises(g.GhApiError) as ei:
            fn("x" * 500)
        assert len(str(ei.value)) < 200 and "x" * 130 not in str(ei.value)


def test_open_prs_for_head_gh_a_genuine_empty_list_is_empty_and_costs_one_call():
    run = ListRestFake(list_raw="[]")
    assert _prs(run) == [] and len(run.calls) == 1


@pytest.mark.parametrize("raw", ["", "null", "{}", '{"message": "x"}', "[1]", "<html>", '["x"]'],
                         ids=["blank", "null", "dict", "message", "non-dict-row", "garbage", "str-row"])
def test_open_prs_for_head_gh_a_non_list_reply_raises_and_never_means_no_pr(raw):
    """C5: nothing but a genuine JSON `[]` is an empty answer."""
    g = _gm()
    run = ListRestFake(list_raw=raw)
    with pytest.raises(g.GhApiError) as ei:
        _prs(run)
    assert ei.value.kind == "other" and run.gql_calls() == []


def test_open_prs_for_head_gh_a_list_at_the_limit_raises_on_both_paths():
    """C4: a full page may hide the real PR."""
    g = _gm()
    for limit in (1, 3):
        run = ListRestFake(prs=[{"number": 100 + i} for i in range(limit)], list_raw=None)
        with pytest.raises(g.GhApiError) as ei:
            _prs(run, limit=limit)
        assert "limit" in str(ei.value) and ei.value.kind == "other" and len(run.rest_calls()) == 1
        rows = [json.loads(prfake.rest_pull())] * limit
        fb = [dict(zip(FIELDS.split(","), [1] * 8)) for _ in range(limit)]
        run = ListRestFake(fail=_err(RATE), gql=json.dumps(fb))
        with pytest.raises(g.GhApiError):
            _prs(run, limit=limit)
        assert len(run.gql_calls()) == 1
    run = ListRestFake(prs=[{"number": 1}, {"number": 2}])
    assert [r["number"] for r in _prs(run, limit=3)] == [1, 2]


def test_open_prs_for_head_gh_cost_is_one_plus_two_per_row():
    run = ListRestFake(prs=[{"number": 1}, {"number": 2}, {"number": 3}])
    _prs(run)
    assert len(run.rest_calls()) == 1 + 2 * 3


@pytest.mark.parametrize("deviation", [
    {"number": "7"}, {"number": True}, {"number": None}, {"number": 7.0}], ids=["str", "bool", "none", "float"])
def test_open_prs_for_head_gh_a_list_row_number_must_be_an_int(deviation):
    g = _gm()
    run = ListRestFake(list_raw=json.dumps([deviation]))
    with pytest.raises(g.GhApiError) as ei:
        _prs(run)
    assert ei.value.kind == "other" and len(run.rest_calls()) == 1


def _pull(**kw):
    d = prfake.design_pull()
    d.update(kw)
    return json.dumps(d)


@pytest.mark.parametrize("which,raw", [
    ("pull", "[]"), ("pull", "null"), ("pull", _pull(number=43)), ("pull", _pull(changed_files=None)),
    ("pull", _pull(changed_files=True)), ("pull", _pull(changed_files="2")), ("pull", _pull(html_url=None)),
    ("pull", _pull(mergeable_state="odd")), ("pull", _pull(mergeable="yes")),
    ("pull", json.dumps({k: v for k, v in prfake.design_pull().items() if k != "mergeable"})),
    ("files", "{}"), ("files", "null"), ("files", ""), ("files", json.dumps([{"path": "x"}])),
    ("files", json.dumps([{"filename": 3}])), ("files", json.dumps(["x"]))],
    ids=["pull-list", "pull-null", "pull-other-number", "changed-null", "changed-bool", "changed-str",
         "no-url", "odd-state", "mergeable-str", "no-mergeable", "files-dict", "files-null", "files-blank",
         "files-no-filename", "files-int-filename", "files-str-row"])
def test_open_prs_for_head_gh_a_malformed_detail_raises(which, raw):
    g = _gm()
    target = prfake.pull_get(42) if which == "pull" else prfake.files_get(42)
    run = ListRestFake()
    run.pulls = {t: (raw if t == target else r) for t, r in run.pulls.items()}
    with pytest.raises(g.GhApiError) as ei:
        _prs(run)
    assert ei.value.kind == "other" and run.gql_calls() == []


def test_open_prs_for_head_gh_a_deleted_fork_reads_as_cross_repository_and_a_short_page_as_a_mismatch():
    run = ListRestFake(prs=[{"number": 42, "cross": None}])
    assert _prs(run)[0]["isCrossRepository"] is True
    run = ListRestFake(prs=[{"number": 42, "changed": 150}])
    row = _prs(run)[0]
    assert row["changedFiles"] == 150 and len(row["files"]) == 2         # distinct: the caller refuses it


def test_open_prs_for_head_gh_argument_refusals_before_any_call():
    g = _gm()
    for limit in (0, -1, 101, True, "30", None):
        run = ListRestFake()
        with pytest.raises(ValueError):
            g.open_prs_for_head_gh(run, "b", limit, "o/r", env={})
        assert run.calls == []
    for repo in ("noslash", "", 3):
        with pytest.raises(ValueError):
            g.open_prs_for_head_gh(ListRestFake(), "b", 30, repo, env={})


@pytest.mark.parametrize("hint", [t[1] for t in TRANSIENT], ids=[t[0] for t in TRANSIENT])
def test_open_prs_for_head_gh_falls_back_once_with_the_same_row_shape(hint):
    row = {"number": 42, "url": "u", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
           "isCrossRepository": False, "headRefName": "sdlc/9", "files": [{"path": "a"}], "changedFiles": 1,
           "extra": "dropped"}
    run = ListRestFake(fail=_err(hint), gql=json.dumps([row]))
    out = _prs(run)
    assert out == [{k: v for k, v in row.items() if k != "extra"}]
    assert run.gql_calls() == [["pr", "list", "--repo", "o/r", "--head", "sdlc/9", "--state", "open",
                                "--limit", "30", "--json", FIELDS]]
    assert len(run.rest_calls()) == 1
    run = ListRestFake(fail=_err(RATE), gql="[]")
    assert _prs(run, repo=None) == []
    assert run.gql_calls()[0][:3] == ["pr", "list", "--head"]               # no --repo when none is known


@pytest.mark.parametrize("gql", ["", "  ", "null", "{}", "<html>", "[1]", '["x"]',
                                 json.dumps([{"number": 1}])],
                         ids=["blank", "spaces", "null", "dict", "garbage", "int-row", "str-row", "missing-keys"])
def test_open_prs_for_head_gh_a_malformed_fallback_raises_while_a_real_empty_list_does_not(gql):
    """C5, fallback half: BLANK output raises (it is never 'no PR'); `[]` is the genuine empty answer."""
    g = _gm()
    with pytest.raises(g.GhApiError) as ei:
        _prs(ListRestFake(fail=_err(RATE), gql=gql))
    assert ei.value.kind not in g.FALLBACK_KINDS


@pytest.mark.parametrize("hint", [c[1] for c in CLIENT] + [PROXY + " (HTTP 502)"],
                         ids=[c[0] for c in CLIENT] + ["proxy"])
def test_open_prs_for_head_gh_never_falls_back_on_client_errors_or_proxy(hint):
    g = _gm()
    run = ListRestFake(fail=_err(hint), gql="[]")
    with pytest.raises(g.GhApiError):
        _prs(run)
    assert run.gql_calls() == []


@pytest.mark.parametrize("env", [{"CLAUDE_CODE_REMOTE": "true"}, {"SIGMA_GH_GRAPHQL": "off"}], ids=["cloud", "off"])
def test_open_prs_for_head_gh_no_fallback_when_graphql_is_unavailable(env):
    g = _gm()
    run = ListRestFake(fail=_err("gh: Server Error (HTTP 502)"), gql="[]")
    with pytest.raises(g.GhApiError):
        _prs(run, env=env)
    assert run.gql_calls() == []


def test_open_prs_for_head_gh_exactly_one_fallback_and_its_failure_propagates():
    g = _gm()
    run = ListRestFake(fail=_err("gh: Server Error (HTTP 502)"), gql=_err("gh: Server Error (HTTP 503)", text="x"))
    with pytest.raises(g.GhApiError):
        _prs(run)
    assert len(run.gql_calls()) == 1


def test_open_prs_for_head_gh_a_detail_429_after_a_good_list_is_one_fallback_never_half_rest():
    run = ListRestFake(prs=[{"number": 1}, {"number": 2}],
                       pull_raw=None, gql="[]")
    run.pulls[prfake.pull_get(2)] = _err(RATE)
    assert _prs(run) == [] and len(run.gql_calls()) == 1


def test_pr_b_fallback_argv_is_built_only_in_gh_api_and_the_ops_are_labelled():
    text = (S / "gh_api.py").read_text()
    assert '["pr", "list", *_repo_flag(repo), "--head", branch,' in text
    assert '"pr_list_read"' in text and '"pr_reviews_read"' in text
    assert "(r,)[:120]" not in text
