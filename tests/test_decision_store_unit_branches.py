"""#1391: publish must recognise a registered unit branch by the real unit record shape."""
import importlib.util
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = {"rulebook": {"records": {"enabled": True, "repo_visibility": "private", "publish": True}}}


def _ds():
    p = ROOT / "skills/sigma-loop/scripts/decision_store.py"
    spec = importlib.util.spec_from_file_location(p.stem, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _unit(root, name, body):
    d = pathlib.Path(root) / ".sdlc" / "features" / "units"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def _real(unit, branch, slug="repo"):
    return {"features": {unit: {"repos": {slug: {"branch": branch}}}}}


def _git(root, *a):
    return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path, branch):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    if branch != "main":
        _git(tmp_path, "checkout", "-q", "-b", branch)
    (tmp_path / ".sdlc" / "decisions").mkdir(parents=True)
    (tmp_path / ".sdlc" / "decisions" / "r1.json").write_text("{}", encoding="utf-8")
    return tmp_path / ".sdlc"


def test_unit_branches_reads_real_shape_legacy_and_skips_malformed(tmp_path):
    _unit(tmp_path, "a.json", _real("a", "feat/a"))
    _unit(tmp_path, "b.json", {"branch": "feat/legacy"})
    _unit(tmp_path, "c.json", "{not json")
    _unit(tmp_path, "d.json", ["list"])
    _unit(tmp_path, "e.json", {"features": {"e": {"repos": {"r": {"branch": 5}, "s": "x"}}}})
    assert _ds()._unit_branches(str(tmp_path)) == {"feat/a", "feat/legacy"}


def test_publish_skips_on_real_shape_unit_branch(tmp_path):
    sdlc = _repo(tmp_path, "feat/a")
    _unit(tmp_path, "a.json", _real("a", "feat/a"))
    head = _git(tmp_path, "rev-parse", "HEAD")
    out = _ds().publish(sdlc, CFG)
    assert out == {"committed": False, "reason": "unit-branch"}
    assert _git(tmp_path, "rev-parse", "HEAD") == head


def test_publish_commits_on_non_unit_branch_and_gate_closed(tmp_path):
    sdlc = _repo(tmp_path, "main")
    _unit(tmp_path, "a.json", _real("a", "feat/a"))
    closed = {"rulebook": {"records": {"enabled": False}}}
    assert _ds().publish(sdlc, closed)["reason"] == "records-closed"
    assert _ds().publish(sdlc, CFG) == {"committed": True, "reason": "committed"}
