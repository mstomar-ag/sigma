"""#1516: the agent conflict level is reachable from configuration. Every test drives the FAKE `claude` of
tests/test_upkeep_level2.py (found through PATH); no real model, no network. The launcher's UNVERIFIED_FLAGS and the
placeholder catalog are untouched: the catalog is patched in these tests only. Control: break the cap check in
`_default_level2_plan` or the ceiling mapping in `resolve_stop` and `test_a_reached_spend_ceiling_*` goes red.
The documented gesture: `python -m pytest tests/test_upkeep_agent_1516.py -q`."""
import copy
import json
import os
import pathlib
import tempfile
import time

import pytest

import test_feature_rebase as base
import test_upkeep_level1 as l1
from test_upkeep_level2 import APP, FEATURE, RESOLVER_CAP, UNIT, conflict_world, mk, rig  # noqa: F401  (rig is a fixture)
import upkeep_support as support

BUDGET, CEILING = 2.0, 100.0


def agent_cfg(budget=BUDGET, ceiling=CEILING, **agent):
    config = l1.cfg(resolve="agent")
    block = {"max_budget_usd_per_unit": budget, "spend_ceiling_usd_per_day": ceiling}
    block.update(agent)
    config["upkeep"]["conflicts"]["agent"] = {k: v for k, v in block.items() if v is not _OMIT}
    return config


_OMIT = object()


