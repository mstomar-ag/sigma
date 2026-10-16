"""Goal 1555: `rulebook.rules.auto_approve` -- the watcher drafts and approves a rule for every learnable
pair with a majority option, at any level, and the applier applies it after the window.

Closed (the default) is byte-identical to before; hard-stop kinds are never touched; a veto holds the rule.
"""
import datetime
import hashlib
import json
import os
import subprocess

import test_applier as T
from test_applier import AP, AU, DS, RB, MIN, Src, _sd
from test_applier_lifecycle import LSrc, _ListSrc

import pytest

RD = T._mod("rule_draft")
DR = T._mod("decision_record")
DOC = T._mod("doctor", T.ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
T0 = T.T0
KIND, AREA, OPT = "merge_conflict", "docs", "rebase it"


def _cfg(auto=True, owners=("boss",), **kw):
    cfg = T._cfg(**kw)
    cfg["rulebook"]["rules"]["auto_approve"] = auto
    if owners is not None:
        cfg["rulebook"]["autonomy"]["owners"] = list(owners)
    return cfg


def _answers(sd, cfg, qkind=KIND, area=AREA, choices=(OPT, OPT, OPT)):
    for c in choices:
        DS.append(sd, {"how": "human", "qkind": qkind, "area": area, "choice": c}, cfg)


def _file(sd):
    return os.path.join(os.path.dirname(os.path.abspath(sd)), ".sdlc", "rulebook.json")


def _rules_raw(sd):
    p = _file(sd)
    return json.load(open(p))["rules"] if os.path.exists(p) else []


def _records(sd, cfg, gesture):
    return [r for r in DS.list_by(sd, cfg) if r.get("gesture") == gesture]


def test_closed_gate_drafts_nothing_and_writes_nothing(tmp_path):
    cfg = _cfg(auto=False)
    sd = _sd(tmp_path)
    _answers(sd, cfg)
    before = len(DS.list_by(sd, cfg))
    assert RD.auto_approve(sd, cfg, T0) == []
    assert not os.path.exists(_file(sd)) and len(DS.list_by(sd, cfg)) == before
    for off in ({"auto_approve": "true"}, {"auto_approve": 1}, {}):
        c = _cfg(auto=False)
        c["rulebook"]["rules"].update(off)
        assert RB.auto_approve_on(c) is False


def test_closed_gate_level_one_pair_is_not_armed_and_does_nothing(tmp_path):
    cfg = _cfg(auto=False)
    sd = _sd(tmp_path)
    _answers(sd, cfg)
    assert AU.level(sd, KIND, AREA, cfg) == 1
    rid = T._rule(sd, cfg)
    src = Src()
    AP.open_for_park(sd, cfg, 7, KIND, source=src, now=T0, info={"labels": ["area:docs"], "park": "c1"})
    assert AP._asks(sd)[0]["armed_at"] is None
    assert AP.tick(sd, cfg, T0 + 300 * MIN, source=src) == [] and src.swaps == []
    rule = [r for r in _rules_raw(sd) if r["id"] == rid][0]
    assert "auto" not in rule and "approved_by" not in rule


def test_existing_seal_bytes_are_unchanged():
    rule = {"id": "r1", "qkind": KIND, "area": AREA, "option": OPT, "reason": "x", "created": "c", "confirmed": "d"}
    legacy = hashlib.sha256(json.dumps({k: rule.get(k) for k in
                                        ("id", "qkind", "area", "option", "reason", "created", "confirmed")},
                                       sort_keys=True).encode()).hexdigest()
    assert RB._seal(rule) == legacy
    assert RB._seal(dict(rule, auto=True, approved_by="boss")) != legacy


def test_open_gate_drafts_and_approves_a_level_one_pair(tmp_path):
    cfg = _cfg()
    sd = _sd(tmp_path)
    _answers(sd, cfg)
    assert AU.level(sd, KIND, AREA, cfg) == 1
    out = RD.auto_approve(sd, cfg, T0)
    assert len(out) == 1
    (rule,) = RB.load(sd, cfg)
    assert (rule["status"], rule["approved_by"], rule["auto"], rule["option"]) == ("approved", "boss", True, OPT)
    assert RB.match(KIND, AREA, None, [rule], cfg).option == OPT and RB.is_auto(rule)
    (rec,) = _records(sd, cfg, "approve")
    assert rec["how"] == "autonomous" and rec["rule_id"] == rule["id"] and rec["qkind"] == KIND
    assert RD.auto_approve(sd, cfg, T0) == []           # idempotent: the pair now has a rule
    assert len(_rules_raw(sd)) == 1 and len(_records(sd, cfg, "approve")) == 1


def test_approve_records_never_feed_answers_or_levels(tmp_path):
    cfg = _cfg()
    sd = _sd(tmp_path)
    _answers(sd, cfg)
    RD.auto_approve(sd, cfg, T0)
    assert DR.is_park_answer(_records(sd, cfg, "approve")[0]) is False
    assert AU.level(sd, KIND, AREA, cfg) == 1


def test_hard_stop_unknown_no_owner_and_tie_are_not_drafted(tmp_path):
    cfg = _cfg()
    sd = _sd(tmp_path)
    _answers(sd, cfg, qkind="irreversible", choices=("stop", "stop", "stop"))
    _answers(sd, cfg, qkind="unknown", choices=("x", "x", "x"))
    _answers(sd, cfg, qkind="dependency", choices=("wait for it", "wait for it", "go on", "go on"))
    assert RD.auto_approve(sd, cfg, T0) == []
    assert not os.path.exists(_file(sd))
    # a configured hard-stop kind is excluded too
    c2 = _cfg()
    c2["rulebook"]["hard_stops"] = {"hardstop_kinds": [KIND]}
    sd2 = _sd(tmp_path / "b")
    _answers(sd2, c2)
    assert RD.auto_approve(sd2, c2, T0) == []
    # no owner configured: nothing is approved on anyone's behalf
    c3 = _cfg(owners=[])
    sd3 = _sd(tmp_path / "c")
    _answers(sd3, c3)
    assert RD.auto_approve(sd3, c3, T0) == [] and not os.path.exists(_file(sd3))


def test_a_hand_edit_of_auto_breaks_the_seal(tmp_path):
    cfg = _cfg()
    sd = _sd(tmp_path)
    _answers(sd, cfg)
    RD.auto_approve(sd, cfg, T0)
    raw = json.load(open(_file(sd)))
    raw["rules"][0].pop("auto")
    json.dump(raw, open(_file(sd), "w"))
    assert RB.load(sd, cfg)[0]["status"] == "proposed"
    # adding auto to a manually approved rule breaks it as well
    sd2 = _sd(tmp_path / "m")
    cfg2 = _cfg(auto=False)
    _answers(sd2, cfg2)
    T._rule(sd2, cfg2)
    raw = json.load(open(_file(sd2)))
    raw["rules"][0].update(auto=True, approved_by="boss")
    json.dump(raw, open(_file(sd2), "w"))
    assert RB.load(sd2, cfg2)[0]["status"] == "proposed"
    assert RB.validate(dict(raw["rules"][0], auto=False))[0] is False


def test_applier_arms_at_once_and_applies_after_the_window(tmp_path):
    cfg = _cfg()
    sd = _sd(tmp_path)
    _answers(sd, cfg)
    RD.auto_approve(sd, cfg, T0)
    src = Src()
    info = {"labels": ["area:docs"], "park": "c1"}
    AP.open_for_park(sd, cfg, 7, KIND, source=src, now=T0 + MIN, info=info)
    assert AP._asks(sd)[0]["armed_at"] is not None and "will apply" in src.notes[0][1]
    assert AP.tick(sd, cfg, T0 + 30 * MIN, source=src) == []          # window still open
    acts = AP.tick(sd, cfg, T0 + 70 * MIN, source=src)
    assert [a["result"] for a in acts] == ["done"] and len(src.swaps) == 1
    assert AP.tick(sd, cfg, T0 + 80 * MIN, source=src) == []


def test_window_and_daily_cap_still_apply(tmp_path):
    cfg = _cfg(applier={"max_auto_applied_per_day": 0})
    sd = _sd(tmp_path)
    _answers(sd, cfg)
    RD.auto_approve(sd, cfg, T0)
    src = Src()
    AP.open_for_park(sd, cfg, 7, KIND, source=src, now=T0, info={"labels": ["area:docs"], "park": "c1"})
    assert AP.tick(sd, cfg, T0 + 500 * MIN, source=src) == [] and src.swaps == []


def test_a_veto_reverts_lowers_the_pair_and_holds_the_rule_until_reconfirmed(tmp_path):
    cfg = _cfg()
    sd = _sd(tmp_path)
    _answers(sd, cfg)
    RD.auto_approve(sd, cfg, T0)
    rid = RB.load(sd, cfg)[0]["id"]
    src = Src()
    aid = AP.open_for_park(sd, cfg, 7, KIND, source=src, now=T0, info={"labels": ["area:docs"], "park": "c1"})
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 61 * MIN, source=src)] == ["done"]
    assert AP.answer(sd, aid, "no", cfg, T0 + 70 * MIN) == "veto-pending"
    acts = AP.tick(sd, cfg, T0 + 71 * MIN, source=src)
    assert [(a["kind"], a["result"]) for a in acts] == [("veto", "done")]
    assert len(src.swaps) == 2                                          # parked again
    assert AU.level(sd, KIND, AREA, cfg) == 0                            # the pair is lowered
    rule = RB.load(sd, cfg)[0]
    assert AU.vetoed_since(sd, KIND, AREA, rule["confirmed"], cfg) is True
    # a later park of the same pair is held, not applied
    AP.open_for_park(sd, cfg, 8, KIND, source=src, now=T0 + 80 * MIN, info={"labels": ["area:docs"], "park": "c2"})
    for t in (90, 200, 400):
        assert [a for a in AP.tick(sd, cfg, T0 + t * MIN, source=src) if a.get("result") == "done"] == []
    assert len(src.swaps) == 2
    # an owner reconfirms by hand: the rule arms again and applies after a fresh window
    RB.approve(sd, rid, "boss", cfg, now=T0 + 500 * MIN)
    assert RB.is_auto(RB.load(sd, cfg)[0])
    assert [a["kind"] for a in AP.tick(sd, cfg, T0 + 510 * MIN, source=src)] == ["arm"]
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 600 * MIN, source=src)] == ["done"]


