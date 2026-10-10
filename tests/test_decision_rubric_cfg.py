"""Slice 2 of the decision rubric: the total config reader and its doctor row."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PARTS = ("records", "hard_stops", "autonomy", "drift", "rulebook")


def _mod(rel):
    p = ROOT / rel
    spec = importlib.util.spec_from_file_location(p.stem, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cfg():
    return _mod("skills/sigma-loop/scripts/decision_rubric_cfg.py")


def test_absent_block_closes_every_part():
    m = _cfg()
    for config in ({}, None, {"decision_rubric": None}, [], "x"):
        r = m.load(config)
        assert not any(r.part_enabled(p) for p in PARTS)
        assert r.closed_reason() is None


def test_wrong_type_closes_whole_block():
    m = _cfg()
    for bad in ({"decision_rubric": "on"},
                {"decision_rubric": {"records": True}},
                {"decision_rubric": {"records": {"enabled": 1}, "drift": {"enabled": True}}}):
        r = m.load(bad)
        assert not any(r.part_enabled(p) for p in PARTS)
        assert "decision_rubric" in r.closed_reason()
    assert "records.enabled" in m.load({"decision_rubric": {"records": {"enabled": "yes"}}}).closed_reason()


def test_only_true_enables_a_part():
    m = _cfg()
    assert m.load({"decision_rubric": {"records": {"enabled": True}}}).part_enabled("records")
    assert not m.load({"decision_rubric": {"records": {"enabled": True}}}).part_enabled("drift")
    r = m.load({"decision_rubric": {"records": {"enabled": "true"}}})
    assert not r.part_enabled("records")
    assert not m.load({"decision_rubric": {"records": {"enabled": True}}}).part_enabled("nonsense")


def test_visibility_defaults_private():
    m = _cfg()
    assert m.visibility({}) == "private"
    assert m.visibility({"decision_rubric": {"records": {"repo_visibility": "Public "}}}) == "private"
    assert m.visibility({"decision_rubric": {"records": {"repo_visibility": 7}}}) == "private"
    assert m.visibility({"decision_rubric": {"records": {"repo_visibility": "public"}}}) == "public"


def test_public_repo_warning_logic():
    m = _cfg()
    assert m.public_repo_warning({}, True)
    assert m.public_repo_warning({}, False) is None
    assert m.public_repo_warning({}, None) is None
    pub = {"decision_rubric": {"records": {"repo_visibility": "public"}}}
    assert m.public_repo_warning(pub, True) is None


def test_public_repo_private_setting_warns():
    d = _mod("skills/sigma-doctor/scripts/doctor.py")
    public_profile = {"ledger": {"enabled": False}, "work": {"auto_merge": "off"}}
    assert "records.repo_visibility" in d._decision_rubric_state(public_profile)
    assert "records.repo_visibility" not in d._decision_rubric_state({})
    assert d._decision_rubric_state({}).startswith("off")
    on = {"decision_rubric": {"records": {"enabled": True}}}
    assert d._decision_rubric_state(on).startswith("on")
    closed = {"decision_rubric": "x"}
    assert "closed" in d._decision_rubric_state(closed)
