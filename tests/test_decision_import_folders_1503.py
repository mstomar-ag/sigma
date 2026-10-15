"""#1503: scan/plan/apply print a `folders read:` line on stderr; repeating the flag reads every folder;
one space-joined value is ONE (missing) folder, the zsh unquoted-variable pitfall."""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("decision_import_t1503", ROOT / "skills/sigma-loop/scripts/decision_import.py")
DI = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DI)

X, Y = "-srv-demo-work-x", "-srv-demo-work-y"
CFG = {"rulebook": {"records": {"enabled": True, "repo_visibility": "private"}, "autonomy": {"enabled": False},
                    "import": {"comments": False}}}


def _world(tmp_path):
    sd = tmp_path / "proj" / ".sdlc"
    sd.mkdir(parents=True)
    (sd / "config.json").write_text(json.dumps(CFG))
    prof = tmp_path / "prof"
    for n in (X, Y):
        (prof / "projects" / n).mkdir(parents=True)
        (prof / "projects" / n / "t.jsonl").write_text("")
    return str(sd), str(prof)


def _run(capsys, verb, sd, *rest):
    rc = DI.main([verb, sd, *rest])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _folders_line(err):
    lines = [ln for ln in err.splitlines() if ln.startswith("folders read:")]
    assert len(lines) == 1, err
    return lines[0]


def test_repeated_flag_reads_both_folders_in_all_verbs(tmp_path, capsys):
    sd, prof = _world(tmp_path)
    for verb in ("scan", "plan", "apply"):
        rc, out, err = _run(capsys, verb, sd, "--profile", prof, "--project-dir", X, "--project-dir", Y)
        line = _folders_line(err)
        assert rc == 0 and X in line and Y in line, verb
        if verb == "scan":
            assert "transcript: files_read=2" in out


def test_space_joined_value_is_one_missing_folder(tmp_path, capsys):
    sd, prof = _world(tmp_path)
    rc, out, err = _run(capsys, "scan", sd, "--profile", prof, "--project-dir", X + " " + Y)
    line = _folders_line(err)
    assert rc == 0 and (X + " " + Y) not in line
    assert (X + " " + Y) in "".join(ln for ln in (out + err).splitlines() if "skipped: project folder" in ln)
    assert "skipped: project folder" in out + err and "transcript: files_read=0" in out


def test_help_names_the_repeat_form():
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            DI.main(["scan", "--help"])
        except SystemExit:
            pass
    assert "one folder per flag" in " ".join(buf.getvalue().split())
