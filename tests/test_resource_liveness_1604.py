"""#1604 - resource guard liveness: slots tail, doctor rows, watcher tick, ledger notes.

Hermetic: an injected Sample, a temp resource state folder (SIGMA_RESOURCE_DIR), no model, network or gh call.
Controls (break one thing, see red): `test_stale_sample_*` fails when the age clause in
`resource_liveness.sample_age_ok` is removed; `test_hold_tail_*` fails when the tail ignores the held state;
`test_ledger_note_rate_limit` fails when the rate-limit stamp is not consulted; `test_tick_*` fails when the
step is not routed through `run_call`; the zero-lease wording test fails when the lease count is not read.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import time
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import resource_guard as guard  # noqa: E402
import resource_lease  # noqa: E402
import resource_liveness as live  # noqa: E402

NOW = 1_000_000.0
TOTAL = 32000


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample(available=20000, cpu_count=8, **extra):
    values = dict(total=TOTAL, available=available, swap_used=0, swap_total=0, swapouts=0, swapouts_at=0.0,
                  pressure=1, cpu_count=cpu_count, load1=0.0)
    values.update(extra)
    return guard.Sample(**values)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(state))
    monkeypatch.delenv("SIGMA_RESOURCE_MODE", raising=False)
    box = {"sample": _sample(), "state": state}
    monkeypatch.setattr(resource_lease, "take_sample", lambda: box["sample"])
    return box


def _hold_json(state, since=NOW - 720, reason="memory", held=True):
    (state / "hold.json").write_text(json.dumps({
        "state": "hold" if held else "open", "since": since if held else None, "reason": reason if held else None,
        "resource": "memory" if held else None, "retry_after": None}))


def _last_json(state, at, hold=False, since=None):
    (state / "last.json").write_text(json.dumps({
        "state": "hold" if hold else "open", "since": since if since is not None else at, "reason": "memory" if hold else "ok",
        "kind": "goal", "goal": "", "at": at, "sample": _sample().as_dict()}))


def _lease(state, name, pid, est=1000):
    (state / "leases").mkdir(exist_ok=True)
    (state / "leases" / (name + ".json")).write_text(json.dumps({
        "id": name, "kind": "goal", "pid": pid, "pgid": None, "start": NOW, "pid_start": None, "repo_hash": "x",
        "goal": "g", "est_mem": est, "workers": 1, "ttl": 10 ** 9, "peak": 0}))


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _sdlc(tmp_path, config=None):
    base = tmp_path / "repo" / ".sdlc"
    (base / "state" / "heartbeat").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps(config or {}))
    return base


def _beat(base, seen):
    (base / "state" / "heartbeat" / "s.json").write_text(json.dumps({"last_seen": seen}))


# ---------------------------------------------------------------------------------------- 1. slots tail
def _log():
    return _load(ROOT / "skills" / "sigma-log" / "scripts" / "log.py", "log_1604")


def test_hold_tail_shows_age_and_reason_when_held(machine):
    _hold_json(machine["state"], since=NOW - 720)
    tail = _log().admission_tail({}, now=NOW)
    assert "admission HELD 12m" in tail and "memory" in tail


def test_hold_tail_is_silent_when_open_absent_or_mode_off(machine):
    log = _log()
    assert log.admission_tail({}, now=NOW) == ""                       # no hold.json at all
    _hold_json(machine["state"], held=False)
    assert log.admission_tail({}, now=NOW) == ""                       # open
    _hold_json(machine["state"])
    assert log.admission_tail({"resource": {"mode": "off"}}, now=NOW) == ""   # a left-over file under mode off


def test_slots_appends_the_admission_tail(machine, tmp_path):
    tl = _load(ROOT / "tests" / "test_log.py", "test_log_for_1604")
    log = _log()
    d = tl._sdlc(tmp_path)
    tl._config(d, tl.GITHUB_ON)
    import time as _t
    tl._running_goal(d, "158", _t.time(), title="Extract the coherence validator")
    _hold_json(machine["state"], since=_t.time() - 720)
    held = log.slots(d)
    assert "admission HELD" in held
    _hold_json(machine["state"], held=False)
    assert "admission HELD" not in log.slots(d)


# --------------------------------------------------------------------------------------- 2. doctor rows
def _doctor():
    return _load(ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py", "doctor_1604")


def _rows(tmp_path, machine, config, now=NOW, sampler=None):
    base = _sdlc(tmp_path, config)
    rows = _doctor()._resource_rows(config, str(base), now=now, sampler=sampler or (lambda: machine["sample"]))
    return {r["name"].split(":")[0]: r for r in rows}, base


def _unsupported():
    raise guard.Unsupported("no reader for platform plan9")


GOALS_ON = {"parallel": {"goals": {"enabled": True, "max_concurrent": 2}}}


def _cfg(**resource):
    cfg = json.loads(json.dumps(GOALS_ON))
    cfg["resource"] = resource
    return cfg


ROW_CASES = [
    # (row key, config, machine setup, ok?)
    ("resource platform", _cfg(), None, True),
    ("resource platform", _cfg(), "unsupported", False),
    ("resource mode", _cfg(mode="enforce"), None, True),
    ("resource mode", _cfg(mode="off"), None, False),
    ("resource admission", _cfg(hold_alert_minutes=30), "short-hold", True),
    ("resource admission", _cfg(hold_alert_minutes=5), "long-hold", False),
    ("resource sample age", _cfg(mode="enforce"), "fresh-sample", True),
    ("resource sample age", _cfg(mode="enforce"), "old-sample", False),
    ("resource stale leases", _cfg(), None, True),
    ("resource stale leases", _cfg(), "stale-lease", False),
    ("resource max_concurrent", _cfg(), None, True),
    ("resource max_concurrent", dict(_cfg(), parallel={"goals": {"enabled": True, "max_concurrent": 99}}), None, False),
    ("resource verify workers", dict(_cfg(), verify={"command": "pytest -n {workers}"}), None, True),
    ("resource verify workers", dict(_cfg(), verify={"command": "pytest -n 64"}), None, False),
]


@pytest.mark.parametrize("key,config,setup,ok", ROW_CASES)
def test_doctor_row_ok_and_not_ok(tmp_path, machine, key, config, setup, ok):
    state = machine["state"]
    sampler = None
    if setup == "unsupported":
        sampler = _unsupported
    elif setup in ("short-hold", "long-hold"):
        _hold_json(state, since=NOW - (600 if setup == "short-hold" else 3600))
        _last_json(state, NOW - 5, hold=True, since=NOW - (600 if setup == "short-hold" else 3600))
        _lease(state, "a", os.getpid())
    elif setup in ("fresh-sample", "old-sample"):
        _last_json(state, NOW - (30 if setup == "fresh-sample" else 99999))
    elif setup == "stale-lease":
        _lease(state, "dead", _dead_pid())
    base = _sdlc(tmp_path, config)
    _beat(base, NOW - 10)
    rows = _doctor()._resource_rows(config, str(base), now=NOW, sampler=sampler or (lambda: machine["sample"]))
    by = {r["name"].split(":")[0]: r for r in rows}
    row = by[key]
    assert row["ok"] is ok
    assert (row["fix"] == "") is ok
    if not ok:
        assert row["fix"]


def test_doctor_rows_are_silent_when_resource_guard_is_off(tmp_path, machine):
    base = _sdlc(tmp_path, {})
    off = {"resource": {"mode": "off"}}
    assert _doctor()._resource_rows(off, str(base), now=NOW, sampler=lambda: machine["sample"]) == []
    assert _doctor()._resource_rows({}, str(base), now=NOW, sampler=lambda: machine["sample"], env={"SIGMA_RESOURCE_MODE": "off"}) == []


def test_doctor_rows_show_on_a_default_install(tmp_path, machine):
    """#1786: an empty config resolves to enforce, so the rows appear."""
    base = _sdlc(tmp_path, {})
    names = [r["name"] for r in _doctor()._resource_rows({}, str(base), now=NOW, sampler=lambda: machine["sample"])]
    assert "resource mode: enforce" in names


