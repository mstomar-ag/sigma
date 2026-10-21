"""#1504: `define.py open` cuts the unit branch from the REMOTE base after a fetch, not the local one.

Real git against a local bare remote (no network). Only the `gh` legs are stubbed, through the
module's own runner seam (`_run_default`), and the CLI is driven the way the docs give it."""
import importlib.util
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("define_1504", ROOT / "skills" / "sigma-define" / "scripts" / "define.py")
define = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(define)
_LOOP = ROOT / "skills" / "sigma-loop" / "scripts"
_rspec = importlib.util.spec_from_file_location("feature_registry_1504", _LOOP / "feature_registry.py")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo, name):
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", name)


def _world(tmp_path, ahead=2):
    """A bare remote whose `main` is `ahead` commits past the local clone's `main`."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _commit(work, "c1")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    for i in range(ahead):
        _commit(other, "r%d" % i)
    _git(other, "push", "-q", "origin", "main")
    (work / ".sdlc").mkdir()
    (work / ".sdlc" / "config.json").write_text(
        '{"discovery": {"github": {"repo": "fixture-org/fixture-repo"}}}')
    return work, remote


def _stub_gh(monkeypatch, login="tester"):
    real = define._run_default

    def run(cwd, argv):
        argv = [str(a) for a in argv]
        if argv[:3] == ["gh", "api", "user"]:
            if login is None:
                raise RuntimeError("not logged in")
            return login
        if argv[0] == "gh":
            return ""
        return real(cwd, argv)
    monkeypatch.setattr(define, "_run_default", run)


def _open(work, capsys, base="main", name="rulebook"):
    rc = define.main(["define.py", "open", str(work / ".sdlc"), "--name", name,
                      "--type", "feature", "--base", base])
    return rc, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_unit_branch_is_cut_at_the_remote_main_two_commits_ahead_of_local(tmp_path, monkeypatch, capsys):
    work, remote = _world(tmp_path, ahead=2)
    _stub_gh(monkeypatch)
    local_main = _git(work, "rev-parse", "main")
    remote_main = _git(remote, "rev-parse", "main")
    assert local_main != remote_main
    rc, report = _open(work, capsys)
    assert rc == 0 and report["ok"], report
    assert _git(remote, "rev-parse", "feature/rulebook") == remote_main
    assert _git(remote, "rev-list", "--count", "%s..%s" % (local_main, remote_main)) == "2"
    assert report["base_commit"] == remote_main
    assert remote_main[:12] in report["steps"][0]["detail"]      # the output names the base commit


def test_a_base_missing_on_the_remote_is_refused_with_one_line_and_pushes_nothing(tmp_path, monkeypatch, capsys):
    work, remote = _world(tmp_path)
    _stub_gh(monkeypatch)
    rc, report = _open(work, capsys, base="nope")
    assert rc == 1 and not report["ok"]
    assert report["steps"][-1]["outcome"] == define.REFUSED
    assert "\n" not in report["why"] and "origin/nope" in report["why"]
    assert _git(remote, "branch", "--list", "feature/*") == ""


def test_the_unit_record_has_the_active_login_and_one_repository_entry(tmp_path, monkeypatch, capsys):
    work, _remote = _world(tmp_path)
    (work / ".sdlc" / "config.json").write_text(
        json.dumps({"discovery": {"github": {"repo": "acme/widgets"}}}))
    _stub_gh(monkeypatch)
    rc, report = _open(work, capsys)
    assert rc == 0, report
    rec = json.loads((work / ".sdlc" / "features" / "units" / "rulebook.json").read_text())
    entry = rec["features"]["rulebook"]
    assert entry["owner"] == "tester"
    assert list(entry["repos"]) == ["acme/widgets"]
    assert entry["repos"]["acme/widgets"]["branch"] == "feature/rulebook"
    assert entry["repos"]["acme/widgets"]["owner"] == "tester"


def test_an_unresolvable_login_leaves_the_owner_empty_but_the_open_still_succeeds(tmp_path, monkeypatch, capsys):
    work, _remote = _world(tmp_path)
    _stub_gh(monkeypatch, login=None)
    rc, report = _open(work, capsys)
    assert rc == 0 and report["ok"]


def test_an_up_to_date_local_base_opens_at_the_same_commit_as_before(tmp_path, monkeypatch, capsys):
    work, remote = _world(tmp_path, ahead=0)
    _stub_gh(monkeypatch)
    before = _git(work, "rev-parse", "main")          # the record commit then advances local main
    rc, report = _open(work, capsys)
    assert rc == 0 and report["ok"]
    assert _git(remote, "rev-parse", "feature/rulebook") == before
    assert [s["step"] for s in report["steps"]] == ["branch", "label", "registry"]
    assert [s["outcome"] for s in report["steps"]] == [define.CREATED] * 3
