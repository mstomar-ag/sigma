"""#1532: tidy of the per-unit upkeep record. Real git (the World fixture of test_feature_rebase.py), driven through the
engine's documented CLI verb and the unit pass's entry point.

- a failed base-tip read still records the outcome, with a null base tip;
- a `current` outcome whose report carries no usable tip refreshes the stored unit tip from the checkout;
- the landed-unit skip is the only writer on its path and its record is unchanged."""
import json

import test_feature_rebase as base
import upkeep_support as support

UNIT, FEATURE = base.UNIT, base.FEATURE


def _cfg():
    cfg = base._cfg()
    cfg["upkeep"] = {"enabled": True}
    return cfg


def _file(world):
    return world.sdlc / "state" / "upkeep" / "units" / (UNIT + ".json")


def _doc(world):
    return json.loads(_file(world).read_text(encoding="utf-8"))


def _verb(m, world):
    (world.sdlc / "config.json").write_text(json.dumps(_cfg()), encoding="utf-8")
    return m.main(["feature_rebase.py", "upkeep", str(world.sdlc), UNIT, "7"])


def test_a_failed_base_tip_read_still_records_the_outcome(tmp_path):
    m = base._mod()
    world = base.World(tmp_path).build()
    base._filer(m)
    real = m._git_read

    def failing(cwd, args, deadline=None):
        if args[:1] == ["rev-parse"]:
            raise RuntimeError("base tip unreadable")
        return real(cwd, args, deadline)
    m._git_read = failing
    assert _verb(m, world) == 0
    doc = _doc(world)
    assert doc["outcome"] == "current" and doc["base_tip"] is None
    assert doc["unit_tip"] == world.tip(FEATURE)


def test_a_current_outcome_without_a_report_tip_refreshes_the_unit_tip(tmp_path):
    m = base._mod()
    world = base.World(tmp_path).build()
    base._filer(m)
    assert _verb(m, world) == 0
    stale = dict(_doc(world), unit_tip="a" * 40)
    _file(world).write_text(json.dumps(stale), encoding="utf-8")
    real = m._run

    def abbreviating(cwd, argv):                 # the pass reads an abbreviated tip: not usable as a record
        out = real(cwd, argv)
        return out[:8] if argv[:2] == ["git", "rev-parse"] and FEATURE in argv[-1] else out
    m._run = abbreviating
    assert _verb(m, world) == 0
    doc = _doc(world)
    assert doc["outcome"] == "current" and doc["unit_tip"] == world.tip(FEATURE)


def test_the_landed_unit_skip_is_the_only_writer_on_its_path(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    import test_upkeep_pass as p
    p.git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a")
    p.git(root, "add", "a.txt")
    p.git(root, "commit", "-q", "-m", "a")
    p.git(root, "branch", "feature/u")
    (root / ".sdlc").mkdir()
    p.commit(root, "main", "b.txt")
    result = p.call(support.script("feature_upkeep_pass"), root)
    assert result["recorded"] is True and p.state_files(root) == ["u.json"]
    doc = json.loads(next((root / ".sdlc").rglob("u.json")).read_text())
    assert (doc["outcome"], doc["unit_tip"], doc["base_tip"]) == ("current", result["unit_tip"], result["base_tip"])