def test_verify_row_names_the_workers_fix_and_the_env_lever_note(tmp_path, machine):
    cfg = dict(_cfg(), verify={"command": "pytest -n auto"})
    rows, _ = _rows(tmp_path, machine, cfg)
    row = rows["resource verify workers"]
    assert row["ok"] is False
    assert "{workers}" in row["fix"] and "PYTEST_XDIST_AUTO_NUM_WORKERS" in row["fix"]


# ------------------------------------------------------------------------- 3. zero leases wording
def test_held_with_zero_leases_says_other_software(tmp_path, machine):
    state = machine["state"]
    _hold_json(state, since=NOW - 3600)
    _last_json(state, NOW - 5, hold=True, since=NOW - 3600)
    rows, _ = _rows(tmp_path, machine, _cfg(hold_alert_minutes=5))
    name = rows["resource admission"]["name"] + rows["resource admission"]["fix"]
    assert "0 leases" in name and "other software" in name and "cannot release" in name


def test_held_with_leases_names_the_count(tmp_path, machine):
    state = machine["state"]
    _hold_json(state, since=NOW - 3600)
    _last_json(state, NOW - 5, hold=True, since=NOW - 3600)
    _lease(state, "a", os.getpid())
    _lease(state, "b", os.getpid())
    rows, _ = _rows(tmp_path, machine, _cfg(hold_alert_minutes=5))
    text = rows["resource admission"]["name"] + rows["resource admission"]["fix"]
    assert "2 Sigma leases" in text and "other software" not in text


