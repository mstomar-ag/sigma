"""#1526: the smart-mode skip marker must not count as the always-mode rollout report.

Driven through the real reconcile tick and the real doctor row, with a fake gh and a temp repo."""
import json

import test_blocker_rank_repair as r

DOCTOR = r._mod("doctor", r.S / "sigma-doctor" / "scripts")
FIRST = "first run: 1 blocker(s)"


def _row(d, cfg, now=2000.0):
    return DOCTOR._blocker_rank_row(cfg, str(d), now=now)


def test_first_always_tick_after_smart_still_prints_the_preview(tmp_path, capsys):
    tick = r._mod("reconcile_tick")
    d = r._sdlc(tmp_path, r._cfg("smart"))
    tick.tick(str(d), config=r._cfg("smart"), run=r._runner(r._p0_blocked_by_p3()), now=1000.0)
    capsys.readouterr()
    tick.tick(str(d), config=r._cfg("always"), run=r._runner(r._p0_blocked_by_p3()), now=1500.0)
    assert FIRST in capsys.readouterr().err
    tick.tick(str(d), config=r._cfg("always"), run=r._runner(r._p0_blocked_by_p3()), now=1600.0)
    assert "first run" not in capsys.readouterr().err


def test_legacy_smart_marker_without_a_mode_is_not_a_report(tmp_path, capsys):
    tick = r._mod("reconcile_tick")
    d = r._sdlc(tmp_path, r._cfg("always"))
    (d / "state" / "blocker-repair.meta.json").write_text(json.dumps({"rollout_reported": True}))
    tick.tick(str(d), config=r._cfg("always"), run=r._runner(r._p0_blocked_by_p3()), now=1500.0)
    assert FIRST in capsys.readouterr().err


def test_doctor_row_after_the_switch_says_first_repair_has_not_run(tmp_path):
    tick = r._mod("reconcile_tick")
    d = r._sdlc(tmp_path, r._cfg("smart"))
    tick.tick(str(d), config=r._cfg("smart"), run=r._runner([]), now=1000.0)
    name = _row(d, r._cfg("always"))["name"]
    assert "first repair has not run" in name and ": 0" not in name
    tick.tick(str(d), config=r._cfg("always"), run=r._runner(r._p0_blocked_by_p3()), now=1500.0)
    assert ": 1 (last repair" in _row(d, r._cfg("always"), now=1600.0)["name"]


def test_always_from_fresh_is_unchanged(tmp_path, capsys):
    tick = r._mod("reconcile_tick")
    d = r._sdlc(tmp_path, r._cfg())
    tick.tick(str(d), config=r._cfg(), run=r._runner(r._p0_blocked_by_p3()), now=1000.0)
    assert FIRST in capsys.readouterr().err
    assert tick.read_repair_state(str(d)) == {
        "violations": 1, "last_repair": 1000.0, "first_seen_nonzero": 1000.0, "rollout_reported": True}
    assert ": 1 (last repair" in _row(d, r._cfg(), now=1100.0)["name"]
    e = r._sdlc(tmp_path / "z", r._cfg())
    tick.tick(str(e), config=r._cfg(), run=r._runner([]), now=1000.0)
    assert ": 0 (no repair yet)" in _row(e, r._cfg(), now=1100.0)["name"]


def test_smart_is_unchanged(tmp_path, capsys):
    tick = r._mod("reconcile_tick")
    d = r._sdlc(tmp_path, r._cfg("smart"))
    tick.tick(str(d), config=r._cfg("smart"), run=r._runner(r._p0_blocked_by_p3()), now=1000.0)
    assert capsys.readouterr().err.count("skipped: mode is smart") == 1
    assert tick.read_repair_state(str(d)) == {"mode": "smart", "rollout_reported": True}
    assert not _row(d, r._cfg("smart"))["ok"]
