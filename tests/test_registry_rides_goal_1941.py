"""#1941: a landed goal's registry update rides the goal's own commit; the root checkout stays clean.

`work.start` used to rewrite `.sdlc/features/units/<unit>.json` and `<unit>.md` in the ROOT checkout
(branching-model section 15), where nothing committed them: the root went dirty on every pick, a later
`git pull --ff-only` there refused, and a goal that never landed still left membership behind. The
pick-time write is now made in the goal's own worktree, so `work.commit`'s `git add -A` carries it to
the unit branch with the goal. Goals of one unit amend the same shard, so a rebase can conflict on it;
`work._registry_rescue` takes upstream's bytes and re-records the goal, for registry paths only.

These drive real git against a bare remote (tests/registry_root.py). Every body runs inside
`registry_root.drained(capfd)` (one captured-output section voids a verify run's reds).
"""
import json
import os
import subprocess

import pytest

import registry_root
from registry_root import CONFIG, PAGE, SHARD, World, git
from resource_mode import carry  # noqa: F401  (the env any subprocess here would carry)


def _goals(text):
    doc = json.loads(text)
    entry = doc["features"]["voice"] if "features" in doc else doc
    return entry["repos"][registry_root.REPO]["goals"]


def _adopted_world(tmp_path):
    """A world whose base TRACKS a registry (one earlier goal, 99), pushed to the remote -- the
    state after a human ran section 15's one-time gesture."""
    w = World(tmp_path).build()
    w.sigma_shard_write(99)
    w.sigma_page_write()
    w.commit_registry()
    git(w.root, "push", "-q", "origin", "main")
    git(w.root, "push", "-q", "-f", "origin", "main:refs/heads/feature/voice")
    return w


def _wt(w, goal):
    return w.work._record(str(w.sdlc), str(goal))["worktree"]


