"""#1474: the acceptance record reaches the goal branch, and landed root copies stop blocking pull.

Real git throughout (a bare remote, a root checkout, a goal worktree); only `gh` is faked."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(LOOP))


def _load(name, path=None):
    spec = importlib.util.spec_from_file_location(name + "_1474", path or LOOP / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


work = _load("work")
state = _load("state")
doctor = _load("doctor", ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
ON = {"work": {"enabled": True}}
GOAL = "0001-x.md"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout


def _run(cwd, argv):
    argv = [str(a) for a in argv]
    if argv[0] == "git":
        done = subprocess.run(["git", "-C", str(cwd), *argv[1:]], capture_output=True, text=True)
        if done.returncode:             # like work._run: the raised text carries git's own words
            raise RuntimeError(" ".join(argv) + ": " + done.stdout + done.stderr)
        return done.stdout.strip()
    if argv[:2] == ["gh", "api"] and any("pulls?head=" in a for a in argv):
        return "9"
    return ""


def _world(tmp_path):
    """bare remote + root checkout on main + goal worktree on sdlc/0001-x with one change."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
        _git(root, "config", k, v)
    (root / ".gitignore").write_text(".sdlc/work/\n.sdlc/state/\n")
    (root / "f.txt").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-q", "-u", "origin", "main")
    sdlc = root / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    (sdlc / "config.json").write_text(json.dumps(ON))
    state.start_run(str(sdlc))
    wt = sdlc / "work" / "0001-x"
    _git(root, "worktree", "add", "-q", "-b", "sdlc/0001-x", str(wt), "main")
    work._save(str(sdlc), GOAL, {"worktree": str(wt), "branch": "sdlc/0001-x", "base": "main",
                                 "remote": "origin", "pr": "9"})
    (wt / "change.txt").write_text("c\n")
    return remote, root, str(sdlc), wt


def _file(sdlc, sub, text="# r\n"):
    p = pathlib.Path(sdlc) / sub / "0001-x.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_pr_refuses_a_branch_missing_its_acceptance_record(tmp_path):
    _, root, sdlc, wt = _world(tmp_path)
    _file(sdlc, "acceptance")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "c")
    out = work.pr(sdlc, ON, GOAL, run=_run)
    assert "acceptance record" in out and ".sdlc/acceptance/0001-x.md" in out
    assert _git(root, "ls-remote", "origin", "sdlc/0001-x").strip() == ""      # nothing pushed


def test_pr_without_a_root_acceptance_record_behaves_as_before(tmp_path):
    _, _, sdlc, wt = _world(tmp_path)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "c")
    assert work.pr(sdlc, ON, GOAL, run=_run) == "PR #9"


def test_pr_does_not_demand_an_acceptance_record_the_repo_ignores(tmp_path):
    _, root, sdlc, wt = _world(tmp_path)
    (root / ".gitignore").write_text(".sdlc/work/\n.sdlc/state/\n.sdlc/acceptance/\n")
    _git(root, "commit", "-qam", "ignore")
    _git(wt, "merge", "-q", "main")
    _file(sdlc, "acceptance")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "c")
    assert work.pr(sdlc, ON, GOAL, run=_run) == "PR #9"


def test_commit_copies_the_root_acceptance_record_and_pr_then_passes(tmp_path):
    _, _, sdlc, wt = _world(tmp_path)
    rec = _file(sdlc, "acceptance", "# done when\n")
    assert work.commit(sdlc, ON, GOAL, run=_run).startswith("committed")
    assert _git(wt, "show", "HEAD:.sdlc/acceptance/0001-x.md") == "# done when\n"
    assert work.pr(sdlc, ON, GOAL, run=_run) == "PR #9"
    assert rec.read_text() == "# done when\n"


def test_commit_never_overwrites_a_worktree_copy_that_differs(tmp_path):
    _, _, sdlc, wt = _world(tmp_path)
    _file(sdlc, "acceptance", "root\n")
    mine = wt / ".sdlc" / "acceptance" / "0001-x.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("worktree\n")
    work.commit(sdlc, ON, GOAL, run=_run)
    assert _git(wt, "show", "HEAD:.sdlc/acceptance/0001-x.md") == "worktree\n"


