"""#942 (part B, slice 1): engine hygiene under the upkeep gate.

Covers the replay config pin (rerere off, no signing), the stop with no unmerged path decided at the replay's call
site, the NUL-safe conflicted-path and stage reader, the emptiness check now living in sigma-loop, the `conflicts`
keys the gate reads, and the recording-trap additions for the new entry points.

MEASURED HERE, WITH REAL GIT (design doubts D-8 and D-6, slice 1):
  * `rr-cache` IS shared with the throwaway worktree through the common git dir: a resolution recorded in the main
    checkout is reused by the replay in the worktree. (If it were not shared, the unpinned replay below would stop
    with a conflicting file exactly as the pinned one does, and `test_rerere_unpinned_stops_with_no_unmerged_path`
    would be red.)
  * the rebase STILL STOPS after an autoUpdate reuse: it stops with the path staged and no unmerged path.
D-6 (the emptiness check at a MERGE stop) stays unverified and is stated as such in `conflict_state`."""
import copy
import importlib.util
import json
import os
import pathlib
import sys

import pytest

import attempt_trap
import test_feature_rebase as base
import upkeep_support as support

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
WALK = ROOT / "skills" / "sigma-rebase" / "scripts" / "conflict_walk.py"

OPEN = {"upkeep": {"enabled": True}}


def _cfg(extra=None):
    cfg = base._cfg()
    cfg.update(copy.deepcopy(extra or {}))
    return cfg


def _conflicted_world(tmp_path):
    world = base.World(tmp_path).build(
        feature_commits=(("seed.txt", "feature edit", "feat: touch the seed (#11)"),))
    world.move_integration(name="seed.txt", body="integration edit")
    return world


def _record_a_resolution(world):
    """Resolve the conflict once in the main checkout with rerere on, so `rr-cache` holds it, then put the feature
    branch back where it was."""
    local = world.local
    tip = base._git(local, "rev-parse", "origin/" + base.FEATURE)
    base._git(local, "config", "rerere.enabled", "true")
    base._git(local, "checkout", "-q", base.FEATURE)
    with pytest.raises(AssertionError):
        base._git(local, "rebase", base.INTEGRATION)
    base._write(local / "seed.txt", "resolved\n")
    base._git(local, "add", "seed.txt")
    base._git(local, "-c", "core.editor=true", "rebase", "--continue")
    base._git(local, "reset", "-q", "--hard", tip)
    base._git(local, "checkout", "-q", base.INTEGRATION)
    base._git(local, "config", "rerere.autoUpdate", "true")
    assert any((local / ".git" / "rr-cache").iterdir()), "the control needs a recorded resolution"


# --------------------------------------------------------------------------- the pin


