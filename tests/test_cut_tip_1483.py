"""#1483: a goal cut from a unit while another pass is rewriting it.

Real git, a local bare remote, no network. Opt-in OFF throughout unless a test says otherwise."""
import os
import subprocess

import pytest

import upkeep_support as support


def work():
    return support.script("work")


def real_run(cwd, argv):
    argv = [str(a) for a in argv]
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": os.environ["PATH"],
           "GIT_EDITOR": "true"}
    if argv[0] == "git":
        argv = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"] + argv[1:]
    out = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip() or "exit %d" % out.returncode)
    return out.stdout.strip()


def git(cwd, *argv):
    return real_run(cwd, ["git", *argv])


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cwd, argv):
        self.calls.append([str(a) for a in argv])
        return real_run(cwd, argv)


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), "clone")
    (clone / "f.txt").write_text("1\n2\n3\n")
    git(clone, "add", "f.txt")
    git(clone, "commit", "-q", "-m", "seed")
    git(clone, "branch", "-M", "main")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "checkout", "-q", "-b", "feature/u")
    (clone / "f.txt").write_text("1\n2\nunit-line\n3\n")
    git(clone, "commit", "-q", "-am", "unit work")
    git(clone, "push", "-q", "origin", "feature/u")
    old = git(clone, "rev-parse", "HEAD")
    goal = tmp_path / "goalwt"
    git(clone, "worktree", "add", "-q", "-b", "sdlc/9", str(goal), "origin/feature/u")
    sdlc = clone / ".sdlc"
    (sdlc / "goals").mkdir(parents=True)
    import types
    return types.SimpleNamespace(clone=clone, goal=goal, sdlc=sdlc, old=old)


def rewrite_unit(w, resolved_line="resolved-line"):
    git(w.clone, "checkout", "-q", "main")
    (w.clone / "f.txt").write_text("1\n2\n3\nmain-extra\n")
    git(w.clone, "commit", "-q", "-am", "main moves")
    git(w.clone, "push", "-q", "origin", "main")
    git(w.clone, "checkout", "-q", "-B", "feature/u", "main")
    (w.clone / "f.txt").write_text("1\n2\n%s\n3\nmain-extra\n" % resolved_line)
    git(w.clone, "commit", "-q", "-am", "unit work (rewritten)")
    git(w.clone, "push", "-q", "-f", "origin", "feature/u")
    return git(w.clone, "rev-parse", "HEAD")


def test_cut_tip_is_the_worktree_head_not_the_shared_remote_ref(world):
    git(world.goal, "fetch", "-q", "origin", "feature/u")
    new = rewrite_unit(world)
    git(world.goal, "fetch", "-q", "origin", "feature/u")       # the remote-tracking ref now holds the NEW tip
    got = work()._cut_tip_fields({}, real_run, str(world.goal), "origin", "feature/u")
    assert got == {"cut_tip": world.old} and got["cut_tip"] != new


def test_cut_tip_recorded_with_the_opt_in_off_only_for_a_unit_base(world):
    calls = []
    assert work()._cut_tip_fields({}, lambda c, a: calls.append(a), "/x", "origin", "main") == {}
    assert calls == []
    assert work()._cut_tip_fields({"upkeep": {"enabled": False}}, real_run, str(world.goal), "origin",
                                  "feature/u") == {"cut_tip": world.old}


def test_second_goal_of_a_rewritten_unit_replays_only_its_own_commits_with_the_opt_in_off(world):
    rec = {"worktree": str(world.goal), "branch": "sdlc/9", "base": "feature/u", "remote": "origin",
           "base_resolved": True, "pr": ""}
    rec.update(work()._cut_tip_fields({}, real_run, str(world.goal), "origin", "feature/u"))
    assert rec["cut_tip"] == world.old
    (world.goal / "g.txt").write_text("goal\n")
    git(world.goal, "add", "g.txt")
    git(world.goal, "commit", "-q", "-m", "goal work")
    git(world.goal, "push", "-q", "origin", "sdlc/9")
    work()._save(str(world.sdlc), "9", rec)
    new = rewrite_unit(world)
    run = Recorder()
    assert work().rebase(str(world.sdlc), {}, "9", run=run) == "rebased"
    assert any("--onto" in c and world.old in c for c in run.calls if c[:2] == ["git", "rebase"])
    assert git(world.goal, "rev-list", "--count", "origin/feature/u..HEAD") == "1"
    assert git(world.goal, "rev-parse", "HEAD~1") == new
    assert work()._record(str(world.sdlc), "9")["cut_tip"] == new       # re-recorded on the unit tip


def test_recut_after_busy_moves_an_empty_goal_onto_the_rewritten_tip(world):
    new = rewrite_unit(world)
    run = Recorder()
    said = work()._recut_after_busy(run, str(world.goal), "origin", "feature/u")
    assert git(world.goal, "rev-parse", "HEAD") == new
    assert any("--onto" in c for c in run.calls if c[:2] == ["git", "rebase"])
    assert world.old[:12] in said and new[:12] in said
    assert work()._cut_tip_fields({}, real_run, str(world.goal), "origin", "feature/u") == {"cut_tip": new}


def test_recut_after_busy_with_an_unmoved_tip_rebases_nothing(world):
    run = Recorder()
    said = work()._recut_after_busy(run, str(world.goal), "origin", "feature/u")
    assert git(world.goal, "rev-parse", "HEAD") == world.old
    assert not [c for c in run.calls if c[:2] == ["git", "rebase"]]
    assert world.old[:12] in said


def test_recut_after_busy_failure_leaves_the_goal_where_it_is(world):
    def boom(cwd, argv):
        raise RuntimeError("no")
    assert "could not" in work()._recut_after_busy(boom, str(world.goal), "origin", "feature/u")
    assert git(world.goal, "rev-parse", "HEAD") == world.old


def test_rebase_upkeep_remembers_a_busy_outcome_and_clears_it_otherwise(monkeypatch):
    w = work()

    class Stub:
        BUSY = "busy"

        def __init__(self, outcome):
            self.outcome = outcome

        def upkeep(self, *a, **k):
            return {"note": "n", "outcome": self.outcome}

    g = w._rebase_upkeep.__globals__            # `support.script` returns a copy of the namespace
    monkeypatch.setitem(g, "_resume_blocked_by_a_live_sibling", lambda *a, **k: None)
    for outcome, busy in (("busy", True), ("current", False)):
        monkeypatch.setitem(g, "_feature_rebase", lambda o=outcome: Stub(o))
        w._rebase_upkeep("/s", {}, "9", "u", real_run, "/x", "origin")
        assert w._upkeep_was_busy() is busy


def test_busy_clause_names_the_real_situation():
    wording = support.script("feature_rebase")._WORDING["busy"]
    assert "may be rewriting" in wording and "before verify" in wording
