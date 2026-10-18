"""#1612: when the stored dependency map is refreshed, and how its death is made visible.

No network, no real model: a scripted repository stands in for `gh`, and the hook test puts a `gh`
that aborts on any call at the front of PATH. Every guard here was run red once against its own
deliberate break; the `control:` line in each docstring names the break."""
import importlib.util
import json
import os
import pathlib
import re
import stat
import time
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
HOOK = ROOT / "hooks" / "session_start.sh"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402


def _mod(name, base=S):
    spec = importlib.util.spec_from_file_location(name, base / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dr = _mod("dag_refresh")
ds = dr.dag_store
rt = _mod("reconcile_tick")
loop = rt.loop
dag_cli = _mod("dag")
doctor = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts")

NOW = 1_800_000_000.0
TTL = 60
CONFIG = {"discovery": {"github": {"repo": "owner/repo"}, "dag": {"mode": "on"}}}
GH = {"discovery": {"source": "github", "github": {"repo": "owner/repo"}, "dag": {"mode": "on"}}}


def _iso(t):
    return dr.dag_collect._iso(t)


class Repo:
    """A scripted repository that counts calls: one open goal, listed under the goal label only."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fail:
            raise RuntimeError("scripted outage")
        ep = args[1]
        if re.search(r"/issues$", ep):
            if "labels=sdlc:goal" in args:
                return json.dumps([{"number": 1, "title": "t", "body": "", "state": "open",
                                    "labels": [{"name": "sdlc:goal"}], "updated_at": "2026-10-01T00:00:00Z"}])
            return "[]"
        raise AssertionError("unexpected call %r" % (args,))


def _sdlc(tmp_path, config=CONFIG):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(config))
    return d


def _store(d, age, config=CONFIG, base=NOW):
    """Publish a one-node map whose last good stamp is `age` seconds before `base`."""
    stamp = _iso(base - age)
    ok, _ = ds.save(d, {"header": {"repo": "owner/repo", "built_at": stamp, "refreshed_at": stamp,
                                   "last_ok_at": stamp, "last_error": None, "partial": False},
                        "nodes": [{"number": 1, "kind": "issue", "state": "open", "life": "goal",
                                   "prio": None, "in_corpus": True}], "edges": []}, config)
    assert ok


@pytest.fixture(autouse=True)
def _fresh_flag():
    loop._DAG_PICKED[0] = False
    yield
    loop._DAG_PICKED[0] = False


# ------------------------------------------------------------------ due or not due

def test_not_due_makes_zero_runner_calls(tmp_path):
    """A map stamped a minute ago is not due: the runner is never touched.
    control: ignore the TTL (is_due returns True whenever the mode is not off) -> calls appear."""
    d = _sdlc(tmp_path)
    _store(d, 60)
    run = Repo()
    res = dr.refresh_if_due(d, CONFIG, run, NOW)
    assert res["status"] == "fresh" and run.calls == []


def test_due_makes_exactly_one_collection(tmp_path):
    """No map: one collection (one list per label set, five), the map is published, and a second call
    at the same instant is not due.
    control: ignore the TTL -> the second call collects again."""
    d = _sdlc(tmp_path)
    run = Repo()
    res = dr.refresh_if_due(d, CONFIG, run, NOW)
    assert res["status"] == "saved" and len(run.calls) == 5
    assert ds.load(d, CONFIG)[0] is not None
    again = dr.refresh_if_due(d, CONFIG, run, NOW + 1)
    assert again["status"] == "fresh" and len(run.calls) == 5


def test_stale_map_is_due_after_ttl_plus_offset(tmp_path):
    """Fresh below the TTL, due above TTL plus the clone's offset (at most 10 percent)."""
    d = _sdlc(tmp_path)
    _store(d, (TTL - 1) * 60)
    assert dr.is_due(d, CONFIG, NOW) == (False, "fresh")
    _store(d, (TTL * 1.1 + 1) * 60)
    assert dr.is_due(d, CONFIG, NOW) == (True, "stale")


def test_force_refreshes_a_fresh_map(tmp_path):
    d = _sdlc(tmp_path)
    _store(d, 10)
    run = Repo()
    assert dr.refresh_if_due(d, CONFIG, run, NOW, force=True)["status"] == "saved"
    assert len(run.calls) == 5


def test_failed_refresh_keeps_old_map_and_waits_before_retry(tmp_path):
    """A failing read leaves the old file byte-identical, counts an error, and is not retried inside
    the retry floor."""
    d = _sdlc(tmp_path)
    _store(d, 5 * TTL * 60)
    before = ds.path(d).read_bytes()
    bad = Repo(fail=True)
    assert dr.refresh_if_due(d, CONFIG, bad, NOW)["status"] == "kept"
    assert ds.path(d).read_bytes() == before and dr.read_meta(d)["errors"] == 1
    n = len(bad.calls)
    assert dr.refresh_if_due(d, CONFIG, bad, NOW + 10)["status"] == "retry-wait" and len(bad.calls) == n
    dr.refresh_if_due(d, CONFIG, bad, NOW + dr.RETRY_SECONDS + 1)
    assert dr.read_meta(d)["errors"] == 2


def test_call_cap_stops_the_read_and_keeps_the_old_map(tmp_path, monkeypatch):
    """Past the cap the next call raises, the read is an error and nothing is published."""
    d = _sdlc(tmp_path)
    _store(d, 5 * TTL * 60)
    before = ds.path(d).read_bytes()
    monkeypatch.setattr(dr, "bounds", lambda: (2, 100.0))
    run = Repo()
    assert dr.refresh_if_due(d, CONFIG, run, NOW)["status"] == "kept"
    assert len(run.calls) == 2 and ds.path(d).read_bytes() == before


def test_bounds_follow_the_watcher_call_timeout(monkeypatch):
    monkeypatch.setenv("SIGMA_WATCH_CALL_TIMEOUT", "40")
    assert dr.bounds() == (60, 20.0)
    monkeypatch.delenv("SIGMA_WATCH_CALL_TIMEOUT")
    assert dr.bounds() == (180, 60.0)


# ------------------------------------------------------------------ offsets

def test_offsets_differ_by_clone_and_are_stable():
    """Two clone paths get different offsets, the same path the same one, all within 10 percent.
    control: return a constant -> the difference assertion fails."""
    a, b = "/work/clone-one", "/work/clone-two"
    assert dr.offset_minutes(a, TTL) == dr.offset_minutes(a, TTL)
    assert dr.offset_minutes(a, TTL) != dr.offset_minutes(b, TTL)
    for p in (a, b, "/x", "/y/z"):
        assert 0 <= dr.offset_minutes(p, TTL) <= 0.10 * TTL


# ------------------------------------------------------------------ the reconcile tick duty

def test_tick_calls_stay_eight_in_the_documented_order():
    wd = _mod("watch_daemon")
    assert [c[0] for c in wd.TICK_CALLS] == [
        "sync.py", "watch.py", "agent_watch.py", "comment_watch.py", "reconcile_tick.py",
        "channel_notify.py", "drift_tick.py", "sync.py"]
    assert [c[1] for c in wd.TICK_CALLS][0] == ("pull",) and [c[1] for c in wd.TICK_CALLS][-1] == ("publish",)


def test_tick_runs_the_duty_and_reports_it(tmp_path):
    d = _sdlc(tmp_path)
    run = Repo()
    out = rt.tick(d, CONFIG, run=run, now=NOW)
    assert "dependency map refreshed" in out and len(run.calls) == 5


def test_tick_duty_exception_never_fails_the_tick(tmp_path, monkeypatch):
    """An exception inside the duty is swallowed; the tick still returns its summary.
    control: let the exception escape (remove the try in _dag_refresh) -> the tick raises."""
    d = _sdlc(tmp_path)
    real = rt._load

    class Boom:
        @staticmethod
        def active(config):
            return True

        @staticmethod
        def refresh_if_due(*a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(rt, "_load", lambda n: Boom if n == "dag_refresh" else real(n))
    assert rt.tick(d, CONFIG, run=Repo(), now=NOW) == ""


def test_tick_without_the_opt_in_makes_no_call_and_writes_nothing(tmp_path):
    """Nothing written for the mode (default) or mode off: zero calls, no new file."""
    for cfg in ({"discovery": {"github": {"repo": "o/r"}}},
                {"discovery": {"github": {"repo": "o/r"}, "dag": {"mode": "off"}}}):
        d = _sdlc(tmp_path / ("a%d" % len(cfg["discovery"])), cfg)
        before = sorted(p.name for p in d.rglob("*"))
        run = Repo()
        assert rt.tick(d, cfg, run=run, now=NOW) == "" and run.calls == []
        assert sorted(p.name for p in d.rglob("*")) == before


# ------------------------------------------------------------------ the first pick of a process

def test_first_pick_refreshes_once_per_process(tmp_path):
    """The first check refreshes a stale map; a second check in the same process makes no call even
    though the map is due again.
    control: remove the once flag -> the second check reads again."""
    d = _sdlc(tmp_path)
    run = Repo()

    class Src:
        _run = staticmethod(run)

    loop._dag_first_pick(d, Src, GH)
    assert len(run.calls) == 5
    (d / "state" / "dag.json").unlink()
    loop._dag_first_pick(d, Src, GH)
    assert len(run.calls) == 5


def test_picks_call_the_first_pick_check():
    """Both entry points make the check (the flag dedupes them)."""
    text = (S / "loop.py").read_text()
    assert len(re.findall(r"^\s+_dag_first_pick\(sdlc_dir, source, config\)", text, re.M)) == 2


def test_first_pick_with_mode_off_touches_nothing(tmp_path):
    cfg = {"discovery": {"dag": {"mode": "off"}}}
    d = _sdlc(tmp_path, cfg)
    before = sorted(p.name for p in d.rglob("*"))
    run = Repo()

    class Src:
        _run = staticmethod(run)

    loop._dag_first_pick(d, Src, cfg)
    assert run.calls == [] and sorted(p.name for p in d.rglob("*")) == before


# ------------------------------------------------------------------ the doctor row

def _live(d, cfg):
    """A fresh watcher heartbeat."""
    sync = doctor._load_loop_script("sync")
    hb = sync.heartbeat_path(d)
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("")
    os.utime(hb, (NOW, NOW))


def _row(d, cfg=CONFIG):
    return doctor._dag_map_row(cfg, str(d), now=NOW)


def test_row_ok_when_fresh(tmp_path):
    d = _sdlc(tmp_path)
    _store(d, 600)
    _live(d, CONFIG)
    r = _row(d)
    assert r["ok"] and r["name"].startswith("dag map: mode on, age 10m, 1 nodes, 0 edges, 0 cycles")


def test_row_fails_when_stale_with_a_live_heartbeat(tmp_path):
    """Clause one alone: old stamp, live heartbeat, no errors -> failing, remedy names the verb.
    control: drop the `live` clause -> the next test (no heartbeat) fails; drop the age clause ->
    the fresh test fails."""
    d = _sdlc(tmp_path)
    _store(d, int(3.5 * TTL * 60))
    _live(d, CONFIG)
    r = _row(d)
    assert not r["ok"] and "dag.py refresh" in r["fix"]


def test_row_ok_when_stale_but_nothing_is_live(tmp_path):
    d = _sdlc(tmp_path)
    _store(d, int(3.5 * TTL * 60))
    assert _row(d)["ok"]


def test_row_fails_after_two_consecutive_errors_and_not_after_one(tmp_path):
    """Clause two alone: a fresh map, no heartbeat, two errors in a row.
    control: threshold 1 -> the one-error assertion fails; threshold 3 -> the two-error one fails."""
    d = _sdlc(tmp_path)
    _store(d, 60)
    dr._write_meta(d, {"last_attempt": NOW, "last_status": "error", "errors": 1})
    assert _row(d)["ok"]
    dr._write_meta(d, {"last_attempt": NOW, "last_status": "error", "errors": 2})
    r = _row(d)
    assert not r["ok"] and "2 refreshes errored" in r["name"]


def test_row_absent_when_mode_off_or_never_opted_in(tmp_path):
    off = {"discovery": {"dag": {"mode": "off"}}}
    d = _sdlc(tmp_path / "a", off)
    assert _row(d, off) is None
    d2 = _sdlc(tmp_path / "b", {})
    assert _row(d2, {}) is None


# ------------------------------------------------------------------ the line and the hook

def _gh_that_aborts(tmp_path):
    mark = tmp_path / "gh-was-called"
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\necho called >> '%s'\nexit 99\n" % mark)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return bindir, mark


def _hook(project, bindir, **extra):
    env = {k: v for k, v in os.environ.items() if k != "SIGMA_RUN_ID"}
    resource_mode.carry(env)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env.update(extra)
    p = subprocess.run(["bash", str(HOOK)], input="{}", capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    return p.stdout


def _project(tmp_path, cfg):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "context").mkdir()
    (d / "context" / "north-star.md").write_text("# n\n")
    cfg = dict(cfg, session_start={"enabled": True})
    (d / "config.json").write_text(json.dumps(cfg))
    return d


def test_hook_prints_the_age_line_and_never_calls_gh(tmp_path):
    """The real hook script prints the line from a STALE map (which a refreshing hook would read
    again) with a `gh` on PATH that aborts on any call: the line is there and gh was never run.
    control: make the hook run `dag.py refresh` -> the stub's marker file appears and this fails."""
    bindir, mark = _gh_that_aborts(tmp_path)
    d = _project(tmp_path, CONFIG)
    _store(d, 5 * TTL * 60, CONFIG, base=time.time())
    out = _hook(tmp_path, bindir)
    assert "Sigma: dependency map: age 5h, 1 nodes, 0 edges" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert not mark.exists()


def test_line_verb_matches_the_hook_without_hooks(tmp_path, capsys):
    d = _sdlc(tmp_path)
    _store(d, 600)
    assert dag_cli.main(["line", "--sdlc", str(d)]) == 0
    assert capsys.readouterr().out.startswith("dependency map: age ")


def test_mode_off_hook_and_line_are_silent_and_identical(tmp_path, capsys):
    """Mode off: no line, and the hook output is byte-identical to a project with no dag key."""
    bindir, mark = _gh_that_aborts(tmp_path)
    off = {"discovery": {"dag": {"mode": "off"}}}
    a = _project(tmp_path / "a", off) if (tmp_path / "a").mkdir() is None else None
    b = _project(tmp_path / "b", {}) if (tmp_path / "b").mkdir() is None else None
    _store(a, 600, {"discovery": {"dag": {"mode": "on"}}}, base=time.time())      # a map exists, but mode is off
    assert dag_cli.main(["line", "--sdlc", str(a)]) == 0 and capsys.readouterr().out == ""
    assert _hook(tmp_path / "a", bindir) == _hook(tmp_path / "b", bindir)
    assert not mark.exists()


def test_refresh_verb_mode_off_prints_off_and_touches_nothing(tmp_path, capsys):
    cfg = {"discovery": {"dag": {"mode": "off"}}}
    d = _sdlc(tmp_path, cfg)
    before = sorted(p.name for p in d.rglob("*"))
    assert dag_cli.main(["refresh", "--force", "--sdlc", str(d)]) == 0
    assert capsys.readouterr().out.strip() == "dag: off" and sorted(p.name for p in d.rglob("*")) == before


# ------------------------------------------------------------------ #1851: local-goals makes no gh call

@pytest.mark.parametrize("cfg", [
    {"discovery": {"dag": {"mode": "on"}}},
    {"discovery": {"source": "local-goals", "dag": {"mode": "on"}}},
    {"discovery": {"source": "local-goals", "dag": {"mode": "shadow"}}},
])
def test_local_goals_first_pick_makes_no_runner_call(tmp_path, cfg):
    """A first pick on a local-goals source (the default) makes zero runner calls even with the dag
    mode written.
    control: remove the `_dag_github_source` guard from `_dag_first_pick` -> 5 calls appear."""
    d = _sdlc(tmp_path, cfg)
    run = Repo()

    class Src:
        _run = staticmethod(run)

    loop._dag_first_pick(d, Src, cfg)
    assert run.calls == []


def test_local_goals_change_check_never_loads_dag_check(tmp_path, monkeypatch):
    """With a stored map and a local-goals source the cheap check is not even loaded.
    control: remove the guard from `_dag_check_pick` -> the stub is loaded."""
    cfg = {"discovery": {"source": "local-goals", "dag": {"mode": "on"}}}
    d = _sdlc(tmp_path, cfg)
    (d / "state" / "dag.json").write_text("{}")
    loaded = []
    monkeypatch.setattr(loop, "_load", lambda name: loaded.append(name))
    loop._dag_check_pick(d, cfg, None)
    assert loaded == []
