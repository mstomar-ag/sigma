"""Goal 1243: the opt-in model classifier for the past-decision import. A fake call only; no network."""
import importlib.util
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_t1243", SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DI, DS, CFG = _mod("decision_import"), _mod("decision_store"), _mod("rulebook_cfg")
CACHE = "state/decision-import-classes.json"


def _cfg(classifier="model", records=True, **extra):
    imp = dict(extra, classifier=classifier)
    return {"rulebook": {"records": {"enabled": records, "repo_visibility": "private"},
                                "autonomy": {"enabled": False}, "import": imp}}


def _sd(tmp_path, areas=("billing",)):
    p = tmp_path / ".sdlc"
    p.mkdir(exist_ok=True)
    (p / "categories.json").write_text(json.dumps(
        {"schema": "sigma/categories@1", "categories": [{"id": "money", "areas": list(areas)}]}))
    return str(p)


def _c(i, q="should we merge the docs change", choice="SECRETCHOICE", reason="SECRETREASON", opts=("yes", "no")):
    return {"source": "transcript", "when": "2026-10-01T10:%02d:00Z" % i, "question": q + " %d" % i,
            "options": list(opts), "choice": choice, "reason": reason, "issue": 7, "id": "c%03d" % i,
            "superseded": False}


class Fake:
    def __init__(self, answer=None, cost=0.01, exc=None):
        self.answer, self.cost, self.exc, self.calls = answer, cost, exc, []

    def __call__(self, prompt, model, max_budget_usd):
        self.calls.append((prompt, model, max_budget_usd))
        if self.exc:
            raise self.exc
        if self.answer is None:
            return None
        return dict(self.answer, cost_usd=self.cost)


GOOD = {"kind": "failing_check", "area": "billing"}


def _recs(sd, cfg):
    return DS.list_by(sd, cfg)


def test_normal_answer_is_stored_and_cached(tmp_path):
    sd, cfg, f = _sd(tmp_path), _cfg(), Fake(GOOD)
    out = DI.apply(sd, cfg, [_c(1)], call=f)
    assert out["records"] == 1 and len(f.calls) == 1 and f.calls[0][1] == "haiku"
    r = _recs(sd, cfg)[0]
    assert (r["qkind"], r["area"]) == ("failing_check", "billing")
    assert json.loads((pathlib.Path(sd) / CACHE).read_text())["classes"][DI._fp(_c(1))] == {
        "qkind": "failing_check", "area": "billing"}


def _sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return _sd(d)


def test_out_of_list_kind_and_area_become_unknown_without_table(tmp_path):
    q = "merge conflict in billing"
    for n, bad in enumerate(({"kind": "made_up", "area": "billing"}, {"kind": "failing_check", "area": "nowhere"})):
        sd, cfg = _sub(tmp_path, "d%d" % n), _cfg()
        DI.apply(sd, cfg, [_c(2, q=q)], call=Fake(bad))
        r = _recs(sd, cfg)[0]
        assert (r["qkind"], r["area"]) == ("unknown", "repo")   # the table would say merge_conflict


def test_error_and_timeout_become_unknown(tmp_path):
    for n, fake in enumerate((Fake(exc=RuntimeError("boom")), Fake(answer=None))):
        sd = _sub(tmp_path, "d%d" % n)
        rows, info = DI.classify_model(sd, _cfg(), [_c(3, q="merge conflict")], fake)
        assert (rows[0]["qkind"], rows[0]["area"]) == ("unknown", "repo")


def test_no_answer_is_not_cached_or_stored_and_a_rerun_retries(tmp_path):
    for n, fake in enumerate((Fake(exc=RuntimeError("boom")), Fake(answer=None))):
        sd, cfg = _sub(tmp_path, "d%d" % n), _cfg()
        out = DI.apply(sd, cfg, [_c(5)], call=fake)
        assert out["records"] == 0 and _recs(sd, cfg) == []
        assert not (pathlib.Path(sd) / CACHE).exists()
        good = Fake(GOOD)
        assert DI.apply(sd, cfg, [_c(5)], call=good)["records"] == 1 and len(good.calls) == 1


