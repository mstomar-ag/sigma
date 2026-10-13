"""#1309: comment-sourced decisions take the issue's sigma-qkind; the resume answer has one spelling."""
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


def _qa(ans, n=1):
    return ("<!-- sigma:unpark-qa:start -->\n## Unpark review\n\n_Pick one?_\n- **resume** — %s\n"
            "<!-- sigma:unpark-qa:end -->" % ans)


def _park(kind):
    return {"id": "P%s" % kind, "body": "Parked.\nsigma-qkind: %s" % kind, "created_at": "2026-10-01T00:00:00Z"}


def _qac(ans, cid="C1"):
    return {"id": cid, "body": _qa(ans), "created_at": "2026-10-02T00:00:00Z"}


def _rows(m, tmp_path, comments, gate=True):
    sdlc = tmp_path / "repo" / ".sdlc"
    sdlc.mkdir(parents=True, exist_ok=True)
    cfg = {"rulebook": {"import": {"comments": gate}}}
    res = m.scan(sdlc, cfg, profile_dirs=[str(tmp_path / "none")], issues=[7],
                 comments_fn=lambda config, issue, **kw: comments)
    return res, m.classify(sdlc, res["candidates"])


def test_declared_kind_reaches_row(tmp_path):
    _res, rows = _rows(_mod(), tmp_path, [_park("quota"), _qac("go")])
    assert [r["qkind"] for r in rows] == ["quota"]


def test_last_declared_kind_wins_and_invalid_is_ignored(tmp_path):
    m = _mod()
    _r, rows = _rows(m, tmp_path, [_park("quota"), _park("dependency"), _qac("go")])
    assert rows[0]["qkind"] == "dependency"
    bad = {"id": "B", "body": "sigma-qkind: nonsense", "created_at": "2026-10-01T00:00:00Z"}
    _r, rows = _rows(m, tmp_path, [bad, _qac("go")])
    assert rows[0]["qkind"] == "unknown"


def test_resume_spellings_are_one_answer(tmp_path):
    for spell in ("start it again", "start it again now", "Start it again now."):
        _r, rows = _rows(_mod(), tmp_path, [_qac(spell)])
        assert rows[0]["choice"] == "start it again", spell


def test_other_answers_untouched(tmp_path):
    _r, rows = _rows(_mod(), tmp_path, [_qac("leave it parked")])
    assert rows[0]["choice"] == "leave it parked"


def test_closed_gate_reads_nothing_and_output_identical(tmp_path, capsys):
    m = _mod()
    sdlc = tmp_path / "repo" / ".sdlc"
    sdlc.mkdir(parents=True)
    (sdlc / "config.json").write_text(json.dumps({"rulebook": {"import": {"comments": False}}}))
    read = []

    def reader(config, goal, **kw):
        read.append(goal)
        return [_park("quota"), _qac("start it again now")]
    argv = ["scan", str(sdlc), "--profile", str(tmp_path / "none")]
    assert m.main(argv, lister=lambda c, l: [7], comments_fn=reader) == 0
    gated = capsys.readouterr().out
    assert read == []
    assert m.main(argv) == 0
    assert capsys.readouterr().out == gated
