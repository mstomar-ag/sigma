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
    assert "unit-line-dropped" in report["why"] and [c["role"] for c in rig.calls()] == ["resolve"]


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
