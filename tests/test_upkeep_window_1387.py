"""#1387: upkeep on a window. A closed window costs one small file read (no unit listing, no git, no gh, no spawn); an open
window starts ONE detached sweep over every unit; the sweep takes the job lock (busy is a refusal), goes through the existing
per-unit engine child, never stops on a refused unit and lists what the total cap left unreached.

The controls at the bottom break each guard once through the documented gesture (a source substitution, then the same
tests); a guard never seen red is decoration. The engine children here are tiny python processes standing in for the real
per-unit engine, whose own behaviour (lease, backup ref, direct-commit refusal) is pinned by the existing engine tests."""
import fcntl
import json
import pathlib
import subprocess
import sys

import pytest

import test_upkeep_sched as T
import upkeep_support as S

ENV = T.ENV
NOW = 2_000_000
HOUR = 3600


def tree(sdlc):
    return sorted((str(p.relative_to(sdlc)), p.read_bytes() if p.is_file() else None) for p in pathlib.Path(sdlc).rglob("*"))


def window(m=None):
    return m or S.script("feature_upkeep_window")


def stamp_at(sdlc, when, outcome="swept", unreached=()):
    assert window().stamp(sdlc, when, outcome, unreached)


class Counter:
    """Every collaborator the closed window must not touch; any call is recorded."""
    def __init__(self):
        self.calls = []

    def __call__(self, label):
        def hit(*a, **k):
            self.calls.append(label)
            raise AssertionError("called while the window was closed: " + label)
        return hit


def closed_tick(m, sdlc, counter, monkeypatch, **kw):
    monkeypatch.setattr(subprocess, "Popen", counter("Popen"))
    monkeypatch.setattr(subprocess, "run", counter("run"))
    deps = {"list_units": counter("list_units"), "measure": counter("measure"), "spawn": counter("spawn")}
    return m.scheduler_tick(T.open_config(), sdlc, environ=ENV, now=NOW, deps=deps, **kw)


# ------------------------------------------------------------------------------------------ the window arithmetic

def test_window_opens_with_no_stamp_an_old_stamp_or_a_stamp_from_the_future(tmp_path):
    w, sdlc = window(), T.project(tmp_path)
    settings = {"window_hours": 12}
    assert w.is_open(settings, sdlc, NOW) is True                                  # nothing recorded yet
    stamp_at(sdlc, NOW - 11 * HOUR)
    assert w.is_open(settings, sdlc, NOW) is False
    stamp_at(sdlc, NOW - 12 * HOUR)
    assert w.is_open(settings, sdlc, NOW) is True                                  # exactly one window has passed
    stamp_at(sdlc, NOW + 10 * HOUR)
    assert w.is_open(settings, sdlc, NOW) is True                                  # a clock set ahead cannot silence upkeep
    pathlib.Path(sdlc, w.STAMP_REL).write_text("{not json")
    assert w.is_open(settings, sdlc, NOW) is True


def test_window_hours_is_validated_loudly_and_defaults_to_twelve():
    g = S.gate()
    assert g.DEFAULTS["window_hours"] == 12
    for bad in ("12", 0, -1, 1.5, True, None, 8761):
        got = g.read({"upkeep": {"enabled": True, "window_hours": bad}})
        assert got.enabled is False and any("window_hours" in p for p in got.problems), (bad, got.problems)
    ok = g.read({"upkeep": {"enabled": True, "window_hours": 6}})
    assert ok.enabled is True and ok.settings["window_hours"] == 6


# ------------------------------------------------------------------------------------------ window closed: nothing happens

def test_closed_window_tick_lists_nothing_calls_no_git_or_gh_and_spawns_nothing(tmp_path, monkeypatch):
    m, sdlc, counter = T.sched(), T.project(tmp_path), Counter()
    stamp_at(sdlc, NOW - HOUR)
    result = closed_tick(m, sdlc, counter, monkeypatch)
    assert result["decision"] == "window-closed" and counter.calls == [], (result, counter.calls)
    assert json.loads(pathlib.Path(sdlc, "state/upkeep/scheduler.json").read_text())["decision"] == "window-closed"


