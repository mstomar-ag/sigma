"""#1304: a decision record is written only for a gesture that landed.

Driven through `unpark.main(["unpark.py", "resolve", <sdlc_dir>, <issue>, "--answers", F,
"--decision", D])` -- the gesture the skill docs give -- with the records part open.
"""
import json, pathlib

import test_unpark as tu
import test_decision_record as tdr

_mod = tu._mod


def _cli(tmp_path, view_labels, decision, fail_on=()):
    u = _mod("unpark")
    cfg = tdr._on(tu._config())
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps({"answers": {"decision": "start it again"}}))
    run = tu._runner(views={"5": tu._view(labels=view_labels, comments=[tu._park("x")])},
                     fail_on=fail_on)
    code = u.main(["unpark.py", "resolve", str(tmp_path), "5", "--answers", str(answers),
                   "--decision", decision], run=run)
    return code, run


def test_a_failed_label_write_writes_no_decision_record(tmp_path, capsys):
    code, run = _cli(tmp_path, ("sdlc:parked",), "unpark", fail_on=["addLabelsToLabelable"])
    assert code == 1 and "did not land" in capsys.readouterr().out
    assert tdr._ids(tmp_path) == []
    # the typed answers survive in the body so the retry need not ask again
    assert any(c[:2] == ["issue", "edit"] and "--body" in c for c in run.calls)


def test_keep_parked_on_an_issue_that_is_not_parked_is_refused(tmp_path, capsys):
    code, run = _cli(tmp_path, ("sdlc:goal",), "keep-parked")
    out = capsys.readouterr().out
    assert code == 1 and "not parked" in out and "[failed]" in out
    assert tdr._ids(tmp_path) == []
    assert tdr._bodies(run, 5) == []
    assert not any(c[:2] == ["issue", "edit"] for c in run.calls)


def test_unpark_on_an_issue_that_is_not_parked_is_refused(tmp_path, capsys):
    code, run = _cli(tmp_path, ("sdlc:goal",), "unpark")
    out = capsys.readouterr().out
    assert code == 1 and "not parked" in out
    assert tdr._ids(tmp_path) == [] and tdr._bodies(run, 5) == []


def test_a_landed_unpark_and_a_parked_keep_parked_still_write_one_record_each(tmp_path):
    for sub, decision in (("u", "unpark"), ("k", "keep-parked")):
        d = tmp_path / sub
        d.mkdir()
        code, run = _cli(d, ("sdlc:parked",), decision)
        assert code == 0
        ids = tdr._ids(d)
        assert len(ids) == 1 and any(ids[0] in b for b in tdr._bodies(run, 5))


def test_the_closed_gate_comment_bytes_are_unchanged(tmp_path):
    u = _mod("unpark")
    run = tu._runner(views={"5": tu._view(comments=[tu._park("x")])})
    u.resolve(str(tmp_path), tu._config(), 5, {"decision": "start it again"}, "unpark", run=run)
    assert tdr._bodies(run, 5) == [u.render_block({"decision": "start it again"})]
    assert not (tmp_path / "decisions").exists()
