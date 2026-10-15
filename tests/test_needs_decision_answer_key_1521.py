"""#1521: a needs_decision closing answer is normalised through the bank, like a route answer."""
import json

import test_rulebook_learning_key as base

U, DS = base.U, base.DS
tu, tdr = base.tu, base.tdr


def _close(tmp_path, number, resume, decision="keep-parked"):
    answers = tmp_path / ("c%s.json" % number)
    answers.write_text(json.dumps({"answers": {"resume": resume}}))
    run = tu._runner(views={str(number): tu._view(number=number, comments=[
        tu._park("hit a wall\nsigma-qkind: needs_decision")])})
    code = U.main(["unpark.py", "resolve", str(tmp_path), str(number), "--answers", str(answers),
                   "--decision", decision], run=run)
    return code


def _answers(tmp_path, cfg):
    return [r.get("answer") for r in DS.list_by(str(tmp_path), cfg, qkind="needs_decision")]


def test_variant_spellings_of_a_bank_option_agree_through_resolve(tmp_path):
    tmp_path = tmp_path / "repo" / ".sdlc"
    tmp_path.mkdir(parents=True)
    cfg = base._cfg()
    base._cfg_file(tmp_path, cfg)
    for n, text in enumerate(["leave it parked", "Leave it parked (recommended)", "  LEAVE   IT  PARKED "], 1):
        assert _close(tmp_path, n, text) == 0
    assert _answers(tmp_path, cfg) == ["leave it parked"] * 3


def test_free_text_closing_answer_is_free_and_never_learnable(tmp_path):
    tmp_path = tmp_path / "repo" / ".sdlc"
    tmp_path.mkdir(parents=True)
    cfg = base._cfg()
    base._cfg_file(tmp_path, cfg)
    assert _close(tmp_path, 1, "park it until the vendor answers") == 0
    assert _answers(tmp_path, cfg) == ["free"]
    rec = DS.list_by(str(tmp_path), cfg, qkind="needs_decision")[0]
    assert base.DR.would_for(str(tmp_path), rec, cfg) is None
    assert base.RD._human(str(tmp_path), "needs_decision", "repo", cfg) == {}


def test_unknown_kind_gets_no_answer():
    assert U.route_answer("unknown", {"resume": "leave it parked"}) is None
    assert U.route_answer("needs_decision", {"resume": "Start it again"}) == "start it again"
    assert U.route_answer("quota", {"route": "wait it out"}) == "wait it out"
