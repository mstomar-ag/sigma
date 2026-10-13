"""Goal 1307: imported decisions agree on a normalised shape, not on free-text labels."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_t1307", SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DI, DS, AU = (_mod(n) for n in ("decision_import", "decision_store", "autonomy"))


def _cfg(records=True, autonomy=True):
    return {"rulebook": {
        "records": {"enabled": records, "repo_visibility": "private"},
        "autonomy": {"enabled": autonomy, "levels": {"l1_min_answers": 2, "l2_min_answers": 4,
                                                      "l2_agreement": 0.75}},
        "rules": {"enabled": False}}}


def _c(i, choice=None, opts=None, q="should we merge the docs change"):
    rec = "Merge variant %d (Recommended)" % i
    return {"source": "transcript", "when": "2026-10-01T10:%02d:00Z" % i, "question": q,
            "options": opts if opts is not None else [rec, "Hold variant %d" % i],
            "choice": choice if choice is not None else rec, "reason": "because", "issue": 7,
            "id": "c%03d" % i, "superseded": False}


def _sd(tmp_path):
    p = tmp_path / ".sdlc"
    p.mkdir(exist_ok=True)
    return str(p)


def _level(plan):
    return plan["pairs"][0]["level"]


def test_same_shape_different_labels_agree_and_reach_level_two(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    cands = [_c(i) for i in range(6)]
    assert _level(DI.plan(sd, cfg, cands)) == 2
    assert DI.apply(sd, cfg, cands)["watch"] == 5
    ws = AU._facts(sd, "watch-")
    assert ws and all(w["would"] == w["actual"] for w in ws)


def test_control_exact_labels_never_agree(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    cands = [dict(_c(i), options=[]) for i in range(6)]
    cands = [dict(c, choice="label %d" % i) for i, c in enumerate(cands)]
    assert _level(DI.plan(sd, cfg, cands)) == 1
    DI.apply(sd, cfg, cands)
    assert all(w["would"] != w["actual"] for w in AU._facts(sd, "watch-"))


def test_different_shapes_do_not_agree(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    cands = [_c(i, choice=("Hold variant %d" % i) if i % 2 else None) for i in range(6)]
    assert _level(DI.plan(sd, cfg, cands)) == 1


def test_no_options_falls_back_to_exact_choice():
    rows = [{"qkind": "k", "area": "repo", "when": "2026-10-01T10:0%d:00Z" % i, "fp": "f%d" % i, "choice": "merge"}
            for i in range(3)]
    assert DI.replay(rows) == {"f1": "merge", "f2": "merge"}


def test_shape_of_buckets():
    assert DI.shape_of(_c(1)) == "-|rec|first"
    assert DI.shape_of(_c(1, choice="Hold variant 1")) == "-|norec|later"
    assert DI.shape_of(_c(1, opts=[], choice="Resume the run")) == "resume|?|?"
    assert DI.shape_of(_c(1, opts=[], choice="merge")) is None


def test_closed_gates_write_nothing_and_no_person_fields(tmp_path):
    sd = _sd(tmp_path)
    cands = [_c(i) for i in range(4)]
    assert DI.apply(sd, _cfg(records=False), cands) == {"gate": False, "records": 0, "watch": 0, "note": ""}
    assert not any(pathlib.Path(sd).rglob("*.json"))
    cfg = _cfg(autonomy=False)
    out = DI.apply(sd, cfg, cands)
    assert out["records"] == 4 and out["watch"] == 0 and not AU._facts(sd, "watch-")
    recs = DS.list_by(sd, cfg)
    assert all(r["shape"] == "-|rec|first" for r in recs)
    assert all(not ({"login", "user", "author", "person"} & set(r)) for r in recs)


def test_second_apply_writes_nothing(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    cands = [_c(i) for i in range(5)]
    DI.apply(sd, cfg, cands)
    again = DI.apply(sd, cfg, cands)
    assert again["records"] == 0 and again["watch"] == 0
