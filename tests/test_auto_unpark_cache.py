"""#2021: the auto-unpark sweep reads each parked issue's comments once per sweep, reuses them while the
issue's updated_at is unchanged, and runs at most once per interval. A fake runner counts the requests."""
import json, pathlib

import gqlfake
import test_auto_unpark as base

au = base._mod("auto_unpark")
bc = base._mod("backlog_check")

CACHE = "state/auto-unpark-cache.json"
LAST = "state/auto-unpark-last.json"
NOTE = "a note that names no blocker"


def _cfg(interval=0):
    cfg = base._config()
    cfg["discovery"]["auto_unpark"] = {"interval_minutes": interval}
    return cfg


def _issues(numbers, updated="2026-01-01T00:00:00Z", blocker=900):
    return [dict(base._blocked_issue(n, body="Blocked by #%d" % blocker), updated_at=updated) for n in numbers]


def _runner(issues, comments=None, states=None):
    comments = comments if comments is not None else {str(i["number"]): [NOTE] for i in issues}
    return base._sweep_runner(parked=json.dumps(issues), comments=comments, states=states or {}), comments


def _comment_reads(run):
    """Comment-page requests the runner saw, one per issue read at the default page size."""
    return [gqlfake.rest_issue_target(c)[0] for c in run.calls
            if gqlfake.rest_issue_target(c) and gqlfake.rest_issue_target(c)[1]]


def _sweep(tmp_path, run, cfg=None, apply=False, **kw):
    return au.sweep_unpark(tmp_path, cfg or _cfg(), apply=apply, run=run, **kw)


def test_same_updated_at_is_not_reread(tmp_path):
    run, _ = _runner(_issues([1, 2, 3]))
    _sweep(tmp_path, run)
    first = len(_comment_reads(run))
    _sweep(tmp_path, run)
    assert first == 3                                   # one read per issue, not one per call site
    assert len(_comment_reads(run)) == first            # the second sweep read none


def test_changed_updated_at_is_reread_and_sees_the_new_comment(tmp_path):
    issues = _issues([5], blocker=900)
    run, comments = _runner(issues, states={"900": "CLOSED"})
    assert _sweep(tmp_path, run)["eligible"] == 1
    comments["5"] = [NOTE, au.keep_parked_comment("on purpose")]
    issues[0]["updated_at"] = "2026-01-02T00:00:00Z"
    run2 = base._sweep_runner(parked=json.dumps(issues), comments=comments, states={"900": "CLOSED"})
    assert _sweep(tmp_path, run2)["eligible"] == 0      # a stale cache would still say eligible
    assert _comment_reads(run2) == ["5"]


def test_missing_updated_at_is_always_reread(tmp_path):
    issues = [base._blocked_issue(7, body="Blocked by #900")]
    run, _ = _runner(issues)
    _sweep(tmp_path, run)
    _sweep(tmp_path, run)
    assert _comment_reads(run) == ["7", "7"]


def test_each_issue_is_read_once_per_sweep(tmp_path):
    run, _ = _runner(_issues(range(1, 21)))
    _sweep(tmp_path, run)
    assert sorted(_comment_reads(run), key=int) == [str(i) for i in range(1, 21)]


def test_interval_gate_skip_run_and_first_pick(tmp_path, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(au, "_now", lambda: t[0])
    monkeypatch.setenv("SIGMA_RUN_ID", "run-a")
    run, _ = _runner(_issues([1]))
    cfg = _cfg(10)
    lists = lambda: len(base.rest_list_calls(run))
    assert "skipped" not in _sweep(tmp_path, run, cfg, apply=True)    # first pick of the session
    n = lists()
    t[0] += 60
    out = _sweep(tmp_path, run, cfg, apply=True)                      # the direct call chain.py makes
    assert out.get("skipped") and out["unparked"] == [] and lists() == n
    t[0] += 11 * 60
    assert "skipped" not in _sweep(tmp_path, run, cfg, apply=True)    # interval elapsed
    n = lists()
    monkeypatch.setenv("SIGMA_RUN_ID", "run-b")
    t[0] += 1
    assert "skipped" not in _sweep(tmp_path, run, cfg, apply=True)    # a new session sweeps at once
    assert lists() > n


def test_session_pid_gates_when_no_run_id(tmp_path, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(au, "_now", lambda: t[0])
    monkeypatch.delenv("SIGMA_RUN_ID", raising=False)
    run, _ = _runner(_issues([1]))
    cfg = _cfg(10)
    assert "skipped" not in _sweep(tmp_path, run, cfg, apply=True, session_pid=111)   # first pick
    t[0] += 60
    assert _sweep(tmp_path, run, cfg, apply=True, session_pid=111).get("skipped")     # same session
    t[0] += 1
    assert "skipped" not in _sweep(tmp_path, run, cfg, apply=True, session_pid=222)   # new session, in interval
    t[0] += 1
    assert _sweep(tmp_path, run, cfg, apply=True).get("skipped")                      # no pid: interval only


def test_interval_zero_sweeps_every_pick_and_dry_run_is_never_gated(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "_now", lambda: 5.0)
    run, _ = _runner(_issues([1]))
    for _ in range(2):
        assert "skipped" not in _sweep(tmp_path, run, _cfg(0), apply=True)
    _sweep(tmp_path, run, _cfg(10), apply=True)
    assert "skipped" not in _sweep(tmp_path, run, _cfg(10), apply=False)


def test_precheck_helper_never_uses_the_cache(tmp_path):
    run, _ = _runner(_issues([1]))
    _sweep(tmp_path, run)
    assert (tmp_path / CACHE).exists()
    before = len(_comment_reads(run))
    for _ in range(2):
        bc._fetch_scrubbed_comments(tmp_path, _cfg(), {"ref": "1", "raw": ""}, run=run)
    assert len(_comment_reads(run)) == before + 2


def test_cache_is_pruned_to_the_parked_set(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / CACHE).write_text(json.dumps({"schema": "auto-unpark-cache/1", "entries": {
        str(n): {"updated_at": "x", "comments": [NOTE]} for n in range(50, 60)}}))
    run, _ = _runner(_issues([5]))
    _sweep(tmp_path, run)
    assert list(json.loads((tmp_path / CACHE).read_text())["entries"]) == ["5"]


def test_cache_entry_cap_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "CACHE_MAX_ENTRIES", 2)
    run, _ = _runner(_issues([1, 2, 3, 4]))
    _sweep(tmp_path, run)
    assert len(json.loads((tmp_path / CACHE).read_text())["entries"]) == 2


def test_corrupt_cache_and_gate_state_fail_open(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / CACHE).write_text("{not json")
    (tmp_path / LAST).write_text("[]")
    run, _ = _runner(_issues([1], blocker=900), states={"900": "CLOSED"})
    assert _sweep(tmp_path, run, _cfg(10), apply=False)["eligible"] == 1


def test_unwritable_state_fails_open(tmp_path):
    (tmp_path / "state").write_text("a file where the directory should be")
    run, _ = _runner(_issues([1], blocker=900), states={"900": "CLOSED"})
    assert _sweep(tmp_path, run, _cfg(10), apply=True)["eligible"] == 1


def test_empty_read_is_not_cached(tmp_path):
    """A failed read fails open to [], so [] must never be remembered as 'no comments'."""
    run, comments = _runner(_issues([1]), comments={"1": []})
    _sweep(tmp_path, run)
    _sweep(tmp_path, run)
    gets = [c for c in run.calls if gqlfake.rest_issue_target(c) == ("1", False)]
    assert len(gets) == 2                               # one read per sweep, none served from the cache
