"""#1947: a recorded decision never commits onto the default branch; `publish --branch` writes a local records branch."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

import resource_mode

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


store = _load("decision_store")
record = _load("decision_record")
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


def _cfg():
    return {"rulebook": {"enabled": True, "records": {"enabled": True, "publish": True},
                         "rules": {"enabled": True}}}


def _make(tmp_path, branch="main"):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", branch, str(bare)], check=True)
    root = tmp_path / "r"
    (root / ".sdlc").mkdir(parents=True)
    (root / "README").write_text("x\n")
    _git(root, "init", "-q", "-b", branch)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "-u", "origin", branch)
    _git(root, "remote", "set-head", "origin", branch)
    return root


@pytest.fixture
def repo(tmp_path):
    return _make(tmp_path)


def _write(root):
    return store.append(str(root / ".sdlc"), {"how": "human", "qkind": "k", "choice": "c"}, _cfg())


def test_publish_on_default_branch_commits_nothing_and_pull_works(repo):
    tip = _git(repo, "rev-parse", "HEAD")
    _write(repo)
    out = store.publish(str(repo / ".sdlc"), _cfg())
    assert out["committed"] is False and out["reason"] == "default-branch"
    assert "decision_store.py" in out["command"] and "--branch" in out["command"]
    assert os.path.isabs(out["command"].split()[1])
    assert _git(repo, "rev-parse", "HEAD") == tip
    _git(repo, "pull", "--ff-only", "-q")


def test_publish_on_other_branch_still_commits(repo):
    _git(repo, "checkout", "-q", "-b", "work")
    _write(repo)
    out = store.publish(str(repo / ".sdlc"), _cfg())
    assert out == {"committed": True, "reason": "committed"}


def test_default_branch_follows_origin_head_not_main_master(tmp_path):
    root = _make(tmp_path, branch="trunk")
    _write(root)
    assert store.publish(str(root / ".sdlc"), _cfg())["reason"] == "default-branch"
    _git(root, "checkout", "-q", "-b", "main")
    out = store.publish(str(root / ".sdlc"), _cfg())
    assert out["reason"] == "committed"


def test_default_branch_without_remote_falls_back_to_main_master(tmp_path):
    root = tmp_path / "n"
    (root / ".sdlc").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "master")
    (root / "f").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "i")
    _write(root)
    assert store.publish(str(root / ".sdlc"), _cfg())["reason"] == "default-branch"


def _snapshot(root):
    return (_git(root, "rev-parse", "HEAD"), _git(root, "status", "--porcelain"),
            _git(root, "ls-files", "-s"), _git(root, "symbolic-ref", "HEAD"))


def test_branch_makes_records_commit_without_touching_head_index_or_tree(repo):
    _write(repo)
    (repo / "unrelated.txt").write_text("keep\n")
    before = _snapshot(repo)
    out = store.publish_branch(str(repo / ".sdlc"), _cfg())
    assert out["committed"] is True and out["branch"] == "sigma/records"
    assert "git push origin sigma/records" == out["push"]
    assert _snapshot(repo) == before
    files = _git(repo, "ls-tree", "-r", "--name-only", "sigma/records").split()
    base = _git(repo, "ls-tree", "-r", "--name-only", "main").split()
    added = set(files) - set(base)
    assert added and all(f.startswith(".sdlc/") for f in added) and "unrelated.txt" not in files
    assert _git(repo, "rev-parse", "sigma/records^") == _git(repo, "rev-parse", "origin/main")
    assert _git(repo, "rev-list", "--count", "main..sigma/records").strip() == "1"


def test_branch_nothing_changed_makes_no_commit_and_change_advances(repo):
    _write(repo)
    store.publish_branch(str(repo / ".sdlc"), _cfg())
    tip = _git(repo, "rev-parse", "sigma/records")
    again = store.publish_branch(str(repo / ".sdlc"), _cfg())
    assert again["committed"] is False and again["reason"] == "nothing-changed"
    assert _git(repo, "rev-parse", "sigma/records") == tip
    _write(repo)
    third = store.publish_branch(str(repo / ".sdlc"), _cfg())
    assert third["committed"] is True
    assert _git(repo, "rev-parse", "sigma/records^") == tip


def test_branch_cli_runs_and_never_pushes(repo):
    _write(repo)
    (repo / ".sdlc" / "config.json").write_text(json.dumps(_cfg()))
    env = resource_mode.carry(dict(os.environ))
    r = subprocess.run([sys.executable, str(SCRIPTS / "decision_store.py"), "publish", str(repo / ".sdlc"),
                        "--branch"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads(r.stdout)["committed"] is True
    bare = _git(repo, "remote", "get-url", "origin").strip()
    assert "sigma/records" not in subprocess.run(["git", "-C", bare, "branch"], capture_output=True,
                                                  text=True).stdout


def test_write_all_prints_hint_once_on_default_branch(repo, capsys):
    rec = {"how": "human", "qkind": "k", "choice": "c"}
    record.write_all(str(repo / ".sdlc"), rec, _cfg(), None, poster=lambda n, b: None)
    err = capsys.readouterr().err
    assert err.count("decision_store.py") == 1 and "--branch" in err
