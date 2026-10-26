"""#2019: GitHub calls counted by component at the runners that spawn `gh`, and cost per goal / lane read from
existing phase rows. Controls run red once each, clause alone (see the `control:` notes)."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import types

import pytest

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_t2019", S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def gc(tmp_path, monkeypatch):
    m = _load("gh_calls")
    monkeypatch.setenv("SIGMA_SDLC_DIR", str(tmp_path / "sd"))
    monkeypatch.delenv("SIGMA_GH_COMPONENT", raising=False)
    m._counts.clear()
    del m._stack[:]
    yield m
    m._counts.clear()
    del m._stack[:]


def _rows(tmp_path):
    p = tmp_path / "sd" / "state" / "gh-calls.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _fake_gh(tmp_path, code=0):
    p = tmp_path / "fakegh"
    p.write_text("#!/bin/sh\necho ok\nexit %d\n" % code)
    p.chmod(0o755)
    return str(p)


def test_classify_buckets(gc):
    assert gc.classify(["api", "repos/o/r/issues/1"]) == "rest"
    assert gc.classify(["api", "graphql", "-f", "query=x"]) == "graphql"
    assert gc.classify(["issue", "view", "3"]) == "graphql"
    assert gc.classify(["api", "search/issues?q=x"]) == "search"
    assert gc.classify(["search", "issues", "x"]) == "search"
    assert gc.classify([]) == "rest"


def test_counts_by_component_and_innermost_wins(gc, monkeypatch):
    gc.count(["api", "a"])
    with gc.component("outer"):
        gc.count(["api", "a"])
        with gc.component("inner"):
            gc.count(["api", "graphql"])
        gc.count(["api", "a"])
    monkeypatch.setenv("SIGMA_GH_COMPONENT", "envcomp")
    gc.count(["api", "a"])
    by = {(k[1], k[2]): n for k, n in gc._counts.items()}
    assert by[("outer", "rest")] == 2 and by[("inner", "graphql")] == 1 and by[("envcomp", "rest")] == 1
    assert sum(by.values()) == 5


def test_hourly_rollover_flushes_the_old_hour(gc, tmp_path):
    t = [3600.0 * 10 + 5]
    gc._clock = lambda: t[0]
    gc.count(["api", "a"])
    t[0] += 3600
    gc.count(["api", "a"])
    rows = _rows(tmp_path)
    assert [r["hour"] for r in rows] == [36000] and rows[0]["n"] == 1
    assert len(gc._counts) == 1


def test_rotation_at_one_megabyte(gc, tmp_path):
    """control: ROTATE_BYTES check removed -> no `.1` file -> red."""
    d = tmp_path / "sd" / "state"
    d.mkdir(parents=True)
    (d / "gh-calls.jsonl").write_bytes(b"x" * gc.ROTATE_BYTES)
    gc.count(["api", "a"])
    gc.flush()
    assert (d / "gh-calls.jsonl.1").stat().st_size == gc.ROTATE_BYTES
    assert len(_rows(tmp_path)) == 1


def test_no_arguments_are_recorded(gc, tmp_path):
    """control: write the argv into the line -> red."""
    gc.count(["issue", "comment", "7", "--body", "SECRET-BODY-TEXT"])
    gc.flush()
    raw = (tmp_path / "sd" / "state" / "gh-calls.jsonl").read_text()
    assert "SECRET" not in raw and "comment" not in raw and "7" not in json.loads(raw.splitlines()[0]).get("args", "")
    assert set(json.loads(raw.splitlines()[0])) == {"hour", "component", "bucket", "n"}


def test_write_failure_is_swallowed(gc, tmp_path, monkeypatch):
    blocker = tmp_path / "sd"
    blocker.write_text("a file where a directory must go")
    gc.count(["api", "a"])
    gc.flush()                                   # must not raise
    gc.count(["api", "a"])


def test_no_sdlc_dir_means_no_write(gc, tmp_path, monkeypatch):
    monkeypatch.delenv("SIGMA_SDLC_DIR")
    monkeypatch.chdir(tmp_path)
    gc.count(["api", "a"])
    gc.flush()
    assert not (tmp_path / ".sdlc").exists()


def test_sources_run_gh_counts_once_and_a_failure_counts(gc, tmp_path):
    src = _load("sources")
    src.gh_calls._counts.clear()
    src._run_gh(["api", "repos/o/r/issues/1"], binary=_fake_gh(tmp_path))
    assert sum(src.gh_calls._counts.values()) == 1
    with pytest.raises(RuntimeError):
        src._run_gh(["api", "repos/o/r/issues/1"], binary=_fake_gh(tmp_path, code=1))
    assert sum(src.gh_calls._counts.values()) == 2


def test_gh_api_runners_count(gc, monkeypatch):
    """control: hook only in sources -> every gh_api runner below stays at zero -> red."""
    api = _load("gh_api")
    api.gh_calls._counts.clear()
    done = types.SimpleNamespace(returncode=0, stdout="o", stderr="")
    monkeypatch.setattr(api.subprocess, "run", lambda *a, **k: done)
    api._default_run(["api", "x"])
    assert sum(api.gh_calls._counts.values()) == 1
    api.run_gh(["api", "x"])
    assert sum(api.gh_calls._counts.values()) == 2
    api.bounded_runner()(["api", "x"])
    assert sum(api.gh_calls._counts.values()) == 3


def test_run_none_sweep_is_counted_under_auto_unpark(gc, tmp_path, monkeypatch):
    """control: `with component("auto_unpark")` removed from the sweep -> component is the script name -> red."""
    loop = _load("loop")
    fake = _fake_gh(tmp_path)
    loop.sources.gh_calls._counts.clear()

    class Stub:
        @staticmethod
        def sweep_unpark(sdlc_dir, config, apply=True, run=None):
            assert run is None                       # the path no injected runner sees
            loop.sources._run_gh(["api", "x"], binary=fake)
            return {"unparked": []}

    monkeypatch.setattr(loop, "_load", lambda name: Stub)
    monkeypatch.setattr(loop.sources, "_auto_unpark", lambda config: "on")
    loop._auto_unpark_sweep(str(tmp_path), {}, run=None)
    comps = {k[1] for k in loop.sources.gh_calls._counts}
    assert comps == {"auto_unpark"}


def test_report_verb_and_exit_codes(gc, tmp_path, capsys):
    gc.count(["api", "a"]); gc.count(["api", "graphql"])
    gc.flush()
    loop = _load("loop")
    assert loop.main(["loop.py", "gh-calls-report", str(tmp_path / "sd")]) == 0
    out = capsys.readouterr().out
    assert "component" in out and " 2" in out
    assert loop.main(["loop.py", "gh-calls-report", str(tmp_path / "none")]) == 0
    assert "not measured" in capsys.readouterr().out
    assert gc.main(["gh_calls.py", "report"]) == 2


def test_report_hours_window(gc, tmp_path):
    p = tmp_path / "sd" / "state"
    p.mkdir(parents=True)
    old = {"hour": 3600, "component": "old", "bucket": "rest", "n": 9}
    new = {"hour": 3600 * 100, "component": "new", "bucket": "rest", "n": 1}
    (p / "gh-calls.jsonl").write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n" + "garbage\n")
    out = gc.report(tmp_path / "sd", hours=2, now=3600 * 100 + 10)
    assert "new" in out and "old" not in out


# ------------------------------------------------------------------ cost per goal / lane

def _phase(tmp_path, goal, cost, state="end"):
    d = tmp_path / "sd" / "state" / "log"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / (goal + ".jsonl"), "a") as f:
        f.write(json.dumps({"kind": "phase", "state": state, "cost": cost, "ts": "2026-01-01T00:00:00Z"}) + "\n")


def test_cost_per_goal_and_lane(tmp_path):
    gcost = _load("goal_cost")
    g = tmp_path / "sd" / "goals"
    g.mkdir(parents=True)
    (g / "g1.md").write_text("---\nlane: small\n---\nbody\n")
    (g / "g2.md").write_text("---\nlane: small\n---\n")
    _phase(tmp_path, "g1", "$1.25"); _phase(tmp_path, "g1", "$0.75"); _phase(tmp_path, "g1", "$9.00", state="start")
    _phase(tmp_path, "g2", "$2.00")
    _phase(tmp_path, "g3", "unavailable")
    goals = gcost.goal_costs(tmp_path / "sd")
    assert goals["g1"]["usd"] == 2.0 and goals["g1"]["priced"] == 2 and goals["g1"]["lane"] == "small"
    assert goals["g3"]["usd"] is None and goals["g3"]["lane"] == "not recorded"
    lanes = gcost.lane_costs(goals)
    assert lanes["small"]["usd"] == 4.0 and lanes["small"]["measured"] == 2
    assert lanes["not recorded"]["usd"] is None
    out = gcost.report(tmp_path / "sd")
    assert "$2.00" in out and "not measured" in out and "$0.00" not in out


def test_cost_not_measured_when_absent(tmp_path):
    gcost = _load("goal_cost")
    assert "not measured" in gcost.report(tmp_path / "empty")
    assert gcost.main(["goal_cost.py", "report"]) == 2


def test_cost_verb_in_loop(tmp_path, capsys):
    _phase(tmp_path, "g1", "$1.00")
    loop = _load("loop")
    assert loop.main(["loop.py", "cost-report", str(tmp_path / "sd")]) == 0
    assert "$1.00" in capsys.readouterr().out
