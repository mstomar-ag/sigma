"""#1361: the spot-check slot is decided per action at apply time from the landed count."""
import json
import os

import test_applier as T
import test_applier_spot_check as S

AP = T.AP


def _fail_issue(bad):
    real = AP.apply

    def fake(action, *a, **k):
        if action.get("issue") == bad:
            return "failed"
        return real(action, *a, **k)
    return real, fake


def _tick_failing(sd, cfg, src, issues, bad):
    real, fake = _fail_issue(bad)
    AP.apply = fake
    try:
        return S._run(sd, cfg, src, issues)
    finally:
        AP.apply = real


def test_failing_middle_action_keeps_one_in_n(tmp_path):
    sd, cfg, _ = T._setup(tmp_path, dict(S.SILENT, spot_check_one_in=2), level=4)
    src = T.Src()
    acts = _tick_failing(sd, cfg, src, [1, 2, 3], bad=2)
    done = [a for a in acts if a["result"] == "done"]
    assert len(done) == 2 and len(src.swaps) == 2
    # of the two that landed (counts 1 and 2) exactly the second comments: never two silent in a row.
    assert len(src.notes) == 1 and src.notes[0][0] == done[1]["issue"]
    with open(S._counter(sd)) as fh:
        assert sum(json.load(fh).values()) == 2
    S._run(sd, cfg, src, [4, 5])   # the cycle continues: count 3 silent, count 4 comments
    assert len(src.swaps) == 5 and len(src.notes) == 2   # the failed ask retries


def test_all_succeed_slots_match_plan(tmp_path):
    sd, cfg, _ = T._setup(tmp_path, dict(S.SILENT, spot_check_one_in=2), level=4)
    src = T.Src()
    for i in (1, 2, 3, 4):
        S._open(sd, cfg, i)
    plan = AP.tick(sd, cfg, T.T0 + 61 * T.MIN, source=src, apply_actions=False)
    acts = AP.tick(sd, cfg, T.T0 + 61 * T.MIN, source=src)
    assert [a["silent"] for a in plan] == [a["silent"] for a in acts] == [True, False, True, False]
    assert [a["text"] for a in plan] == [a["text"] for a in acts]
    assert len(src.notes) == 2


def test_closed_gate_is_byte_identical(tmp_path):
    sd, cfg, _ = T._setup(tmp_path, dict(S.SILENT, spot_check_one_in=1), level=4)
    S._open(sd, cfg, 1)
    cfg["rulebook"]["autonomy"]["applier"]["enabled"] = False
    src = T.Src()
    assert AP.tick(sd, cfg, T.T0 + 61 * T.MIN, source=src) == []
    assert not os.path.exists(S._counter(sd)) and src.notes == [] and src.swaps == []
