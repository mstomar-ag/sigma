"""#1536: agent level hardening (follow-up of #1516). Drives the FAKE `claude` of tests/test_upkeep_level2.py; no real model.
Controls: remove the run-id comparison in `sweep_take` and the replaced-file test goes red; remove the base-side check and the
base-line scenario goes red. The documented gesture: `python -m pytest tests/test_upkeep_agent_1536.py -q`."""
import json
import pathlib

import pytest

import test_upkeep_agent_1516 as A
import test_upkeep_level1 as l1
from test_upkeep_agent_1516 import agent_cfg, go_wired, wired  # noqa: F401  (wired is a fixture)
from test_upkeep_level2 import FEATURE, conflict_world, mk, rig  # noqa: F401  (rig is a fixture)

DROP_BASE = '''
import pathlib
for p in pathlib.Path(".").rglob("*"):
    if p.is_file() and "<<<<<<<" in p.read_text():
        keep = [l for l in p.read_text().splitlines(True)
                if l[:7] not in ("<<<<<<<", "=======", ">>>>>>>") and "integration side" not in l]
        p.write_text("".join(keep))
'''


def _counter(sdlc, text):
    return A._sweep_file(sdlc, text)


def test_a_sweep_child_fails_closed_when_the_counter_is_missing_or_belongs_to_another_run(tmp_path, monkeypatch):
    level2 = A._l2()
    monkeypatch.setenv(level2.SWEEP_RUN_ENV, "run1")
    assert level2.sweep_take(str(tmp_path), "u1", 5) is False                   # deleted mid-sweep
    _counter(tmp_path, '{"active": true, "run_id": "other", "units": []}')
    assert level2.sweep_take(str(tmp_path), "u1", 5) is False                   # replaced by another run
    _counter(tmp_path, '{"active": false, "run_id": "", "units": []}')
    assert level2.sweep_take(str(tmp_path), "u1", 5) is False                   # inactive
    _counter(tmp_path, '{"active": true, "run_id": "run1", "units": []}')
    assert level2.sweep_take(str(tmp_path), "u1", 5) is True


def test_without_the_variable_the_single_unit_pass_keeps_todays_behaviour(tmp_path, monkeypatch):
    level2 = A._l2()
    monkeypatch.delenv(level2.SWEEP_RUN_ENV, raising=False)
    assert level2.sweep_take(str(tmp_path), "u1", 5) is True
    _counter(tmp_path, '{"active": false, "run_id": "", "units": []}')
    assert level2.sweep_take(str(tmp_path), "u1", 5) is True


def test_the_sweep_hands_its_run_id_to_each_child(tmp_path):
    import test_upkeep_sched as T
    import test_upkeep_window_1387 as W
    j = T.job()
    sdlc = T.project(tmp_path / "a")
    config = T.open_config()
    config["upkeep"]["conflicts"]["resolve"] = "agent"
    seen = []
    real = j._sibling("bounded_run").run_group

    def spy(argv, root, cap, env=None, **kw):
        seen.append((env or {}).get("SIGMA_UPKEEP_SWEEP_RUN"))
        return real(argv, root, cap, env=env, **kw)
    bounded = j._sibling("bounded_run")
    orig, bounded.run_group = bounded.run_group, spy
    try:
        j.run_sweep(config, sdlc, str(tmp_path), "run7", 600, environ=W.ENV, command=lambda u: W.engine([], {})(u),
                    list_units=lambda _s: ["a"], now=W.NOW)
    finally:
        bounded.run_group = orig
    assert seen == ["run7"]


def test_a_resolver_that_drops_a_base_added_line_is_refused_parked_and_names_the_line(rig, tmp_path, wired):
    m = wired
    rig.binary.write_text(rig.binary.read_text().replace("how = mode.get(\"resolve\", \"both\")", DROP_BASE.strip() + "\nhow = 'dropped'"))
    w = conflict_world(mk(tmp_path / "w"))
    before = w.tip(FEATURE)
    w, report, spy = go_wired(rig, tmp_path, m, agent_cfg(), w)
    assert report["outcome"] in (m.CONFLICT, m.PARKED) and not spy.pushes() and w.tip(FEATURE) == before
    assert "base-line-dropped" in report["why"] and "integration side" in report["why"]
    assert [c["role"] for c in rig.calls()] == ["resolve"]


def test_dropped_base_lines_is_a_trimmed_multiset_and_bounded():
    level2 = A._l2()
    assert level2.dropped_base_lines("a\n", "a\n  x \nx\n", "a\nx\n") == ["x"]
    assert level2.dropped_base_lines("a\n", "a\nx\n", "a\n  x\n") == []
    assert level2.dropped_base_lines("a\n", "a\n" + "".join("l%d\n" % i for i in range(20)), "a\n") == sorted("l%d" % i for i in range(20))


def test_doctor_fails_the_configured_row_on_the_placeholder_model(tmp_path):
    D, d = A._doctor()
    cfg = A._agent_doctor_cfg(max_budget_usd_per_unit=2, spend_ceiling_usd_per_day=5)
    found = D.rows(d, D.project(tmp_path, cfg), cfg, which=lambda name: "/bin/" + name)
    row = next(c for c in found if c["name"].startswith("resolver configured"))
    assert row["ok"] is False and "placeholder" in row["name"]


@pytest.mark.parametrize("resolve", ["off", "mechanical"])
def test_doctor_has_no_rows_unless_resolve_is_agent(tmp_path, resolve):
    D, d = A._doctor()
    cfg = D.config(resolve)
    assert D.rows(d, D.project(tmp_path, cfg), cfg) == []
