"""#1492: the engine's single exit (`feature_rebase.upkeep`) writes the per-unit upkeep record, under the unit lock.

Real git (the `World` fixture of test_feature_rebase.py), driven through the documented CLI verb
`feature_rebase.py upkeep <sdlc_dir> <unit> [goal]` and the job entry. Gate closed writes nothing.
"""
import json
import subprocess

import test_feature_rebase as base

UNIT, FEATURE = base.UNIT, base.FEATURE
DIRECT = (("f.txt", "f", "feat: land a goal (#11)"), ("h.txt", "h", "quick fix, straight onto the branch"))


def _open_cfg():
    cfg = base._cfg()
    cfg["upkeep"] = {"enabled": True}
    return cfg


def _units_file(world):
    return world.sdlc / "state" / "upkeep" / "units" / (UNIT + ".json")


def _doc(world):
    return json.loads(_units_file(world).read_text(encoding="utf-8"))


def _run_verb(m, world, cfg):
    (world.sdlc / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return m.main(["feature_rebase.py", "upkeep", str(world.sdlc), UNIT, "7"])


def test_the_cli_verb_leaves_the_unit_record_truthful(tmp_path):
    m = base._mod()
    world = base.World(tmp_path).build()
    world.move_integration()
    base._filer(m)
    assert _run_verb(m, world, _open_cfg()) == 0
    doc = _doc(world)
    tip = world.tip(FEATURE)
    base_tip = base._git(world.local, "ls-remote", "origin", "refs/heads/main").split("\t")[0]
    assert doc["unit_tip"] == tip
    assert doc["base_tip"] == base_tip
    assert doc["outcome"] == "rebased"
    assert doc["last_success_at"] is not None and doc["last_attempt_at"] is not None
    assert doc["due_reason"] in ("drift", "backstop", "backstop-dormant", "state-unreadable")
    assert doc["consecutive_failures"] == 0


def test_a_failure_then_a_success_resets_the_count(tmp_path):
    m = base._mod()
    world = base.World(tmp_path).build(feature_commits=DIRECT)
    base._filer(m)
    # a direct commit on the feature branch, then the base moves: the pass refuses
    world.move_integration()
    _run_verb(m, world, _open_cfg())
    doc = _doc(world)
    assert doc["outcome"] == "direct-commits" and doc["consecutive_failures"] == 1, doc
    # a hand rebase onto the base, then the verb again
    base._git(world.local, "checkout", "-q", FEATURE)
    base._git(world.local, "rebase", "-q", "origin/main")
    base._git(world.local, "push", "-q", "--force", "origin", FEATURE)
    base._git(world.local, "checkout", "-q", "main")
    assert _run_verb(m, world, _open_cfg()) == 0
    doc = _doc(world)
    assert doc["outcome"] == "current" and doc["consecutive_failures"] == 0, doc
    assert doc["last_success_at"] is not None


def test_one_failing_pass_counts_one_failure_through_the_job_entry(tmp_path):
    """The job used to record the child's outcome again after the child recorded it."""
    m = base._mod()
    world = base.World(tmp_path).build(feature_commits=DIRECT)
    base._filer(m)
    world.move_integration()
    (world.sdlc / "config.json").write_text(json.dumps(_open_cfg()), encoding="utf-8")
    src = (base.SCRIPTS / "feature_upkeep_job.py").read_text(encoding="utf-8")
    assert "state.OUTCOMES" not in src and src.count("record_outcome(") == 2, "the job records only a killed child's failure"
    m.main(["feature_rebase.py", "upkeep", str(world.sdlc), UNIT, "7"])
    assert _doc(world)["consecutive_failures"] == 1


def test_gate_closed_writes_no_unit_file(tmp_path):
    m = base._mod()
    world = base.World(tmp_path).build()
    world.move_integration()
    base._filer(m)
    assert _run_verb(m, world, base._cfg()) == 0
    assert not (world.sdlc / "state" / "upkeep" / "units").exists()
    assert not _units_file(world).exists()
