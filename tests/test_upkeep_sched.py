"""#923: the upkeep scheduler (decision step, detached bounded job, notes, doctor rows). Fake clocks and fake processes; no model,
no network. The one real process is a tiny python child under the bounded runner."""
import copy
import fcntl
import json
import os
import pathlib
import subprocess
import sys

import upkeep_support as S

ENV = {"SIGMA_UPKEEP_JOB": "1"}
H40 = "a" * 40


def _real(stem):
    """A real module object (functions see the patches a test makes), loaded by path under its own name."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(stem + "_923", S.SCRIPTS / (stem + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sched(edit=None):
    return S.script("feature_upkeep_sched", edit)


def job(edit=None):
    return S.script("feature_upkeep_job", edit)


def open_config():
    cfg = copy.deepcopy(S.template_cfg())
    cfg["upkeep"]["enabled"] = True
    cfg.setdefault("ledger", {})["enabled"] = True
    cfg["ledger"]["actor"] = "robot"
    cfg.setdefault("work", {})["base"] = "main"
    return cfg


def project(tmp_path, cfg=None):
    sdlc = tmp_path / "proj" / ".sdlc"
    sdlc.mkdir(parents=True, exist_ok=True)
    (sdlc / "config.json").write_text(json.dumps(cfg or open_config()), encoding="utf-8")
    return str(sdlc)


def drift_of(m, behind):
    d = m._sibling("feature_upkeep_drift")
    return d.Drift(behind, behind, 3, False, ())


def tick(m, cfg, sdlc, units, behind, spawned, now=1_000_000, **kw):
    deps = {"list_units": lambda _s: units, "measure": lambda n, _d: drift_of(m, behind[n]),
            "spawn": lambda *a: spawned.append(a[2])}
    return m.scheduler_tick(cfg, sdlc, environ=ENV, now=now, deps=deps, **kw)


# ------------------------------------------------------------------------------------------ the decision step

def test_open_tick_picks_one_due_unit_the_most_behind(tmp_path):
    m, sdlc, spawned = sched(), project(tmp_path), []
    result = tick(m, open_config(), sdlc, ["a", "b", "c"], {"a": 5, "b": 9, "c": 2}, spawned)
    assert spawned == ["b"], spawned
    assert result["decision"] == "started" and result["unit"] == "b"
    assert json.loads(pathlib.Path(sdlc, "state/upkeep/scheduler.json").read_text())["decision"] == "started"
    assert pathlib.Path(sdlc, "state/upkeep/units/b.json").is_file()          # the attempt was recorded BEFORE the work


def test_an_unmeasurable_unit_sorts_after_every_measured_one(tmp_path):
    m, sdlc, spawned = sched(), project(tmp_path), []
    unknown = m._sibling("feature_upkeep_drift").UNKNOWN
    tick(m, open_config(), sdlc, ["a", "b"], {"a": unknown, "b": 1}, spawned)
    assert spawned == ["b"], spawned


def test_the_time_budget_stops_measuring_and_the_next_tick_resumes_after_it(tmp_path):
    m, sdlc = sched(), project(tmp_path)
    clock = {"t": 0.0}
    seen = []

    def measure(name, _doc):
        seen.append(name)
        clock["t"] += 10
        return drift_of(m, 0)                                  # nothing behind: nothing is due, nothing is recorded

    deps = {"list_units": lambda _s: ["a", "b", "c", "d"], "measure": measure, "spawn": lambda *a: None}
    kw = dict(environ=ENV, now=1_000_000, deps=deps, budget=15, clock=lambda: clock["t"])
    first = m.scheduler_tick(open_config(), sdlc, **kw)
    assert first["decision"] == "idle" and seen == ["a", "b"] and first["out_of_time"] is True, (first, seen)
    clock["t"] = 0.0
    m.scheduler_tick(open_config(), sdlc, **kw)
    assert seen[2:4] == ["c", "d"], seen


def test_include_and_exclude_choose_the_units_and_exclude_wins():
    m = sched()
    assert m.selected(["Voice", "voice-2", "x"], ["voice*"], []) == ["Voice", "voice-2"]
    assert m.selected(["Voice", "voice-2", "x"], ["*"], ["VOICE-2"]) == ["Voice", "x"]


def test_a_unit_in_its_cooldown_is_never_measured(tmp_path):
    m, sdlc = sched(), project(tmp_path)
    state = m._sibling("feature_upkeep_state")
    assert state.record_attempt(sdlc, "a", 1_000_000, "backstop").ok
    asked = []
    deps = {"list_units": lambda _s: ["a"], "measure": lambda n, d: asked.append(n), "spawn": lambda *a: None}
    result = m.scheduler_tick(open_config(), sdlc, environ=ENV, now=1_000_010, deps=deps)
    assert asked == [] and result["decision"] == "idle", (asked, result)


def test_a_live_job_means_no_new_unit_is_started(tmp_path):
    m, sdlc, spawned = sched(), project(tmp_path), []
    lock = pathlib.Path(sdlc, "state/upkeep/job.lock")
    lock.parent.mkdir(parents=True)
    pathlib.Path(sdlc, "state/upkeep/job.heartbeat").touch()
    with open(lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = tick(m, open_config(), sdlc, ["a"], {"a": 4}, spawned, now=int(os.stat(lock).st_mtime))
    assert spawned == [] and result["decision"] == "job-running", (spawned, result)


def test_a_failed_start_is_recorded_and_costs_the_cooldown(tmp_path):
    m, sdlc = sched(), project(tmp_path)

    def refuse(*_a):
        raise OSError("no")
    deps = {"list_units": lambda _s: ["a"], "measure": lambda n, d: drift_of(m, 3), "spawn": refuse}
    result = m.scheduler_tick(open_config(), sdlc, environ=ENV, now=1_000_000, deps=deps)
    doc = json.loads(pathlib.Path(sdlc, "state/upkeep/units/a.json").read_text())
    assert result["decision"] == "spawn-failed" and doc["outcome"] == "failed" and doc["consecutive_failures"] == 1, doc


def test_windows_is_refused_and_the_refusal_is_recorded_once(tmp_path, monkeypatch):
    m, sdlc, spawned = sched(), project(tmp_path), []
    monkeypatch.setattr(m._sibling("bounded_run"), "group_refusal", lambda: "no process groups here")
    first = tick(m, open_config(), sdlc, ["a"], {"a": 4}, spawned)
    status = pathlib.Path(sdlc, "state/upkeep/scheduler.json")
    written = status.read_bytes()
    os.utime(status, ns=(1, 1))
    second = tick(m, open_config(), sdlc, ["a"], {"a": 4}, spawned, now=1_000_900)
    assert first["decision"] == second["decision"] == "refused" and spawned == []
    assert status.read_bytes() == written and os.stat(status).st_mtime_ns == 1, "the refusal was written twice"
    assert not pathlib.Path(sdlc, "state/upkeep/units").exists()


# ------------------------------------------------------------------------------------------ the gate, in the real tick

def _run_watch_tick(tmp_path, name, cfg, stub):
    wd = _real("watch_daemon")
    d = tmp_path / name / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    p = wd.paths(str(d))
    p.heartbeat.touch()
    events = []
    wd.touch_heartbeat = lambda paths: events.append("hb")
    wd.run_call = lambda *a, **k: events.append("call") or ""
    if stub:
        wd.scheduler_step = lambda *a, **k: None
    wd.tick(p, str(d), "120", 1, cfg)
    tree = sorted(str(x.relative_to(d)) for x in d.rglob("*"))
    log = [line for line in p.log.read_text().splitlines() if "tick #" not in line]
    return events, tree, log


def test_gate_closed_the_watch_tick_is_byte_identical_and_spawns_nothing(tmp_path, monkeypatch):
    def no_spawn(*a, **k):
        raise AssertionError("a process was started with the gate closed")
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    cfg = {"ledger": {"enabled": True, "actor": "robot"}}
    assert _run_watch_tick(tmp_path, "with", cfg, False) == _run_watch_tick(tmp_path, "without", cfg, True)
    template = S.template_cfg()                        # the shipped block, still disabled
    template.setdefault("ledger", {})["enabled"] = True
    monkeypatch.setenv("SIGMA_UPKEEP_JOB", "1")
    assert _run_watch_tick(tmp_path, "with2", template, False) == _run_watch_tick(tmp_path, "without2", template, True)


def test_an_exception_in_the_step_never_skips_the_eight_calls_and_logs_the_class_only(tmp_path):
    wd = _real("watch_daemon")
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    p = wd.paths(str(d))
    wd.run_call = lambda *a, **k: ""

    def boom():
        raise KeyError("secret-path-in-message")
    wd._scheduler = boom
    wd.tick(p, str(d), "120", 1)
    log = p.log.read_text()
    assert "scheduler step failed (non-fatal): KeyError" in log and "secret-path" not in log


# ------------------------------------------------------------------------------------------ the job process

def test_the_job_is_detached_with_null_stdio_an_allowlisted_environment_and_its_own_session(tmp_path, monkeypatch):
    m, sdlc = sched(), project(tmp_path)
    seen = {}

    class Fake:
        def poll(self):
            return None

    def popen(argv, **kw):
        seen.update(kw, argv=argv)
        return Fake()
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    m.start_job(sdlc, str(tmp_path / "proj"), "a", "r1", open_config(), m.feature_upkeep.read(open_config()).settings,
                {"PATH": "/bin", "GH_TOKEN": "t", "HOME": "/h", "SECRET_THING": "x", "SIGMA_RUN_ID": "run"})
    assert seen["stdin"] == seen["stdout"] == seen["stderr"] == subprocess.DEVNULL, seen
    assert seen["start_new_session"] is True and seen["close_fds"] is True
    env = seen["env"]
    assert "SECRET_THING" not in env and "SIGMA_RUN_ID" not in env and env["GH_TOKEN"] == "t"
    assert env["SIGMA_UPKEEP_JOB"] == "1" and env["SIGMA_WATCH_CALL_TIMEOUT"] == str(60 * 60 + 900)
    assert os.path.isabs(seen["argv"][seen["argv"].index("--sdlc") + 1])


def test_the_heartbeat_comes_from_the_poll_clock_not_a_thread():
    j, touched, now = job(), [], [0.0]
    clock = j.BeatingClock(lambda: touched.append(now[0]), every=5, base=lambda: now[0])
    for t in (0, 1, 4, 5, 6, 11, 12):
        now[0] = t
        clock()
    assert touched == [0, 5, 11], touched


def ok_command(outcome="rebased", tip=H40):
    return [sys.executable, "-c", "import json;print(json.dumps({'outcome': %r, 'unit_tip': %r}))" % (outcome, tip)]


def run(j, tmp_path, sdlc, command, cap=30):
    return j.run_job(open_config(), sdlc, str(tmp_path / "proj"), "voice", "r1", cap, environ=ENV, command=command)


def test_the_job_records_how_the_pass_ended_and_frees_its_lock(tmp_path):
    j, sdlc = job(), project(tmp_path)
    result = run(j, tmp_path, sdlc, ok_command())
    record = json.loads(pathlib.Path(sdlc, "state/upkeep/job.json").read_text())
    assert result["outcome"] == "rebased" and record["state"] == "done" and record["outcome"] == "rebased", record
    assert record["noted"] is False and pathlib.Path(sdlc, "state/upkeep/job.heartbeat").is_file()
    assert j.sched.job_alive(sdlc, 10 ** 10) == "idle"
    assert json.loads(pathlib.Path(sdlc, "state/upkeep/units/voice.json").read_text())["unit_tip"] == H40


def test_a_crashed_pass_is_a_recorded_failure_not_silence(tmp_path):
    j, sdlc = job(), project(tmp_path)
    run(j, tmp_path, sdlc, [sys.executable, "-c", "raise SystemExit(3)"])
    record = json.loads(pathlib.Path(sdlc, "state/upkeep/job.json").read_text())
    assert record["state"] == "done" and record["outcome"] == "failed", record


def test_the_wall_clock_cap_stops_the_pass(tmp_path):
    j, sdlc = job(), project(tmp_path)
    result = run(j, tmp_path, sdlc, [sys.executable, "-c", "import time; time.sleep(60)"], cap=1)
    assert result["outcome"] == "timeout", result
    assert json.loads(pathlib.Path(sdlc, "state/upkeep/units/voice.json").read_text())["outcome"] == "failed"


def test_either_stop_file_stops_the_job_and_neither_is_deleted(tmp_path):
    j, sdlc = job(), project(tmp_path)
    for rel in ("state/upkeep.stop", "state/watch.stop"):
        stop = pathlib.Path(sdlc, rel)
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("")
        assert run(j, tmp_path, sdlc, ok_command())["outcome"] == "stopped", rel
        assert stop.exists()
        stop.unlink()


def test_a_second_job_does_not_run_while_one_holds_the_lock(tmp_path):
    j, sdlc = job(), project(tmp_path)
    lock = pathlib.Path(sdlc, "state/upkeep/job.lock")
    lock.parent.mkdir(parents=True)
    with open(lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run(j, tmp_path, sdlc, ok_command()) == {"outcome": "busy"}
    assert not pathlib.Path(sdlc, "state/upkeep/job.json").exists()


def test_the_engine_is_handed_no_goal_so_a_filing_is_addressed_to_nobody(tmp_path, capsys):
    j, sdlc, calls = job(), project(tmp_path), []

    def fake(sdlc_dir, config, goal, unit, **kw):
        calls.append((goal, unit))
        return {"outcome": "conflict", "after": H40}
    line = j.run_engine(open_config(), sdlc, str(tmp_path), "voice", environ=ENV, upkeep=fake)
    assert calls == [(None, "voice")] and line == {"outcome": "conflict", "unit_tip": H40}
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == line


# ------------------------------------------------------------------------------------------ the notes

def finished(m, sdlc, **extra):
    doc = {"schema": m.RECORD_SCHEMA, "state": "done", "unit": "Voice", "outcome": "rebased", "unit_tip": H40, "noted": False}
    doc.update(extra)
    assert m.write_json(sdlc, m.RECORD_REL, doc)


def test_the_watcher_writes_one_unaddressed_note_as_the_configured_actor(tmp_path, monkeypatch):
    m, sdlc, calls = sched(), project(tmp_path), []
    monkeypatch.setenv("USER", "shelluser")
    cfg = open_config()
    del cfg["ledger"]["actor"]
    finished(m, sdlc)

    def append(sdlc_dir, kind, goal, config=None, **fields):
        calls.append((kind, goal, config["ledger"]["actor"], fields))
        return {"id": "x"}
    assert m.collect(cfg, sdlc, 10 ** 10, append) == "noted"
    assert m.collect(cfg, sdlc, 10 ** 10, append) == "none"                     # once
    assert calls == [("note", "upkeep-voice", "shelluser", {"ref": "upkeep:rebased:" + H40[:12]})], calls
    assert "to" not in calls[0][3]


def test_a_note_that_cannot_be_written_is_tried_a_bounded_number_of_times(tmp_path):
    m, sdlc, calls = sched(), project(tmp_path), []
    finished(m, sdlc)
    for _ in range(6):
        m.collect(open_config(), sdlc, 10 ** 10, lambda *a, **k: calls.append(1))
    assert len(calls) == m.NOTE_ATTEMPTS, calls


def running_record(m, sdlc):
    doc = {"schema": m.RECORD_SCHEMA, "state": "running", "unit": "Voice", "run_id": "1-2", "noted": False, "started_at": 5}
    assert m.write_json(sdlc, m.RECORD_REL, doc)


def test_a_job_killed_before_its_final_record_gets_a_failed_note_on_the_next_tick(tmp_path):
    m, sdlc, calls = sched(), project(tmp_path), []
    running_record(m, sdlc)                                    # the job process died: record stuck at running, lock free
    append = lambda sdlc_dir, kind, goal, config=None, **f: calls.append((kind, goal, f)) or {"id": "x"}
    assert m.collect(open_config(), sdlc, 10 ** 10, append) == "noted"
    assert calls == [("note", "upkeep-voice", {"ref": "upkeep:failed:none"})], calls
    record = json.loads(pathlib.Path(sdlc, m.RECORD_REL).read_text())
    assert record["state"] == "done" and record["outcome"] == "failed" and record["noted"] is True, record
    assert m.collect(open_config(), sdlc, 10 ** 10, append) == "none" and len(calls) == 1      # once


def test_a_running_record_with_the_lock_held_is_a_live_job_not_a_killed_one(tmp_path):
    m, sdlc, calls = sched(), project(tmp_path), []
    running_record(m, sdlc)
    lock = pathlib.Path(sdlc, m.LOCK_REL)
    lock.parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(sdlc, m.BEAT_REL).touch()
    with open(lock, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        got = m.collect(open_config(), sdlc, int(pathlib.Path(sdlc, m.BEAT_REL).stat().st_mtime),
                        lambda *a, **k: calls.append(1))
    assert got == "running" and not calls, (got, calls)


def test_the_budget_is_checked_inside_the_unit_not_only_between_units(tmp_path):
    m, sdlc = sched(), project(tmp_path)
    seen = []

    def measure(name, _doc):
        seen.append(name)
        if name == "b":
            raise m.OutOfTime()                                # the unit's own measurement found the budget spent
        return drift_of(m, 0)

    deps = {"list_units": lambda _s: ["a", "b", "c"], "measure": measure, "spawn": lambda *a: None}
    out = m.scheduler_tick(open_config(), sdlc, environ=ENV, now=1_000_000, deps=deps)
    assert seen == ["a", "b"] and out["out_of_time"] is True, (seen, out)
    assert json.loads(pathlib.Path(sdlc, m.STATUS_REL).read_text())["cursor"] == "a"           # b is retried first next tick


def test_make_measure_stops_before_the_git_reads_when_the_deadline_has_passed(tmp_path):
    m, sdlc = sched(), project(tmp_path)
    cfg = open_config()
    measure = m.make_measure(cfg, m.feature_upkeep.read(cfg).settings, sdlc, str(tmp_path), 1_000_000, expired=lambda: True)
    try:
        measure("Voice", None)
    except m.OutOfTime:
        return
    raise AssertionError("measure ran past an expired budget")


# ------------------------------------------------------------------------------------------ the doctor's rows

def test_health_rows_show_liveness_and_readiness_only_when_the_block_is_on(tmp_path):
    m, sdlc = sched(), project(tmp_path)
    assert m.health(S.template_cfg(), sdlc, 1_000_000) == []
    rows = dict((label, (ok, detail)) for label, ok, detail in m.health(open_config(), sdlc, 1_000_000))
    assert rows["upkeep scheduler liveness"][0] is False and "no tick" in rows["upkeep scheduler liveness"][1]
    assert rows["upkeep scheduler readiness"][0] is False and "machine" in rows["upkeep scheduler readiness"][1]
    m.write_json(sdlc, m.STATUS_REL, {"schema": m.STATUS_SCHEMA, "decision": "idle"})
    mtime = os.stat(pathlib.Path(sdlc, m.STATUS_REL)).st_mtime
    fresh = dict((l, o) for l, o, _ in m.health(open_config(), sdlc, mtime + 10))
    stale = dict((l, o) for l, o, _ in m.health(open_config(), sdlc, mtime + 10 ** 6))
    assert fresh["upkeep scheduler liveness"] is True and stale["upkeep scheduler liveness"] is False


def test_a_hung_job_reads_as_hung_and_a_dead_one_as_idle(tmp_path):
    m, sdlc = sched(), project(tmp_path)
    lock, beat = pathlib.Path(sdlc, m.LOCK_REL), pathlib.Path(sdlc, m.BEAT_REL)
    lock.parent.mkdir(parents=True)
    beat.touch()
    now = os.stat(beat).st_mtime
    assert m.job_alive(sdlc, now + 10 ** 6) == "idle"                   # the lock is free: whatever held it is gone
    with open(lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert m.job_alive(sdlc, now + 1) == "running"
        assert m.job_alive(sdlc, now + m.BEAT_STALE_SECONDS + 5) == "hung"


# ------------------------------------------------------------------------------------------ the git floor

def test_git_versions_with_vendor_suffixes_parse():
    pf = _preflight()
    table = {"git version 2.39.5 (Apple Git-154)": (2, 39, 5), "git version 2.43.0.windows.1": (2, 43, 0),
             "git version 2.40.1.vfs.0.0": (2, 40, 1), "git version 2.45.0-rc1": (2, 45, 0), "git version 2.34": (2, 34, 0),
             "nonsense": None, "": None}
    assert {k: pf.parse_git_version(k) for k in table} == table


def _preflight():
    import importlib.util
    path = S.ROOT / "skills" / "sigma-init" / "scripts" / "preflight.py"
    spec = importlib.util.spec_from_file_location("preflight_923", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_git_floor_refuses_below_2_34_and_exactly_2_35_0():
    pf = _preflight()
    verdicts = {}
    for text in ("2.33.9", "2.34.0", "2.35.0", "2.35.1", "2.54.0 (Apple Git-157)"):
        row = pf.check_git_floor(lambda argv, cwd, timeout, t=text: (0, "git version " + t), lambda _n: "/usr/bin/git", 5)
        verdicts[text] = row["ok"]
    assert verdicts == {"2.33.9": False, "2.34.0": True, "2.35.0": False, "2.35.1": True, "2.54.0 (Apple Git-157)": True}


def test_the_floor_check_is_gated_in_the_preflight_list():
    pf = _preflight()
    runner = lambda argv, cwd=None, timeout=None: (0, "true" if "rev-parse" in argv and "--is-inside-work-tree" in argv
                                                 else "git version 2.40.0" if "--version" in argv else "x")
    plain = pf.preflight(".", {}, runner=runner, which=lambda _n: "/bin/git")
    asked = pf.preflight(".", {}, runner=runner, which=lambda _n: "/bin/git", git_floor=True)
    assert [c["id"] for c in plain] == ["git"]
    assert [c["id"] for c in asked] == ["git", "git-floor"]


# ------------------------------------------------------------------------------------------ break-it controls

def test_control_a_stdout_left_inherited_is_caught(tmp_path, monkeypatch):
    broken = sched(("stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True",
                    "stderr=subprocess.DEVNULL, start_new_session=True"))
    seen = {}
    monkeypatch.setattr(broken.subprocess, "Popen", lambda argv, **kw: seen.update(kw) or type("F", (), {"poll": lambda s: None})())
    broken.start_job(project(tmp_path), str(tmp_path), "a", "r", open_config(), broken.feature_upkeep.read(open_config()).settings, {})
    assert seen.get("stdout") != subprocess.DEVNULL


def test_control_b_ranking_least_behind_first_picks_the_wrong_unit(tmp_path):
    broken = sched(("-(drift.behind if known else 0)", "(drift.behind if known else 0)"))
    spawned = []
    tick(broken, open_config(), project(tmp_path), ["a", "b", "c"], {"a": 5, "b": 9, "c": 2}, spawned)
    assert spawned == ["c"], spawned


def test_control_c_a_refusal_written_every_tick_is_caught(tmp_path, monkeypatch):
    broken = sched(("if prior.get(\"refusal\") != WINDOWS:", "if True:"))
    monkeypatch.setattr(broken._sibling("bounded_run"), "group_refusal", lambda: "no")
    sdlc = project(tmp_path)
    tick(broken, open_config(), sdlc, [], {}, [], now=1_000_000)
    tick(broken, open_config(), sdlc, [], {}, [], now=1_000_900)
    assert json.loads(pathlib.Path(sdlc, "state/upkeep/scheduler.json").read_text())["last_tick"] == 1_000_900


def test_control_d_a_heartbeat_that_never_beats_is_caught():
    broken = job(("self.last = now\n            self.touch()", "self.last = now"))
    touched = []
    clock = broken.BeatingClock(lambda: touched.append(1), every=5, base=lambda: 0.0)
    clock()
    assert touched == []


def test_control_e_a_note_written_without_the_offline_actor_is_caught(tmp_path):
    broken = sched(("block[\"actor\"] = _sibling(\"sync\").knowledge_actor(config)", "pass"))
    sdlc, seen = project(tmp_path), []
    cfg = open_config()
    del cfg["ledger"]["actor"]
    finished(broken, sdlc)
    broken.collect(cfg, sdlc, 10 ** 10, lambda s, k, g, config=None, **f: seen.append(config["ledger"].get("actor")) or {})
    assert seen == [None], seen


# ------------------------------------------------------------------------------------------ the doctor

def _doctor():
    import importlib.util
    path = S.ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py"
    spec = importlib.util.spec_from_file_location("doctor_923", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_doctor_shows_scheduler_rows_with_the_git_floor_only_when_the_block_is_on(tmp_path):
    doctor, sdlc = _doctor(), pathlib.Path(project(tmp_path))
    version = {"text": "git version 2.35.0"}

    def run(argv):
        return version["text"] if "--version" in argv else "true"
    closed = doctor._upkeep_rows(sdlc, S.template_cfg(), run, lambda _n: "/bin/git", True, False)
    rows = doctor._upkeep_rows(sdlc, open_config(), run, lambda _n: "/bin/git", True, False)
    names = {r["name"].split(":")[0]: r["ok"] for r in rows}
    assert closed == [] and names["git version floor"] is False and "upkeep scheduler liveness" in names, names
    version["text"] = "git version 2.54.0 (Apple Git-157)"
    again = {r["name"].split(":")[0]: r["ok"] for r in doctor._upkeep_rows(sdlc, open_config(), run, lambda _n: "/bin/git", True, False)}
    assert again["git version floor"] is True
    cheap = doctor._upkeep_rows(sdlc, open_config(), run, lambda _n: "/bin/git", True, True)
    assert not any(r["name"].startswith("git version floor") for r in cheap)
