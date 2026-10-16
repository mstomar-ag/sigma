"""#1559: `feature_rebase.py preview`, the sweep-level dry run, against a real throwaway repo and bare remote.

The prediction for a conflict is a `git merge-tree` probe, so a rebase may differ; the tests pin the probe's answer."""
import json
import os
import pathlib
import subprocess
import sys

import test_feature_rebase as base

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "sigma-loop" / "scripts" / "feature_rebase.py"
git, write = base._git, base._write


def _unit(world, name, files, subject="feat: land a goal (#11)", from_ref="main"):
    """A registered unit whose remote branch carries `files` on top of `from_ref`."""
    registry = base._load("feature_registry")
    branch = "feature/" + name
    git(world.local, "checkout", "-q", "-b", branch, from_ref)
    for fname, body in files:
        write(world.local / fname, body + "\n")
        git(world.local, "add", fname)
        git(world.local, "commit", "-q", "-m", subject)
    git(world.local, "push", "-q", "origin", branch)
    git(world.local, "checkout", "-q", "main")
    registry.write_unit(registry.registry_dir(world.sdlc), name,
                        {"open": True, "repos": {base.REPO: {"branch": branch, "goals": []}}})


def _world(tmp_path):
    world = base.World(tmp_path).build(feature_commits=None)
    (world.sdlc / "config.json").write_text(json.dumps(base._cfg()))
    git(world.local, "fetch", "-q", "origin")
    return world


def _rows(result):
    return {r["unit"]: r for r in result["units"]}


def _refs(world):
    return git(world.local, "for-each-ref", "--format=%(refname) %(objectname)")


def _tree(world):
    return sorted(str(p.relative_to(world.local)) for p in world.local.rglob("*")
                  if ".git" not in p.relative_to(world.local).parts and p.is_file())


def _cli(world, *extra):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    return subprocess.run([sys.executable, str(SCRIPT), "preview", str(world.sdlc), *extra],
                          capture_output=True, text=True, env=env)


def _five_units(world):
    _unit(world, "ff", [])                                      # same tip as main: nothing to carry
    _unit(world, "clean", [("clean.txt", "x")])
    _unit(world, "clash", [("seed.txt", "feature edit")])
    _unit(world, "direct", [("d.txt", "d")], subject="a direct commit")
    world.move_integration(name="seed.txt", body="integration edit")   # main moves: clean/clash/direct fall behind
    _unit(world, "cur", [("c.txt", "c")])                      # cut after main moved: the base is already inside
    git(world.local, "fetch", "-q", "origin")


def test_each_outcome_is_predicted(tmp_path):
    world = _world(tmp_path)
    _five_units(world)
    result = base._mod().preview(str(world.sdlc), base._cfg())
    rows = _rows(result)
    assert rows["cur"]["outcome"] == "current"
    assert rows["ff"]["outcome"] == "fast-forward"
    assert rows["clean"]["outcome"] == "clean rebase"
    assert rows["clash"]["outcome"] == "conflict" and rows["clash"]["files"] == ["seed.txt"]
    assert rows["direct"]["outcome"] == "refused" and "direct" in rows["direct"]["why"]
    assert result["as_of"] == "last fetch"


def test_missing_remote_branch_is_refused(tmp_path):
    world = _world(tmp_path)
    registry = base._load("feature_registry")
    registry.write_unit(registry.registry_dir(world.sdlc), "ghost",
                        {"open": True, "repos": {base.REPO: {"branch": "feature/ghost", "goals": []}}})
    row = _rows(base._mod().preview(str(world.sdlc), base._cfg()))["ghost"]
    assert row["outcome"] == "refused" and "feature/ghost" in row["why"]


def test_no_base_names_it_and_lists_nothing(tmp_path):
    world = _world(tmp_path)
    _unit(world, "clean", [("clean.txt", "x")])
    result = base._mod().preview(str(world.sdlc), base._cfg(base=""))
    assert result["units"] == [] and any("work.base" in n for n in result["notes"])


def test_include_exclude_selects_units(tmp_path):
    world = _world(tmp_path)
    _unit(world, "keep", [("k.txt", "k")])
    _unit(world, "skip", [("s.txt", "s")])
    cfg = base._cfg()
    cfg["upkeep"] = {"units": {"exclude": ["skip", "billing"]}}
    assert list(_rows(base._mod().preview(str(world.sdlc), cfg))) == ["keep"]


def test_it_moves_nothing_and_writes_nothing_with_the_gate_closed(tmp_path):
    world = _world(tmp_path)
    _five_units(world)
    before_refs, before_tree = _refs(world), _tree(world)
    remote_before = git(world.remote, "for-each-ref")
    cfg = base._cfg()
    assert "upkeep" not in cfg                                 # the gate is closed, and it still previews
    result = base._mod().preview(str(world.sdlc), cfg)
    assert len(result["units"]) == 6
    assert _refs(world) == before_refs and _tree(world) == before_tree
    assert git(world.remote, "for-each-ref") == remote_before
    assert git(world.local, "worktree", "list").count("\n") == 0


def test_the_documented_gesture_prints_json_and_exits_zero(tmp_path):
    world = _world(tmp_path)
    _unit(world, "clean", [("clean.txt", "x")])
    world.move_integration()
    git(world.local, "fetch", "-q", "origin")
    done = _cli(world)
    assert done.returncode == 0, done.stderr
    assert _rows(json.loads(done.stdout))["clean"]["outcome"] == "clean rebase"
    assert _cli(world, "--bogus").returncode == 2


def test_fetch_flag_refreshes_the_tracking_refs(tmp_path):
    world = _world(tmp_path)
    _unit(world, "clean", [("clean.txt", "x")])
    world.move_integration()                                    # pushed, but never fetched into `origin/main`
    stale = _rows(base._mod().preview(str(world.sdlc), base._cfg()))["clean"]
    fresh = base._mod().preview(str(world.sdlc), base._cfg(), fetch=True)
    assert fresh["as_of"] == "fetched now"
    assert stale["outcome"] in ("current", "fast-forward", "clean rebase")
    assert _rows(fresh)["clean"]["outcome"] == "clean rebase"
