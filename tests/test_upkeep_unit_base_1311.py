"""#1311: a `work.base` that is itself a unit branch is refused by upkeep: no unit is offered, no unit is rewritten onto it, and
the refusal says so. A default-branch base and a closed gate behave as before."""
import copy
import json

import test_feature_rebase as R
import test_upkeep_sched as T

UNIT_BASE = "feature/other"


def _config(base):
    cfg = copy.deepcopy(T.open_config())
    cfg["work"]["base"] = base
    return cfg


def test_refs_of_offers_nothing_for_a_unit_branch_base_and_is_unchanged_for_the_default_branch():
    sched = T.sched()
    assert sched.refs_of(_config(UNIT_BASE), "voice") == (None, None)
    assert sched.refs_of(_config("main"), "voice") == ("origin/main", "origin/feature/voice")


def test_the_engine_refuses_a_unit_branch_base_without_calling_either_pass(tmp_path, capsys):
    calls = []
    line = T.job().run_engine(
        _config(UNIT_BASE), T.project(tmp_path), str(tmp_path), "voice", environ=T.ENV,
        upkeep=lambda *a, **k: calls.append("older") or {"outcome": "rebased"},
        unit_pass=lambda *a, **k: calls.append("unit") or {"result": "rewritten"})
    assert calls == []
    assert line["outcome"] == "no-base" and line["unit_pass"] == "skipped"
    assert "unit branch" in line["reason"] and "default branch" in line["reason"]
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == line


def test_the_engine_is_unchanged_for_a_default_branch_base_and_a_closed_gate(tmp_path, capsys):
    seen = []

    def entry(config, sdlc_dir, unit, unit_ref, base_ref, base_name, now, **kw):
        seen.append((unit_ref, base_ref, base_name))
        return {"result": "rewritten"}
    quiet = lambda *a, **k: {"outcome": "current"}
    line = T.job().run_engine(_config("main"), T.project(tmp_path), str(tmp_path), "voice", environ=T.ENV, upkeep=quiet, unit_pass=entry)
    assert seen == [("refs/remotes/origin/feature/voice", "refs/remotes/origin/main", "main")]
    assert line == {"outcome": "current", "unit_tip": None, "unit_pass": "rewritten"}
    shut = T.job().run_engine(_config(UNIT_BASE), T.project(tmp_path), str(tmp_path), "voice", environ={}, upkeep=quiet, unit_pass=entry)
    assert shut["closed"] is True and len(seen) == 1
    capsys.readouterr()


def test_the_older_pass_does_not_rewrite_a_unit_onto_another_unit_branch(tmp_path):
    """Control on a real bare remote: the other unit branch is ahead of the integration branch, so before the fix the
    billing branch was rebased onto it and force-pushed."""
    m = R._mod()
    world = R.World(tmp_path).build()
    R._git(world.local, "checkout", "-q", "-b", UNIT_BASE, R.INTEGRATION)
    R._write(world.local / "o.txt", "o\n")
    R._git(world.local, "add", "o.txt")
    R._git(world.local, "commit", "-q", "-m", "other unit work")
    R._git(world.local, "push", "-q", "origin", UNIT_BASE)
    R._git(world.local, "checkout", "-q", R.INTEGRATION)
    before = world.tip(R.FEATURE)
    report = R._upkeep(m, world, config=R._cfg(base=UNIT_BASE))
    assert report["outcome"] == m.NO_BASE, report
    assert "unit branch" in report["why"] and "default branch" in report["why"], report
    assert world.tip(R.FEATURE) == before
