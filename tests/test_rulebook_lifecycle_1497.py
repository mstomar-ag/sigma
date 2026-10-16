"""#1497: rules fall due and are reconfirmed, retire/reject verbs, any-area rules, category order, shape-only note."""
import datetime
import json
import pathlib
import subprocess

import test_applier as T

AP, AU, DS, RB = T.AP, T.AU, T.DS, T.RB
RD = T._mod("rule_draft")
CT = T._mod("categories")
DOC = T._mod("doctor", T.ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
T0, MIN = T.T0, T.MIN
DAY = datetime.timedelta(days=1)
OWNER = {"login": "boss", "confirmed": True}


def _repo(tmp_path, monkeypatch, login="boss"):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    monkeypatch.setattr(RD, "_identity", lambda config: {"login": login, "confirmed": True})
    (tmp_path / ".sdlc").mkdir(exist_ok=True)
    cfg = T._cfg()
    return T._sd(tmp_path), cfg


def _file(tmp_path):
    return tmp_path / ".sdlc" / "rulebook.json"


def _prime(sd, cfg, area="docs"):
    T._train(sd, cfg, area=area, level=3)
    return T._rule(sd, cfg, area=area)


# ---- F-04 -----------------------------------------------------------------

def test_due_lists_age_and_reconfirm_restamps(tmp_path, monkeypatch, capsys):
    sd, cfg = _repo(tmp_path, monkeypatch)
    rid = _prime(sd, cfg)
    assert RD.due(sd, cfg, now=T0 + 10 * DAY) == []
    assert RD.due(sd, cfg, now=T0 + 91 * DAY) == [(rid, 91)]
    seal = RB.load(sd, cfg)[0]["seal"]
    ok, line = RD.reconfirm(sd, rid, cfg)
    assert ok and "boss" in line
    rule = RB.load(sd, cfg)[0]
    assert rule["status"] == "approved" and rule["seal"] != seal      # confirmed time moved, so the seal did
    assert RD.due(sd, cfg, now=T0 + 91 * DAY) == []


def test_reconfirm_is_owner_only_and_leaves_file(tmp_path, monkeypatch):
    sd, cfg = _repo(tmp_path, monkeypatch, login="stranger")
    rid = _prime(sd, cfg)
    before = _file(tmp_path).read_bytes()
    ok, line = RD.reconfirm(sd, rid, cfg)
    assert not ok and "owner" in line and _file(tmp_path).read_bytes() == before


def test_reconfirm_refuses_proposed(tmp_path, monkeypatch):
    sd, cfg = _repo(tmp_path, monkeypatch)
    RB.propose(sd, {"qkind": "merge_conflict", "area": "docs", "option": "rebase it", "reason": "r"}, cfg, now=T0)
    ok, line = RD.reconfirm(sd, RB.load(sd, cfg)[0]["id"], cfg)
    assert not ok and "not approved" in line


def test_due_verb_prints_through_main(tmp_path, monkeypatch, capsys):
    sd, cfg = _repo(tmp_path, monkeypatch)
    (tmp_path / ".sdlc" / "config.json").write_text(json.dumps(cfg))
    rid = _prime(sd, cfg)
    assert RD.main(["due", sd]) == 0
    assert capsys.readouterr().out == ""                    # confirmed just now (real clock vs T0 in 2026-10)


def test_applier_leaves_due_rule_pending_then_applies_after_reconfirm(tmp_path, monkeypatch):
    sd, cfg = _repo(tmp_path, monkeypatch)
    rid = _prime(sd, cfg)
    T._ask(sd, cfg)
    src = T.Src()
    late = T0 + 100 * DAY
    acts = AP.tick(sd, cfg, late, source=src)
    assert [(a["kind"], a["result"], a.get("skip_reason")) for a in acts] == [
        ("skipped", "skipped", "rule due for re-confirmation")]
    assert src.swaps == []
    ask = json.load(open(AP._ask_path(sd, acts[0]["ask"])))
    assert ask["state"] == "pending" and ask.get("answer") is None
    # an owner reconfirms (stamped at the tick's clock), the next tick may apply
    RB.approve(sd, rid, "boss", cfg, now=late)
    # #1496: a rule applies on comparisons recorded since its confirmation, so reconfirming starts that count anew
    for _ in range(8):
        AU.record_watch(sd, "merge_conflict", "docs", "rebase it", "rebase it", cfg, now=late, machine="m1")
    acts = AP.tick(sd, cfg, late + 61 * MIN, source=src)
    assert [(a["kind"], a["result"]) for a in acts] == [("unpark", "done")]


def test_comparison_against_due_rule_is_still_recorded(tmp_path, monkeypatch):
    sd, cfg = _repo(tmp_path, monkeypatch)
    _prime(sd, cfg)
    before = len(AU._facts(sd, "watch-"))
    rules = RB.load(sd, cfg)
    assert RB.due_for_reask(rules, T0 + 200 * DAY, cfg)
    # recording runs on the match, which a due rule still makes
    assert RB.match("merge_conflict", "docs", None, rules, cfg) is not None
    AU.record_watch(sd, "merge_conflict", "docs", "rebase it", "rebase it", cfg, now=T0 + 200 * DAY, machine="m2")
    assert len(AU._facts(sd, "watch-")) == before + 1


def test_doctor_row_names_due_and_invalid(tmp_path, monkeypatch):
    sd, cfg = _repo(tmp_path, monkeypatch)
    cfg["rulebook"]["rules"]["reask_days"] = 1
    rid = _prime(sd, cfg)
    data = json.loads(_file(tmp_path).read_text())
    data["rules"].append({"id": "rule-bad", "qkind": "merge_conflict", "area": "docs", "option": "not an option",
                          "status": "proposed"})
    _file(tmp_path).write_text(json.dumps(data))
    # confirmed T0 is far in the past for the real clock, so the rule is due now
    state = DOC._rulebook_state(cfg, sd)
    assert "due for confirmation" in state and rid in state
    assert "invalid rule" in state and "rule-bad" in state
    assert "rule-bad" not in [r["id"] for r in RB.load(sd, cfg)]


# ---- M-11 -----------------------------------------------------------------

def test_retire_and_reject_free_the_pair_for_drafting(tmp_path, monkeypatch):
    sd, cfg = _repo(tmp_path, monkeypatch)
    rid = _prime(sd, cfg)
    ok, line = RD.retire(sd, rid, cfg, reject=True)
    assert not ok and "use retire" in line and RB.load(sd, cfg)
    ok, line = RD.retire(sd, rid, cfg)
    assert ok and "retired" in line and RB.load(sd, cfg) == []
    RB.propose(sd, {"qkind": "merge_conflict", "area": "docs", "option": "rebase it", "reason": "r"}, cfg, now=T0)
    pid = RB.load(sd, cfg)[0]["id"]
    ok, line = RD.retire(sd, pid, cfg, reject=True)
    assert ok and "rejected" in line and RB.load(sd, cfg) == []
    assert RD.retire(sd, "rule-nope", cfg)[0] is False


def test_retire_is_owner_only(tmp_path, monkeypatch):
    sd, cfg = _repo(tmp_path, monkeypatch, login="stranger")
    rid = _prime(sd, cfg)
    before = _file(tmp_path).read_bytes()
    ok, _ = RD.retire(sd, rid, cfg)
    assert not ok and _file(tmp_path).read_bytes() == before


def test_retire_removes_an_invalid_rule_the_load_leaves_out(tmp_path, monkeypatch, capsys):
    sd, cfg = _repo(tmp_path, monkeypatch)
    (tmp_path / ".sdlc" / "config.json").write_text(json.dumps(cfg))
    _file(tmp_path).write_text(json.dumps({"rules": [{"id": "rule-bad", "qkind": "merge_conflict", "area": "docs",
                                                      "option": "nope", "status": "proposed"}]}))
    assert [i for i, _ in RB.invalid(sd, cfg)] == ["rule-bad"]
    assert RD.main(["retire", sd, "rule-bad"]) == 0
    assert RB.invalid(sd, cfg) == [] and json.loads(_file(tmp_path).read_text())["rules"] == []


# ---- F-16 -----------------------------------------------------------------

def _star_rule(sd, cfg, area, option="rebase it"):
    RB.propose(sd, {"qkind": "merge_conflict", "area": area, "option": option, "reason": "r"}, cfg, now=T0)
    rid = RB.load(sd, cfg)[-1]["id"]
    RB.approve(sd, rid, "boss", cfg, now=T0)
    return rid


def test_star_area_matches_every_area_and_exact_wins(tmp_path, monkeypatch):
    sd, cfg = _repo(tmp_path, monkeypatch)
    star = _star_rule(sd, cfg, "*")
    rules = RB.load(sd, cfg)
    for area in ("repo", "docs", "backend"):
        assert RB.match("merge_conflict", area, None, rules, cfg).rule_id == star
    exact = _star_rule(sd, cfg, "docs", option="start fresh")
    rules = RB.load(sd, cfg)
    m = RB.match("merge_conflict", "docs", None, rules, cfg)
    assert (m.rule_id, m.option) == (exact, "start fresh")
    assert RB.match("merge_conflict", "repo", None, rules, cfg).rule_id == star


def test_two_star_rules_are_ambiguous(tmp_path, monkeypatch):
    sd, cfg = _repo(tmp_path, monkeypatch)
    _star_rule(sd, cfg, "*")
    _star_rule(sd, cfg, "*", option="start fresh")
    assert RB.match("merge_conflict", "docs", None, RB.load(sd, cfg), cfg) is None


# ---- F-13 -----------------------------------------------------------------

def _cats(tmp_path, cats):
    (tmp_path / ".sdlc").mkdir(exist_ok=True)
    (tmp_path / ".sdlc" / "categories.json").write_text(json.dumps({"schema": CT.SCHEMA, "categories": cats}))
    return str(tmp_path / ".sdlc")


def test_category_breaking_order_is_dropped_with_a_note(tmp_path):
    sd = _cats(tmp_path, [{"id": "bad", "areas": ["docs"], "levels": {"l2_min_answers": 9, "l3_min_answers": 4}},
                          {"id": "badrate", "areas": ["ui"], "levels": {"l2_agreement": 0.99, "l3_agreement": 0.9}},
                          {"id": "good", "areas": ["api"], "levels": {"l2_min_answers": 3}}])
    assert [c["id"] for c in CT.load(sd)] == ["good"]
    assert sorted(i for i, _ in CT.dropped(sd)) == ["bad", "badrate"]
    assert any("bad dropped" in n for n in AU.category_notes(sd, T._cfg()))


def test_category_that_breaks_order_on_top_of_global_levels_is_not_applied(tmp_path):
    cfg = T._cfg()          # global l3_min_answers is 8
    sd = _cats(tmp_path, [{"id": "low", "areas": ["docs"], "levels": {"l2_min_answers": 12}}])
    thresholds, cat, has_file = AU._pair_settings(sd, "merge_conflict", "docs", cfg)
    assert cat is None and has_file and thresholds["l2_min_answers"] == 4
    assert any("low dropped" in n for n in AU.category_notes(sd, cfg))


def test_star_category_covers_every_area_exact_wins(tmp_path):
    sd = _cats(tmp_path, [{"id": "all", "areas": ["*"], "levels": {"l2_min_answers": 5}},
                          {"id": "docs", "areas": ["docs"], "levels": {"l2_min_answers": 6}}])
    assert CT.resolve(sd, "merge_conflict", "docs")["id"] == "docs"
    assert CT.resolve(sd, "merge_conflict", "repo")["id"] == "all"
    assert CT.resolve(sd, "merge_conflict", "ui")["id"] == "all"


# ---- F-18 -----------------------------------------------------------------

def test_shape_only_key_says_so_in_draft(tmp_path, monkeypatch):
    sd, cfg = _repo(tmp_path, monkeypatch)
    for _ in range(40):
        DS.append(sd, {"how": "human", "qkind": "merge_conflict", "area": "docs", "choice": "some free shape"}, cfg)
        AU.record_watch(sd, "merge_conflict", "docs", "some free shape", "some free shape", cfg, now=T0, machine="m1")
    out = RD.draft(sd, "merge_conflict", "docs", cfg)
    assert out["status"] == "refused" and "normalised shape; no single option can be drafted" in out["reason"]
    assert RB.load(sd, cfg) == []
    rows = RD.list_patterns(sd, cfg)
    assert rows and all("normalised shape" in r["note"] for r in rows)


# ---- compatibility --------------------------------------------------------

SHAPE25 = {"quota": "wait it out", "merge_conflict": "rebase it", "no_evidence": "add a test",
           "dependency": "wait for it", "reviewer_down": "fix and re-run the reviewer"}


def test_existing_25_rule_shape_stays_valid_and_sealed(tmp_path, monkeypatch):
    sd, cfg = _repo(tmp_path, monkeypatch)
    areas = ("repo", "docs", "backend", "ui", "api")
    for q, opt in SHAPE25.items():
        for a in areas:
            RB.propose(sd, {"qkind": q, "area": a, "option": opt, "reason": "r"}, cfg, now=T0)
    for r in RB.load(sd, cfg):
        RB.approve(sd, r["id"], "boss", cfg, now=T0)
    rules = RB.load(sd, cfg)
    assert len(rules) == 25 and RB.invalid(sd, cfg) == []
    assert all(r["status"] == "approved" for r in rules)
    for q, opt in SHAPE25.items():
        for a in areas:
            assert RB.match(q, a, None, rules, cfg).option == opt


def test_closed_gate_is_inert(tmp_path):
    cfg = {"rulebook": {"rules": {"enabled": False}}}
    sd = str(tmp_path / ".sdlc")
    assert RB.invalid(sd, cfg) == [] and RB.due_ages([], T0, cfg) == [] and RD.due(sd, cfg) == []
    assert RD.retire(sd, "x", cfg)[0] is False and RD.reconfirm(sd, "x", cfg)[0] is False
    assert AP.tick(sd, cfg, T0) == [] and not (tmp_path / ".sdlc").exists()
