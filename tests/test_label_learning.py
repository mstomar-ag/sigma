"""#1581: run labels learn by kind. The arming question is recorded under `label-kind:<kind>`, an approved
rule for that area is shown first as a recommendation (never applied), the answer record carries
`rule_id` / `shown` / `accepted` the way a park's does, and a public repository never sees the name.

Driven through `label_drain.plan` / `answer` / `main` with the stateful fake gh of the answer tests.
"""
import hashlib
import json
import pathlib
import subprocess

import pytest

import test_label_answer as tla
from test_label_answer import Fake, issue, records
from test_label_drain_arm import DW, _mod, _repo
from test_applier import RB

ARM = "arm them"
LEAVE = "leave them"
T0 = __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc)


def _world(tmp_path, issues, rules=False, autonomy=False, public=False, records_on=True):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    base = _repo(tmp_path, "alice")
    cfgp = pathlib.Path(base) / "config.json"
    cfg = json.loads(cfgp.read_text())
    cfg["hard_stops"] = {"enabled": True}
    cfg["spend_approval"] = {"enabled": True, "approvers": ["alice"]}
    if records_on:
        rb = {"records": {"enabled": True}, "hard_stops": {"enabled": True}}
        if public:
            rb["records"]["repo_visibility"] = "public"
        if rules:
            rb["rules"] = {"enabled": True, "reask_days": 100000}
        if autonomy:
            rb["autonomy"] = {"enabled": True, "owners": ["boss"]}
        cfg["rulebook"] = rb
    cfgp.write_text(json.dumps(cfg))
    return _mod("label_drain"), base, Fake(issues), cfg


def _unarmed(label, n=1):
    return [issue(n, label)]


def _q(p, prefix="label_set:"):
    return next(q for q in p["questions"] if q["id"].startswith(prefix))


def _approve_rule(base, cfg, qkind, area, option):
    RB.propose(base, {"qkind": qkind, "area": area, "option": option, "reason": "low risk"}, cfg, now=T0)
    RB.approve(base, RB.load(base, cfg)[-1]["id"], "boss", cfg, now=T0)


def _all_text(base):
    return "\n".join(p.read_text(errors="replace") for p in pathlib.Path(base).rglob("*") if p.is_file())


# 1 -- the kind of a label ---------------------------------------------------------------------

@pytest.mark.parametrize("label,config,want", [
    ("release:2026-q1", None, "release"),
    ("nightly/batch-7", None, "nightly"),
    ("validation-run-1", None, "validation-run-1"),
    ("run:a", {"label_drain": {"kinds": {"run:a": "special"}}}, "special"),
    ("run:a", {"label_drain": {"kinds": {"other": "special"}}}, "run"),
    ("run:a", {"label_drain": {"kinds": ["run:a"]}}, "run"),
    ("run:a", {"label_drain": {"kinds": {"run:a": 7}}}, "run"),
    ("run:a", {"label_drain": {"kinds": {"run:a": "  "}}}, "run"),
    ("run:a", {"label_drain": "nope"}, "run"),
    (":odd", None, ":odd"),
    ("", None, "other"),
])
def test_label_kind_table(label, config, want):
    assert _mod("label_drain").label_kind(label, config) == want


# 2 -- ten answers in one kind reach level 1 ----------------------------------------------------

def test_ten_arming_answers_in_one_kind_reach_level_one_and_other_kinds_stay_at_zero(tmp_path):
    labels = ["run:%d" % i for i in range(10)]
    ld, base, fk, cfg = _world(tmp_path, [issue(i + 1, l) for i, l in enumerate(labels)], autonomy=True)
    au = _mod("autonomy")
    for i, l in enumerate(labels):
        assert au.level(base, "label_set", "label-kind:run", cfg) == 0
        res = ld.answer(base, l, {"label_set:" + l: LEAVE}, run=fk)
        assert res["errors"] == [] and res["results"][0]["outcome"] == "applied"
    assert au.level(base, "label_set", "label-kind:run", cfg) == 1
    assert au.level(base, "label_set", "label-kind:other", cfg) == 0
    assert {r["area"] for r in records(base)} == {"label-kind:run"}