# ------------------------------------------------------------------------------- 4. stale sample
def test_stale_sample_with_fresh_heartbeat_is_not_ok(tmp_path, machine):
    state = machine["state"]
    base = _sdlc(tmp_path, _cfg(mode="enforce"))
    _beat(base, NOW - 10)
    bound = live.sample_stale_after(_cfg(mode="enforce"))
    _last_json(state, NOW - bound - 60)
    rows = _doctor()._resource_rows(_cfg(mode="enforce"), str(base), now=NOW, sampler=lambda: machine["sample"])
    old = {r["name"].split(":")[0]: r for r in rows}["resource sample age"]
    assert old["ok"] is False
    _last_json(state, NOW - 30)
    rows = _doctor()._resource_rows(_cfg(mode="enforce"), str(base), now=NOW, sampler=lambda: machine["sample"])
    assert {r["name"].split(":")[0]: r for r in rows}["resource sample age"]["ok"] is True


def test_old_sample_is_fine_when_no_loop_runs(tmp_path, machine):
    base = _sdlc(tmp_path, _cfg(mode="enforce"))
    _beat(base, NOW - 10 ** 6)                        # the loop heartbeat itself is old: nothing is expected to sample
    _last_json(machine["state"], NOW - 10 ** 6)
    rows = _doctor()._resource_rows(_cfg(mode="enforce"), str(base), now=NOW, sampler=lambda: machine["sample"])
    assert {r["name"].split(":")[0]: r for r in rows}["resource sample age"]["ok"] is True


@pytest.mark.parametrize("cfg", [{"resource": {"mode": "off"}}, dict(_cfg(mode="off"))])
def test_mode_off_fresh_heartbeat_no_last_json_has_no_sampler_row(tmp_path, machine, cfg):
    base = _sdlc(tmp_path, cfg)
    _beat(base, NOW - 10)
    rows = _doctor()._resource_rows(cfg, str(base), now=NOW, sampler=lambda: machine["sample"])
    assert not any(r["name"].startswith("resource sample age") for r in rows)
    if cfg.get("parallel") is None:
        assert rows == []
    else:                       # goals on + off: only the deliberate mode warning may fail
        assert [r["name"] for r in rows if not r["ok"]] == ["resource mode: off"]