def _land(w, goal, message="goal"):
    """Commit the worktree's tree the way `work.commit` stages it (`git add -A`) and land it on the
    unit branch, then fast-forward main to it -- the shape of a merged goal PR."""
    wt = _wt(w, goal)
    (__import__("pathlib").Path(wt) / ("goal-%s.txt" % goal)).write_text("work\n", encoding="utf-8")
    git(wt, "add", "-A")
    git(wt, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", message)
    git(wt, "push", "-q", "origin", "HEAD:refs/heads/feature/voice")
    git(wt, "push", "-q", "origin", "HEAD:refs/heads/main")


def _pull_ff(root):
    return subprocess.run(["git", "pull", "--ff-only", "-q", "origin", "main"], cwd=str(root),
                          capture_output=True, text=True)


def test_start_leaves_the_root_registry_untouched_and_edits_the_worktree(tmp_path, capfd):
    with registry_root.drained(capfd):
        w = _adopted_world(tmp_path)
        before = w.read(SHARD)
        assert w.start("100").startswith("worktree")
        assert w.porcelain() == ""
        assert w.read(SHARD) == before
        assert _goals(before) == [99]
        mine = (__import__("pathlib").Path(_wt(w, 100)) / SHARD).read_text(encoding="utf-8")
        assert _goals(mine) == [99, 100]


def test_a_landed_goal_lets_the_root_pull_ff_only_and_carries_the_goal(tmp_path, capfd):
    with registry_root.drained(capfd):
        w = _adopted_world(tmp_path)
        assert w.start("100").startswith("worktree")
        _land(w, 100)
        pulled = _pull_ff(w.root)
        assert pulled.returncode == 0, pulled.stderr
        assert _goals(w.read(SHARD).decode()) == [99, 100]
        assert w.porcelain() == ""


def test_control_a_write_in_the_root_makes_the_same_pull_refuse(tmp_path, capfd, monkeypatch):
    """The sensitivity proof for the test above: put the write back in the ROOT and the landed base lacks the goal and the root stays dirty."""
    with registry_root.drained(capfd):
        w = _adopted_world(tmp_path)
        real = w.work._sync_registry
        monkeypatch.setattr(w.work, "_sync_registry",
                            lambda sdlc, config, goal, unit, run, base_root, remote:
                            real(w.sdlc, config, goal, unit, run, w.root, remote))
        assert w.start("100").startswith("worktree")
        assert w.porcelain() != ""
        _land(w, 100)
        # the goal landed WITHOUT its membership, and the root is left carrying it as local dirt:
        # exactly the state in which a later pull that moves the shard refuses
        assert _pull_ff(w.root).returncode == 0
        assert _goals(git(w.root, "show", "HEAD:" + SHARD)) == [99]
        assert w.porcelain() != ""


def test_a_goal_that_never_lands_writes_nothing_shared(tmp_path, capfd):
    with registry_root.drained(capfd):
        w = _adopted_world(tmp_path)
        assert w.start("100").startswith("worktree")
        assert w.porcelain() == ""
        git(w.root, "fetch", "-q", "origin")
        for ref in ("origin/main", "origin/feature/voice"):
            assert _goals(git(w.root, "show", ref + ":" + SHARD)) == [99]


def _two_goals(tmp_path, commit_second):
    w = _adopted_world(tmp_path)
    assert w.start("100").startswith("worktree")
    assert w.start("101").startswith("worktree")
    _land(w, 100)
    if commit_second:
        wt = _wt(w, 101)
        git(wt, "add", "-A")
        git(wt, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "second")
    return w


@pytest.mark.parametrize("commit_second", [True, False], ids=["committed", "uncommitted"])
def test_a_shard_conflict_between_two_goals_of_one_unit_is_rescued(tmp_path, capfd, commit_second):
    with registry_root.drained(capfd):
        w = _two_goals(tmp_path, commit_second)
        out = w.work.rebase(str(w.sdlc), CONFIG, "101", run=w.run)
        assert out.startswith("rebased"), out
        wt = _wt(w, 101)
        text = (__import__("pathlib").Path(wt) / SHARD).read_text(encoding="utf-8")
        assert "<<<<<<<" not in text
        assert sorted(_goals(text)) == [99, 100, 101]
        assert git(wt, "diff", "--name-only", "--diff-filter=U") == ""


def test_control_with_the_rescue_off_the_rebase_is_deferred(tmp_path, capfd, monkeypatch):
    with registry_root.drained(capfd):
        w = _two_goals(tmp_path, True)
        monkeypatch.setattr(w.work, "_registry_rescue", lambda *a, **k: False)
        assert w.work.rebase(str(w.sdlc), CONFIG, "101", run=w.run).startswith("rebase deferred")


def test_control_with_the_stash_rescue_off_the_uncommitted_case_is_deferred(tmp_path, capfd, monkeypatch):
    with registry_root.drained(capfd):
        w = _two_goals(tmp_path, False)
        monkeypatch.setattr(w.work, "_stash_pop_registry_rescue", lambda *a, **k: False)
        assert w.work.rebase(str(w.sdlc), CONFIG, "101", run=w.run).startswith("rebase deferred")


def _fake_run(unmerged):
    calls = []

    def run(cwd, argv):
        calls.append(list(argv))
        if argv[:3] == ["git", "diff", "--name-only"]:
            return "\n".join(unmerged)
        return ""
    run.calls = calls
    return run


@pytest.mark.parametrize("unmerged", [
    [SHARD, "src/app.py"],
    [SHARD, "CHANGELOG.md"],
    ["src/app.py"],
    [".sdlc/features-evil/x.json"],
    [".sdlc/features/../../etc/x"],
    [],
], ids=["mixed", "changelog-too", "outside", "prefix-lookalike", "dotdot", "none"])
def test_the_rescue_declines_unless_every_unmerged_path_is_a_registry_path(unmerged):
    work = registry_root.module("work")
    run = _fake_run(unmerged)
    assert work._registry_rescue("/x", run, lambda: True) is False
    assert not any(a[:2] == ["git", "checkout"] for a in run.calls)


def test_the_rescue_declines_when_the_goal_cannot_be_re_recorded():
    work = registry_root.module("work")
    run = _fake_run([SHARD])
    assert work._registry_rescue("/x", run, lambda: False) is False
    assert not any("--continue" in a for a in run.calls)


def test_the_rescue_accepts_a_registry_only_conflict():
    work = registry_root.module("work")
    run = _fake_run([SHARD, PAGE])
    assert work._registry_rescue("/x", run, lambda: True) is True
    assert any("--continue" in a for a in run.calls)