# 3 -- plan: closed gate identical, own history, kind fallback ---------------------------------

def test_records_gate_closed_leaves_the_plan_and_its_text_as_they_were(tmp_path):
    ld, base, fk, cfg = _world(tmp_path, _unarmed("run:a"), records_on=False)
    p = ld.plan(base, "run:a", config=cfg, run=fk)
    assert p["questions"] == [{"id": "label_set:run:a", "kind": "label_set", "area": "repo",
                               "ask": p["questions"][0]["ask"], "options": p["questions"][0]["options"],
                               "unblocks": [1]}]
    text = ld.render_plan(p)
    assert "history" not in text and "recommended" not in text
    assert "recorded under area repo" in text


def test_plan_shows_the_last_answer_on_this_label_then_counts_for_the_kind(tmp_path):
    ld, base, fk, cfg = _world(tmp_path, _unarmed("run:a") + [issue(2, "run:b")])
    assert "history" not in _q(ld.plan(base, "run:a", config=cfg, run=fk))
    ld.answer(base, "run:a", {"label_set:run:a": LEAVE}, run=fk)
    # #1732: the answered question is no longer asked on this label; the kind's history reaches other labels
    assert not [q for q in ld.plan(base, "run:a", config=cfg, run=fk)["questions"]
                if q["id"].startswith("label_set:")]
    other = _q(ld.plan(base, "run:b", config=cfg, run=fk))
    assert other["history"] == "on labels of this kind: %s 1" % LEAVE
    assert "history: on labels of this kind" in ld.render_plan(ld.plan(base, "run:b", config=cfg, run=fk))


# 4 -- public visibility -------------------------------------------------------------------------

def test_public_visibility_hashes_the_label_and_its_kind_everywhere(tmp_path):
    name = "zebra-secret"
    ld, base, fk, cfg = _world(tmp_path, [issue(1, name)], public=True)
    p = ld.plan(base, name, config=cfg, run=fk)
    q = _q(p)
    assert name not in q["area"] and q["area"].startswith("label-kind:")
    res = ld.answer(base, name, {"label_set:" + name: LEAVE}, run=fk)
    assert res["errors"] == [] and res["results"][0]["outcome"] == "applied"
    (rec,) = records(base)
    assert rec["scope"] == "label:" + hashlib.sha256(name.encode()).hexdigest()[:12]
    assert name not in _all_text(base) and not any(name in c for c in fk.stored_comments(1))
    again = ld.answer(base, name, {"label_set:" + name: LEAVE}, run=fk)      # dedupe still works
    assert again["results"][0]["outcome"] == "already-recorded"
    assert len(records(base)) == 1


def test_private_visibility_keeps_the_name_in_the_scope(tmp_path):
    ld, base, fk, cfg = _world(tmp_path, [issue(1, "run:a")])
    ld.answer(base, "run:a", {"label_set:run:a": LEAVE}, run=fk)
    (rec,) = records(base)
    assert rec["scope"] == "label:run:a" and rec["area"] == "label-kind:run"


# 5 -- a rule is shown, never applied ------------------------------------------------------------

def test_an_approved_rule_is_listed_first_and_marked_and_nothing_happens_without_an_answer(tmp_path):
    ld, base, fk, cfg = _world(tmp_path, _unarmed("run:a"), rules=True, autonomy=True)
    _approve_rule(base, cfg, "label_set", "label-kind:run", LEAVE)
    p = ld.plan(base, "run:a", config=cfg, run=fk)
    q = _q(p)
    rid = RB.load(base, cfg)[-1]["id"]
    assert q["recommendation"] == {"rule_id": rid, "option": LEAVE} and q["options"][0] == LEAVE
    assert "recommended: %s" % LEAVE in ld.render_plan(p)
    assert fk.writes() == [] and records(base) == []
    assert "sdlc:goal" not in fk._names(1)


