"""#1494 -- the goal-replay conflict finding says whether the upkeep pass caused the conflict.

Real git throughout: a probe that is wrong about git is the whole risk. The probe is a read-only
`git merge-tree` of the goal tip against the unit's previous tip.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_feature_rebase import FEATURE, INTEGRATION, World, _filer, _git, _mod, _register_agent, _upkeep, _write  # noqa: E402


def _goal_bodies(filed):
    return [f["body"] for f in filed if f.get("title", "").startswith("Rebase conflict: sdlc/")]


def _move_feature(world, name, body):
    _git(world.local, "checkout", "-q", FEATURE)
    _write(world.local / name, body + "\n")
    _git(world.local, "add", name)
    _git(world.local, "commit", "-q", "-m", "feat: later change (#12)")
    _git(world.local, "push", "-q", "origin", FEATURE)
    _git(world.local, "checkout", "-q", INTEGRATION)


def test_a_conflict_the_previous_tip_already_had_is_not_blamed_on_the_pass(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    world.goal_branch(11, files=(("f.txt", "goal side"),))
    world.write_registry(goals=[11])
    _register_agent(world.sdlc, 11, 999999)
    _move_feature(world, "f.txt", "unit side")          # the unit changes F after the goal was cut
    previous_tip = world.tip(FEATURE)
    world.move_integration()
    filed = _filer(m)
    report = _upkeep(m, world)
    assert report["conflicts"] == ["sdlc/11"], report
    (body,) = _goal_bodies(filed)
    assert "did not cause it" in body and previous_tip[:12] in body and "`f.txt`" in body, body
    assert "brought forward" not in body, body


def test_a_conflict_only_the_new_tip_has_is_said_to_be_introduced_by_the_pass(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    world.goal_branch(11, files=(("seed.txt", "goal side"),))
    world.write_registry(goals=[11])
    _register_agent(world.sdlc, 11, 999999)
    previous_tip = world.tip(FEATURE)
    world.move_integration(name="seed.txt", body="integration side")
    filed = _filer(m)
    report = _upkeep(m, world)
    assert report["conflicts"] == ["sdlc/11"], report
    (body,) = _goal_bodies(filed)
    assert "brought forward" in body and "replays cleanly onto the unit's previous tip" in body, body
    assert "`seed.txt`" in body, body                    # the new-tip probe's paths are shown too
    assert previous_tip[:12] in body and "did not cause it" not in body, body


def test_an_unavailable_probe_makes_no_causal_claim(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    world.goal_branch(11, files=(("seed.txt", "goal side"),))
    world.write_registry(goals=[11])
    _register_agent(world.sdlc, 11, 999999)
    world.move_integration(name="seed.txt", body="integration side")

    def no_probe(*_a, **_k):
        raise RuntimeError("git too old")

    m._merge_probe = no_probe
    filed = _filer(m)
    _upkeep(m, world)
    (body,) = _goal_bodies(filed)
    assert "could not be determined" not in body and "did not cause it" not in body, body
    assert "replays cleanly" not in body and "brought forward" in body, body   # legacy wording, unchanged


def test_the_probe_is_read_only(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    world.goal_branch(11, files=(("seed.txt", "goal side"),))
    world.write_registry(goals=[11])
    _register_agent(world.sdlc, 11, 999999)
    world.move_integration(name="seed.txt", body="integration side")
    _upkeep(m, world)
    refs = _git(world.local, "for-each-ref")
    trees = _git(world.local, "worktree", "list")
    tip, goal = world.tip(FEATURE), world.tip("sdlc/11")
    assert m._conflict_cause(str(world.local), tip, goal, tip, "rebase deferred: CONFLICT (content)")[0] in (m.CAUSE_INTRODUCED, m.CAUSE_PRE_EXISTING)
    assert _git(world.local, "for-each-ref") == refs
    assert _git(world.local, "worktree", "list") == trees


def test_the_body_names_the_backup_ref_and_bounds_the_paths():
    m = _mod()
    paths = ["p%d.txt" % n for n in range(30)]
    body = m._goal_conflict_body("u", "sdlc/1", "feature/u", "boom", ["clean"],
                                 cause=(m.CAUSE_PRE_EXISTING, paths),
                                 unit_before="a" * 40, backup_ref="refs/backup/u/stamp")
    assert "refs/backup/u/stamp" in body and "`p19.txt`" in body and "p20.txt" not in body
    assert "and 10 more" in body


CONFLICT = "rebase deferred: CONFLICT (content): Merge conflict in f.txt"
LEGACY = (
    "`sdlc/1` could not be replayed onto `feature/u` after that feature branch was brought forward.\n\n"
    "Measured afterwards:\n\n- clean\n\n"
    "Reproduce and resolve:\n\n```\ngit switch sdlc/1\ngit rebase origin/feature/u\n```\n\n"
    "it reported:\n\n```\nboom\n```\n\n"
    "Filed by Sigma's rebase upkeep for unit `u`.\n")


def _probe_returning(m, results):
    calls = []

    def probe(cwd, left, right):
        calls.append((left, right))
        result = results[len(calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result
    m._merge_probe = probe
    return calls


def test_the_unknown_body_is_byte_identical_to_the_legacy_body():
    m = _mod()
    assert m._goal_conflict_body("u", "sdlc/1", "feature/u", "boom", ["clean"]) == LEGACY
    assert m._goal_conflict_body("u", "sdlc/1", "feature/u", "boom", ["clean"],
                                 cause=(m.CAUSE_UNKNOWN, []), unit_before="a" * 40, backup_ref="r") == LEGACY


def test_a_non_conflict_outcome_is_not_probed_and_keeps_the_legacy_wording():
    m = _mod()
    calls = _probe_returning(m, [(0, "")] * 4)
    for outcome in ("not started -- run `work.py start`", "refused: push lease", "gated: needs approval",
                    "rebase deferred: could not confirm whether the autostash popped cleanly (x)",
                    "rebase deferred: fatal: unable to access remote"):
        assert m._conflict_cause("/x", "a" * 40, "b" * 40, "c" * 40, outcome) == (m.CAUSE_UNKNOWN, []), outcome
    assert calls == []


def test_a_non_conflict_replay_outcome_files_the_legacy_body_end_to_end(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    world.goal_branch(11, files=(("seed.txt", "goal side"),))
    world.write_registry(goals=[11])
    _register_agent(world.sdlc, 11, 999999)
    world.move_integration(name="seed.txt", body="integration side")
    m._work().rebase = lambda *a, **k: "refused: push lease"
    calls = _probe_returning(m, [])
    filed = _filer(m)
    _upkeep(m, world)
    (body,) = _goal_bodies(filed)
    assert calls == [] and "brought forward" in body and "could not be determined" not in body, body
    assert "replays cleanly" not in body and "did not cause it" not in body, body


def test_a_clean_new_tip_probe_is_unknown_not_introduced():
    m = _mod()
    _probe_returning(m, [(0, "tree\n"), (0, "tree\n")])
    assert m._conflict_cause("/x", "a" * 40, "b" * 40, "c" * 40, CONFLICT) == (m.CAUSE_UNKNOWN, [])


def test_an_erroring_new_tip_probe_is_unknown_not_introduced():
    m = _mod()
    _probe_returning(m, [(0, "tree\n"), (129, "")])
    assert m._conflict_cause("/x", "a" * 40, "b" * 40, "c" * 40, CONFLICT) == (m.CAUSE_UNKNOWN, [])
    _probe_returning(m, [(0, "tree\n"), RuntimeError("timeout")])
    assert m._conflict_cause("/x", "a" * 40, "b" * 40, "c" * 40, CONFLICT) == (m.CAUSE_UNKNOWN, [])


def test_introduced_needs_clean_previous_and_conflicting_new_tip_and_lists_new_paths():
    m = _mod()
    calls = _probe_returning(m, [(0, "tree\n"), (1, "tree\nf.txt\n\nCONFLICT info\n")])
    assert m._conflict_cause("/x", "a" * 40, "b" * 40, "c" * 40, CONFLICT) == (m.CAUSE_INTRODUCED, ["f.txt"])
    assert calls == [("a" * 40, "b" * 40), ("c" * 40, "b" * 40)]
    _probe_returning(m, [(1, "tree\nold.txt\n")])
    assert m._conflict_cause("/x", "a" * 40, "b" * 40, "c" * 40, CONFLICT) == (m.CAUSE_PRE_EXISTING, ["old.txt"])


def test_introduced_paths_are_capped_at_twenty():
    m = _mod()
    body = m._goal_conflict_body("u", "sdlc/1", "feature/u", "boom", ["clean"],
                                 cause=(m.CAUSE_INTRODUCED, ["p%d.txt" % n for n in range(30)]),
                                 unit_before="a" * 40)
    assert "`p19.txt`" in body and "p20.txt" not in body and "and 10 more" in body and "brought forward" in body


def test_paths_from_a_branch_are_sanitised_inside_their_code_span():
    m = _mod()
    nasty = "a`b\nc\x1b[31md\x00.txt"
    for kind in (m.CAUSE_PRE_EXISTING, m.CAUSE_INTRODUCED):
        body = m._goal_conflict_body("u", "sdlc/1", "feature/u", "boom", ["clean"],
                                     cause=(kind, [nasty]), unit_before="a" * 40)
        assert "`a'b?c?[31md?.txt`" in body, body
        assert "\x1b" not in body and "\x00" not in body and "a`b" not in body, body
