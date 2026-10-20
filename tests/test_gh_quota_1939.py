"""#1939: GraphQL quota is spent account-wide. The wait comes from the refused response, the label lookup
is REST, a secondary limit is named as one, and a shared per-account bucket spaces GraphQL use.

Zero network: fakes only. Every control named in a docstring was run red once (see the goal handoff).
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import resource_mode  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"
DOCTOR = ROOT / "skills" / "sigma-doctor" / "scripts"
NOW = 1_800_000_000


def _mod(name, base=LOOP):
    spec = importlib.util.spec_from_file_location(name + "_t1939", base / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def q():
    return _mod("gh_quota")


@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(d))
    return d


def _refusal(remaining, reset=None, retry_after=None, words="HTTP 403: API rate limit exceeded"):
    lines = ["HTTP/2.0 403 Forbidden", "X-RateLimit-Remaining: %s" % remaining]
    if reset is not None:
        lines.append("X-RateLimit-Reset: %d" % reset)
    if retry_after is not None:
        lines.append("Retry-After: %d" % retry_after)
    return "\n".join(lines) + "\n\n" + words


# --- 1 / 2: classification ---------------------------------------------------------------------

def test_primary_refusal_is_primary_and_retries_at_the_reset_header(q):
    got = q.parse_refusal(_refusal(0, reset=NOW + 120), now=NOW)
    assert got == {"kind": "primary", "retry_at": NOW + 120}


def test_secondary_refusal_is_secondary_and_reads_retry_after(q):
    text = _refusal(4000, retry_after=45, words="You have exceeded a secondary rate limit")
    got = q.parse_refusal(text, now=NOW)
    assert got["kind"] == "secondary" and got["retry_at"] == NOW + 45
    assert q.wait_seconds(text, now=NOW) == 45


def test_secondary_by_wording_alone_has_a_bounded_default(q):
    assert q.parse_refusal("abuse detection mechanism", now=NOW)["kind"] == "secondary"
    assert q.wait_seconds("secondary rate limit", now=NOW) == q.DEFAULT_SECONDARY


def test_not_a_refusal_waits_zero(q):
    assert q.parse_refusal("HTTP 404 Not Found", now=NOW) == {"kind": None, "retry_at": None}
    assert q.wait_seconds("HTTP 404 Not Found", now=NOW) == 0


# --- 3: the wait comes from the refused response, never /rate_limit ----------------------------

def test_wait_follows_the_refused_headers_not_rate_limit(q):
    calls = []

    def run(args):                                  # a fake /rate_limit that disagrees with the refusal
        calls.append(list(args))
        return json.dumps({"resources": {"graphql": {"remaining": 4988, "reset": NOW + 36 * 60}}})

    refused = _refusal(0, reset=NOW + 120)
    assert q.wait_seconds(refused, now=NOW) == 120
    assert q.wait_seconds(refused, now=NOW, run=run) == 120      # even when a runner is offered
    assert not any("rate_limit" in " ".join(a) for a in calls)


def test_the_loop_hold_message_uses_the_refused_text(q, state):
    """note_refusal records the hold from the refused text; held() reports that, not any counter."""
    q.note_refusal("acct", _refusal(0, reset=NOW + 120), now=NOW)
    assert q.held("acct", now=NOW) == 120


# --- 4: the shared bucket ----------------------------------------------------------------------

def test_two_processes_share_one_bucket(state):
    code = ("import importlib.util,sys;s=importlib.util.spec_from_file_location('g',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "print(sum(1 for _ in range(15) if m.acquire('acct',now=%d)[0]))" % NOW)
    env = resource_mode.carry(dict(os.environ, SIGMA_RESOURCE_DIR=str(state)))
    outs = [int(subprocess.run([sys.executable, "-I", "-c", code, str(LOOP / "gh_quota.py")], env=env,
                               capture_output=True, text=True, timeout=60, check=True).stdout)
            for _ in range(2)]
    assert outs == [15, 5]          # burst 20 shared across both processes, none refilled at one instant


def test_burst_is_capped(q, state):
    assert all(q.acquire("a", now=NOW)[0] for _ in range(q.BURST))
    ok, wait = q.acquire("a", now=NOW)
    assert not ok and wait == q.REFILL_SECONDS
    # a long idle never banks more than BURST
    assert sum(1 for _ in range(q.BURST + 5) if q.acquire("a", now=NOW + 10 ** 6)[0]) == q.BURST


def test_refill_gives_one_point_per_interval(q, state):
    for _ in range(q.BURST):
        q.acquire("a", now=NOW)
    assert not q.acquire("a", now=NOW + q.REFILL_SECONDS - 0.5)[0]
    assert q.acquire("a", now=NOW + q.REFILL_SECONDS)[0]


def test_a_recorded_refusal_holds_every_caller_until_hold_until(q, state):
    q.note_refusal("a", _refusal(4000, retry_after=45, words="secondary rate limit"), now=NOW)
    ok, wait = q.acquire("a", now=NOW + 10)
    assert not ok and wait == 35
    assert q.acquire("a", now=NOW + 46)[0]


def test_accounts_do_not_share_tokens(q, state):
    for _ in range(q.BURST):
        q.acquire("a", now=NOW)
    assert not q.acquire("a", now=NOW)[0]
    assert q.acquire("b", now=NOW)[0]


# --- 5: damage fails open ----------------------------------------------------------------------

def test_corrupt_bucket_file_is_open(q, state):
    state.mkdir()
    (state / q.BUCKET_FILE).write_text("{not json")
    assert q.acquire("a", now=NOW) == (True, 0)
    (state / q.BUCKET_FILE).write_text(json.dumps({"accounts": {"a": {"tokens": "x", "hold_until": "y"}}}))
    assert q.acquire("a", now=NOW)[0] is True


# --- wiring: _run_gh records a refusal ---------------------------------------------------------

def test_run_gh_records_a_refusal_for_every_later_caller(q, state, monkeypatch):
    src = _mod("sources")
    quota = src.gh_quota
    monkeypatch.setenv("SIGMA_GH_ACCOUNT", "acct")

    class P:
        returncode, stdout = 1, ""
        stderr = "HTTP 403: You have exceeded a secondary rate limit.\nRetry-After: 90"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    with pytest.raises(RuntimeError):
        src._run_gh(["api", "graphql", "-f", "query=x"])
    assert 0 < quota.held("acct") <= 90
    with pytest.raises(RuntimeError) as err:                 # later callers do not even call gh
        src._run_gh(["api", "graphql", "-f", "query=x"])
    assert "secondary" in str(err.value).lower()


# --- 6 / 7: label lookup is REST ---------------------------------------------------------------

def _source(run):
    src = _mod("sources")
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}}
    return src.GitHubSource(cfg, run=run)


def _fake(label_reply, calls):
    def run(args):
        args = [str(a) for a in args]
        calls.append(args)
        if args[:2] == ["api", "graphql"]:
            raise RuntimeError("HTTP 403: API rate limit exceeded")
        if args[0] == "api" and "/labels/" in args[1]:
            if isinstance(label_reply, Exception):
                raise label_reply
            return json.dumps({"name": "x"})
        return ""
    return run


def _http(status):
    exc = RuntimeError("gh api failed: HTTP %d" % status)
    exc.hint = "gh: Not Found (HTTP %d)" % status if status == 404 else "gh: error (HTTP %d)" % status
    return exc


def test_label_exists_uses_rest_with_graphql_refused():
    calls = []
    assert _source(_fake("ok", calls)).label_exists("x") is True
    assert not any(c[:2] == ["api", "graphql"] for c in calls)
    assert any("/labels/x" in c[1] for c in calls)


def test_label_exists_rest_404_is_false_and_5xx_is_unknown():
    calls = []
    assert _source(_fake(_http(404), calls)).label_exists("x") is False
    assert _source(_fake(_http(502), calls)).label_exists("x") is None
    assert not any(c[:2] == ["api", "graphql"] for c in calls)


def test_label_drain_plan_succeeds_with_graphql_refused(tmp_path):
    sys.path.insert(0, os.path.dirname(__file__))
    import test_label_drain_plan as base
    ld = _mod("label_drain")
    inner = base._runner([base._goal(1)], [])
    graphql = []

    def run(args):
        if [str(a) for a in args][:2] == ["api", "graphql"]:
            graphql.append(args)
            raise RuntimeError("HTTP 403: API rate limit exceeded")
        return inner(args)

    got = ld.plan(base._repo(tmp_path), base.LBL, run=run)
    assert got["members"] and not graphql


# --- 8: doctor names the secondary limit -------------------------------------------------------

def test_doctor_board_row_says_secondary_limit_and_when_to_retry(state):
    doc = _mod("doctor", DOCTOR)
    gh_cfg = {"project": {"number": 4, "owner": "acme"}, "repo": "acme/widget"}
    secondary = "gh: You have exceeded a secondary rate limit.\nRetry-After: 90"
    primary = "gh: API rate limit exceeded for user\nX-RateLimit-Remaining: 0"
    got_s = doc._pinned_board_state(gh_cfg, _failing_run(doc, secondary))
    got_p = doc._pinned_board_state(gh_cfg, _failing_run(doc, primary))
    assert got_s[0] == "refused" and "secondary limit" in got_s[1] and "retry" in got_s[1].lower()
    assert got_p[0] == "unverifiable" and got_p[1] is None      # primary wording is unchanged
    assert "90" in got_s[1] or "minute" in got_s[1]


def _failing_run(doc, stderr):
    """A doctor `run` whose falsy result carries gh's stderr on `.raw`, as `_failure_text` reads it."""
    def run(argv):
        res = doc._RawFailure("")
        res.raw = stderr
        return res
    return run
