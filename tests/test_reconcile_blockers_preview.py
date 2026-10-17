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


# ---- #1707: the preview applies the same ownership test as the apply pass ----

def _foreign_board():
    return [base._issue(9, [GOAL, "priority:P0"], body="blocked by #7; blocked by #8"),
            base._issue(8, [GOAL, "priority:P3"]), base._issue(7, [])]


def _hs_src(run, on, tmp_path=None):
    cfg = base._cfg()
    if on:
        cfg["rulebook"] = {"hard_stops": {"enabled": True}}
    return base._mod("sources").GitHubSource(cfg, run=run, sdlc_dir=str(tmp_path) if tmp_path else None)


def test_preview_lists_a_foreign_blocker_as_skipped_and_apply_raises_exactly_the_raise_list(tmp_path):
    prev = _hs_src(base._runner(_foreign_board()), True).repair_blocker_ranks(preview=True)
    assert [p["blocker"] for p in prev["plan"]] == ["8"] and prev["foreign"] == ["7"], prev
    run = base._runner(_foreign_board())
    res = _hs_src(run, True, tmp_path).repair_blocker_ranks()
    assert res["raised"] == ["8"] and res["refused"] == ["7"], res


def test_preview_is_unchanged_with_hard_stops_off(tmp_path):
    prev = _hs_src(base._runner(_foreign_board()), False).repair_blocker_ranks(preview=True)
    assert sorted(p["blocker"] for p in prev["plan"]) == ["7", "8"] and "foreign" in prev
    assert prev["foreign"] == []
    res = _hs_src(base._runner(_foreign_board()), False, tmp_path).repair_blocker_ranks()
    assert list(res) == APPLY_KEYS


def test_cli_names_the_skipped_blocker(tmp_path, capsys):
    rec = base._mod("reconcile")
    cfg = base._cfg("always")
    cfg["rulebook"] = {"hard_stops": {"enabled": True}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    rc = rec.main(["reconcile.py", "blockers", str(tmp_path)], run=base._runner(_foreign_board()))
    out = capsys.readouterr().out
    assert rc == 0 and "1 to raise" in out and "would be skipped: not a Sigma issue: #7" in out


def test_doctor_names_an_ownership_refusal_and_does_not_blame_label_permission(tmp_path):
    import time
    tick = base._mod("reconcile_tick", base.SL)
    cfg = base._cfg("always")
    cfg["rulebook"] = {"hard_stops": {"enabled": True}}
    board = [base._issue(9, [GOAL, "priority:P0"], body="blocked by #7"), base._issue(7, [])]
    tick._blocker_rank_repair(str(tmp_path), cfg, run=base._runner(board), now=time.time() - 99999)
    st = tick.read_repair_state(str(tmp_path))
    assert st["refused"] == 1 and st["violations"] == 1, st
    doc = base._mod("doctor", base.S / "sigma-doctor" / "scripts")
    row = doc._blocker_rank_row(cfg, str(tmp_path))
    assert row["ok"] and "not a Sigma issue" in row["name"] and "label" not in row["fix"].replace("Sigma labels", ""), row


# ---- #1757: a refused foreign blocker spends none of the apply write budget ----

def _crowded_board():
    foreign = list(range(11, 23))                       # twelve unlabelled foreign issues, sorted before "8"
    body = "; ".join("blocked by #%d" % n for n in [8] + foreign)
    return ([base._issue(9, [GOAL, "priority:P0"], body=body), base._issue(8, [GOAL, "priority:P3"])]
            + [base._issue(n, []) for n in foreign])


def test_refusals_do_not_consume_the_write_budget_and_apply_raises_the_preview_list(tmp_path):
    prev = _hs_src(base._runner(_crowded_board()), True).repair_blocker_ranks(preview=True)
    assert [p["blocker"] for p in prev["plan"]] == ["8"] and len(prev["foreign"]) == 10, prev
    res = _hs_src(base._runner(_crowded_board()), True, tmp_path).repair_blocker_ranks()
    assert res["raised"] == ["8"] and len(res["refused"]) == 10, res


def test_doctor_does_not_blame_the_watcher_when_only_foreign_blockers_remain(tmp_path):
    import time
    tick = base._mod("reconcile_tick", base.SL)
    cfg = base._cfg("always")
    cfg["rulebook"] = {"hard_stops": {"enabled": True}}
    run = base._runner(_crowded_board())
    tick._blocker_rank_repair(str(tmp_path), cfg, run=run, now=time.time() - 99999)     # raises the one Sigma-owned blocker
    assert tick.read_repair_state(str(tmp_path)).get("last_repair"), "the first tick wrote nothing: refusals ate the budget"
    after = _crowded_board()
    after[1] = base._issue(8, [GOAL, "priority:P0"])                                     # what the first tick wrote
    tick._blocker_rank_repair(str(tmp_path), cfg, run=base._runner(after), now=time.time() - 99998)
    st = tick.read_repair_state(str(tmp_path))
    assert st["refused"] == 10 and st["violations"] == 10, st
    doc = base._mod("doctor", base.S / "sigma-doctor" / "scripts")
    row = doc._blocker_rank_row(cfg, str(tmp_path))
    assert row["ok"] and "not a Sigma issue" in row["name"], row