def test_mode_null_reads_as_the_resolved_default_for_the_sample_age_row(tmp_path, machine):
    """#1759: an unset mode resolves to enforce, which the watcher samples under, so the age row is present."""
    base = _sdlc(tmp_path, _cfg())
    _beat(base, NOW - 10)
    rows = _doctor()._resource_rows(_cfg(), str(base), now=NOW, sampler=lambda: machine["sample"])
    row = {r["name"].split(":")[0]: r for r in rows}["resource sample age"]
    assert "never sampled" in row["name"] and row["ok"] is False
    off = _cfg(mode="off")
    rows = _doctor()._resource_rows(off, str(base), now=NOW, sampler=lambda: machine["sample"])
    assert "resource sample age" not in {r["name"].split(":")[0] for r in rows}


# ------------------------------------------------------------------------------------ 5. watcher tick
def _wd():
    return _load(SCRIPTS / "watch_daemon.py", "watch_daemon_1604")


def test_tick_step_goes_through_the_timeout_runner(tmp_path, machine):
    wd = _wd()
    seen = []
    wd.run_call = lambda p, sdlc_dir, call_timeout, script, subcmd, mode: seen.append((script, subcmd, mode)) or ""
    wd.touch_heartbeat = lambda p: None
    wd.resource_step(None, str(_sdlc(tmp_path, _cfg(mode="enforce"))), "120", config=_cfg(mode="enforce"))
    assert seen == [("resource_guard.py", ("sample", "--record"), "summary")]
    argv = wd.call_argv("d", "120", "resource_guard.py", ("sample", "--record"))
    assert argv[1].endswith("run_with_timeout.py") and argv[3].endswith("resource_guard.py")
    assert argv[4:] == ["sample", "--record", "d"]


def test_tick_itself_calls_the_resource_step(tmp_path, machine):
    """Drive `tick`, not `resource_step`: dropping the call from the tick body must go red."""
    wd = _wd()
    seen = []
    wd.run_call = lambda p, sdlc_dir, call_timeout, script, subcmd, mode: seen.append((script, subcmd)) or ""
    wd.touch_heartbeat = lambda p: None
    wd.scheduler_step = lambda *a, **k: None
    wd.applier_step = lambda *a, **k: None
    cfg = _cfg(mode="enforce")
    base = _sdlc(tmp_path, cfg)
    p = wd.paths(str(base))
    p.state.mkdir(parents=True, exist_ok=True)
    wd.tick(p, str(base), "120", 1, config=cfg)
    assert seen[-1] == ("resource_guard.py", ("sample", "--record"))
    assert [s for s in seen if s[0] == "resource_guard.py"] == [seen[-1]]


def test_tick_step_runs_nothing_when_mode_is_off(tmp_path, machine):
    wd = _wd()
    seen = []
    wd.run_call = lambda *a, **k: seen.append(a) or ""
    wd.touch_heartbeat = lambda p: seen.append("beat")
    off = {"resource": {"mode": "off"}}
    assert wd.resource_step(None, str(_sdlc(tmp_path, off)), "120", config=off) == ""
    assert seen == []


def test_tick_spawns_no_long_lived_process(tmp_path, machine, monkeypatch):
    wd = _wd()
    popen = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: popen.append(a))
    runs = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: runs.append(argv) or subprocess.CompletedProcess(argv, 0, b"", b""))
    cfg = _cfg(mode="enforce")
    base = _sdlc(tmp_path, cfg)
    p = wd.paths(str(base))
    p.state.mkdir(parents=True, exist_ok=True)
    wd.resource_step(p, str(base), "120", config=cfg)
    assert popen == [] and len(runs) == 1
    assert runs[0][1].endswith("run_with_timeout.py") and runs[0][3].endswith("resource_guard.py")


# --------------------------------------------------------------------------------- refresh + notes
def _refresh(base, machine, config, now, notes):
    s = machine["sample"]
    if s is not None and not isinstance(s, type(None)):
        machine["sample"] = guard.Sample(**dict(s.as_dict(), swapouts_at=now))   # the reading time moves with the clock
    return live.refresh(str(base), config, now=now, append=lambda *a, **k: notes.append((a, k)))


