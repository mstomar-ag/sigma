"""#1355: the comments-import lister also covers parked issues; a lister failure is one stderr line."""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills/sigma-loop/scripts/decision_import.py"


def _mod():
    spec = importlib.util.spec_from_file_location("decision_import", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _run_factory(calls):
    def run(args):
        calls.append(args)
        label = [a for a in args if a.startswith("labels=")][0]
        if label == "labels=sdlc:goal":
            return json.dumps([{"number": 5}, {"number": 9}])
        if label == "labels=sdlc:parked":
            return json.dumps([{"number": 9}, {"number": 3}])
        return "[]"
    return run


def test_lister_includes_parked_issues():
    calls = []
    got = _mod().list_issues({"discovery": {"github": {}}}, {"max_issues": 50}, run=_run_factory(calls))
    assert 3 in got                      # parked-only issue is listed
    assert sorted(got) == [3, 5, 9]      # no duplicate for an issue seen under both labels
    assert len(calls) == 2


def test_lister_uses_configured_parked_label():
    calls = []

    def run(args):
        calls.append(args)
        return "[]"
    cfg = {"discovery": {"github": {"parked_label": "x:held"}}}
    _mod().list_issues(cfg, {"max_issues": 50}, run=run)
    assert any("labels=x:held" in a for c in calls for a in c)


def _sdlc(tmp_path, comments):
    sdlc = tmp_path / "repo" / ".sdlc"
    sdlc.mkdir(parents=True)
    cfg = {"rulebook": {"import": {"comments": comments}, "records": {"enabled": False}}}
    (sdlc / "config.json").write_text(json.dumps(cfg))
    return sdlc


def test_lister_failure_one_stderr_line_stdout_unchanged(tmp_path, capsys):
    m = _mod()
    sdlc = _sdlc(tmp_path, True)

    def boom(config, limits):
        raise RuntimeError("gh down\nsecond line")
    assert m.main(["scan", str(sdlc)], lister=boom) == 0
    bad = capsys.readouterr()
    assert m.main(["scan", str(sdlc)], lister=lambda c, l: []) == 0
    clean = capsys.readouterr()
    assert bad.out == clean.out
    bad_err = [ln for ln in bad.err.splitlines() if not ln.startswith("folders read:")]
    assert len(bad_err) == 1 and "decision_import" in bad.err


def test_gate_closed_no_list_call_and_silent(tmp_path, capsys):
    m = _mod()
    sdlc = _sdlc(tmp_path, False)
    called = []
    assert m.main(["scan", str(sdlc)], lister=lambda c, l: called.append(1) or []) == 0
    out = capsys.readouterr()
    assert called == [] and [ln for ln in out.err.splitlines() if not ln.startswith("folders read:")] == []
