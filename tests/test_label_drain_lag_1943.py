"""#1943: `label_drain.plan` re-reads a listing that may lag a label write (offline, fake `gh`)."""
import datetime
import json

import pytest

import gqlfake
from test_label_drain_plan import LBL, _goal, _mod, _repo, _runner

OLD = "2020-01-01T00:00:00Z"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(i, ts):
    return dict(i, updated_at=ts)


def _lagged(first, second):
    """Fake whose label-list GET serves `first` on the 1st read and `second` afterwards."""
    inner = _runner(first + second)
    reads = {"n": 0}

    def run(args):
        args = [str(a) for a in args]
        params = gqlfake.rest_list_params(args) if args[0] == "api" and args[1:2] != ["graphql"] else None
        if params is not None and "labels" in params:
            reads["n"] += 1
            inner.calls.append(args)
            hits = first if reads["n"] == 1 else first + second
            pub = lambda i: {k: v for k, v in i.items() if not k.startswith("_")} | {"comments": 0}
            return json.dumps([pub(i) for i in hits] if int(params["page"]) == 1 else [])
        return inner(args)

    run.reads = reads
    run.calls = inner.calls
    return run


@pytest.fixture
def ld():
    return _mod("label_drain")


def _plan(ld, tmp_path, run, sleeps):
    return ld.plan(_repo(tmp_path), LBL, run=run, sleep=sleeps.append)


def test_recent_listing_is_reread_and_unioned(ld, tmp_path):
    run = _lagged([_stamp(_goal(1), _now())], [_stamp(_goal(2), _now())])
    sleeps = []
    p = _plan(ld, tmp_path, run, sleeps)
    assert {r["number"] for r in p["members"]} == {1, 2}
    assert run.reads["n"] == 2 and len(sleeps) == 1
    assert p["lag"] == {"window_s": 60, "reread": True, "added": [2]}


def test_old_listing_read_once_no_wait(ld, tmp_path):
    run = _lagged([_stamp(_goal(1), OLD)], [_stamp(_goal(2), OLD)])
    sleeps = []
    p = _plan(ld, tmp_path, run, sleeps)
    assert run.reads["n"] == 1 and sleeps == []
    assert [r["number"] for r in p["members"]] == [1]
    assert p["lag"]["reread"] is False
    assert "may lag" not in ld.render_plan(p)


def test_empty_listing_is_reread_once(ld, tmp_path):
    run = _lagged([], [_stamp(_goal(3), OLD)])
    sleeps = []
    p = _plan(ld, tmp_path, run, sleeps)
    assert run.reads["n"] == 2 and len(sleeps) == 1
    assert [r["number"] for r in p["members"]] == [3]


def test_empty_both_times_is_drained_after_one_reread(ld, tmp_path):
    run = _lagged([], [])
    p = _plan(ld, tmp_path, run, [])
    assert run.reads["n"] == 2 and p["drained"] is True


def test_listing_without_timestamps_is_not_in_window(ld, tmp_path):
    run = _lagged([_goal(1)], [_goal(2)])
    sleeps = []
    _plan(ld, tmp_path, run, sleeps)
    assert run.reads["n"] == 1 and sleeps == []


def test_wait_env_override_and_zero(ld, tmp_path, monkeypatch):
    monkeypatch.setenv("SIGMA_LABEL_LAG_WAIT", "0")
    sleeps = []
    _plan(ld, tmp_path, _lagged([_stamp(_goal(1), _now())], []), sleeps)
    assert sleeps == [0.0]
    monkeypatch.setenv("SIGMA_LABEL_LAG_WAIT", "7")
    sleeps = []
    (tmp_path / "b").mkdir()
    _plan(ld, tmp_path / "b", _lagged([_stamp(_goal(1), _now())], []), sleeps)
    assert sleeps == [7.0]


def test_render_shows_lag_line_only_when_window_applied(ld, tmp_path):
    hot = _plan(ld, tmp_path, _lagged([_stamp(_goal(1), _now())], []), [])
    assert "may lag" in ld.render_plan(hot)
    (tmp_path / "x").mkdir()
    cold = _plan(ld, tmp_path / "x", _lagged([_stamp(_goal(1), OLD)], []), [])
    assert "may lag" not in ld.render_plan(cold)
