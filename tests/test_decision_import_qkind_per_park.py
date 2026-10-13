"""#1354: each unpark answer takes the nearest PRECEDING sigma-qkind line, and the model path keeps it."""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills/sigma-loop/scripts/decision_import.py"


def _mod():
    spec = importlib.util.spec_from_file_location("decision_import_t1354", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _park(kind, cid):
    return {"id": cid, "body": "Parked.\nsigma-qkind: %s" % kind, "created_at": "2026-10-01T00:00:00Z"}


def _qac(ans, cid, q="Pick one?"):
    body = ("<!-- sigma:unpark-qa:start -->\n## Unpark review\n\n_%s_\n- **resume** — %s\n"
            "<!-- sigma:unpark-qa:end -->" % (q, ans))
    return {"id": cid, "body": body, "created_at": "2026-10-02T00:00:00Z"}


def _scan(m, tmp_path, comments):
    sdlc = tmp_path / "repo" / ".sdlc"
    sdlc.mkdir(parents=True, exist_ok=True)
    cfg = {"rulebook": {"import": {"comments": True}}}
    res = m.scan(sdlc, cfg, profile_dirs=[str(tmp_path / "none")], issues=[7],
                 comments_fn=lambda config, issue, **kw: comments)
    return sdlc, res


def test_parked_twice_each_answer_gets_its_own_kind(tmp_path):
    m = _mod()
    comments = [_park("quota", "P1"), _qac("first answer", "A1", "Q one?"),
                _park("dependency", "P2"), _qac("second answer", "A2", "Q two?")]
    sdlc, res = _scan(m, tmp_path, comments)
    rows = m.classify(sdlc, res["candidates"])
    assert {r["choice"]: r["qkind"] for r in rows} == {"first answer": "quota", "second answer": "dependency"}


def test_answer_before_any_line_falls_back_to_last_line(tmp_path):
    m = _mod()
    sdlc, res = _scan(m, tmp_path, [_qac("early", "A0"), _park("quota", "P1")])
    assert [c["qkind"] for c in res["candidates"]] == ["quota"]


def test_single_park_unchanged(tmp_path):
    m = _mod()
    sdlc, res = _scan(m, tmp_path, [_park("quota", "P1"), _qac("go", "A1")])
    assert [r["qkind"] for r in m.classify(sdlc, res["candidates"])] == ["quota"]


def test_model_path_keeps_declared_kind(tmp_path):
    m = _mod()
    sdlc, res = _scan(m, tmp_path, [_park("quota", "P1"), _qac("go", "A1")])
    areas = (sdlc / "categories.json")
    areas.write_text(json.dumps({"schema": "sigma/categories@1",
                                 "categories": [{"id": "money", "areas": ["billing"]}]}))
    cfg = {"rulebook": {"records": {"enabled": True, "repo_visibility": "private"},
                        "autonomy": {"enabled": False}, "import": {"classifier": "model"}}}

    def call(prompt, model, budget):
        return {"kind": "dependency", "area": "billing", "cost_usd": 0.0}
    rows, _info = m.classify_model(str(sdlc), cfg, res["candidates"], call)
    assert rows[0]["qkind"] == "quota"
    assert rows[0]["area"] == "billing"