def _land(tmp_path, remote):
    """Merge sdlc/0001-x into main on the remote from a second clone, as GitHub would."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
        _git(other, "config", k, v)
    _git(other, "merge", "-q", "--no-ff", "-m", "land", "origin/sdlc/0001-x")
    _git(other, "push", "-q", "origin", "main")


def test_finish_removes_landed_identical_root_copies_and_pull_then_succeeds(tmp_path):
    remote, root, sdlc, wt = _world(tmp_path)
    _file(sdlc, "acceptance", "acc\n")
    _file(sdlc, "plans", "plan\n")
    _file(sdlc, "research", "res-root\n")
    (wt / ".sdlc" / "plans").mkdir(parents=True)
    (wt / ".sdlc" / "plans" / "0001-x.md").write_text("plan\n")
    (wt / ".sdlc" / "research").mkdir(parents=True)
    (wt / ".sdlc" / "research" / "0001-x.md").write_text("res-branch\n")    # differs from root
    work.commit(sdlc, ON, GOAL, run=_run)
    work.pr(sdlc, ON, GOAL, run=_run)
    _land(tmp_path, remote)
    out = work.finish(sdlc, ON, GOAL, run=_run, merged=True)
    assert not (pathlib.Path(sdlc) / "acceptance" / "0001-x.md").exists()
    assert not (pathlib.Path(sdlc) / "plans" / "0001-x.md").exists()
    assert (pathlib.Path(sdlc) / "research" / "0001-x.md").read_text() == "res-root\n"   # kept
    assert ".sdlc/research/0001-x.md" in out and "differs" in out
    (pathlib.Path(sdlc) / "research" / "0001-x.md").unlink()                         # operator action
    assert "acceptance" not in _git(root, "status", "--porcelain")
    _git(root, "pull", "--ff-only", "-q")
    assert (root / ".sdlc" / "acceptance" / "0001-x.md").read_text() == "acc\n"      # now tracked


def test_finish_never_removes_an_unlanded_copy(tmp_path):
    remote, root, sdlc, wt = _world(tmp_path)
    _file(sdlc, "plans", "only-in-root\n")                  # reached no branch
    work.commit(sdlc, ON, GOAL, run=_run)
    _git(wt, "push", "-q", "origin", "sdlc/0001-x")          # pr() would (rightly) refuse this branch
    _land(tmp_path, remote)
    out = work.finish(sdlc, ON, GOAL, run=_run, merged=True)
    assert (pathlib.Path(sdlc) / "plans" / "0001-x.md").read_text() == "only-in-root\n"
    assert "plans/0001-x.md" in out and "not on" in out


def test_finish_without_merge_confirmation_leaves_root_copies(tmp_path):
    _, _, sdlc, _ = _world(tmp_path)
    _file(sdlc, "acceptance", "acc\n")
    work.commit(sdlc, ON, GOAL, run=_run)
    work.finish(sdlc, ON, GOAL, run=_run, force=True)
    assert (pathlib.Path(sdlc) / "acceptance" / "0001-x.md").exists()


def _age(path, days=2):
    t = path.stat().st_mtime - days * 86400
    os.utime(path, (t, t))


def test_doctor_reports_old_untracked_records_split_by_whether_they_landed(tmp_path):
    remote, root, sdlc, wt = _world(tmp_path)
    landed = _file(sdlc, "acceptance", "acc\n")
    lost = _file(sdlc, "plans", "never-committed\n")
    work.commit(sdlc, ON, GOAL, run=_run)
    _git(wt, "push", "-q", "origin", "sdlc/0001-x")
    _land(tmp_path, remote)
    _git(root, "fetch", "-q", "origin")
    for p in (landed, lost):
        _age(p)
    (row,) = doctor._stale_record_rows(pathlib.Path(sdlc), doctor._real_run)
    text = row["name"] + " " + row["fix"]
    assert row["ok"] is False
    assert "landed duplicate (safe to remove)" in text
    assert "on no branch (commit it or it is lost)" in text
    assert "acceptance/0001-x.md" in text and "plans/0001-x.md" in text


def test_doctor_ignores_fresh_untracked_records(tmp_path):
    _, _, sdlc, _ = _world(tmp_path)
    _file(sdlc, "acceptance")
    (row,) = doctor._stale_record_rows(pathlib.Path(sdlc), doctor._real_run)
    assert row["ok"] is True


def test_doctor_reports_a_tracked_but_modified_unit_record_with_the_documented_command(tmp_path):
    _, root, sdlc, _ = _world(tmp_path)
    unit = pathlib.Path(sdlc) / "features" / "units" / "voice.json"
    unit.parent.mkdir(parents=True)
    unit.write_text("{}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "r")
    unit.write_text('{"goals": [1]}\n')
    (row,) = doctor._unit_record_rows(pathlib.Path(sdlc), doctor._real_run, True)
    assert row["ok"] is False
    assert "modified record voice.json" in row["fix"]
    assert "add .sdlc/features && git" in row["fix"] and "registry: record unit membership" in row["fix"]
