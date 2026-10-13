"""Goal 1317: the readout and the learning line use the category thresholds in force."""
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


A = _mod(SCRIPTS / "autonomy.py")
DS = _mod(SCRIPTS / "decision_store.py")
DOC = _mod(ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py")


def _cfg():
    lv = {"l1_min_answers": 4, "l2_min_answers": 8, "l2_agreement": 0.75, "l3_min_answers": 16,
          "l3_agreement": 0.9, "l4_min_answers": 32, "l4_agreement": 0.99}
    return {"rulebook": {"records": {"enabled": True}, "autonomy": {"enabled": True, "levels": lv}}}


def _write(sd, levels):
    (sd / "categories.json").write_text(json.dumps({"schema": "sigma/categories@1", "categories": [
        {"id": "fast", "areas": ["docs"], "levels": levels}]}))


def _answer(sd, cfg, n, area="docs"):
    for _ in range(n):
        DS.append(sd, {"how": "human", "qkind": "needs_decision", "area": area, "choice": "a"}, cfg)


def test_level_zero_line_uses_category_l1(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 1)
    _write(tmp_path, {"l1_min_answers": 2})
    row = A.readout(tmp_path, cfg)["kinds"][0]
    assert row["answers_needed"] == 2
    assert "1 of 2 answers" in A.describe(row)
    assert "1 of 2 answers" in DOC._autonomy_state(cfg, tmp_path)


def test_level_one_line_uses_category_l2(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 2)
    _write(tmp_path, {"l1_min_answers": 2, "l2_min_answers": 3, "l2_agreement": 0.5})
    row = A.readout(tmp_path, cfg)["kinds"][0]
    assert (row["next_level"], row["answers_needed"], row["comparisons_needed"], row["agreement_needed"]) == (2, 3, 3, 0.5)
    text = DOC._autonomy_state(cfg, tmp_path)
    assert "level 2 needs 3 answers" in text and "50% agreement" in text


def test_learning_line_uses_category_l1(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 1)
    assert "(1 of 4 answers in area docs)" in A.learning_line(tmp_path, "needs_decision", cfg)
    _write(tmp_path, {"l1_min_answers": 2})
    assert "(1 of 2 answers in area docs)" in A.learning_line(tmp_path, "needs_decision", cfg)


def test_no_file_is_byte_identical_and_global(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 1)
    row = A.readout(tmp_path, cfg)["kinds"][0]
    assert "thresholds" not in row and row["answers_needed"] == 4
    assert "1 of 4 answers" in A.describe(row)
    assert A.learning_line(tmp_path, "needs_decision", cfg) == (
        "the rulebook is still learning and will keep asking this kind (1 of 4 answers in area docs)")
    assert A._need({"level": 1, "capped": False}, cfg) == {"next_level": 2, "answers_needed": 8, "comparisons_needed": 8,
                                          "agreement_needed": 0.75}
