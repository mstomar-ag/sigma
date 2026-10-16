"""#1560: `reconcile.py blockers <sdlc_dir>` previews the blocker priority repair, read-only, in any mode."""
import json

import test_blocker_rank_repair as base

GOAL = base.GOAL
APPLY_KEYS = ["violations", "raised", "stripped", "unmarked", "unwritable", "skipped"]


def _corpus():
    return [base._issue(9, [GOAL, "priority:P0"], body="blocked by #7"), base._issue(7, [GOAL, "priority:P3"]),
            base._issue(20, [GOAL, "priority:P2"], body="blocked by #21"), base._issue(21, [GOAL, "priority:P1"])]


def _cycle():
    return [base._issue(9, [GOAL, "priority:P0"], body="blocked by #8"),
            base._issue(8, [GOAL, "priority:P3"], body="blocked by #9")]


def test_preview_lists_the_raise_and_skips_the_correct_pair_without_writing(tmp_path):
    run = base._runner(_corpus())
    res = base._src(run, sdlc_dir=str(tmp_path)).repair_blocker_ranks(preview=True)
    assert res["plan"] == [{"blocker": "7", "current": "P3", "new": "P0", "cause": "9"}]
    assert res["violations"] == 1 and res["raised"] == [] and res["cycles"] == []
    assert base._writes(run) == []
    assert not (tmp_path / "state").exists()


def test_preview_runs_when_the_mode_is_off_and_smart():
    for mode in ("off", "smart"):
        run = base._runner(_corpus())
        res = base._src(run, mode=mode).repair_blocker_ranks(preview=True)
        assert res["skipped"] == "" and len(res["plan"]) == 1, mode
        assert base._writes(run) == []


def test_preview_reports_a_cycle():
    res = base._src(base._runner(_cycle())).repair_blocker_ranks(preview=True)
    assert res["cycles"] == ["8", "9"]


def test_apply_path_result_keys_are_unchanged():
    for kw in ({}, {"apply": False}):
        res = base._src(base._runner(_corpus())).repair_blocker_ranks(**kw)
        assert list(res) == APPLY_KEYS, kw
    assert list(base._src(base._runner([]), mode="off").repair_blocker_ranks()) == APPLY_KEYS


def test_cli_prints_the_listing_and_counts(tmp_path, capsys):
    rec = base._mod("reconcile")
    cfg = base._cfg("off")
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    cyc = [base._issue(31, [GOAL, "priority:P0"], body="blocked by #32"),
           base._issue(32, [GOAL, "priority:P3"], body="blocked by #31")]
    run = base._runner(_corpus() + cyc)
    rc = rec.main(["reconcile.py", "blockers", str(tmp_path)], run=run)
    out = capsys.readouterr().out
    assert rc == 0 and "mode: off" in out
    assert "#7" in out and "P3" in out and "P0" in out and "#9" in out
    assert "cycles: 2" in out and "outside or unreadable blockers: 0" in out
    assert base._writes(run) == []


def test_cli_refuses_a_non_github_source(tmp_path, capsys):
    rec = base._mod("reconcile")
    (tmp_path / "config.json").write_text(json.dumps({"discovery": {"source": "local-goals"}}))
    assert rec.main(["reconcile.py", "blockers", str(tmp_path)]) == 2
    assert "github" in capsys.readouterr().err
