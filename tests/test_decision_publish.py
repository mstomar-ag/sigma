"""#1313: publishing human decisions and the rulebook file (commit only, never a push)."""
import importlib.util
import json
import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


store = _load("decision_store")
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root)] + list(args), capture_output=True, text=True, check=True).stdout


def _cfg(publish=True, enabled=True):
    return {"rulebook": {"enabled": True, "records": {"enabled": enabled, "publish": publish},
                         "rules": {"enabled": True}}}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "r"
    (root / ".sdlc").mkdir(parents=True)
    (root / "README").write_text("x\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _write_record(root, config=None):
    return store.append(str(root / ".sdlc"), {"how": "human", "qkind": "k", "choice": "c"}, config or _cfg())


def _count(root):
    return int(_git(root, "rev-list", "--count", "HEAD").strip())


def test_publish_commits_only_the_three_paths(repo):
    config = _cfg()
    _write_record(repo, config)
    (repo / ".sdlc" / "autonomy").mkdir()
    (repo / ".sdlc" / "autonomy" / "a.json").write_text("{}\n")
    (repo / ".sdlc" / "rulebook.json").write_text("[]\n")
    (repo / "unrelated.txt").write_text("dirty\n")
    out = store.publish(str(repo / ".sdlc"), config)
    assert out["committed"] is True
    assert _count(repo) == 2
    files = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert files == {".sdlc/rulebook.json", ".sdlc/autonomy/a.json",
                     ".sdlc/decisions/" + os.listdir(repo / ".sdlc" / "decisions")[0]}
    assert "?? unrelated.txt" in _git(repo, "status", "--porcelain")


def test_second_call_makes_no_commit(repo):
    config = _cfg()
    _write_record(repo, config)
    assert store.publish(str(repo / ".sdlc"), config)["committed"] is True
    out = store.publish(str(repo / ".sdlc"), config)
    assert out == {"committed": False, "reason": "nothing-changed"}
    assert _count(repo) == 2


def test_goal_worktree_is_skipped(repo, tmp_path):
    wt = repo / ".sdlc" / "work" / "9"
    _git(repo, "worktree", "add", "-q", "-b", "sdlc/9", str(wt))
    config = _cfg()
    _write_record(wt, config)
    out = store.publish(str(wt / ".sdlc"), config)
    assert out == {"committed": False, "reason": "goal-worktree"}
    assert _count(wt) == 1


def test_linked_worktree_outside_work_dir_is_skipped(repo, tmp_path):
    wt = tmp_path / "elsewhere"
    _git(repo, "worktree", "add", "-q", "-b", "other", str(wt))
    (wt / ".sdlc").mkdir()
    config = _cfg()
    _write_record(wt, config)
    assert store.publish(str(wt / ".sdlc"), config)["reason"] == "goal-worktree"


def test_registered_unit_branch_is_skipped(repo):
    units = repo / ".sdlc" / "features" / "units"
    units.mkdir(parents=True)
    (units / "u.json").write_text(json.dumps({"branch": "feature/u"}))
    _git(repo, "checkout", "-q", "-b", "feature/u")
    config = _cfg()
    _write_record(repo, config)
    assert store.publish(str(repo / ".sdlc"), config) == {"committed": False, "reason": "unit-branch"}
    assert _count(repo) == 1


def test_detached_head_is_skipped(repo):
    _git(repo, "checkout", "-q", "--detach")
    config = _cfg()
    _write_record(repo, config)
    assert store.publish(str(repo / ".sdlc"), config)["reason"] == "unit-branch"


def test_publish_false_is_skipped(repo):
    config = _cfg(publish=False)
    _write_record(repo, config)
    assert store.publish(str(repo / ".sdlc"), config) == {"committed": False, "reason": "publish-off"}
    assert _count(repo) == 1


def test_closed_gate_runs_no_git_command(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(store.subprocess, "run", lambda *a, **k: calls.append(a) or pytest.fail("git ran"))
    out = store.publish(str(repo / ".sdlc"), _cfg(enabled=False))
    assert out == {"committed": False, "reason": "records-closed"}
    assert calls == []


def test_unrelated_dirty_file_is_not_staged(repo):
    config = _cfg()
    _write_record(repo, config)
    (repo / "README").write_text("changed\n")
    (repo / "new.txt").write_text("n\n")
    _git(repo, "add", "new.txt")
    store.publish(str(repo / ".sdlc"), config)
    files = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert not files & {"README", "new.txt"}
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["new.txt"]


def test_never_pushes(repo):
    config = _cfg()
    _write_record(repo, config)
    _git(repo, "remote", "add", "origin", "/nonexistent/path")
    assert store.publish(str(repo / ".sdlc"), config)["committed"] is True


def test_cli_publish_verb(repo):
    config = _cfg()
    _write_record(repo, config)
    (repo / ".sdlc" / "config.json").write_text(json.dumps(config))
    r = subprocess.run(["python3", str(SCRIPTS / "decision_store.py"), "publish", str(repo / ".sdlc")],
                       capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["committed"] is True


def test_unpark_gesture_publishes_the_record(repo):
    """The write seam the gestures share (decision_record.write_all) commits a human record."""
    dr = _load("decision_record")
    config = _cfg()
    rec = dr.build("unpark", 5, "k", "go", "why", "human")
    out = dr.write_all(str(repo / ".sdlc"), rec, config, None, poster=lambda n, b: None)
    assert out["id"]
    assert _count(repo) == 2
