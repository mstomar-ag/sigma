"""Slice 2 of the rulebook: the total config reader and its doctor row."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PARTS = ("records", "hard_stops", "autonomy", "drift", "rules")


def _mod(rel):
    p = ROOT / rel
    spec = importlib.util.spec_from_file_location(p.stem, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cfg():
    return _mod("skills/sigma-loop/scripts/rulebook_cfg.py")


def test_absent_block_closes_every_part():
    m = _cfg()
    for config in ({}, None, {"rulebook": None}, [], "x"):
        r = m.load(config)
        assert not any(r.part_enabled(p) for p in PARTS)
        assert r.closed_reason() is None


def test_wrong_type_closes_whole_block():
    m = _cfg()
    for bad in ({"rulebook": "on"},
                {"rulebook": {"records": True}},
                {"rulebook": {"records": {"enabled": 1}, "drift": {"enabled": True}}}):
        r = m.load(bad)
        assert not any(r.part_enabled(p) for p in PARTS)
        assert "rulebook" in r.closed_reason()
    assert "records.enabled" in m.load({"rulebook": {"records": {"enabled": "yes"}}}).closed_reason()


def test_only_true_enables_a_part():
    m = _cfg()
    assert m.load({"rulebook": {"records": {"enabled": True}}}).part_enabled("records")
    assert not m.load({"rulebook": {"records": {"enabled": True}}}).part_enabled("drift")
    r = m.load({"rulebook": {"records": {"enabled": "true"}}})
    assert not r.part_enabled("records")
    assert not m.load({"rulebook": {"records": {"enabled": True}}}).part_enabled("nonsense")


def test_visibility_defaults_private():
    m = _cfg()
    assert m.visibility({}) == "private"
    assert m.visibility({"rulebook": {"records": {"repo_visibility": "Public "}}}) == "private"
    assert m.visibility({"rulebook": {"records": {"repo_visibility": 7}}}) == "private"
    assert m.visibility({"rulebook": {"records": {"repo_visibility": "public"}}}) == "public"


def test_public_repo_warning_logic():
    m = _cfg()
    assert m.public_repo_warning({}, True)
    assert m.public_repo_warning({}, False) is None
    assert m.public_repo_warning({}, None) is None
    pub = {"rulebook": {"records": {"repo_visibility": "public"}}}
    assert m.public_repo_warning(pub, True) is None


def test_public_repo_private_setting_warns():
    d = _mod("skills/sigma-doctor/scripts/doctor.py")
    public_profile = {"ledger": {"enabled": False}, "work": {"auto_merge": "off"}}
    assert "records.repo_visibility" in d._rulebook_state(public_profile)
    assert "records.repo_visibility" not in d._rulebook_state({})
    assert d._rulebook_state({}).startswith("off")
    on = {"rulebook": {"records": {"enabled": True}}}
    assert d._rulebook_state(on).startswith("on")
    closed = {"rulebook": "x"}
    assert "closed" in d._rulebook_state(closed)


def test_retired_key_is_not_read_and_doctor_says_so():
    m = _cfg()
    old = {"decision_rubric": {"records": {"enabled": True}}}
    assert not m.load(old).part_enabled("records")
    d = _mod("skills/sigma-doctor/scripts/doctor.py")
    assert "`decision_rubric` was renamed `rulebook`; it is not read" in d._rulebook_state(old)
    assert "renamed" not in d._rulebook_state({"rulebook": {"records": {"enabled": True}}})


def test_new_key_opens_each_part_and_rules_is_the_sub_block():
    m = _cfg()
    for p in PARTS:
        assert m.load({"rulebook": {p: {"enabled": True}}}).part_enabled(p)
    assert not m.load({"rulebook": {"rulebook": {"enabled": True}}}).part_enabled("rules")


def test_doctor_names_a_paused_drift_judge(tmp_path):
    import json
    import time
    d = _mod("skills/sigma-doctor/scripts/doctor.py")
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    drift = {"enabled": True, "model": "m", "spend_ceiling_usd_per_day": 1.0}
    cfg = {"rulebook": {"drift": drift}}
    base = d._rulebook_state(cfg, sdlc)
    assert "drift judge paused" not in base
    now = time.time()
    (sdlc / "state" / "drift-judge-spend.json").write_text(
        json.dumps({"records": [{"ts": now - 60, "usd": 1.5}]}))
    hhmm = time.strftime("%H:%M", time.localtime(now - 60))
    assert d._rulebook_state(cfg, sdlc).endswith("; drift judge paused: daily ceiling reached at " + hhmm)
    drift["spend_ceiling_usd_per_day"] = 9.0
    assert "drift judge paused" not in d._rulebook_state(cfg, sdlc)
    drift["spend_ceiling_usd_per_day"] = 1.0
    drift["enabled"] = False
    assert "drift judge paused" not in d._rulebook_state(cfg, sdlc)