def test_refresh_records_a_sample_and_reclaims_stale_leases(tmp_path, machine):
    base = _sdlc(tmp_path, _cfg(mode="enforce"))
    _lease(machine["state"], "dead", _dead_pid())
    out = _refresh(base, machine, _cfg(mode="enforce"), NOW, [])
    last = json.loads((machine["state"] / "last.json").read_text())
    assert last["at"] == NOW and last["sample"]["total"] == TOTAL
    assert not list((machine["state"] / "leases").glob("*.json"))
    assert out["state"] == "open"


def test_refresh_off_touches_nothing(tmp_path, machine):
    base = _sdlc(tmp_path, {"resource": {"mode": "off"}})
    assert _refresh(base, machine, {"resource": {"mode": "off"}}, NOW, []) is None
    assert list(machine["state"].iterdir()) == []


def test_refresh_unsupported_writes_nothing_and_raises_typed(tmp_path, machine, monkeypatch):
    monkeypatch.setattr(resource_lease, "take_sample", _unsupported)
    base = _sdlc(tmp_path, _cfg(mode="enforce"))
    with pytest.raises(guard.Unsupported):
        _refresh(base, machine, _cfg(mode="enforce"), NOW, [])
    assert list(machine["state"].iterdir()) == []


def _squeeze(machine):
    machine["sample"] = _sample(available=1000)          # below the reserve: a hold


def test_ledger_note_at_hold_start_and_end_and_rate_limited(tmp_path, machine):
    cfg = _cfg(mode="enforce", hold_alert_minutes=10)
    cfg["ledger"] = {"enabled": True}
    base = _sdlc(tmp_path, cfg)
    notes = []
    _squeeze(machine)
    _refresh(base, machine, cfg, NOW, notes)
    assert len(notes) == 1 and "HELD" in json.dumps(notes[0])               # start
    _refresh(base, machine, cfg, NOW + 3, notes)
    assert len(notes) == 1                                                # still held: no repeat
    machine["sample"] = _sample(available=30000)
    _refresh(base, machine, cfg, NOW + 6, notes)
    _refresh(base, machine, cfg, NOW + 9, notes)
    assert len(notes) == 2 and "open" in json.dumps(notes[1]).lower()      # end
    _squeeze(machine)
    _refresh(base, machine, cfg, NOW + 12, notes)                          # new hold start inside the window
    assert len(notes) == 2
    machine["sample"] = _sample(available=30000)
    _refresh(base, machine, cfg, NOW + 15, notes)
    _refresh(base, machine, cfg, NOW + 18, notes)
    assert len(notes) == 2                                                # end inside the window too
    _squeeze(machine)
    _refresh(base, machine, cfg, NOW + 700, notes)                         # past hold_alert_minutes: a start again
    assert len(notes) == 3


def test_ledger_note_none_when_ledger_disabled(tmp_path, machine):
    cfg = _cfg(mode="enforce", hold_alert_minutes=10)
    cfg["ledger"] = {"enabled": False}
    base = _sdlc(tmp_path, cfg)
    notes = []
    _squeeze(machine)
    _refresh(base, machine, cfg, NOW, notes)
    machine["sample"] = _sample(available=30000)
    _refresh(base, machine, cfg, NOW + 6, notes)
    _refresh(base, machine, cfg, NOW + 9, notes)
    assert notes == []
    assert json.loads((machine["state"] / "hold.json").read_text())["state"] == "open"


def test_cli_record_exit_codes(tmp_path, machine, monkeypatch, capsys):
    base = _sdlc(tmp_path, _cfg(mode="enforce"))
    monkeypatch.setattr(guard, "sample", lambda root="/": machine["sample"])
    assert guard.main(["sample", "--record", str(base)]) == 0
    assert (machine["state"] / "last.json").exists()
    assert guard.main(["sample", "--record"]) == 2


