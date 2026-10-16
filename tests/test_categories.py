"""Goal 1221: per-category autonomy thresholds from .sdlc/categories.json."""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


C = _mod(SCRIPTS / "categories.py")
A = _mod(SCRIPTS / "autonomy.py")
DS = _mod(SCRIPTS / "decision_store.py")
DOC = _mod(ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py")


def _cfg():
    lv = {"l1_min_answers": 2, "l2_min_answers": 4, "l2_agreement": 0.75, "l3_min_answers": 8,
          "l3_agreement": 0.9, "l4_min_answers": 16, "l4_agreement": 0.99}
    return {"rulebook": {"records": {"enabled": True}, "autonomy": {"enabled": True, "levels": lv}}}


def _write(sd, cats, schema="sigma/categories@1"):
    (sd / "categories.json").write_text(json.dumps({"schema": schema, "categories": cats}))


def _answer(sd, cfg, n, area):
    for _ in range(n):
        DS.append(sd, {"how": "human", "qkind": "needs_decision", "area": area, "choice": "a"}, cfg)


def test_reader_failsafe(tmp_path):
    assert C.load(tmp_path) == [] and C.resolve(tmp_path, "k", "docs") is None
    (tmp_path / "categories.json").write_text("{not json")
    assert C.load(tmp_path) == []
    _write(tmp_path, [{"id": "a", "areas": ["docs"]}], schema="other@1")
    assert C.load(tmp_path) == []
    _write(tmp_path, [
        {"id": "a", "areas": ["docs"]},
        {"id": "a", "areas": ["x"]},                                        # duplicate id
        {"id": "b", "areas": ["x"], "levels": {"l9_min_answers": 1}},       # unknown key
        {"id": "c", "areas": ["x"], "levels": {"l1_min_answers": "3"}},     # wrong type
        {"id": "d", "areas": ["x"], "levels": {"l2_agreement": 1.5}},
        {"id": "e", "areas": "x"}, "junk"])
    assert [c["id"] for c in C.load(tmp_path)] == ["a"]


def test_first_match_and_qkinds(tmp_path):
    _write(tmp_path, [{"id": "one", "areas": ["area:docs"], "qkinds": ["other"]},
                      {"id": "two", "areas": ["docs"]}, {"id": "three", "areas": ["docs"]}])
    assert C.resolve(tmp_path, "needs_decision", "docs")["id"] == "two"
    assert C.resolve(tmp_path, "other", "docs")["id"] == "one"
    assert C.resolve(tmp_path, "needs_decision", "src") is None


def test_override_applies_to_its_pair_only(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 2, "docs")
    _answer(tmp_path, cfg, 2, "src")
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 1
    assert A.level(tmp_path, "needs_decision", "src", cfg) == 1
    _write(tmp_path, [{"id": "slow", "title": "Slow", "areas": ["docs"], "levels": {"l1_min_answers": 3}}])
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 0
    assert A.level(tmp_path, "needs_decision", "src", cfg) == 1
    _write(tmp_path, [{"id": "fast", "areas": ["docs"], "levels": {"l1_min_answers": 1}}])
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 1


def test_readout_and_doctor_show_category(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 2, "docs")
    _answer(tmp_path, cfg, 2, "src")
    base_rows = A.readout(tmp_path, cfg)
    base_text = DOC._autonomy_state(cfg, tmp_path)
    assert all("category" not in r and "thresholds" not in r for r in base_rows["kinds"])
    assert "[category" not in base_text
    _write(tmp_path, [{"id": "slow", "areas": ["docs"], "levels": {"l1_min_answers": 1}}])
    rows = {r["area"]: r for r in A.readout(tmp_path, cfg)["kinds"]}
    assert rows["docs"]["category"] == "slow" and rows["docs"]["thresholds"]["l1_min_answers"] == 1
    assert rows["src"]["category"] is None and rows["src"]["thresholds"]["l1_min_answers"] == 2
    text = DOC._autonomy_state(cfg, tmp_path)
    assert "[category slow: l1>=1" in text and "[category none: l1>=2" in text


def test_closed_gate_is_unchanged_even_with_a_file(tmp_path):
    cfg = _cfg()
    _write(tmp_path, [{"id": "slow", "areas": ["docs"]}])
    cfg["rulebook"]["autonomy"]["enabled"] = False
    assert A.readout(tmp_path, cfg) == {"enabled": False}
    assert DOC._autonomy_state(cfg, tmp_path) == "off (default)"


def test_schema_precedence_and_docs_pinned():
    assert C.SCHEMA == "sigma/categories@1"
    assert set(C.KEYS) == set(A.DEFAULTS)
    doc = (ROOT / "docs" / "configuration.md").read_text()
    assert "sigma/categories@1" in doc and ".sdlc/categories.json" in doc
    assert "global default, then the first category" in doc
    assert not list((ROOT / "skills" / "sigma-init" / "templates").glob("*categories*"))
