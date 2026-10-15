"""#1489: mode "smart" keeps the blocker priority rule off; the tick says so once and the doctor row
says so every time. always and off are unchanged."""
import json

import test_blocker_rank_repair as r

DOCTOR = r._mod("doctor", r.S / "sigma-doctor" / "scripts")
STATE = "blocker-repair.meta.json"


def _row(d, cfg, now=2000.0):
    return DOCTOR._blocker_rank_row(cfg, str(d), now=now)


def test_smart_tick_is_silent_on_gh_prints_one_line_and_records_marker(tmp_path, capsys):
    tick = r._mod("reconcile_tick")
    d = r._sdlc(tmp_path, r._cfg("smart"))
    run = r._runner(r._p0_blocked_by_p3())
    tick.tick(str(d), config=r._cfg("smart"), run=run, now=1000.0)
    assert not r._writes(run)
    err = capsys.readouterr().err
    assert err.count("blocker rank repair skipped: mode is smart") == 1 and "set always" in err
    assert tick.read_repair_state(str(d)) == {"mode": "smart", "rollout_reported": True}
    tick.tick(str(d), config=r._cfg("smart"), run=r._runner(r._p0_blocked_by_p3()), now=1100.0)
    assert "skipped" not in capsys.readouterr().err


def test_smart_doctor_row_is_not_ok_with_or_without_marker(tmp_path):
    for sub, state in (("a", None), ("b", {"mode": "smart", "rollout_reported": True})):
        d = r._sdlc(tmp_path / sub, r._cfg("smart"))
        if state:
            (d / "state" / STATE).write_text(json.dumps(state))
        row = _row(d, r._cfg("smart"))
        assert not row["ok"] and "inactive" in row["name"] and '"smart"' in row["name"]
        assert '"always"' in row["fix"] and "not yet checked" not in row["name"]


def test_scenario_tick_then_doctor_smart_then_always_then_off(tmp_path, capsys):
    tick = r._mod("reconcile_tick")
    d = r._sdlc(tmp_path / "s", r._cfg("smart"))
    tick.tick(str(d), config=r._cfg("smart"), run=r._runner(r._p0_blocked_by_p3()), now=1000.0)
    assert "skipped: mode is smart" in capsys.readouterr().err
    assert not _row(d, r._cfg("smart"))["ok"]
    # switching to always: one raise, state replaced, row reads 0 with the last repair
    run = r._runner(r._p0_blocked_by_p3())
    tick.tick(str(d), config=r._cfg(), run=run, now=1500.0)
    assert r._raised(run, 7, "priority:P0")
    row = _row(d, r._cfg(), now=1600.0)
    assert row["ok"] and ": 1 (last repair" in row["name"]
    # off: no call, no state, no row
    d2 = r._sdlc(tmp_path / "o", r._cfg("off"))
    orun = r._runner(r._p0_blocked_by_p3())
    tick.tick(str(d2), config=r._cfg("off"), run=orun, now=1.0)
    assert orun.calls == [] and not (d2 / "state" / STATE).exists()
    assert _row(d2, r._cfg("off")) is None


def test_always_unchanged_state_and_row(tmp_path):
    tick = r._mod("reconcile_tick")
    d = r._sdlc(tmp_path, r._cfg())
    tick.tick(str(d), config=r._cfg(), run=r._runner([]), now=1000.0)
    assert tick.read_repair_state(str(d))["violations"] == 0
    assert "mode" not in tick.read_repair_state(str(d))
    assert ": 0 (" in _row(d, r._cfg(), now=1100.0)["name"]
