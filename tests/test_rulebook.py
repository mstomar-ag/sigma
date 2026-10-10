"""Slice 13 of the decision rubric: the rulebook file and the propose-approve flow."""
import ast
import datetime
import importlib.util
import json
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
NOW = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
LOGIN = "log" + "in"


def _mod(name):
    p = SCRIPTS / (name + ".py")
    spec = importlib.util.spec_from_file_location(p.stem, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


R = _mod("rulebook")
QK = _mod("qkind")


def _cfg(enabled=True, **extra):
    rb = {"enabled": enabled}
    rb.update(extra)
    return {"decision_rubric": {"rulebook": rb, "autonomy": {"owners": ["boss"]}}}


def _draft(**kw):
    d = {"qkind": "needs_decision", "area": "docs", "option": "merge", "reason": "docs are low risk"}
    d.update(kw)
    return d


def _sdlc(tmp_path):
    p = tmp_path / ".sdlc"
    p.mkdir()
    return p


def _file(tmp_path):
    return tmp_path / ".sdlc" / "rulebook.json"


def _proposed(tmp_path, cfg=None, **kw):
    cfg = cfg or _cfg()
    sd = _sdlc(tmp_path) if not (tmp_path / ".sdlc").exists() else tmp_path / ".sdlc"
    R.propose(sd, _draft(**kw), cfg, now=NOW)
    return sd, cfg, R.load(sd, cfg)[-1]["id"]


def test_unapproved_rule_never_acts(tmp_path):
    sd, cfg, rid = _proposed(tmp_path)
    assert R.match("needs_decision", "docs", None, R.load(sd, cfg)) is None
    R.approve(sd, rid, "boss", cfg, now=NOW)
    m = R.match("needs_decision", "docs", None, R.load(sd, cfg))
    assert (m.rule_id, m.option, m.mode) == (rid, "merge", "recommend")


def test_hand_edited_approved_rule_without_seal_never_acts(tmp_path):
    sd, cfg, rid = _proposed(tmp_path)
    data = json.loads(_file(tmp_path).read_text())
    data["rules"][0]["status"] = "approved"
    _file(tmp_path).write_text(json.dumps(data))
    assert R.match("needs_decision", "docs", None, R.load(sd, cfg)) is None
    data["rules"][0]["option"] = "reject"
    R.approve(sd, rid, "boss", cfg, now=NOW)
    data = json.loads(_file(tmp_path).read_text())
    data["rules"][0]["option"] = "reject"  # edit after approval breaks the seal
    _file(tmp_path).write_text(json.dumps(data))
    assert R.match("needs_decision", "docs", None, R.load(sd, cfg)) is None


def test_two_matches_ask(tmp_path):
    sd, cfg, a = _proposed(tmp_path)
    R.propose(sd, _draft(option="reject"), cfg, now=NOW)
    ids = [r["id"] for r in R.load(sd, cfg)]
    for rid in ids:
        R.approve(sd, rid, "boss", cfg, now=NOW)
    assert len(ids) == 2
    assert R.match("needs_decision", "docs", None, R.load(sd, cfg)) is None


def test_rule_has_no_person_scope():
    ok, _ = R.validate(dict(_draft(), id="r1", status="proposed"))
    assert ok
    for bad in ({LOGIN: "x"}, {"scope": {LOGIN: "x"}}, {"owner": "x"}):
        ok, reason = R.validate(dict(_draft(), id="r1", status="proposed", **bad))
        assert not ok and reason


def test_proposal_is_a_reviewable_diff(tmp_path):
    sd = _sdlc(tmp_path)
    cfg = _cfg()
    R.propose(sd, _draft(), cfg, now=NOW)
    before = _file(tmp_path).read_text().splitlines()
    path = R.propose(sd, _draft(area="api"), cfg, now=NOW)
    after = pathlib.Path(path).read_text().splitlines()
    assert path == str(_file(tmp_path))
    added = [l for l in after if l not in before]
    assert any('"area": "api"' in l for l in added)
    norm = [l.rstrip(",") for l in after]
    assert not [l for l in before if l.rstrip(",") not in norm]
    rid = R.load(sd, cfg)[-1]["id"]
    park = R.park_line(rid)
    assert park.count(QK.LINE_PREFIX) == 1 and QK.parse_line(park) == "owner_hold" and rid in park


def test_aged_rule_is_reasked(tmp_path):
    sd, cfg, rid = _proposed(tmp_path, cfg=_cfg(reask_days=30))
    R.approve(sd, rid, "boss", cfg, now=NOW)
    rules = R.load(sd, cfg)
    assert R.due_for_reask(rules, NOW + datetime.timedelta(days=29), cfg) == []
    assert R.due_for_reask(rules, NOW + datetime.timedelta(days=31), cfg) == [rid]
    assert R.settings(_cfg(reask_days="x"))["reask_days"] == 90


def test_non_owner_cannot_approve(tmp_path):
    sd, cfg, rid = _proposed(tmp_path)
    before = _file(tmp_path).read_text()
    for who in ("stranger", "", None):
        with pytest.raises(PermissionError):
            R.approve(sd, rid, who, cfg, now=NOW)
    assert _file(tmp_path).read_text() == before


def test_unknown_rule_cannot_be_approved(tmp_path):
    sd, cfg, _ = _proposed(tmp_path)
    with pytest.raises(KeyError):
        R.approve(sd, "nope", "boss", cfg, now=NOW)


def test_closed_gate_does_nothing(tmp_path):
    sd = _sdlc(tmp_path)
    for cfg in (_cfg(enabled=False), {}, _cfg(enabled="true"), {"decision_rubric": []}):
        assert R.propose(sd, _draft(), cfg, now=NOW) is None
        assert R.import_rules(sd, [_draft()], cfg, now=NOW) == []
        assert R.load(sd, cfg) == []
        assert R.due_for_reask([], NOW, cfg) == []
        with pytest.raises(PermissionError):
            R.approve(sd, "x", "boss", cfg, now=NOW)
    assert os.listdir(sd) == []


def test_import_goes_through_the_same_gate(tmp_path):
    sd = _sdlc(tmp_path)
    cfg = _cfg()
    ids = R.import_rules(sd, [_draft(), _draft(area="api"), {"login": "x"}], cfg, now=NOW)
    assert len(ids) == 2
    assert {r["status"] for r in R.load(sd, cfg)} == {"proposed"}


def test_hardstop_kind_and_other_areas_never_match(tmp_path):
    sd, cfg, rid = _proposed(tmp_path, qkind="irreversible")
    R.approve(sd, rid, "boss", cfg, now=NOW)
    assert R.match("irreversible", "docs", None, R.load(sd, cfg)) is None
    sd2 = _sdlc(tmp_path / "b") if (tmp_path / "b").mkdir() is None else None
    R.propose(sd2, _draft(), cfg, now=NOW)
    r2 = R.load(sd2, cfg)[0]["id"]
    R.approve(sd2, r2, "boss", cfg, now=NOW)
    assert R.match("needs_decision", "api", None, R.load(sd2, cfg)) is None
    assert R.match("needs_decision", "docs", ["other"], R.load(sd2, cfg)) is None
    assert R.match("needs_decision", "docs", ["merge", "x"], R.load(sd2, cfg)).option == "merge"


def test_prefill_recommends(tmp_path):
    sd, cfg, rid = _proposed(tmp_path)
    assert R.prefill(sd, "needs_decision", "docs", None, cfg) is None
    R.approve(sd, rid, "boss", cfg, now=NOW)
    assert R.prefill(sd, "needs_decision", "docs", None, cfg) == {"recommend": "merge", "rule": rid, "mode": "recommend"}


def test_path_escape_is_refused(tmp_path):
    sd = _sdlc(tmp_path)
    for p in ("../x.json", "/tmp/x.json", 5):
        out = R.propose(sd, _draft(), _cfg(path=p), now=NOW)
        assert out is None or out == str(_file(tmp_path))
    assert not (tmp_path.parent / "x.json").exists()


def _scan_outside_approve(src):
    tree = ast.parse(src)
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "approve":
            skip.update(id(n) for n in ast.walk(node))
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
    found = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        vals = []
        if isinstance(node, ast.Name):
            vals = [node.id]
        elif isinstance(node, ast.Attribute):
            vals = [node.attr]
        elif isinstance(node, ast.arg):
            vals = [node.arg]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            vals = [node.value]
        found += [v for v in vals if LOGIN in v.lower()]
    return found


def test_only_approve_reads_a_login():
    src = (SCRIPTS / "rulebook.py").read_text(encoding="utf-8")
    assert _scan_outside_approve(src) == []
    assert _scan_outside_approve(src + "\ndef leak(r):\n    return r['%s']\n" % LOGIN) == [LOGIN]


def test_match_honours_configured_hardstop_kind(tmp_path):
    sd, cfg, rid = _proposed(tmp_path)
    R.approve(sd, rid, "boss", cfg, now=NOW)
    rules = R.load(sd, cfg)
    assert R.match("needs_decision", "docs", None, rules, cfg) is not None
    cfg["decision_rubric"]["hard_stops"] = {"hardstop_kinds": ["needs_decision"]}
    assert R.match("needs_decision", "docs", None, rules, cfg) is None
    assert R.prefill(sd, "needs_decision", "docs", None, cfg) is None


def test_match_refuses_unsealed_or_invalid_rules_from_a_direct_caller(tmp_path):
    sd, cfg, rid = _proposed(tmp_path)
    forged = dict(R._read_raw(_file(tmp_path))[0], status="approved")  # no seal, no confirmed
    assert R.match("needs_decision", "docs", None, [forged]) is None
    R.approve(sd, rid, "boss", cfg, now=NOW)
    good = R._read_raw(_file(tmp_path))[0]
    assert R.match("needs_decision", "docs", None, [good]).rule_id == rid
    assert R.match("needs_decision", "docs", None, [dict(good, option="reject")], ) is None  # seal broken
    assert R.match("needs_decision", "docs", None, [dict(good, login="someone")]) is None  # schema-invalid
