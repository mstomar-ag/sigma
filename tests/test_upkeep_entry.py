"""Goal 1085: the shipped entry point hands the pass its rewrite and push callables. Local bare remotes, no network."""
import subprocess

import pytest

import upkeep_support as support
from test_upkeep_pass import OPEN, git, commit

import test_upkeep_pass as base

mod = base.mod


@pytest.fixture
def wired(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    root = tmp_path / "w"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "remote", "add", "origin", str(bare))
    (root / "a.txt").write_text("a")
    git(root, "add", "a.txt")
    git(root, "commit", "-q", "-m", "a")
    git(root, "branch", "feature/u")
    (root / ".sdlc").mkdir()
    commit(root, "feature/u", "u.txt")
    commit(root, "main", "b.txt")
    git(root, "push", "-q", "origin", "main", "feature/u")
    return root, bare


def enter(root, config=OPEN, **kw):
    return mod().run_unit_pass(config, str(root / ".sdlc"), "u", "feature/u", "main", "main", 1000, remote="origin",
                               branch="feature/u", cwd=root, **kw)


def remote_tip(bare, ref):
    return git(bare, "rev-parse", ref)


def test_entry_point_replays_and_pushes_end_to_end(wired):
    root, bare = wired
    old, main = remote_tip(bare, "feature/u"), remote_tip(bare, "main")
    result = enter(root)
    new = remote_tip(bare, "feature/u")
    assert result["result"] == "rewritten"
    assert new != old and git(bare, "rev-parse", new + "^") == main and git(bare, "show", new + ":u.txt") == "u.txt"
    assert git(root, "rev-parse", "feature/u") == old            # the local unit ref is never moved by the rewrite
    assert git(root, "worktree", "list").count("\n") == 0        # the scratch worktree is gone


def test_conflict_is_failed_and_nothing_is_pushed(wired):
    root, bare = wired
    git(root, "checkout", "-q", "main")
    (root / "u.txt").write_text("clash")                         # base now has a u.txt the unit also adds, differently
    git(root, "add", "u.txt")
    git(root, "commit", "-q", "-m", "clash")
    git(root, "push", "-q", "origin", "main")
    old = remote_tip(bare, "feature/u")
    result = enter(root)
    assert result["result"] == "failed" and remote_tip(bare, "feature/u") == old


def test_moved_remote_branch_is_refused_by_the_lease(wired):
    root, bare = wired
    old = remote_tip(bare, "feature/u")
    other = bare.parent / "o"
    git(bare.parent, "clone", "-q", str(bare), str(other))
    git(other, "checkout", "-q", "feature/u")
    (other / "x.txt").write_text("x")
    git(other, "add", "x.txt")
    git(other, "commit", "-q", "-m", "x")
    moved = git(other, "rev-parse", "HEAD")
    rewrite, push = mod().unit_callables(root, "origin", "feature/u")
    run = mod().engine_runner("off")
    assert rewrite(run, (old, remote_tip(bare, "main")))["ok"] is True
    git(other, "push", "-q", "origin", "feature/u")              # somebody else pushes after the pass read the tip
    with pytest.raises(Exception):
        push(run, (old, remote_tip(bare, "main")))
    assert remote_tip(bare, "feature/u") == moved


def test_gate_closed_is_byte_identical_to_no_entry_point(wired):
    root, bare = wired
    old = remote_tip(bare, "feature/u")
    calls = []
    for config in ({}, {"upkeep": {"enabled": False}}):
        closed = enter(root, config, run=lambda cwd, argv: calls.append(argv))
        assert closed == mod().upkeep_pass(config, str(root / ".sdlc"), "u", "feature/u", "main", "main", 1000, cwd=root,
                                           run=lambda cwd, argv: calls.append(argv))
        assert closed["closed"] is True
    assert calls == [] and remote_tip(bare, "feature/u") == old
    assert sorted(p.name for p in (root / ".sdlc").rglob("*") if p.is_file()) == []


BAD = ["-x", "--upload-pack=touch /tmp/p", "", "a b", "a..b", "a:b", "x.lock", "/a", "a/", "a//b", "a@{1}", ".hid", "a~1", None, 5]


@pytest.mark.parametrize("bad", BAD)
def test_dash_or_malformed_remote_and_branch_are_refused_before_any_git(wired, bad):
    root, bare = wired
    old = remote_tip(bare, "feature/u")
    calls = []
    for kw in ({"remote": bad, "branch": "feature/u"}, {"remote": "origin", "branch": bad}):
        got = mod().run_unit_pass(OPEN, str(root / ".sdlc"), "u", "feature/u", "main", "main", 1000, cwd=root,
                                  run=lambda cwd, argv: calls.append(argv), **kw)
        assert got["result"] == "failed" and got["reason"] == "invalid-ref"
        with pytest.raises(ValueError):
            mod().unit_callables(root, kw["remote"], kw["branch"])
    assert calls == [] and remote_tip(bare, "feature/u") == old


def test_plain_names_are_valid():
    assert all(mod().valid_ref(v) for v in ("origin", "feature/u", "sdlc/1098", "a.b-c_d"))


def test_the_push_is_a_lease_not_a_plain_force(wired):
    """The control: with the lease swapped for a plain force the moved-branch test below goes red (seen once by hand)."""
    root, bare = wired
    seen = []
    rewrite, push = mod().unit_callables(root, "origin", "feature/u")
    real = mod().engine_runner("off")

    def spy(cwd, argv):
        seen.append(argv)
        return real(cwd, argv)
    tips = (remote_tip(bare, "feature/u"), remote_tip(bare, "main"))
    rewrite(spy, tips)
    push(spy, tips)
    pushes = [a for a in seen if a[:2] == ["git", "push"]]
    assert len(pushes) == 1 and pushes[0][2].startswith("--force-with-lease=refs/heads/feature/u:") and "--force" not in pushes[0]


# ------------------------------------------------------------------------------------------ goal 1101: valid_ref vs git
_NAMES = ["main", "feature/x", "a/b/c", "a.b", "a-b_c", "x@y", "feat/v1.2", "A/B", "-lead", "/lead", "trail/", "trail.", "a..b",
          "a b", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "a@{b", "@", "a//b", ".hidden", "a/.hidden", "x.lock",
          "a/x.lock/b", "", "a\tb", "a\x7fb", "héllo", "a/b.", "@{", "refs/heads/ok", "a.lock.b"]


def test_valid_ref_is_never_looser_than_git_check_ref_format(tmp_path):
    import subprocess
    vr = mod().valid_ref
    stricter = []
    for name in _NAMES:
        git_ok = bool(name) and not name.startswith("-") and subprocess.run(
            ["git", "check-ref-format", "refs/heads/" + name], capture_output=True).returncode == 0
        if name and not name.startswith("-"):
            branch_ok = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True).returncode == 0
            git_ok = git_ok and branch_ok if name != "@" else False
        if vr(name):
            assert git_ok, "valid_ref accepts %r but git refuses it" % name
        elif git_ok:
            stricter.append(name)
    assert vr("main") and vr("feature/x") and not vr("")        # the table is not vacuous