def test_closed_window_session_start_spawns_nothing_and_writes_nothing(tmp_path, monkeypatch):
    mod = _session()
    sdlc, counter = T.project(tmp_path), Counter()
    stamp_at(sdlc, NOW - HOUR)
    before = tree(sdlc)
    monkeypatch.setattr(subprocess, "Popen", counter("Popen"))
    deps = {"list_units": counter("list_units"), "spawn": counter("spawn")}
    got = mod.start(sdlc, environ=ENV, now=NOW, deps=deps)
    assert got == {"decision": "window-closed", "noted": None} or got.get("decision") == "window-closed", got
    assert counter.calls == [] and tree(sdlc) == before


def _session():
    import importlib.util
    spec = importlib.util.spec_from_file_location("upkeep_session_1387", S.SCRIPTS / "upkeep_session.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------------------------------ gates: byte-identical when closed

@pytest.mark.parametrize("which", ["disabled", "no-machine-variable", "no-ledger"])
def test_a_closed_gate_writes_nothing_and_spawns_nothing(tmp_path, monkeypatch, which):
    m, counter = T.sched(), Counter()
    cfg, env = T.open_config(), ENV
    if which == "disabled":
        cfg["upkeep"]["enabled"] = False
    elif which == "no-machine-variable":
        env = {}
    else:
        cfg["ledger"]["enabled"] = False
    sdlc = T.project(tmp_path, cfg)
    before = tree(sdlc)
    monkeypatch.setattr(subprocess, "Popen", counter("Popen"))
    out = m.scheduler_tick(cfg, sdlc, environ=env, now=NOW, deps={"spawn": counter("spawn"), "list_units": counter("list")})
    assert out.get("closed") is True and counter.calls == [] and tree(sdlc) == before, (out, counter.calls)


# ------------------------------------------------------------------------------------------ window open: one detached sweep

def test_open_window_session_start_spawns_one_detached_sweep_and_does_not_wait(tmp_path, monkeypatch):
    mod, sdlc, spawned = _session(), T.project(tmp_path), []
    got = mod.start(sdlc, environ=ENV, now=NOW, deps={"spawn": lambda *a: spawned.append(a)})
    assert got["decision"] == "started" and len(spawned) == 1 and spawned[0][2] == "*" and spawned[0][-1] is True, spawned
    assert window().read(sdlc)["attempt_at"] == NOW and window().read(sdlc)["last_at"] == 0   # an attempt, not a completed sweep
    # the real spawn: its own session, null stdio, --sweep, and no wait on the handle
    m, seen = T.sched(), {}

    class Handle:
        def poll(self):
            return None

        def wait(self, *a, **k):
            raise AssertionError("the session waited for the sweep")
    monkeypatch.setattr(m.subprocess, "Popen", lambda argv, **kw: seen.update(kw, argv=argv) or Handle())
    cfg = T.open_config()
    m.start_job(sdlc, str(tmp_path), "*", "r", cfg, m.feature_upkeep.read(cfg).settings, {}, True)
    assert "--sweep" in seen["argv"] and seen["start_new_session"] is True
    assert seen["stdin"] == seen["stdout"] == seen["stderr"] == subprocess.DEVNULL


def test_the_hook_runs_start_in_the_background_shape():
    text = (S.SCRIPTS.parent.parent.parent / "hooks" / "session_start.sh").read_text()
    assert 'upkeep_session.py" start "$PROJECT/.sdlc" >/dev/null 2>&1 </dev/null || true' in text


# ------------------------------------------------------------------------------------------ the sweep

def engine(log, words, delay=None):
    """-> command(unit): a tiny child that prints the engine's last-line JSON; `words` maps unit -> outcome (default current)."""
    def command(unit):
        log.append(unit)
        word = words.get(unit, "current")
        line = json.dumps({"outcome": word, "unit_tip": ("b" * 40) if word == "rebased" else None})
        return [sys.executable, "-c", "print(%r)" % line]
    return command


def sweep(m, sdlc, root, units, command, cap=600, **kw):
    return m.run_sweep(T.open_config(), sdlc, str(root), "run1", cap, environ=ENV, command=command,
                       list_units=lambda _s: units, now=NOW, **kw)


def test_sweep_rebases_the_three_behind_leaves_the_current_one_and_stamps_the_window(tmp_path):
    j, sdlc, log = T.job(), T.project(tmp_path), []
    out = sweep(j, sdlc, tmp_path, ["a", "b", "c", "d"], engine(log, {"a": "rebased", "b": "rebased", "d": "rebased"}))
    assert out["outcome"] == "swept" and out["sweep"]["counts"] == {"rebased": 3, "current": 1}, out
    assert log == ["a", "b", "c", "d"] and out["sweep"]["unreached"] == []
    unit = lambda n: json.loads(pathlib.Path(sdlc, "state/upkeep/units/%s.json" % n).read_text())   # noqa: E731
    assert [unit(n)["outcome"] for n in "abcd"] == ["rebased", "rebased", "current", "rebased"]
    stamp = window().read(sdlc)
    assert stamp["outcome"] == "swept" and stamp["last_at"] == NOW and stamp["unreached"] == []
    record = json.loads(pathlib.Path(sdlc, "state/upkeep/job.json").read_text())
    assert record["state"] == "done" and record["outcome"] == "swept" and record["sweep"]["counts"]["rebased"] == 3


def test_a_sweep_after_another_machines_finds_everything_current_and_does_nothing(tmp_path):
    j, sdlc, log = T.job(), T.project(tmp_path), []
    out = sweep(j, sdlc, tmp_path, ["a", "b"], engine(log, {}))
    assert out["sweep"]["counts"] == {"current": 2}, out                            # no rebase, no push, no comment


def test_a_second_sweep_while_the_first_holds_the_lease_backs_off_as_a_refusal(tmp_path):
    j, sdlc, log = T.job(), T.project(tmp_path), []
    lock = pathlib.Path(sdlc, "state/upkeep/job.lock")
    lock.parent.mkdir(parents=True)
    with open(lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = sweep(j, sdlc, tmp_path, ["a"], engine(log, {}))
    assert out == {"outcome": "busy"} and log == [], (out, log)
    assert window().read(sdlc)["outcome"] == "busy"                                 # a refusal, never a failure
    assert window().read(sdlc)["last_at"] == 0 and window().read(sdlc)["attempt_at"] == NOW   # and it never closes the window
    assert not pathlib.Path(sdlc, "state/upkeep/units/a.json").exists()


def test_a_refused_or_parked_unit_does_not_stop_the_others(tmp_path):
    j, sdlc, log = T.job(), T.project(tmp_path), []
    out = sweep(j, sdlc, tmp_path, ["a", "b", "c"], engine(log, {"a": "direct-commits", "b": "conflict", "c": "rebased"}))
    assert log == ["a", "b", "c"] and out["sweep"]["counts"] == {"direct-commits": 1, "conflict": 1, "rebased": 1}, out
    assert out["sweep"]["unreached"] == []


def test_units_over_the_total_cap_are_reported_as_not_reached(tmp_path):
    j, sdlc, log = T.job(), T.project(tmp_path), []
    clock = {"t": 0.0}
    base = engine(log, {})

    def command(unit):
        clock["t"] += 100                                    # each unit "takes" 100 s of the 250 s total
        return base(unit)
    out = sweep(j, sdlc, tmp_path, list("abcde"), command, cap=250, beat=lambda: clock["t"])
    assert log == ["a", "b", "c"] and out["sweep"]["unreached"] == ["d", "e"], (log, out)
    assert window().read(sdlc)["unreached"] == ["d", "e"]
    record = json.loads(pathlib.Path(sdlc, "state/upkeep/job.json").read_text())
    assert record["sweep"]["unreached"] == ["d", "e"]


def test_a_stop_file_leaves_the_rest_unreached_and_is_not_deleted(tmp_path):
    j, sdlc, log = T.job(), T.project(tmp_path), []
    stop = pathlib.Path(sdlc, "state/upkeep.stop")
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("")
    out = sweep(j, sdlc, tmp_path, ["a", "b"], engine(log, {}))
    assert log == [] and out["sweep"]["unreached"] == ["a", "b"] and stop.exists()


def test_a_unit_cut_short_by_a_stop_file_is_itself_listed_unreached(tmp_path):
    j, sdlc, log = T.job(), T.project(tmp_path), []
    stop = pathlib.Path(sdlc, "state/upkeep.stop")
    stop.parent.mkdir(parents=True, exist_ok=True)
    base = engine(log, {})

    def command(unit):
        if unit != "b":
            return base(unit)
        log.append(unit)
        return [sys.executable, "-c", "import pathlib,time; pathlib.Path(%r).write_text(''); time.sleep(30)" % str(stop)]
    out = sweep(j, sdlc, tmp_path, ["a", "b", "c"], command)
    assert log == ["a", "b"] and out["sweep"]["unreached"] == ["b", "c"], (log, out)
    assert window().read(sdlc)["unreached"] == ["b", "c"]


def test_include_and_exclude_lists_still_choose_the_units(tmp_path):
    j, log = T.job(), []
    cfg = T.open_config()
    cfg["upkeep"]["units"] = {"include": ["v*"], "exclude": ["vx"]}
    sdlc = T.project(tmp_path, cfg)
    j.run_sweep(cfg, sdlc, str(tmp_path), "r", 600, environ=ENV, command=engine(log, {}), list_units=lambda _s: ["va", "vx", "w"], now=NOW)
    assert log == ["va"]


def test_a_finished_sweep_is_noted_once_by_the_watcher_under_the_window_key(tmp_path):
    j, m, sdlc, log = T.job(), T.sched(), T.project(tmp_path), []
    sweep(j, sdlc, tmp_path, ["a"], engine(log, {"a": "rebased"}))
    notes = []
    assert m.collect(T.open_config(), sdlc, NOW + 1, append=lambda *a, **k: notes.append(a) or {"ok": True}) in ("noted", "failed")
    assert not pathlib.Path(sdlc, "state/upkeep/units/window.json").exists()


# ------------------------------------------------------------------------------------------ the doctor and the session line

def test_doctor_rows_show_the_window_the_last_sweep_unreached_units_and_warn_after_two_windows(tmp_path):
    m, sdlc = T.sched(), T.project(tmp_path)
    cfg = T.open_config()
    rows = {r[0]: r for r in m.health(cfg, sdlc, now=NOW)}
    assert rows["upkeep window"][1] is True and "12 h window" in rows["upkeep window"][2] and "no sweep yet" in rows["upkeep window"][2]
    stamp_at(sdlc, NOW - 2 * HOUR, "swept", ["x", "y"])
    row = {r[0]: r for r in m.health(cfg, sdlc, now=NOW)}["upkeep window"]
    assert row[1] is True and "unreached: x, y" in row[2] and "no longer schedule" in row[2], row
    stamp_at(sdlc, NOW - 25 * HOUR)
    row = {r[0]: r for r in m.health(cfg, sdlc, now=NOW)}["upkeep window"]
    assert row[1] is False and "older than 2 windows" in row[2], row


def test_session_start_script_help_and_usage():
    p = subprocess.run([sys.executable, str(S.SCRIPTS / "upkeep_session.py"), "--help"], capture_output=True, text=True)
    assert p.returncode == 0 and "start <sdlc_dir>" in p.stdout


# ------------------------------------------------------------------------------------------ attempt versus completed (round 2)

RETRY = {"window_hours": 12, "retry_minutes": 60}


def test_an_attempt_holds_off_only_for_the_retry_window_and_never_closes_the_twelve_hours(tmp_path):
    w, sdlc = window(), T.project(tmp_path)
    assert w.stamp_attempt(sdlc, NOW - 59 * 60, "attempted")
    assert w.is_open(RETRY, sdlc, NOW) is False                                     # inside the retry window
    assert w.is_open(RETRY, sdlc, NOW + 2 * 60) is True                             # 61 minutes after the attempt
    assert w.is_open({"window_hours": 12, "retry_minutes": 5}, sdlc, NOW) is True   # the retry window is configurable
    assert w.retry_minutes_of({}) == 60 and w.retry_minutes_of({"retry_minutes": 0}) == 60
    stamp_at(sdlc, NOW - 2 * HOUR)                                                  # a completed sweep closes the 12 hours
    w.stamp_attempt(sdlc, NOW - 3 * HOUR, "attempted")
    assert w.is_open(RETRY, sdlc, NOW) is False


def test_an_attempt_keeps_the_last_completed_time_and_a_completed_sweep_clears_the_attempt(tmp_path):
    w, sdlc = window(), T.project(tmp_path)
    stamp_at(sdlc, NOW - 13 * HOUR)
    w.stamp_attempt(sdlc, NOW - 5, "attempted")
    got = w.read(sdlc)
    assert got["last_at"] == NOW - 13 * HOUR and got["attempt_at"] == NOW - 5
    stamp_at(sdlc, NOW)
    assert w.read(sdlc)["attempt_at"] is None and w.is_open(RETRY, sdlc, NOW + HOUR) is False


def test_the_scheduler_attempt_then_a_failed_spawn_retries_after_the_retry_window_not_after_twelve_hours(tmp_path):
    m, sdlc, spawned = T.sched(), T.project(tmp_path), []

    def boom(*a):
        raise OSError("no spawn")
    got = m.scheduler_tick(T.open_config(), sdlc, environ=ENV, now=NOW, deps={"spawn": boom})
    assert got["decision"] == "spawn-failed"
    again = m.scheduler_tick(T.open_config(), sdlc, environ=ENV, now=NOW + 30 * 60, deps={"spawn": lambda *a: spawned.append(a)})
    assert again["decision"] == "window-closed" and spawned == []
    later = m.scheduler_tick(T.open_config(), sdlc, environ=ENV, now=NOW + 61 * 60, deps={"spawn": lambda *a: spawned.append(a)})
    assert later["decision"] == "started" and len(spawned) == 1


def test_a_sweep_that_dies_or_is_cut_off_before_its_first_unit_is_an_attempt_not_a_completed_sweep(tmp_path):
    j, sdlc, log = T.job(), T.project(tmp_path), []
    stop = pathlib.Path(sdlc, "state/upkeep.stop")
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("")
    out = sweep(j, sdlc, tmp_path, ["a", "b"], engine(log, {}))
    got = window().read(sdlc)
    assert out["sweep"]["unreached"] == ["a", "b"] and got["last_at"] == 0 and got["attempt_at"] == NOW, got
    sdlc2 = T.project(tmp_path / "two")

    def explode(unit):
        raise RuntimeError("killed")
    with pytest.raises(RuntimeError):
        sweep(j, sdlc2, tmp_path, ["a"], explode)
    got = window().read(sdlc2)
    assert got["outcome"] == "failed" and got["last_at"] == 0 and got["attempt_at"] == NOW, got


def test_a_sweep_whose_first_unit_is_cut_short_by_a_stop_file_is_an_attempt(tmp_path):
    j, sdlc = T.job(), T.project(tmp_path)
    stop = pathlib.Path(sdlc, "state/upkeep.stop")
    stop.parent.mkdir(parents=True, exist_ok=True)
    cmd = lambda unit: [sys.executable, "-c", "import pathlib,time; pathlib.Path(%r).write_text(''); time.sleep(30)" % str(stop)]   # noqa: E731
    sweep(j, sdlc, tmp_path, ["a", "b"], cmd)
    got = window().read(sdlc)
    assert got["unreached"] == ["a", "b"] and got["last_at"] == 0 and got["attempt_at"] == NOW, got


def test_a_sweep_that_finished_with_some_units_unreached_still_closes_the_window(tmp_path):
    j, sdlc, log = T.job(), T.project(tmp_path), []
    clock = {"t": 0.0}
    base = engine(log, {})

    def command(unit):
        clock["t"] += 100
        return base(unit)
    sweep(j, sdlc, tmp_path, list("abcde"), command, cap=250, beat=lambda: clock["t"])
    got = window().read(sdlc)
    assert got["last_at"] == NOW and got["attempt_at"] is None and got["unreached"] == ["d", "e"]


def test_retry_minutes_is_validated_and_defaults_to_sixty():
    g = S.gate()
    assert g.DEFAULTS["retry_minutes"] == 60
    for bad in ("60", 0, -1, 1.5, True, 10081):
        got = g.read({"upkeep": {"enabled": True, "retry_minutes": bad}})
        assert got.enabled is False and any("retry_minutes" in p for p in got.problems), (bad, got.problems)
    assert g.read({"upkeep": {"enabled": True, "retry_minutes": 15}}).settings["retry_minutes"] == 15


# ------------------------------------------------------------------------------------------ break-it controls

def test_control_an_attempt_that_closes_the_window_is_caught(tmp_path):
    broken = S.script("feature_upkeep_window", ('"last_at": prior.get("last_at") or 0, "attempt_at": int(now)', '"last_at": int(now), "attempt_at": int(now)'))
    sdlc = T.project(tmp_path)
    broken.stamp_attempt(sdlc, NOW - 2 * HOUR, "attempted")
    assert broken.is_open(RETRY, sdlc, NOW) is False                                # the broken version silences 12 hours
    sdlc2 = T.project(tmp_path / "real")
    window().stamp_attempt(sdlc2, NOW - 2 * HOUR, "attempted")
    assert window().is_open(RETRY, sdlc2, NOW) is True                              # the real one re-opens after the retry window


def test_control_window_check_removed_is_caught(tmp_path, monkeypatch):
    broken = S.script("feature_upkeep_sched", ("if not window.is_open(settings, sdlc_dir, now):", "if False:"))
    sdlc, counter = T.project(tmp_path), Counter()
    stamp_at(sdlc, NOW - HOUR)
    with pytest.raises(AssertionError, match="called while the window was closed"):
        closed_tick(broken, sdlc, counter, monkeypatch)


def test_control_busy_refusal_removed_is_caught(tmp_path):
    broken = S.script("feature_upkeep_job", ('            prior = window.read(sdlc_dir) or {}\n            window.stamp_attempt(sdlc_dir, stamp_now(), "busy", prior.get("unreached") or [])\n            return {"outcome": "busy"}', "            pass"))
    sdlc, log = T.project(tmp_path), []
    lock = pathlib.Path(sdlc, "state/upkeep/job.lock")
    lock.parent.mkdir(parents=True)
    with open(lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = sweep(broken, sdlc, tmp_path, ["a"], engine(log, {}))
    assert out.get("outcome") != "busy" and log == ["a"]


def test_control_unreached_made_silent_is_caught(tmp_path):
    broken = S.script("feature_upkeep_job", ("unreached = names[index:]", "unreached = []"))
    log, clock = [], {"t": 0.0}
    base = engine(log, {})

    def command(unit):
        clock["t"] += 100
        return base(unit)
    out = sweep(broken, T.project(tmp_path), tmp_path, list("abcde"), command, cap=250, beat=lambda: clock["t"])
    assert out["sweep"]["unreached"] == []                                          # the real code lists d and e


def test_control_gate_removed_is_caught(tmp_path, monkeypatch):
    broken = S.script("feature_upkeep_sched", ('@feature_upkeep.gated("machine")\ndef scheduler_tick', "def scheduler_tick"))
    cfg = T.open_config()
    cfg["upkeep"]["enabled"] = False
    sdlc, spawned = T.project(tmp_path, cfg), []
    broken.scheduler_tick(cfg, sdlc, environ=ENV, now=NOW, deps={"spawn": lambda *a: spawned.append(a)})
    assert spawned, "with the gate removed a disabled project spawns"
