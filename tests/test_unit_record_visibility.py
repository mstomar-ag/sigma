"""#1312: `define.py open` commits the unit record, and doctor flags records other machines cannot see.

Real git throughout (a temp repository), because the claim is about what a commit contains."""
import importlib.util
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(LOOP))


def _mod(name, path):
    spec = importlib.util.spec_from_file_location(name + "_1312", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


define = _mod("define", ROOT / "skills" / "sigma-define" / "scripts" / "define.py")
doctor = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout


def _repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("x")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-qm", "init")
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    return sdlc


def _record(sdlc, name="voice"):
    fr = _mod("feature_registry", LOOP / "feature_registry.py")
    return fr.write_unit(fr.registry_dir(sdlc), name, {"open": True})


def _runner(tmp_path):
    """git goes to the real binary; the push/label legs are faked (no remote exists here)."""
    def run(cwd, argv):
        argv = [str(a) for a in argv]
        if argv[:2] == ["git", "fetch"]:
            return ""
        if argv[:3] == ["git", "rev-parse", "--verify"]:
            return "a" * 40                       # #1504: the fetched `<remote>/<base>` commit
        if argv[:3] == ["gh", "api", "user"]:
            return "tester"
        if argv[:2] == ["git", "ls-remote"] and argv[-1].startswith("refs/heads/main"):
            return "abc\t" + argv[-1]
        if argv[0] == "git" and argv[1] not in ("push", "ls-remote"):
            return _git(cwd, *argv[1:]).strip()
        if argv[:2] == ["git", "ls-remote"]:
            return ""
        if argv[:2] == ["git", "push"]:
            return ""
        if argv[:3] == ["gh", "label", "list"]:
            return ""
        return ""
    return run


def test_open_commits_only_the_unit_record(tmp_path):
    sdlc = _repo(tmp_path)
    rec = _record(sdlc)
    (tmp_path / "other.txt").write_text("unrelated")
    _git(tmp_path, "add", "other.txt")                       # staged, must stay out of the commit
    report = define.open_unit(sdlc, "voice", "feature", run=_runner(tmp_path), cwd=tmp_path,
                              base="main")
    assert report["ok"], report
    files = _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split()
    assert files == [str(rec.relative_to(tmp_path))]
    assert "A  other.txt" in _git(tmp_path, "status", "--porcelain")


def test_an_uncommittable_record_prints_the_command_and_still_reports_ok(tmp_path, capsys):
    sdlc = _repo(tmp_path)
    _record(sdlc)
    base = _runner(tmp_path)

    def run(cwd, argv):
        if [str(a) for a in argv][:2] == ["git", "commit"]:
            raise RuntimeError("index locked")
        return base(cwd, argv)
    report = define.open_unit(sdlc, "voice", "feature", run=run, cwd=tmp_path, base="main")
    assert report["ok"], report
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "git add -- " in err and "git commit -m" in err


def test_open_on_a_fresh_unit_writes_and_commits_its_record(tmp_path):
    """The documented gesture: no record exists beforehand."""
    sdlc = _repo(tmp_path)
    report = define.open_unit(sdlc, "voice", "feature", run=_runner(tmp_path), cwd=tmp_path,
                              base="main")
    assert report["ok"], report
    rel = ".sdlc/features/units/voice.json"
    assert _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split() == [rel]
    assert _git(tmp_path, "status", "--porcelain", "--", rel).strip() == ""
    text = (tmp_path / rel).read_text()
    assert "login" not in text and "person" not in text


def test_open_on_a_feature_branch_writes_the_record_but_does_not_commit_it(tmp_path, capsys):
    """#1325: the documented gesture on a fresh unit, run while a unit branch is checked out."""
    sdlc = _repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "feature/voice")
    head = _git(tmp_path, "rev-parse", "HEAD")
    report = define.open_unit(sdlc, "voice", "feature", run=_runner(tmp_path), cwd=tmp_path,
                              base="main")
    assert report["ok"], report
    rel = ".sdlc/features/units/voice.json"
    assert (tmp_path / rel).is_file()
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "git add -- " in err and "git commit -m" in err and rel in err


def test_an_unreadable_branch_fails_closed_and_prints_the_command(tmp_path, capsys):
    sdlc = _repo(tmp_path)
    base = _runner(tmp_path)

    def run(cwd, argv):
        if [str(a) for a in argv][:3] == ["git", "rev-parse", "--abbrev-ref"]:
            raise RuntimeError("no head")
        return base(cwd, argv)
    head = _git(tmp_path, "rev-parse", "HEAD")
    report = define.open_unit(sdlc, "voice", "feature", run=run, cwd=tmp_path, base="main")
    assert report["ok"], report
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert "git commit -m" in capsys.readouterr().err


def test_a_committed_unchanged_record_is_not_recommitted(tmp_path):
    sdlc = _repo(tmp_path)
    _record(sdlc)
    run = _runner(tmp_path)
    define.open_unit(sdlc, "voice", "feature", run=run, cwd=tmp_path, base="main")
    head = _git(tmp_path, "rev-parse", "HEAD")
    define.open_unit(sdlc, "voice", "feature", run=run, cwd=tmp_path, base="main")
    assert _git(tmp_path, "rev-parse", "HEAD") == head


def _doctor_rows(sdlc, run):
    return doctor._unit_record_rows(sdlc, run, True)


def test_doctor_row_is_absent_without_a_registry(tmp_path):
    sdlc = _repo(tmp_path)
    assert _doctor_rows(sdlc, doctor._real_run) == []


def test_doctor_row_is_red_for_an_untracked_record_and_green_once_committed(tmp_path):
    sdlc = _repo(tmp_path)
    rec = _record(sdlc)
    (row,) = _doctor_rows(sdlc, doctor._real_run)
    assert row["ok"] is False and "voice.json" in row["fix"]
    _git(tmp_path, "add", "--", str(rec))
    _git(tmp_path, "commit", "-qm", "r")
    (row,) = _doctor_rows(sdlc, doctor._real_run)
    assert row["ok"] is True and row["fix"] == ""


def test_doctor_row_flags_a_remote_feature_branch_with_no_record(tmp_path):
    sdlc = _repo(tmp_path)
    _record(sdlc, "known")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "r")

    def run(args):
        if "ls-remote" in args:
            return "abc\trefs/heads/feature/known\nabc\trefs/heads/feature/ghost\n"
        return doctor._real_run(args)
    (row,) = doctor._unit_record_rows(sdlc, run, False)
    assert row["ok"] is False and "feature/ghost" in row["fix"] and "feature/known" not in row["fix"]
    (row,) = doctor._unit_record_rows(sdlc, run, True)           # cheap_only: no remote read
    assert row["ok"] is True


def test_the_row_is_wired_into_check(tmp_path):
    sdlc = _repo(tmp_path)
    (sdlc / "config.json").write_text("{}")
    _record(sdlc)
    names = [c["name"] for c in doctor.check(str(sdlc), run=lambda a: "", cheap_only=True)]
    assert "unit records visible to other machines" in names
