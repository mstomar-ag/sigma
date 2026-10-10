"""Slice 12 of the decision rubric: autonomy levels 0 to 2 and the watch recorder."""
import ast
import datetime
import importlib.util
import json
import os
import pathlib

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


A = _mod("autonomy")
DS = _mod("decision_store")


def _cfg(enabled=True, levels=None, **extra):
    au = {"enabled": enabled, "levels": {"l1_min_answers": 2, "l2_min_answers": 4, "l2_agreement": 0.75,
                                          "l3_min_answers": 8, "l3_agreement": 0.9,
                                          "l4_min_answers": 16, "l4_agreement": 0.99}}
    if levels:
        au["levels"].update(levels)
    au.update(extra)
    return {"decision_rubric": {"records": {"enabled": True}, "autonomy": au}}


def _answer(sd, cfg, n, qkind="needs_decision", area="docs", how="human", **kw):
    for i in range(n):
        DS.append(sd, dict({"how": how, "qkind": qkind, "area": area, "choice": "a"}, **kw), cfg)


def _watch(sd, cfg, agree, disagree=0, qkind="needs_decision", area="docs", machine="m1"):
    for i in range(agree):
        A.record_watch(sd, qkind, area, "a", "a", cfg, now=NOW, machine=machine)
    for i in range(disagree):
        A.record_watch(sd, qkind, area, "a", "b", cfg, now=NOW, machine=machine)


def test_level_rises_with_counts_and_agreement(tmp_path):
    cfg = _cfg()
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 0
    _answer(tmp_path, cfg, 2)
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 1
    _answer(tmp_path, cfg, 2)
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 1  # no watch data yet
    _watch(tmp_path, cfg, 3, 1)
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 2  # 4 comparisons, 0.75
    _watch(tmp_path, cfg, 0, 1)
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 1  # agreement 0.6


def test_agents_answers_do_not_count(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 5, how="autonomous")
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 0


def test_missing_record_never_raises(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 2)
    store = tmp_path / "decisions"
    (store / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "autonomy").mkdir()
    (tmp_path / "autonomy" / "watch-x.json").write_text("[1]", encoding="utf-8")
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 1
    assert A.level(tmp_path / "nowhere", "needs_decision", "docs", cfg) == 0
    assert A.level(tmp_path, None, None, None) == 0
    assert A.veto(tmp_path, "no-such-id", cfg) is None


def test_veto_drops_one_level(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 4)
    _watch(tmp_path, cfg, 4)
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 2
    rid = DS.list_by(tmp_path, cfg)[0]["id"]
    assert A.veto(tmp_path, rid, cfg, now=NOW, machine="m1")
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 1


def test_veto_pause(tmp_path):
    cfg = _cfg(veto_pause_consecutive=2)
    _answer(tmp_path, cfg, 4)
    _watch(tmp_path, cfg, 4)
    rid = DS.list_by(tmp_path, cfg)[0]["id"]
    A.veto(tmp_path, rid, cfg, now=NOW + datetime.timedelta(seconds=5), machine="m1")
    A.veto(tmp_path, rid, cfg, now=NOW + datetime.timedelta(seconds=6), machine="m1")
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 0
    assert A.readout(tmp_path, cfg)["kinds"][0]["paused"] is True


def test_hard_stop_kind_caps_at_two(tmp_path):
    cfg = _cfg(levels={"l3_min_answers": 2, "l3_agreement": 0.5, "l4_min_answers": 2, "l4_agreement": 0.5})
    for kind in ("irreversible", "needs_decision"):
        _answer(tmp_path, cfg, 6, qkind=kind)
        _watch(tmp_path, cfg, 6, qkind=kind)
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 4
    assert A.level(tmp_path, "irreversible", "docs", cfg) == 2


def test_record_with_a_hardstop_class_caps_at_two(tmp_path):
    cfg = _cfg(levels={"l3_min_answers": 2, "l3_agreement": 0.5})
    _answer(tmp_path, cfg, 6, hardstop_class="spend")
    _watch(tmp_path, cfg, 6)
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 2


