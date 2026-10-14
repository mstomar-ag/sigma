"""#1411: the worked cases of the blocker priority rule, one scenario each, driven through the real
entry points (`GitHubSource` writers and repair, the filing priority, the picker, the reconcile tick)
with the fake-gh runners of the slice tests. A blocker is never ranked below what it blocks."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_blocker_filing_priority as fp
import test_blocker_rank_repair as r
import test_blocker_rank_writers as w
import test_blocking_tier_order as order

GOAL = "sdlc:goal"
LEGACY = "sdlc:needs-confirmation"
TIERS = ("P0", "P1", "P2", "P3", "P4")


# ---- A P0 goal becomes blocked by an open P3 issue: raised in the same step -------------------

def test_p0_blocked_by_p3_is_raised_in_the_step_that_marks_the_pair():
    run = w._runner({"7": {"labels": ["priority:P3"]}, "9": {"labels": ["priority:P0"]}})
    assert w._src(run).raise_blocker_priority("7", "9") == "raised"
    assert w._raised(run, 7, "priority:P0")


# ---- P3 goal blocked by P0, P1 or P2: no change on either side --------------------------------

def test_a_lower_goal_blocked_by_a_higher_blocker_changes_nothing_either_side():
    for blocker in ("P0", "P1", "P2"):
        run = w._runner({"7": {"labels": ["priority:" + blocker]}, "9": {"labels": ["priority:P3"]}})
        assert w._src(run).raise_blocker_priority("7", "9") == "unchanged", blocker
        assert w._writes(run) == [], blocker
        rep = r._runner([r._issue(9, [GOAL, "priority:P3"], body="blocked by #7"),
                         r._issue(7, [GOAL, "priority:" + blocker])])
        assert r._src(rep).repair_blocker_ranks()["violations"] == 0, blocker
        assert r._writes(rep) == [], blocker


# ---- Equal ranks at every tier P0 to P4 stay put ------------------------------------------------

def test_equal_ranks_stay_put_at_every_tier():
    for tier in TIERS:
        run = w._runner({"7": {"labels": ["priority:" + tier]}, "9": {"labels": ["priority:" + tier]}})
        assert w._src(run).raise_blocker_priority("7", "9") == "unchanged", tier
        rep = r._runner([r._issue(9, [GOAL, "priority:" + tier], body="blocked by #7"),
                         r._issue(7, [GOAL, "priority:" + tier])])
        assert r._src(rep).repair_blocker_ranks()["violations"] == 0, tier
        assert w._writes(run) == [] and r._writes(rep) == [], tier


# ---- A follow-up with no priority yet, or an unknown one, is filed at the goal's tier ----------

def test_a_follow_up_with_no_or_unknown_priority_is_filed_at_the_blocked_goals_tier(tmp_path):
    for i, own in enumerate((None, "P9", "P4")):
        kw = {} if own is None else {"priority": own}
        _, src, _ = fp._file(tmp_path / str(i), {"9": {"labels": ["priority:P0"]}}, **kw)
        assert fp._prio(src) == ["priority:P0"], own


# ---- Chains and cycles -------------------------------------------------------------------------

def test_a_two_step_chain_raises_both_blockers_to_the_head_of_the_chain():
    run = r._runner([r._issue(9, [GOAL, "priority:P0"], body="blocked by #8"),
                     r._issue(8, [GOAL, "priority:P3"], body="blocked by #7"),
                     r._issue(7, [GOAL, "priority:P4"])])
    res = r._src(run).repair_blocker_ranks()
    assert sorted(res["raised"]) == ["7", "8"]
    assert r._raised(run, 7, "priority:P0") and r._raised(run, 8, "priority:P0")


def test_a_cycle_is_reported_once_and_a_second_pass_terminates(capsys):
    issues = [r._issue(9, [GOAL, "priority:P0"], body="blocked by #8"),
              r._issue(8, [GOAL, "priority:P3"], body="blocked by #9")]
    r._src(r._runner(issues)).repair_blocker_ranks()
    assert capsys.readouterr().err.count("cycle was detected") == 1


# ---- Confirmation label: never a blocker, never raised -------------------------------------------

def test_a_legacy_confirmation_blocker_is_stripped_and_never_raised_by_any_writer():
    rep = r._runner([r._issue(9, [GOAL, "priority:P0"], body="blocked by #7"),
                     r._issue(7, [GOAL, "priority:P3", LEGACY])])
    res = r._src(rep).repair_blocker_ranks()
    assert res["stripped"] == ["7"] and res["raised"] == []
    assert not r._raised(rep, 7, "priority:P0")
    run = w._runner({"7": {"labels": ["priority:P3", LEGACY]}, "9": {"labels": ["priority:P0"]}})
    assert w._src(run).raise_blocker_priority("7", "9").startswith("skipped")
    assert w._writes(run) == []


# ---- Cross-repository blocker: reported, never silently skipped ----------------------------------

def test_a_cross_repository_blocker_is_reported(capsys):
    run = r._runner([r._issue(9, [GOAL, "priority:P0"], body="blocked by other/repo#7")])
    res = r._src(run).repair_blocker_ranks()
    assert res["unwritable"] and "not enforced" in capsys.readouterr().err


# ---- The eleven-P0 queue -------------------------------------------------------------------------

def test_a_raised_blocker_is_picked_before_eleven_waiting_p0s_and_a_p3_blocker_waits_its_tier():
    issues = [order._issue(n, "P0") for n in range(1, 12)] + [order._issue(50, "P0", order.BLOCK)]
    assert order._label_src(issues).next_pending() == "50"
    issues = [order._issue(9, "P0"), order._issue(3, "P3", order.BLOCK)]
    assert order._label_src(issues).next_pending() == "9"


# ---- A refused GraphQL budget: the REST path still works ------------------------------------------

def test_a_refused_graphql_budget_still_raises_through_rest():
    run = r._runner([r._issue(9, [GOAL, "priority:P0"], body="blocked by #7"),
                     r._issue(7, [GOAL, "priority:P3"])], fail_graphql=True)
    assert r._src(run).repair_blocker_ranks()["raised"] == ["7"]
    assert r._raised(run, 7, "priority:P0")
    assert not any("graphql" in c for c in run.calls)


# ---- Explicit off: zero calls from every entry point --------------------------------------------

def test_explicit_off_makes_zero_calls_from_every_entry_point(tmp_path):
    views = {"7": {"labels": ["priority:P3", LEGACY]}, "9": {"labels": ["priority:P0"]}}
    run = w._runner(views)
    src = w._src(run, "off")
    assert src.raise_blocker_priority("7", "9") == "skipped: off"
    assert src.strip_blocker_confirmation("7") == "skipped: off"
    assert src.repair_blocker_ranks()["skipped"] == "off"
    assert run.calls == []
    frun, fsrc, _ = fp._file(tmp_path / "f", {"9": {"labels": ["priority:P0"]}}, mode="off", priority="P3")
    assert fp._prio(fsrc) == ["priority:P3"] and frun.calls == []
    d = r._sdlc(tmp_path / "t", r._cfg("off"))
    trun = r._runner([r._issue(9, [GOAL, "priority:P0"], body="blocked by #7"),
                      r._issue(7, [GOAL, "priority:P3"])])
    r._mod("reconcile_tick").tick(str(d), config=r._cfg("off"), run=trun, now=1.0)
    assert trun.calls == [] and not (d / "state" / "blocker-repair.meta.json").exists()


def test_the_default_mode_is_on_without_any_config_key():
    run = w._runner({"7": {"labels": ["priority:P3"]}, "9": {"labels": ["priority:P0"]}})
    assert w._src(run).raise_blocker_priority("7", "9") == "raised"


# ---- Rollout: one report line, printed before the first raise -----------------------------------

def test_the_rollout_line_is_printed_once_and_before_the_first_raise(tmp_path, capsys):
    tick = r._mod("reconcile_tick")
    d = r._sdlc(tmp_path, r._cfg())
    inner = r._runner(r._p0_blocked_by_p3())
    seen = {"err": "", "at_first_raise": None}

    def run(args):
        seen["err"] += capsys.readouterr().err
        out = inner(args)
        if seen["at_first_raise"] is None and r._raised(inner, 7, "priority:P0"):
            seen["at_first_raise"] = seen["err"]
        return out
    run.calls = inner.calls
    tick.tick(str(d), config=r._cfg(), run=run, now=1000.0)
    seen["err"] += capsys.readouterr().err
    assert seen["at_first_raise"] is not None and "first run: 1 blocker(s)" in seen["at_first_raise"]
    assert seen["err"].count("first run:") == 1
    tick.tick(str(d), config=r._cfg(), run=r._runner(r._p0_blocked_by_p3()), now=1100.0)
    assert "first run" not in capsys.readouterr().err