@pytest.mark.parametrize("choice,accepted", [(LEAVE, True), (ARM, False)])
def test_the_answer_record_carries_rule_id_shown_and_accepted(tmp_path, choice, accepted):
    ld, base, fk, cfg = _world(tmp_path, _unarmed("run:a"), rules=True, autonomy=True)
    _approve_rule(base, cfg, "label_set", "label-kind:run", LEAVE)
    rid = RB.load(base, cfg)[-1]["id"]
    ld.answer(base, "run:a", {"label_set:run:a": choice}, run=fk)
    (rec,) = records(base)
    assert (rec["rule_id"], rec["shown"], rec["accepted"]) == (rid, True, accepted)
    assert rec["qkind"] == "label_set" and rec["area"] == "label-kind:run"
    assert ("sdlc:goal" in fk._names(1)) == (choice == ARM)


def test_no_rule_means_no_recommendation_keys_on_the_record(tmp_path):
    ld, base, fk, cfg = _world(tmp_path, _unarmed("run:a"), rules=True, autonomy=True)
    ld.answer(base, "run:a", {"label_set:run:a": LEAVE}, run=fk)
    (rec,) = records(base)
    assert not {"rule_id", "shown", "accepted"} & set(rec)


# 6 -- spend stays a hard stop -------------------------------------------------------------------

def test_spend_is_never_recommended_and_stays_capped_at_level_two(tmp_path):
    paid = issue(1, "sdlc:goal", "run:a", body="Paid step: runpodctl create pod for the render\n\n" + DW)
    ld, base, fk, cfg = _world(tmp_path, [paid], rules=True, autonomy=True)
    _approve_rule(base, cfg, "irreversible", "repo", "go ahead")
    p = ld.plan(base, "run:a", config=cfg, run=fk)
    q = _q(p, "irreversible:")
    assert "recommendation" not in q
    ds = _mod("decision_store")
    for _ in range(400):
        ds.append(base, {"how": "human", "qkind": "irreversible", "area": "repo", "gesture": "ack",
                         "choice": "go ahead", "answer": "go ahead"}, cfg)
    assert _mod("autonomy").level(base, "irreversible", "repo", cfg) <= 2


# 7 -- doctor ------------------------------------------------------------------------------------

def _doctor():
    spec = __import__("importlib.util").util.spec_from_file_location(
        "doctor_labels", pathlib.Path(tla.__file__).resolve().parents[1] / "skills/sigma-doctor/scripts/doctor.py")
    m = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_doctor_lists_one_line_per_label_kind_and_nothing_with_the_gate_closed(tmp_path):
    ld, base, fk, cfg = _world(tmp_path, _unarmed("run:a"), rules=True, autonomy=True)
    d = _doctor()
    assert "run label kind" not in d._autonomy_state(cfg, base)
    ld.answer(base, "run:a", {"label_set:run:a": LEAVE}, run=fk)
    assert "run label kind run: 1 answer(s), level 0, rule covers: no" in d._autonomy_state(cfg, base)
    _approve_rule(base, cfg, "label_set", "label-kind:run", LEAVE)
    assert "rule covers: yes" in d._autonomy_state(cfg, base)
    closed = json.loads(json.dumps(cfg))
    closed["rulebook"]["autonomy"]["enabled"] = False
    assert d._autonomy_state(closed, base) == "off (default)"


# 8 -- the CLI path ------------------------------------------------------------------------------

def test_cli_answer_with_a_rule_prints_and_records_the_acceptance(tmp_path, capsys):
    ld, base, fk, cfg = _world(tmp_path, _unarmed("run:a"), rules=True, autonomy=True)
    _approve_rule(base, cfg, "label_set", "label-kind:run", ARM)
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"label_set:run:a": ARM}))
    assert ld.main(["label_drain.py", "answer", base, "run:a", "--answers", str(f)], run=fk) == 0
    assert "sdlc:goal" in fk._names(1)
    (rec,) = records(base)
    assert rec["accepted"] is True and rec["scope"] == "label:run:a"
    assert "APPLIED" in capsys.readouterr().out
