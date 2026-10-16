"""#1554: the agent level runs with no spend limit configured. Same fake-claude harness as test_upkeep_agent_1516.py;
no real model, no network. Controls: re-require the caps in feature_rebase.level2_off_reason, and drop the
would-drop check (the lossy resolution then lands) -- each seen red once."""
import pytest

import test_upkeep_level1 as l1
from test_upkeep_agent_1516 import DROP, agent_cfg, go_wired, wired  # noqa: F401  (wired is a fixture)
from test_upkeep_level2 import FEATURE, conflict_world, mk, rig  # noqa: F401  (rig is a fixture)


def test_off_reason_is_none_with_unset_caps_and_unchanged_with_set_caps(wired):
    m = wired
    which = lambda name: "/bin/" + name  # noqa: E731
    assert m.level2_off_reason(agent_cfg(budget=None, ceiling=None), which) is None
    assert m.level2_off_reason(agent_cfg(), which) is None
    assert "claude" in m.level2_off_reason(agent_cfg(budget=None, ceiling=None), lambda name: None)


def test_no_budget_configured_resolves_runs_the_proofs_and_pushes(rig, tmp_path, wired):
    m = wired
    w = conflict_world(mk(tmp_path / "w"))
    w, report, spy = go_wired(rig, tmp_path, m, agent_cfg(budget=None, ceiling=None), w)
    assert report["outcome"] == m.REBASED and report["resolved"]["level"] == 2, report.get("why")
    assert len(spy.pushes()) == 1 and report["backup"]
    assert [c["role"] for c in rig.calls()] == ["resolve", "review"]
    assert "sigma-resolution: 2 " in l1.remote_log(w)
    for call in rig.calls():
        assert "--max-budget-usd" not in call["argv"]


def test_a_dropped_line_is_still_refused_and_parked_with_no_budget(rig, tmp_path, wired):
    m = wired
    rig.binary.write_text(rig.binary.read_text().replace("how = mode.get(\"resolve\", \"both\")", DROP.strip() + "\nhow = 'dropped'"))
    w = conflict_world(mk(tmp_path / "w"))
    before = w.tip(FEATURE)
    w, report, spy = go_wired(rig, tmp_path, m, agent_cfg(budget=None, ceiling=None), w)
    assert report["outcome"] in (m.CONFLICT, m.PARKED) and not spy.pushes() and w.tip(FEATURE) == before
    assert "unit-line-dropped" in report["why"] and set(c["role"] for c in rig.calls()) == {"resolve"}   # session mode retries the refused proof (#1692)


def test_doctor_row_names_the_mode_in_both_cases(tmp_path):
    import test_upkeep_agent_1516 as A
    D, d = A._doctor()
    which = lambda name: "/bin/" + name  # noqa: E731
    for kwargs, words in (({}, ("no spend limit",)),
                          ({"max_budget_usd_per_unit": 2, "spend_ceiling_usd_per_day": 5}, ("up to $2 per unit", "$5 per day"))):
        cfg = A._agent_doctor_cfg(**kwargs)
        found = D.rows(d, D.project(tmp_path / str(len(kwargs)), cfg), cfg, which=which)
        row = next(c for c in found if c["name"].startswith("resolver configured"))
        assert row["ok"] is True and all(w in row["name"] for w in words), row["name"]


def test_a_daily_ceiling_without_a_per_unit_budget_behaves_as_off(wired):
    which = lambda name: "/bin/" + name  # noqa: E731
    reason = wired.level2_off_reason(agent_cfg(budget=None, ceiling=5), which)
    assert reason and "max_budget_usd_per_unit" in reason


# --------------------------------------------------------------------------- #1692: the attempt record belongs to a conflict


def _l2():
    import test_feature_rebase as base
    return base._load("feature_upkeep_level2")


def test_1692_a_moved_base_starts_the_attempt_count_fresh_and_a_same_conflict_stays_used(tmp_path):
    level2, unit = _l2(), "u"
    import test_feature_rebase as base
    unit = base.UNIT
    for n in range(2):
        level2.begin_attempt(str(tmp_path), unit, "r%d" % n, 1000 + n, key="unit1:base1")
        level2.settle_attempt(str(tmp_path), unit, "r%d" % n, "markers", 1.0)
    assert level2.attempts_used(str(tmp_path), unit, key="unit1:base1") == 2
    assert level2.attempts_used(str(tmp_path), unit, key="unit1:base2") == 0          # the base tip moved
    assert level2.attempts_used(str(tmp_path), unit) == 2                              # no key: the old whole-unit count


def test_1692_a_started_entry_that_never_settled_is_not_a_used_attempt(tmp_path):
    level2 = _l2()
    import test_feature_rebase as base
    for n in range(3):
        level2.begin_attempt(str(tmp_path), base.UNIT, "killed%d" % n, 1000 + n, key="k")      # killed by the sweep cap or a crash
    assert level2.attempts_used(str(tmp_path), base.UNIT, key="k") == 0
    assert level2.attempts_used(str(tmp_path), base.UNIT) == 3                          # keyless (legacy) counting is unchanged


def test_1692_old_entries_age_out_when_a_keyed_attempt_begins(tmp_path):
    level2 = _l2()
    import test_feature_rebase as base
    level2.begin_attempt(str(tmp_path), base.UNIT, "old", 1000, key="k0")
    level2.settle_attempt(str(tmp_path), base.UNIT, "old", "markers", 0.0)
    level2.begin_attempt(str(tmp_path), base.UNIT, "new", 1000 + level2.ATTEMPT_MAX_AGE_SECONDS + 1, key="k1")
    assert [i["run_id"] for i in level2.read_attempts(str(tmp_path), base.UNIT)] == ["new"]


def test_1692_a_unit_with_two_failed_attempts_is_retried_after_the_base_moves(rig, tmp_path, wired):
    """Through the shipped path in session mode: two used attempts under an old key do not block the run."""
    import sys
    import test_feature_rebase as base
    from test_upkeep_session_1573 import STUB, resolves, run_session
    rig.binary.write_text(STUB % {"python": sys.executable, "dir": str(rig.fake)})
    w = conflict_world(mk(tmp_path / "w"))
    level2 = _l2()
    for n in range(2):
        level2.begin_attempt(str(w.sdlc), base.UNIT, "old%d" % n, 1000 + n, key="oldunit:oldbase")
        level2.settle_attempt(str(w.sdlc), base.UNIT, "old%d" % n, "dropped", 1.0)
    m, w, report, spy = run_session(rig, tmp_path, world=w)
    assert report["outcome"] == m.REBASED, report.get("why")
    assert len(resolves(rig)) == 1 and len(spy.pushes()) == 1