def test_two_machines_take_the_lower(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 4)
    _watch(tmp_path, cfg, 4, machine="m1")
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 2
    _watch(tmp_path, cfg, 1, 3, machine="m2")
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 1


def test_state_is_per_area_not_per_login(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 1, login_a="x")
    _answer(tmp_path, cfg, 1, login_b="y")
    assert A.level(tmp_path, "needs_decision", "docs", cfg) == 1
    assert A.level(tmp_path, "needs_decision", "other", cfg) == 0
    assert A.level(tmp_path, "scope_hold", "docs", cfg) == 0


def test_watch_records_are_pruned(tmp_path):
    cfg = _cfg(watch={"retention_days": 30})
    old = NOW - datetime.timedelta(days=31)
    A.record_watch(tmp_path, "needs_decision", "docs", "a", "a", cfg, now=old, machine="m1")
    A.record_watch(tmp_path, "needs_decision", "docs", "a", "a", cfg, now=NOW - datetime.timedelta(days=29), machine="m1")
    A.veto(tmp_path, _first_id(tmp_path, cfg), cfg, now=old, machine="m1")
    removed = A.prune_watch(tmp_path, NOW, cfg)
    assert removed == 1
    names = sorted(os.listdir(tmp_path / "autonomy"))
    assert sum(n.startswith("watch-") for n in names) == 1
    assert sum(n.startswith("veto-") for n in names) == 1  # history kept


def _first_id(sd, cfg):
    _answer(sd, cfg, 1)
    return DS.list_by(sd, cfg)[0]["id"]


def test_owner_falls_back_to_approvers():
    own = {"decision_rubric": {"autonomy": {"owners": ["Alice"]}}, "spend_approval": {"approvers": ["bob"]}}
    assert A.is_owner("alice", own) is True
    assert A.is_owner("bob", own) is False
    unset = {"spend_approval": {"approvers": [" Bob "]}}
    assert A.is_owner("BOB", unset) is True
    assert A.is_owner("carol", unset) is False
    assert A.is_owner("bob", {}) is False
    assert A.is_owner("bob", {"decision_rubric": {"autonomy": {"owners": []}}, "spend_approval": {"approvers": []}}) is False
    assert A.is_owner("bob", {"decision_rubric": {"autonomy": {"owners": []}}, "spend_approval": {"approvers": ["bob"]}}) is False
    assert A.is_owner(None, unset) is False
    assert A.is_owner("bob", {"decision_rubric": {"autonomy": {"owners": "bob"}}}) is False


def test_closed_gate_writes_nothing_and_holds_level_zero(tmp_path):
    for cfg in (_cfg(enabled=False), _cfg(enabled="true"), {}, None):
        assert A.record_watch(tmp_path, "needs_decision", "docs", "a", "a", cfg, now=NOW) is None
        assert A.veto(tmp_path, "x", cfg) is None
        assert A.prune_watch(tmp_path, NOW, cfg) == 0
        assert A.level(tmp_path, "needs_decision", "docs", cfg) == 0
        assert A.readout(tmp_path, cfg) == {"enabled": False}
    assert os.listdir(tmp_path) == []


def test_readout_shape(tmp_path):
    cfg = _cfg()
    _answer(tmp_path, cfg, 2)
    out = A.readout(tmp_path, cfg)
    assert out["enabled"] is True
    row = out["kinds"][0]
    assert (row["qkind"], row["area"], row["level"], row["answers"]) == ("needs_decision", "docs", 1, 2)


def _scan_outside_owner(src):
    tree = ast.parse(src)
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "is_owner":
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


def test_only_is_owner_reads_a_login():
    src = (SCRIPTS / "autonomy.py").read_text(encoding="utf-8")
    assert _scan_outside_owner(src) == []
    assert _scan_outside_owner(src + "\ndef leak(rec):\n    return rec['%s']\n" % LOGIN) == [LOGIN]


def test_no_network_or_process_modules():
    src = (SCRIPTS / "autonomy.py").read_text(encoding="utf-8")
    for bad in ("import subprocess", "import socket\n", "urllib", "import requests"):
        assert bad not in src