@pytest.fixture
def wired(rig, monkeypatch):  # noqa: F811
    """The shipped factory, no monkeypatched seam: the fake claude on PATH, a catalog id, HOME and the temp root in the rig."""
    monkeypatch.setenv("PATH", str(rig.fake) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("HOME", str(rig.tmp / "home"))
    monkeypatch.setattr(tempfile, "tempdir", str(mk(rig.tmp / "tmp")))
    m = l1.mod()
    monkeypatch.setitem(m._launcher().CATALOG, "sonnet", "test-model-id-1")
    l1.verify_ok(m, monkeypatch)
    return m


def go_wired(rig, tmp_path, m, config, world=None):
    w = world or conflict_world(mk(tmp_path / "w"))
    report, spy = l1.run_pass(m, w, config)
    return w, report, spy


# --------------------------------------------------------------------------- the keys


@pytest.mark.parametrize("key,good,bad", [
    ("model", ["haiku", "sonnet", "opus", "fable"], ["gpt", "", None, 3, "Sonnet"]),
    ("max_budget_usd_per_unit", [None, 0.5, 3], [0, -1, "2", True, float("nan"), float("inf"), 10 ** 400]),
    ("spend_ceiling_usd_per_day", [None, 10, 99.5], [0, -0.1, "5", False, float("inf")]),
    ("timeout_minutes", [1, 30, 240], [0, 241, 1.5, "30", True]),
    ("max_units_per_sweep", [1, 5, 100], [0, 101, 2.5, None, True]),
])
def test_each_agent_key_accepts_its_range_and_refuses_the_rest(key, good, bad):
    gate = support.gate()
    for value in good:
        got = gate.read({"upkeep": {"enabled": True, "conflicts": {"agent": {key: value}}}})
        assert got.enabled and not got.problems, (key, value, got.problems)
    for value in bad:
        got = gate.read({"upkeep": {"enabled": True, "conflicts": {"agent": {key: value}}}})
        assert not got.enabled and any("conflicts.agent." + key in p for p in got.problems), (key, value)


def test_reviewer_keys_are_validated_and_a_typo_names_the_key():
    gate = support.gate()
    ok = gate.read({"upkeep": {"enabled": True, "conflicts": {"agent": {"reviewer": {"enabled": True, "model": "opus"}}}}})
    assert ok.enabled and ok.settings["conflicts.agent.reviewer.model"] == "opus"
    bad = gate.read({"upkeep": {"enabled": True, "conflicts": {"agent": {"reviewer": {"enabled": "yes"}}}}})
    assert not bad.enabled and any("conflicts.agent.reviewer.enabled" in p for p in bad.problems)
    typo = gate.read({"upkeep": {"enabled": True, "conflicts": {"agent": {"modle": "opus"}}}})
    assert not typo.enabled and any("did you mean 'model'" in p for p in typo.problems)
    broken = gate.read({"upkeep": {"enabled": True, "conflicts": {"agent": 5}}})
    assert not broken.enabled and any("upkeep.conflicts.agent" in p for p in broken.problems)


def test_defaults_are_off_by_money_and_equal_the_template():
    gate = support.gate()
    s = gate.read({}).settings
    assert s["conflicts.agent.max_budget_usd_per_unit"] is None and s["conflicts.agent.spend_ceiling_usd_per_day"] is None
    assert (s["conflicts.agent.model"], s["conflicts.agent.timeout_minutes"], s["conflicts.agent.max_units_per_sweep"]) == ("sonnet", 30, 5)
    assert s["conflicts.agent.reviewer.enabled"] is True and s["conflicts.agent.reviewer.model"] == "sonnet"
    tmpl = support.parse_template()["upkeep"]["conflicts"]["agent"]
    assert tmpl == {"model": "sonnet", "max_budget_usd_per_unit": None, "spend_ceiling_usd_per_day": None,
                    "timeout_minutes": 30, "max_units_per_sweep": 5, "reviewer": {"enabled": True, "model": "sonnet"}}


# --------------------------------------------------------------------------- acceptance 2


def test_the_shipped_factory_builds_the_plan_from_config_and_lands_one_atomic_push(rig, tmp_path, wired):
    m, w = wired, conflict_world(mk(tmp_path / "w"))
    assert m.LEVEL2_PLAN_FACTORY is not None
    w, report, spy = go_wired(rig, tmp_path, m, agent_cfg(), w)
    assert report["outcome"] == m.REBASED and report["resolved"]["level"] == 2, report.get("why")
    assert len(spy.pushes()) == 1 and report["backup"]
    assert "feature side" in base._git(w.local, "show", "origin/%s:%s" % (FEATURE, APP))
    assert [c["role"] for c in rig.calls()] == ["resolve", "review"]
    assert "sigma-resolution: 2 " in l1.remote_log(w)
    plan = m._level1_options(agent_cfg(), str(w.sdlc), str(w.local))["level2"]
    assert plan["launch"]["cap_usd"] == BUDGET and plan["launch"]["ceiling_machine_usd"] == CEILING
    assert plan["review_cap_usd"] == BUDGET / 2 and plan["launch"]["model"] == "test-model-id-1" and plan["launch"]["flags"] == ()
    assert plan["launch"]["timeout"] == 30 * 60


def test_the_reviewer_stays_mandatory_even_when_configured_off(rig, tmp_path, wired):
    m = wired
    w, report, spy = go_wired(rig, tmp_path, m, agent_cfg(reviewer={"enabled": False, "model": "sonnet"}))
    assert report["outcome"] == m.REBASED
    assert [c["role"] for c in rig.calls()] == ["resolve", "review"]


# --------------------------------------------------------------------------- acceptance 3


DROP = '''
import pathlib
for p in pathlib.Path(".").rglob("*"):
    if p.is_file() and "<<<<<<<" in p.read_text():
        keep = [l for l in p.read_text().splitlines(True)
                if l[:7] not in ("<<<<<<<", "=======", ">>>>>>>") and "feature side" not in l]
        p.write_text("".join(keep) + "resolver extra line\\n")
'''


def test_a_resolver_that_drops_a_unit_line_is_refused_parked_and_the_finding_shows_the_attempt(rig, tmp_path, wired):
    m = wired
    text = rig.binary.read_text().replace("how = mode.get(\"resolve\", \"both\")", DROP.strip() + "\nhow = 'dropped'")
    rig.binary.write_text(text)
    w = conflict_world(mk(tmp_path / "w"))
    before = w.tip(FEATURE)
    w, report, spy = go_wired(rig, tmp_path, m, agent_cfg(), w)
    assert report["outcome"] in (m.CONFLICT, m.PARKED) and not spy.pushes() and w.tip(FEATURE) == before
    assert "unit-line-dropped" in report["why"]
    assert [c["role"] for c in rig.calls()] == ["resolve"]            # the reviewer is never asked about a lossy attempt
    brief = report["park"]["brief"]
    assert "attempted resolution" in brief and "-feature side" in brief and "integration side" in brief
    assert report["level2_charged"] == BUDGET


# --------------------------------------------------------------------------- acceptance 4


def _seed_spend(m, w, amount, ceiling):
    launcher = m._launcher()
    state = os.path.join(str(w.sdlc), *m.LEVEL2_SPEND_REL.split("/"))
    now = time.time()
    ok, why = launcher.reserve_machine(state, ceiling, amount, "seed", now)
    assert ok, why
    launcher.settle_machine(state, "seed", amount, now)


def test_a_reached_spend_ceiling_parks_the_unit_with_the_text_and_calls_no_model(rig, tmp_path, wired):
    m = wired
    w = conflict_world(mk(tmp_path / "w"))
    _seed_spend(m, w, 2.0, 2.5)
    before = w.tip(FEATURE)
    w, report, spy = go_wired(rig, tmp_path, m, agent_cfg(ceiling=2.5), w)
    assert report["outcome"] in (m.CONFLICT, m.PARKED) and not spy.pushes() and w.tip(FEATURE) == before
    assert "spend ceiling reached" in report["why"] and "spend ceiling reached" in report["park"]["brief"]
    assert rig.calls() == []


# --------------------------------------------------------------------------- acceptance 5


@pytest.mark.parametrize("agent", [{"max_budget_usd_per_unit": None}, {"spend_ceiling_usd_per_day": None},
                                   {"max_budget_usd_per_unit": None, "spend_ceiling_usd_per_day": None}])
def test_agent_without_a_cap_behaves_exactly_as_off(rig, tmp_path, wired, agent):
    m = wired
    config = agent_cfg(**agent)
    assert m._level1_options(config)["level2"] is None
    w, report, spy = go_wired(rig, tmp_path, m, config)
    w2, off, spy2 = go_wired(rig, tmp_path / "off", m, l1.cfg(resolve="off"))
    assert report["outcome"] == off["outcome"] and "level 2" not in report["why"] and "level 2" not in str(report["park"])
    assert rig.calls() == [] and not spy.pushes() and "resolved" not in report and "level2_charged" not in report


def test_agent_whose_model_does_not_resolve_behaves_as_off(rig, tmp_path, wired, monkeypatch):
    m = wired
    monkeypatch.delitem(m._launcher().CATALOG, "sonnet")
    assert m._level1_options(agent_cfg())["level2"] is None
    w, report, spy = go_wired(rig, tmp_path, m, agent_cfg())
    assert rig.calls() == [] and not spy.pushes()


def test_with_the_shipped_placeholder_catalog_the_resolver_refuses_at_run_time_and_spends_nothing(rig, tmp_path, wired, monkeypatch):
    m = wired
    monkeypatch.setitem(m._launcher().CATALOG, "sonnet", m._launcher().PLACEHOLDER_MODEL)
    w, report, spy = go_wired(rig, tmp_path, m, agent_cfg())
    assert rig.calls() == [] and not spy.pushes() and "placeholder" in report["why"]


# --------------------------------------------------------------------------- max_units_per_sweep


def test_max_units_per_sweep_parks_the_rest_without_a_model_call(rig, tmp_path, wired):
    m = wired
    level2 = m._load("feature_upkeep_level2")
    w = conflict_world(mk(tmp_path / "w"))
    level2.sweep_begin(str(w.sdlc), "sweep1")
    for name in ("u1", "u2"):
        assert level2.sweep_take(str(w.sdlc), name, 2) is True
    assert level2.sweep_take(str(w.sdlc), "u1", 2) is True            # a unit already counted is not counted twice
    assert level2.sweep_take(str(w.sdlc), "u3", 2) is False
    w, report, spy = go_wired(rig, tmp_path, m, agent_cfg(max_units_per_sweep=2), w)
    assert "max_units_per_sweep reached" in report["why"] and rig.calls() == [] and not spy.pushes()
    level2.sweep_end(str(w.sdlc))
    assert level2.sweep_take(str(w.sdlc), "u9", 1) is True            # no active sweep: no limit


def test_the_sweep_opens_and_closes_the_counter_only_at_level_agent(tmp_path):
    import test_upkeep_sched as T
    import test_upkeep_window_1387 as W
    j = T.job()
    seen = []
    sdlc = T.project(tmp_path / "a")
    path = pathlib.Path(sdlc, "state/upkeep/level2-sweep.json")

    def command(unit):
        seen.append(json.loads(path.read_text()) if path.exists() else None)
        return W.engine([], {})(unit)
    config = T.open_config()
    config["upkeep"]["conflicts"]["resolve"] = "agent"
    j.run_sweep(config, sdlc, str(tmp_path), "run1", 600, environ=W.ENV, command=command, list_units=lambda _s: ["a"], now=W.NOW)
    assert seen[0]["run_id"] == "run1" and seen[0]["active"] is True and json.loads(path.read_text())["active"] is False
    seen.clear()
    sdlc2 = T.project(tmp_path / "b")
    path = pathlib.Path(sdlc2, "state/upkeep/level2-sweep.json")
    j.run_sweep(T.open_config(), sdlc2, str(tmp_path), "run2", 600, environ=W.ENV, command=command, list_units=lambda _s: ["a"], now=W.NOW)
    assert seen == [None] and not path.exists()


# --------------------------------------------------------------------------- the seam stays testable, the closed gate is unchanged


def test_a_monkeypatched_factory_still_wins_and_the_closed_gate_never_reaches_it(rig, tmp_path, monkeypatch):
    m = l1.mod()
    tripped = []
    monkeypatch.setattr(m, "LEVEL2_PLAN_FACTORY", lambda config: tripped.append(1))
    for config in (l1.cfg(open_=False), l1.cfg(resolve="off"), l1.cfg(resolve="mechanical")):
        assert (m._level1_options(config) or {}).get("level2") is None
    assert tripped == []
    assert m._level1_options(agent_cfg())["level2"] is None and tripped == [1]


# --------------------------------------------------------------------------- the doctor rows


def _doctor():
    import test_doctor_resolver_readiness as D
    return D, D.doctor()


def _agent_doctor_cfg(**agent):
    import test_doctor_resolver_readiness as D
    cfg = D.config()
    cfg["upkeep"]["conflicts"]["agent"].update(agent)
    return cfg


def test_doctor_says_why_an_unconfigured_agent_behaves_as_off(tmp_path):
    D, d = _doctor()
    cfg = _agent_doctor_cfg()
    found = D.rows(d, D.project(tmp_path, cfg), cfg)
    row = next(c for c in found if c["name"].startswith("resolver configured"))
    assert row["ok"] is False and "behaves as off" in row["name"] and "max_budget_usd_per_unit" in row["name"] and row["fix"]
    assert not any("no effect in this release" in c["fix"] for c in found)


def test_doctor_reports_the_configured_caps_todays_spend_and_the_last_resolution(tmp_path, monkeypatch):
    D, d = _doctor()
    cfg = _agent_doctor_cfg(max_budget_usd_per_unit=2, spend_ceiling_usd_per_day=5)
    sdlc = D.project(tmp_path, cfg)
    rebase = l1.mod()
    monkeypatch.setitem(rebase._launcher().CATALOG, "sonnet", "test-model-id-1")
    state = sdlc / "state" / "upkeep" / "spend"
    launcher = rebase._launcher()
    ok, _ = launcher.reserve_machine(str(state), 50.0, 2.0, "seed", time.time())
    launcher.settle_machine(str(state), "seed", 2.0, time.time())
    res = sdlc / "state" / "upkeep" / "resolutions"
    res.mkdir(parents=True)
    (res / "u-abc.json").write_text(json.dumps({"level": 2, "verdict": "approve"}))
    (sdlc / "state" / "features").mkdir(parents=True)
    (sdlc / "state" / "features" / "u.rebase-parked.json").write_text("{}")
    found = D.rows(d, sdlc, cfg, which=lambda name: "/bin/" + name)
    by = {c["name"]: c for c in found}
    assert any(n.startswith("resolver configured: model sonnet, up to $2 per unit, $5 per day") for n in by)
    assert "resolver spend today: $2.00 of $5" in by and by["resolver spend today: $2.00 of $5"]["ok"] is True
    assert "last resolution: level 2, verdict approve, pushed" in by and "units parked on conflict: 1" in by
    cfg2 = _agent_doctor_cfg(max_budget_usd_per_unit=2, spend_ceiling_usd_per_day=2)
    again = D.rows(d, D.project(tmp_path / "x", cfg2), cfg2)
    assert not any(c["name"].startswith("resolver spend today") and c["ok"] is False for c in again)   # nothing spent there


def test_doctor_rows_are_absent_unless_resolve_is_agent(tmp_path):
    D, d = _doctor()
    for resolve in ("off", "mechanical"):
        cfg = D.config(resolve)
        assert D.rows(d, D.project(tmp_path / resolve, cfg), cfg) == []