def test_doctor_row_only_when_on(tmp_path):
    sd = _sd(tmp_path)
    os.makedirs(sd)
    on = DOC._rulebook_state(_cfg(), sd)
    assert "rules: auto-approve ON (rulebook.rules.auto_approve)" in on
    assert "rules: auto-approve ON" not in DOC._rulebook_state(_cfg(auto=False), sd)
    assert "no owner" in DOC._rulebook_state(_cfg(owners=[]), sd)


def test_scenario_through_the_list_verb_and_the_applier_verb(tmp_path, monkeypatch, capsys):
    """`rule_draft.py list` (auto approval), then the applier verb on a temp repo with a fake source."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    cfg = _cfg()
    sd = _sd(tmp_path)
    _answers(sd, cfg)
    json.dump(cfg, open(os.path.join(sd, "config.json"), "w"))
    assert RD.main(["list", sd]) == 0
    (rule,) = RB.load(sd, cfg)
    assert rule["auto"] is True and rule["approved_by"] == "boss"
    src = _ListSrc({7: [("c1", KIND)]})
    real_load = AP._load

    class _Sources:
        GitHubSource = staticmethod(lambda config, run=None: src)

    monkeypatch.setattr(AP, "_load", lambda name: _Sources if name == "sources" else real_load(name))
    assert AP.main(["applier.py", sd]) == 0
    assert [a["state"] for a in AP._asks(sd)] == ["pending"] and AP._asks(sd)[0]["armed_at"]
    now = datetime.datetime.now(datetime.timezone.utc) + 2 * 60 * MIN
    assert [a["result"] for a in AP.tick(sd, cfg, now, source=src)] == ["done"]
    assert AP.main(["applier.py", sd]) == 0
    assert [a["state"] for a in AP._asks(sd)] == ["applied"]
    assert sum(1 for _, n in src.notes if "Applied automatically" in n) == 1
