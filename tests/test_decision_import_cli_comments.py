"""#1308: decision_import.main() passes the labelled issues to scan() when the comments gate is open."""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills/sigma-loop/scripts/decision_import.py"

QA = ("<!-- sigma:unpark-qa:start -->\n## Unpark review\n\n_Which lane?_\n- **lane** — fast lane\n"
      "<!-- sigma:unpark-qa:end -->")


def _mod():
    spec = importlib.util.spec_from_file_location("decision_import", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sdlc(tmp_path, comments):
    sdlc = tmp_path / "repo" / ".sdlc"
    sdlc.mkdir(parents=True)
    cfg = {"rulebook": {"import": {"comments": comments}, "records": {"enabled": False}}}
    (sdlc / "config.json").write_text(json.dumps(cfg))
    return sdlc


def _fakes(listed, read):
    def lister(config, limits):
        listed.append(limits["max_issues"])
        return [7, 8]

    def reader(config, goal, **kw):
        read.append(goal)
        return [{"id": "IC_%s" % goal, "author": "x", "body": QA, "created_at": "2026-10-03T00:00:00Z"}]
    return lister, reader


def test_scan_verb_open_gate_lists_and_reads(tmp_path, capsys):
    m, listed, read = _mod(), [], []
    lister, reader = _fakes(listed, read)
    sdlc = _sdlc(tmp_path, True)
    assert m.main(["scan", str(sdlc), "--profile", str(tmp_path / "none")], lister=lister, comments_fn=reader) == 0
    out = capsys.readouterr().out
    assert listed == [50] and read == [7, 8]
    assert "unpark-qa: files_read=2 candidates=2 " in out, out


def test_plan_verb_open_gate_sees_candidates(tmp_path, capsys):
    m, listed, read = _mod(), [], []
    lister, reader = _fakes(listed, read)
    sdlc = _sdlc(tmp_path, True)
    assert m.main(["plan", str(sdlc), "--profile", str(tmp_path / "none")], lister=lister, comments_fn=reader) == 0
    assert read == [7, 8]


def test_apply_verb_open_gate_lists(tmp_path, capsys):
    m, listed, read = _mod(), [], []
    lister, reader = _fakes(listed, read)
    sdlc = _sdlc(tmp_path, True)
    m.main(["apply", str(sdlc), "--profile", str(tmp_path / "none")], lister=lister, comments_fn=reader)
    assert listed == [50] and read == [7, 8]


def test_closed_gate_makes_no_fetch_and_output_unchanged(tmp_path, capsys):
    m, listed, read = _mod(), [], []
    lister, reader = _fakes(listed, read)
    sdlc = _sdlc(tmp_path, False)
    argv = ["scan", str(sdlc), "--profile", str(tmp_path / "none")]
    assert m.main(argv, lister=lister, comments_fn=reader) == 0
    gated = capsys.readouterr().out
    assert listed == [] and read == []
    assert m.main(argv) == 0
    assert capsys.readouterr().out == gated
    assert "unpark-qa: files_read=0 candidates=0" in gated


def test_lister_failure_is_fail_open(tmp_path, capsys):
    m = _mod()

    def boom(config, limits):
        raise RuntimeError("no gh")
    sdlc = _sdlc(tmp_path, True)
    assert m.main(["scan", str(sdlc), "--profile", str(tmp_path / "none")], lister=boom) == 0
    assert "unpark-qa: files_read=0 candidates=0" in capsys.readouterr().out


def test_default_lister_asks_rest_for_goal_label(tmp_path):
    m = _mod()
    calls = []

    def run(args):
        calls.append(args)
        return json.dumps([{"number": 3}, {"number": 4, "pull_request": {}}])
    got = m.list_issues({"discovery": {"github": {"repo": "o/r"}}}, {"max_issues": 5}, run=run)
    assert got == [3]
    a = calls[0]
    assert a[1] == "repos/o/r/issues" and "labels=sdlc:goal" in a and "state=all" in a
