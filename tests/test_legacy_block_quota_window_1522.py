"""#1522: a legacy unstamped answers block is not carried into a stamped park; a quota
wait-it-out ask is applied only after its recorded reset time."""
import datetime

import test_applier as ta
import test_decision_record as tdr
import test_unpark as tu

U = tu._mod("unpark")
AP, AU, DS, RB = ta.AP, ta.AU, ta.DS, ta.RB
T0, MIN = ta.T0, ta.MIN


def _brief(body, config):
    run = tu._runner(views={"5": tu._view(body=body, comments=[tu._park("hit a wall\nsigma-qkind: quota")])})
    return U.brief(".sdlc", config, 5, run=run)


def test_unstamped_block_is_ignored_for_a_stamped_park_when_records_are_open():
    cfg = tdr._on(tu._config())
    legacy = "Do it.\n\n" + U.render_block({"route": "wait it out"})
    assert "sigma-park" not in legacy
    assert _brief(legacy, cfg)["prior_answers"] == {}
    stamped = "Do it.\n\n" + U.render_block({"route": "wait it out"}, config=cfg, park="c0")
    assert "sigma-park: c0" in stamped
    assert _brief(stamped, cfg)["prior_answers"] == {"route": "wait it out"}


def test_unstamped_block_still_counts_when_records_are_closed():
    cfg = tu._config()
    legacy = "Do it.\n\n" + U.render_block({"route": "wait it out"})
    assert _brief(legacy, cfg)["prior_answers"] == {"route": "wait it out"}


def test_resolve_cli_does_not_reuse_a_legacy_block(tmp_path):
    cfg = tdr._on(tu._config())
    legacy = "Do it.\n\n" + U.render_block({"route": "wait it out"})
    run = tu._runner(views={"5": tu._view(body=legacy, comments=[tu._park("hit a wall\nsigma-qkind: quota")])})
    assert U.brief(str(tmp_path), cfg, 5, run=run)["prior_answers"] == {}


# ---- applier: quota wait-it-out only after the reset window ----------------------------------

def _quota(tmp_path, kind="quota", option="wait it out", reset_at=None, others=("wait it out", "retry now")):
    cfg = ta._cfg()
    sd = ta._sd(tmp_path)
    for _ in range(8):
        DS.append(sd, {"how": "human", "qkind": kind, "area": "docs", "choice": option}, cfg)
        AU.record_watch(sd, kind, "docs", option, option, cfg, now=T0, machine="m1")
    ta._rule(sd, cfg, qkind=kind, option=option)
    AP.open_ask(sd, 7, kind, "docs", list(others), cfg, T0, reset_at=reset_at)
    return sd, cfg


def test_quota_wait_it_out_without_reset_time_waits_a_day(tmp_path):
    sd, cfg = _quota(tmp_path)
    src = ta.Src()
    assert AP.tick(sd, cfg, T0 + 61 * MIN, source=src) == []
    assert src.swaps == [] and src.notes == []
    acts = AP.tick(sd, cfg, T0 + 1441 * MIN, source=src)
    assert [a["result"] for a in acts] == ["done"]


def test_quota_wait_it_out_not_applied_before_reset_then_applied_after(tmp_path):
    sd, cfg = _quota(tmp_path, reset_at=T0 + 180 * MIN)
    src = ta.Src()
    assert AP.tick(sd, cfg, T0 + 120 * MIN, source=src) == [] and src.swaps == []
    acts = AP.tick(sd, cfg, T0 + 181 * MIN, source=src)
    assert [a["result"] for a in acts] == ["done"] and len(src.swaps) == 1


def test_other_kinds_are_not_gated_by_a_reset_time(tmp_path):
    cfg = ta._cfg()
    sd = ta._sd(tmp_path)
    ta._train(sd, cfg)
    ta._rule(sd, cfg)
    ta._ask(sd, cfg)
    src = ta.Src()
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 61 * MIN, source=src)] == ["done"]
