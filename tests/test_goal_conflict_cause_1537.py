"""#1537 -- polish of the #1494 goal-replay cause probe: a probe/replay mismatch stays unknown, the probe
has a per-pass time budget, and the reported text is sanitised and capped."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_feature_rebase import FEATURE, World, _filer, _git, _mod, _register_agent, _upkeep, _write  # noqa: E402
from test_goal_conflict_cause_1494 import CONFLICT, _goal_bodies, _move_feature, _probe_returning  # noqa: E402


def test_a_clean_probe_with_a_conflicting_commit_by_commit_replay_is_unknown(tmp_path):
    m = _mod()
    world = World(tmp_path).build()
    path = world.goal_branch(11, files=())
    original = (path / "seed.txt").read_text()
    for body in ("goal side\n", original):               # a change added, then reverted: net diff is empty
        _write(path / "seed.txt", body)
        _git(path, "add", "seed.txt")
        _git(path, "commit", "-q", "-m", "sdlc: 11")
    _git(path, "push", "-q", "origin", "sdlc/11")
    world.write_registry(goals=[11])
    _register_agent(world.sdlc, 11, 999999)
    _move_feature(world, "seed.txt", "unit side")
    world.move_integration()
    filed = _filer(m)
    report = _upkeep(m, world)
    assert report["conflicts"] == ["sdlc/11"], report
    (body,) = _goal_bodies(filed)
    assert "brought forward" in body and "did not cause it" not in body and "replays cleanly" not in body, body


def test_a_spent_probe_budget_skips_git_and_keeps_the_legacy_wording():
    m = _mod()
    calls = _probe_returning(m, [(1, "tree\nf.txt\n")] * 4)
    now = [0.0]
    m._clock = lambda: now[0]
    deadline = m._probe_deadline()
    assert deadline > 0
    assert m._conflict_cause("/x", "a" * 40, "b" * 40, "c" * 40, CONFLICT, deadline=deadline) == \
        (m.CAUSE_PRE_EXISTING, ["f.txt"])
    assert len(calls) == 1                                # before the budget: probes run as today
    now[0] = deadline + 1
    assert m._conflict_cause("/x", "a" * 40, "b" * 40, "c" * 40, CONFLICT, deadline=deadline) == \
        (m.CAUSE_UNKNOWN, [])
    assert len(calls) == 1, calls                         # after it: no merge-tree call


def test_the_budget_can_run_out_between_the_two_probes():
    m = _mod()
    now = [0.0]
    m._clock = lambda: now[0]
    deadline = m._probe_deadline()
    calls = []

    def probe(cwd, left, right):
        calls.append(left)
        now[0] = deadline + 1                             # the first probe spends the budget
        return 0, "tree\n"
    m._merge_probe = probe
    assert m._conflict_cause("/x", "a" * 40, "b" * 40, "c" * 40, CONFLICT, deadline=deadline) == \
        (m.CAUSE_UNKNOWN, [])
    assert len(calls) == 1


def test_the_reported_text_is_neutralised_and_capped():
    m = _mod()
    nasty = "x```\nfake\x1b[31m `tick` " + "z" * 5000
    for cause in (None, (m.CAUSE_PRE_EXISTING, ["f.txt"])):
        body = m._goal_conflict_body("u", "sdlc/1", "feature/u", nasty, ["clean"], cause=cause, unit_before="a" * 40)
        reported = body.split("it reported:\n\n```\n", 1)[1].split("\n```\n", 1)[0]
        assert "`" not in reported and "\n" not in reported and "\x1b" not in reported, reported
        assert len(reported) <= m._REPORTED_MAX, len(reported)
