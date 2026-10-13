"""#1337: the level-4 spot check is a per-(kind, area) counter, not a hash sample."""
import os

import test_applier as T

AP = T.AP
T0, MIN = T.T0, T.MIN
SILENT = {"allow_silent": ["needs_decision"], "max_auto_applied_per_day": 100}


def _counter(sd):
    return os.path.join(sd, "autonomy", "spot-counter.json")


def _open(sd, cfg, issue, area="docs"):
    return AP.open_ask(sd, issue, "needs_decision", area, ["merge", "wait"], cfg, T0)


def _run(sd, cfg, src, issues, area="docs", step=61):
    for i in issues:
        _open(sd, cfg, i, area)
    return AP.tick(sd, cfg, T0 + step * MIN, source=src)


def test_nine_applies_n3_gives_exactly_three_comments_across_ticks(tmp_path):
    sd, cfg, _ = T._setup(tmp_path, dict(SILENT, spot_check_one_in=3), level=4)
    src = T.Src()
    for batch in ([1, 2, 3, 4], [5, 6], [7, 8, 9]):
        acts = _run(sd, cfg, src, batch)
        assert all(a["result"] == "done" for a in acts)
    assert len(src.swaps) == 9 and len(src.notes) == 3
    assert len(src.swaps) == len(src.notes) * 3


def test_n1_comments_every_time(tmp_path):
    sd, cfg, _ = T._setup(tmp_path, dict(SILENT, spot_check_one_in=1), level=4)
    src = T.Src()
    _run(sd, cfg, src, [1, 2, 3])
    assert len(src.notes) == 3


def test_dry_run_does_not_advance(tmp_path):
    sd, cfg, _ = T._setup(tmp_path, dict(SILENT, spot_check_one_in=2), level=4)
    src = T.Src()
    _open(sd, cfg, 1)
    for _ in range(3):
        acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src, apply_actions=False)
        assert [a["silent"] for a in acts] == [True]
    assert not os.path.exists(_counter(sd))


def test_counters_are_separate_per_kind_and_area(tmp_path):
    sd, cfg, _ = T._setup(tmp_path, dict(SILENT, spot_check_one_in=2), level=4)
    T._train(sd, cfg, area="ops", level=4)
    T._rule(sd, cfg, area="ops")
    src = T.Src()
    _run(sd, cfg, src, [1], area="docs")      # docs count 1: silent
    _run(sd, cfg, src, [2], area="ops")       # ops count 1: silent, not docs' second
    assert src.notes == []
    _run(sd, cfg, src, [3], area="docs")      # docs count 2: comments
    assert [n[0] for n in src.notes] == [3]


def test_missing_or_corrupt_counter_starts_at_zero(tmp_path):
    sd, cfg, _ = T._setup(tmp_path, dict(SILENT, spot_check_one_in=2), level=4)
    src = T.Src()
    os.makedirs(os.path.dirname(_counter(sd)), exist_ok=True)
    with open(_counter(sd), "w") as fh:
        fh.write("{not json")
    _run(sd, cfg, src, [1])
    assert src.notes == []
    _run(sd, cfg, src, [2])
    assert [n[0] for n in src.notes] == [2]


def test_closed_gate_writes_no_file(tmp_path):
    sd, cfg, _ = T._setup(tmp_path, dict(SILENT, spot_check_one_in=1), level=4)
    _open(sd, cfg, 1)
    cfg["rulebook"]["autonomy"]["applier"]["enabled"] = False
    assert AP.tick(sd, cfg, T0 + 61 * MIN, source=T.Src()) == []
    assert not os.path.exists(_counter(sd))


def test_failed_apply_is_not_counted(tmp_path):
    sd, cfg, _ = T._setup(tmp_path, dict(SILENT, spot_check_one_in=2), level=4)
    src = T.Src()
    real = AP.apply
    AP.apply = lambda *a, **k: "failed"
    try:
        _run(sd, cfg, src, [1])
    finally:
        AP.apply = real
    assert not os.path.exists(_counter(sd))