# ------------------------------------------------------------------------- 6. review follow-up (#1750)
def test_log_hold_path_rule_matches_the_guard(machine, tmp_path, monkeypatch):
    """The log skill copies `hold_path`; the tail must read the very file the guard writes."""
    _hold_json(machine["state"], since=NOW - 720)
    assert "admission HELD" in _log().admission_tail({}, now=NOW)
    assert os.path.dirname(guard.hold_path()) == str(machine["state"])
    monkeypatch.delenv("SIGMA_RESOURCE_DIR")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert guard.hold_path() == os.path.join(str(tmp_path), "sigma", "resource", "hold.json")
    pathlib.Path(tmp_path, "sigma", "resource").mkdir(parents=True)
    _hold_json(pathlib.Path(tmp_path, "sigma", "resource"), since=NOW - 720)
    assert "admission HELD" in _log().admission_tail({}, now=NOW)


def test_observe_mode_tail_says_not_enforced(machine):
    _hold_json(machine["state"], since=NOW - 720)
    observed = _log().admission_tail({"resource": {"mode": "observe"}}, now=NOW)
    assert observed.endswith(" (observe: not enforced)")
    assert "not enforced" not in _log().admission_tail({"resource": {"mode": "enforce"}}, now=NOW)


def test_slots_reads_the_admission_tail_once(machine, tmp_path, monkeypatch):
    log = _log()
    calls = []
    real = log.admission_tail
    monkeypatch.setattr(log, "admission_tail", lambda *a, **k: calls.append(1) or real(*a, **k))
    tl = _load(ROOT / "tests" / "test_log.py", "test_log_for_1604_once")
    d = tl._sdlc(tmp_path)
    tl._config(d, tl.GITHUB_ON)
    tl._running_goal(d, "158", time.time(), title="Extract the coherence validator")
    _hold_json(machine["state"], since=time.time() - 720)
    out = log.slots(d)
    assert "admission HELD" in out        # the directory is non-empty, so slots reached the tail
    assert len(calls) == 1


# ------------------------------------------------------------------- 7. held admission with nothing to render (#1808)
def _held_log(tmp_path, claimed):
    tl = _load(ROOT / "tests" / "test_log.py", "test_log_for_1808")
    d = tl._sdlc(tmp_path)
    tl._config(d, tl.GITHUB_ON)
    return tl, d


def test_slots_says_held_with_no_goal_files(machine, tmp_path):
    _tl, d = _held_log(tmp_path, False)
    _hold_json(machine["state"], since=time.time() - 720)
    out = _log().slots(d)
    assert "nothing logged yet" in out and out.endswith("admission HELD 12m: memory")
    _hold_json(machine["state"], held=False)
    assert "admission HELD" not in _log().slots(d)


def test_slots_says_held_with_goals_that_are_not_renderable(machine, tmp_path):
    tl, d = _held_log(tmp_path, True)
    tl._seed(d, "158", "claimed", now=time.time() - 10)      # claimed, no phase recorded: withheld
    _hold_json(machine["state"], since=time.time() - 720)
    out = _log().slots(d)
    assert "none renderable" in out and out.endswith("admission HELD 12m: memory")


def test_hold_json_absent_last_json_held_agrees_with_the_doctor(machine, tmp_path):
    base = _sdlc(tmp_path)
    _last_json(machine["state"], NOW, hold=True, since=NOW - 720)
    assert not (machine["state"] / "hold.json").exists()
    tail = _log().admission_tail({}, now=NOW)
    view = live.hold_view(now=NOW)
    assert view and tail == "admission HELD %dm: %s" % (view["age"] // 60, view["reason"])
    # an explicit open hold.json is final, as in hold_view
    (machine["state"] / "hold.json").write_text(json.dumps({
        "state": "open", "since": NOW - 5, "reason": None, "resource": None, "retry_after": None}))
    assert _log().admission_tail({}, now=NOW) == "" and live.hold_view(now=NOW) is None
    # a corrupt hold.json falls back to last.json in both
    (machine["state"] / "hold.json").write_text("{")
    assert _log().admission_tail({}, now=NOW) != "" and live.hold_view(now=NOW) is not None