def test_no_answer_still_counts_the_per_call_ceiling(tmp_path):
    rows, info = DI.classify_model(_sd(tmp_path), _cfg(), [_c(6)], Fake(exc=RuntimeError("x")))
    assert info["sent"] == 1 and info["spent"] == DI._load("rulebook_cfg").import_limits(_cfg())["max_budget_usd_per_call"]


def test_second_apply_makes_no_call_and_cannot_relabel(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    DI.apply(sd, cfg, [_c(1)], call=Fake(GOOD))
    f2 = Fake({"kind": "quota", "area": "repo"})
    out = DI.apply(sd, cfg, [_c(1)], call=f2)
    assert f2.calls == [] and out["records"] == 0
    assert _recs(sd, cfg)[0]["qkind"] == "failing_check"


def test_cached_class_is_used_without_a_call_for_a_new_record(tmp_path):
    sd = _sd(tmp_path)
    cache = pathlib.Path(sd) / CACHE
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"classes": {DI._fp(_c(4)): {"qkind": "quota", "area": "repo"}}}))
    f = Fake(GOOD)
    rows, info = DI.classify_model(sd, _cfg(), [_c(4)], f)
    assert f.calls == [] and rows[0]["qkind"] == "quota" and info["cached"] == 1


def test_run_ceiling_stops_mid_run_and_is_reported(tmp_path):
    sd = _sd(tmp_path)
    f = Fake(GOOD, cost=0.4)
    cfg = _cfg(max_budget_usd_per_run=0.5, max_budget_usd_per_call=0.4)
    rows, info = DI.classify_model(sd, cfg, [_c(i, q="merge conflict") for i in range(5)], f)
    assert len(f.calls) == 2 and info["stopped_after"] == 2
    assert [r["qkind"] for r in rows[2:]] == ["unknown"] * 3          # not the table's merge_conflict
    cache = json.loads((pathlib.Path(sd) / CACHE).read_text())["classes"]
    assert len(cache) == 2                                            # stopped ones stay uncached
    assert f.calls[0][2] == 0.4
    out = DI.apply(sd, cfg, [_c(i, q="merge conflict") for i in range(5)], call=f)
    assert out["stopped_after"] == 2


def test_missing_cost_counts_as_the_per_call_ceiling(tmp_path):
    sd = _sd(tmp_path)
    f = Fake(GOOD, cost=None)
    cfg = _cfg(max_budget_usd_per_run=0.1, max_budget_usd_per_call=0.06)
    rows, info = DI.classify_model(sd, cfg, [_c(i) for i in range(5)], f)
    assert len(f.calls) == 2