def test_pin_env_appends_the_pins_after_the_callers_own_and_never_edits_argv():
    m = base._mod()
    out = m.pin_env({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c", "X": "y"})
    n = 1 + len(m.REPLAY_CONFIG)
    assert out["GIT_CONFIG_COUNT"] == str(n) and out["X"] == "y" and out["GIT_CONFIG_KEY_0"] == "a.b"
    pairs = {out["GIT_CONFIG_KEY_%d" % i]: out["GIT_CONFIG_VALUE_%d" % i] for i in range(n)}
    assert pairs["rerere.enabled"] == "false" and pairs["rerere.autoUpdate"] == "false"
    assert pairs["commit.gpgSign"] == "false", "no signing is stated through the same seam"
    assert m.pin_env({"GIT_CONFIG_COUNT": "junk"})["GIT_CONFIG_COUNT"] == str(len(m.REPLAY_CONFIG))


def test_runner_is_untouched_while_the_gate_is_closed():
    """Empty config, the shipped template and the near-enabled matrix all keep the runner the very same object."""
    m = base._mod()

    def run(cwd, argv):
        return ""
    template = support.template_cfg()
    near = [{}, template, _cfg(), {"upkeep": {"enabled": "true"}},
            {"upkeep": {"conflicts": {"resolve": "agent"}}},
            {"upkeep": {"enabled": False, "conflicts": {"resolve": "mechanical"}}},
            {"upkeep": {"enabled": True, "conflicts": {"resolve": "Agent"}}}]
    with attempt_trap.AttemptTrap() as trap:
        for config in near:
            assert m.pinned_run(run, config) is run, config
    assert trap.quiet, "a closed gate must not launch a process, touch the network or write a file"


def test_runner_is_pinned_once_the_gate_opens():
    import os
    m = base._mod()
    seen = []

    def run(cwd, argv):
        seen.append((list(argv), dict(os.environ)))
        return ""
    before = dict(os.environ)
    wrapped = m.pinned_run(run, OPEN)
    assert wrapped is not run
    wrapped("/x", ["git", "status"])
    argv, env = seen[0]
    assert argv == ["git", "status"], "the argv is spelled exactly as before the gate"
    assert env["GIT_CONFIG_COUNT"] and "rerere.enabled" in env.values()
    assert dict(os.environ) == before, "the environment is restored after the call"


def _recording_run(m, argvs, envs):
    real = m._run

    def run(cwd, argv):
        argvs.append(list(argv))
        if argv and argv[0] == "git":
            envs.append("rerere.enabled" in os.environ.values())
        return real(cwd, argv)
    return run


def test_every_engine_git_call_is_pinned_under_the_gate_and_none_when_closed(tmp_path):
    m = base._mod()
    base._filer(m)
    for sub, config, pinned in (("closed", _cfg(), False), ("open", _cfg(OPEN), True)):
        (tmp_path / sub).mkdir()
        world = base.World(tmp_path / sub).build()
        world.move_integration()
        argvs, envs = [], []
        report = m.upkeep(str(world.sdlc), config, "7", base.UNIT, run=_recording_run(m, argvs, envs))
        assert report["outcome"] == m.REBASED, report
        gits = [a for a in argvs if a and a[0] == "git"]
        assert gits
        assert not any("-c" in a for a in gits), (sub, gits)   # argv is never edited, gate open or closed
        assert all(envs) if pinned else not any(envs), (sub, envs)


# --------------------------------------------------------------------------- rerere, measured


def test_rerere_pinned_still_sees_the_conflict(tmp_path):
    """With an existing `rr-cache`, local rerere on and autoUpdate on, the pinned replay stops WITH a conflicting file."""
    m = base._mod()
    filed = base._filer(m)
    world = _conflicted_world(tmp_path)
    _record_a_resolution(world)
    before = world.dirt()
    report = m.upkeep(str(world.sdlc), _cfg(OPEN), "7", base.UNIT)
    assert report["outcome"] == m.CONFLICT, report
    assert world.dirt() == before and len(filed) == 1


def test_rerere_unpinned_stops_with_no_unmerged_path(tmp_path, monkeypatch):
    """CONTROL (seen red by removing the pin): without the pin the same fixture reuses the recorded resolution, the
    rebase STILL STOPS, and no path is unmerged, so under the gate the outcome is FAILED. This is the measurement of
    both D-8 claims: the cache is shared with the throwaway worktree, and a stop survives an autoUpdate."""
    m = base._mod()
    base._filer(m)
    monkeypatch.setattr(m, "REPLAY_CONFIG", ())
    world = _conflicted_world(tmp_path)
    _record_a_resolution(world)
    report = m.upkeep(str(world.sdlc), _cfg(OPEN), "7", base.UNIT)
    assert report["outcome"] == m.FAILED, report
    assert "no unmerged path" in report["why"], report


def test_closed_gate_keeps_the_old_outcome_for_a_stop_with_no_unmerged_path(tmp_path):
    """Byte-identical while closed: the same rerere fixture is still CONFLICT, as before this change."""
    m = base._mod()
    base._filer(m)
    world = _conflicted_world(tmp_path)
    _record_a_resolution(world)
    report = m.upkeep(str(world.sdlc), _cfg(), "7", base.UNIT)
    assert report["outcome"] == m.CONFLICT, report


def _stub_world(unmerged):
    """A runner standing in for a stopped rebase, with `unmerged` as the answer to the unmerged-path read."""
    calls = []

    def run(cwd, argv):
        calls.append(list(argv))
        if argv[:2] == ["git", "rebase"]:
            raise RuntimeError("stopped")
        if argv[:2] == ["git", "ls-files"]:
            return unmerged
        return ""
    return run, calls


@pytest.mark.parametrize("strict, unmerged, want", [
    (True, "", "FAILED"), (False, "", "CONFLICT"),
    (True, "100644 abc 2\tf.txt\0", "CONFLICT"), (False, "100644 abc 2\tf.txt\0", "CONFLICT")])
def test_the_decision_is_at_the_replay_call_site(tmp_path, monkeypatch, strict, unmerged, want):
    m = base._mod()
    monkeypatch.setattr(m, "rebase_stopped", lambda run, path: True)
    run, _calls = _stub_world(unmerged)
    report = {}
    outcome = m._rebase_feature(run, str(tmp_path), tmp_path / "wt", "feature/x", "main", "abc", "origin", report, strict)
    assert outcome == getattr(m, want), (strict, unmerged, report)


def test_an_unreadable_unmerged_set_is_not_a_conflict(tmp_path, monkeypatch):
    m = base._mod()
    monkeypatch.setattr(m, "rebase_stopped", lambda run, path: True)

    def run(cwd, argv):
        if argv[:2] == ["git", "rebase"]:
            raise RuntimeError("stopped")
        if argv[:2] == ["git", "ls-files"]:
            raise RuntimeError("index unreadable")
        return ""
    report = {}
    assert m._rebase_feature(run, str(tmp_path), tmp_path / "wt", "f", "main", "abc", "o", report, True) == m.FAILED
    assert "could not be read" in report["why"]


def test_the_predicate_itself_is_not_changed(tmp_path):
    """`rebase_stopped` still answers only "is a rebase in progress": a stop with no unmerged path is True."""
    m = base._mod()
    world = _conflicted_world(tmp_path)
    _record_a_resolution(world)
    local = world.local
    base._git(local, "checkout", "-q", base.FEATURE)
    try:
        base._git(local, "rebase", base.INTEGRATION)
    except AssertionError:
        pass
    assert base._git(local, "diff", "--name-only", "--diff-filter=U") == ""
    assert m.rebase_stopped(m._run, str(local)) is True


# --------------------------------------------------------------------------- the reader


def _state():
    spec = importlib.util.spec_from_file_location("conflict_state", SCRIPTS / "conflict_state.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(cwd, argv):
    import subprocess
    p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr or p.stdout)
    return p.stdout.strip()


ODD = 'we ird "q" é.txt'


def test_reader_is_nul_safe_for_odd_paths(tmp_path):
    cs = _state()
    repo = tmp_path / "r"
    repo.mkdir()
    base._git(repo, "init", "-q", "-b", "main")
    for name in (ODD, "plain.txt", "clean.txt"):
        base._write(repo / name, "base\n")
    base._write(repo / "clean.txt", "same\n")
    base._git(repo, "add", "-A")
    base._git(repo, "commit", "-qm", "base")
    base._git(repo, "checkout", "-qb", "other")
    for name in (ODD, "plain.txt"):
        base._write(repo / name, "other\n")
    base._git(repo, "commit", "-qam", "other")
    base._git(repo, "checkout", "-q", "main")
    for name in (ODD, "plain.txt"):
        base._write(repo / name, "main\n")
    base._git(repo, "commit", "-qam", "main")
    with pytest.raises(AssertionError):
        base._git(repo, "merge", "other")
    stages = cs.stage_map(_run, repo)
    assert stages == {ODD: [1, 2, 3], "plain.txt": [1, 2, 3]}
    assert cs.conflicted_paths(_run, repo) == sorted([ODD, "plain.txt"])
    listing = cs.stage0_listing(_run, repo)
    assert list(listing) == ["clean.txt"] and listing["clean.txt"][0] == "100644"
    base._git(repo, "merge", "--abort")
    assert cs.stage_map(_run, repo) == {} and cs.conflicted_paths(_run, repo) == []


def test_reader_does_not_swallow_a_failed_read(tmp_path):
    cs = _state()

    def boom(cwd, argv):
        raise RuntimeError("no git")
    with pytest.raises(RuntimeError):
        cs.stage_map(boom, tmp_path)


def _empty_stop(tmp_path):
    local = tmp_path / "e"
    local.mkdir()
    base._git(local, "init", "-q", "-b", "main")
    base._write(local / "f.txt", "a\n")
    base._git(local, "add", "-A")
    base._git(local, "commit", "-qm", "base")
    base._git(local, "checkout", "-qb", "feat")
    base._write(local / "f.txt", "feat\n")
    base._git(local, "commit", "-qam", "feat change")
    base._git(local, "checkout", "-q", "main")
    base._write(local / "f.txt", "main\n")
    base._git(local, "commit", "-qam", "main change")
    base._git(local, "checkout", "-q", "feat")
    with pytest.raises(AssertionError):
        base._git(local, "rebase", "main")
    return local


def test_emptiness_check_in_sigma_loop(tmp_path):
    cs = _state()
    local = _empty_stop(tmp_path)
    base._write(local / "f.txt", "main\n")                  # resolved to exactly what HEAD has
    base._git(local, "add", "f.txt")
    found = cs.empty_commit_about_to_land(_run, local)
    assert found and found["subject"] == "feat change" and len(found["sha"]) == 40
    base._write(local / "f.txt", "both\n")
    base._git(local, "add", "f.txt")
    assert cs.empty_commit_about_to_land(_run, local) is None


def test_the_walker_delegates_to_the_moved_check(tmp_path):
    spec = importlib.util.spec_from_file_location("conflict_walk_hyg", WALK)
    sys.modules.pop("conflict_walk_hyg", None)
    walk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(walk)
    local = _empty_stop(tmp_path)
    base._write(local / "f.txt", "main\n")
    base._git(local, "add", "f.txt")
    assert walk._empty_commit_about_to_land(_run, local) == _state().empty_commit_about_to_land(_run, local)
    assert "git\", \"diff\"" not in WALK.read_text(encoding="utf-8").split("def _empty_commit_about_to_land")[1].split("def ")[0]


# --------------------------------------------------------------------------- the keys


def test_conflict_level_is_a_setting_of_the_project_opt_in():
    g = support.gate()
    assert g.DEFAULTS["conflicts.resolve"] == "off" and g.DEFAULTS["conflicts.mechanical_without_verify"] is False
    assert g.conflict_level({}) == "off"
    assert g.conflict_level({"upkeep": {"conflicts": {"resolve": "mechanical"}}}) == "off", "needs upkeep.enabled"
    for level in ("off", "mechanical", "agent"):
        assert g.conflict_level({"upkeep": {"enabled": True, "conflicts": {"resolve": level}}}) == level
    for typo in ("Agent", "MECHANICAL", "on", "", 1, None, True):
        cfg = {"upkeep": {"enabled": True, "conflicts": {"resolve": typo}}}
        assert g.conflict_level(cfg) == "off", typo
        reading = g.read(cfg)
        assert reading.enabled is False and any("conflicts.resolve" in p for p in reading.problems), typo
    bad = {"upkeep": {"enabled": True, "conflicts": {"mechanical_without_verify": "yes"}}}
    assert g.read(bad).enabled is False


def test_template_carries_the_conflicts_keys_and_a_note():
    cfg = support.template_cfg()
    assert cfg["upkeep"]["conflicts"] == {"resolve": "off", "mechanical_without_verify": False}
    note = cfg["_upkeep_conflicts"]
    assert isinstance(note, str) and "upkeep.enabled" in note and '"off"' in note
    missing, extra = support.key_gaps(cfg["upkeep"], support.gate().SCHEMA)
    assert missing == [] and extra == []
    assert json.dumps(cfg["upkeep"]).count("resolve") == 1


def test_no_new_entry_point_launches_anything_while_closed(tmp_path):
    """The recording trap over the new read-only entry points under a closed gate: the gate-reading functions launch
    no process, touch no network and write no file."""
    m = base._mod()
    g = support.gate()
    configs = [{}, support.template_cfg(), {"upkeep": {"conflicts": {"resolve": "agent"}}},
               {"upkeep": {"enabled": True, "conflicts": {"resolve": "Agent"}}}]
    with attempt_trap.AttemptTrap() as trap:
        for config in configs:
            g.conflict_level(config)
            m.pinned_run(lambda cwd, argv: "", config)
    assert trap.quiet