def test_plan_sends_nothing_and_counts(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    out = DI.plan(sd, cfg, [_c(i) for i in range(3)])
    m = out["model"]
    assert m["would_send"] == 3 and m["model"] == "haiku" and m["run_ceiling"] > 0
    assert not (pathlib.Path(sd) / "state").exists()


def test_table_and_other_values_are_byte_identical(tmp_path):
    cands = [_c(i, q="merge conflict") for i in range(3)]
    base = DI.apply(_sd(tmp_path), _cfg("table"), cands)
    assert "model" not in DI.plan(_sd(tmp_path), _cfg("table"), cands)
    for val in ("Model", "haiku", True, None, ""):
        assert CFG.import_limits(_cfg(val))["classifier"] == "table"
    f = Fake(GOOD)
    t2 = tmp_path / "b"
    t2.mkdir()
    sd2 = _sd(t2)
    out = DI.apply(sd2, _cfg("bogus"), cands, call=f)
    assert f.calls == [] and out == base
    assert _recs(sd2, _cfg())[0]["qkind"] == "merge_conflict"


def test_gate_closed_is_untouched_and_calls_nothing(tmp_path):
    sd, f = _sd(tmp_path), Fake(GOOD)
    out = DI.apply(sd, _cfg(records=False), [_c(1)], call=f)
    assert f.calls == [] and out["records"] == 0 and not (pathlib.Path(sd) / "state").exists()
    assert "model" not in DI.plan(sd, _cfg(records=False), [_c(1)])


def _data_block(prompt):
    m = re.search(r"<<<(\S+)-BEGIN>>>\n(.*)\n<<<\1-END>>>", prompt, re.S)
    assert m, "no nonce-delimited data block"
    return m.group(2)


def assert_narrow(prompt, question, labels):
    data = _data_block(prompt)
    assert data == "\n".join([question[:300]] + list(labels)), "request carries more than question and labels"
    assert "SECRETCHOICE" not in prompt and "SECRETREASON" not in prompt


def test_request_is_only_clipped_question_and_labels(tmp_path):
    sd, f = _sd(tmp_path), Fake(GOOD)
    c = _c(1, q="Q" * 500)
    DI.classify_model(sd, _cfg(), [c], f)
    assert_narrow(f.calls[0][0], c["question"], c["options"])
    assert len(_data_block(f.calls[0][0]).splitlines()[0]) == 300
    instr = f.calls[0][0].split("<<<DATA-")[0]
    assert "billing" in instr and "repo" in instr          # the allowed areas, outside the data block
    assert "billing" not in _data_block(f.calls[0][0])


def test_ceiling_skipped_rows_are_not_stored_and_a_later_run_sends_them(tmp_path):
    sd = _sd(tmp_path)
    cands = [_c(i, q="merge conflict") for i in range(5)]
    cfg = _cfg(max_budget_usd_per_run=0.5, max_budget_usd_per_call=0.4)
    f = Fake(GOOD, cost=0.4)
    out = DI.apply(sd, cfg, cands, call=f)
    assert len(f.calls) == 2 and out["records"] == 2 and len(_recs(sd, cfg)) == 2
    assert all(r["qkind"] != "unknown" for r in _recs(sd, cfg))
    f2 = Fake(GOOD, cost=0.01)
    out2 = DI.apply(sd, cfg, cands, call=f2)
    assert len(f2.calls) == 3 and out2["records"] == 3 and len(_recs(sd, cfg)) == 5


def test_control_a_wider_request_fails_the_width_test(tmp_path):
    import pytest
    c = _c(1)
    wide = DI.build_request(c, "n0nce") + "\nextra"
    wider = wide.replace("<<<DATA-n0nce-END>>>", "SECRETCHOICE\n<<<DATA-n0nce-END>>>")
    with pytest.raises(AssertionError):
        assert_narrow(wider, c["question"], c["options"])


def test_no_free_text_in_stored_record_or_cache(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    DI.apply(sd, cfg, [_c(1)], call=Fake(GOOD))
    blob = "".join(p.read_text() for p in (pathlib.Path(sd) / "state").rglob("*") if p.is_file())
    assert "SECRETCHOICE" not in blob and "SECRETREASON" not in blob
    assert "SECRETCHOICE" not in (pathlib.Path(sd) / CACHE).read_text()
    assert "should we merge" not in (pathlib.Path(sd) / CACHE).read_text()


def test_limits_defaults_and_validation():
    d = CFG.import_limits({})
    assert d["classifier"] == "table" and d["model"] == "haiku"
    assert d["max_budget_usd_per_call"] > 0 and d["max_budget_usd_per_run"] >= d["max_budget_usd_per_call"]
    o = CFG.import_limits(_cfg(model="sonnet", max_budget_usd_per_call=-1, max_budget_usd_per_run="x"))
    assert o["model"] == "sonnet" and o["classifier"] == "model"
    assert o["max_budget_usd_per_call"] == d["max_budget_usd_per_call"]
    assert o["max_budget_usd_per_run"] == d["max_budget_usd_per_run"]
