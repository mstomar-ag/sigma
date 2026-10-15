"""Goal 1496: an approved rule is shown as the recommendation, and agreement is explained."""
import datetime
import json
import os
import subprocess
import sys

import test_unpark as tu
from test_decision_record import _mod, _on

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
PARK = "x\nsigma-qkind: dependency"


def _cfg(rules=True, autonomy=True, l3=None):
    cfg = _on(tu._config())
    if rules:
        cfg["rulebook"]["rules"] = {"enabled": True}
    if autonomy:
        lv = {"l1_min_answers": 1, "l2_min_answers": 2, "l2_agreement": 0.75,
              "l3_min_answers": 2, "l3_agreement": 0.9}
        cfg["rulebook"]["autonomy"] = {"enabled": True, "owners": ["boss"], "levels": lv}
    return cfg


def _rule(sd, cfg, option="wait for it", area="docs", when=NOW):
    rb = _mod("rulebook")
    rb.propose(sd, {"qkind": "dependency", "area": area, "option": option, "reason": "r"}, cfg, now=when)
    rid = [r for r in rb.load(sd, cfg) if r["status"] == "proposed"][-1]["id"]
    rb.approve(sd, rid, "boss", cfg, now=when)
    return rid


def _run(labels=("sdlc:parked", "area:docs"), park=PARK):
    return tu._runner(views={"5": tu._view(labels=labels, comments=[tu._park(park)])})


def test_brief_shows_recommendation_and_pair(tmp_path):
    u, sd, cfg = _mod("unpark"), str(tmp_path / ".sdlc"), _cfg()
    rid = _rule(sd, cfg, when=None)
    b = u.brief(sd, cfg, 5, run=_run())
    assert b["recommendation"] == {"rule_id": rid, "option": "wait for it", "gesture": "keep-parked",
                                   "due": False}
    assert b["pair"]["level"] == 0 and "answers_needed" in b["pair"]
    text = u.render_brief(b)
    assert "approved rule %s recommends: wait for it (will keep parked)" % rid in text
    assert "  pair: level 0" in text
    assert json.loads(json.dumps(b))["recommendation"]["rule_id"] == rid


def test_brief_shows_nothing_for_hard_stop_unknown_other_area_or_filtered_option(tmp_path):
    u, rb, sd, cfg = _mod("unpark"), _mod("rulebook"), str(tmp_path / ".sdlc"), _cfg()
    _rule(sd, cfg)
    assert "recommendation" not in u.brief(sd, cfg, 5, run=_run(park="x"))                       # unknown kind
    assert "recommendation" not in u.brief(sd, cfg, 5, run=_run(labels=("sdlc:parked",)))       # other area
    hs = _mod("hard_stop")
    hard = next(k for k in _mod("qkind").QKINDS if k != "unknown" and hs.is_hardstop_kind(k, None, cfg))
    assert "recommendation" not in u.brief(sd, cfg, 5, run=_run(park="x\nsigma-qkind: " + hard))
    assert rb.prefill(sd, "dependency", "docs", ["drop this"], cfg) is None                      # option not offered


def test_resolve_records_rule_id_shown_and_accepted(tmp_path):
    u, sd, cfg = _mod("unpark"), str(tmp_path / ".sdlc"), _cfg()
    rid = _rule(sd, cfg)
    u.resolve(sd, cfg, 5, {"route": "wait for it"}, "keep-parked", run=_run(), rule_id=rid)
    u.resolve(sd, cfg, 5, {"route": "drop this"}, "keep-parked", run=_run(), rule_id=rid)
    recs = sorted(_mod("decision_store").list_by(sd, cfg), key=lambda r: r["id"])
    assert [(r["rule_id"], r["shown"], r["accepted"]) for r in recs if r["answer"] == "wait for it"] == [(rid, True, True)]
    assert [(r["shown"], r["accepted"]) for r in recs if r["answer"] == "drop this"] == [(True, False)]
    # rule_id is a payload key, never an answer
    assert "rule_id" not in json.dumps([r.get("answer") for r in recs])


def test_misses_majority_and_describe(tmp_path):
    au, sd, cfg = _mod("autonomy"), str(tmp_path / ".sdlc"), _cfg()
    for i, (w, a) in enumerate([("wait for it", "drop this"), ("wait for it", "free"), ("wait for it", "wait for it")]):
        _mod("decision_store").append(sd, {"how": "human", "qkind": "dependency", "area": "docs", "choice": a,
                                           "answer": a}, cfg)
        au.record_watch(sd, "dependency", "docs", w, a, cfg, now=NOW + datetime.timedelta(minutes=i), machine="m")
    row = au._state(sd, "dependency", "docs", cfg)
    assert row["misses"][0]["actual"] in ("drop this", "a free-text answer")
    assert {m["actual"] for m in row["misses"]} == {"drop this", "a free-text answer"}
    assert abs(row["majority_share"] - 0.5) < 1e-9
    line = au.describe(dict(row, **au._need(row, cfg)))
    assert "most misses:" in line and "a free-text answer instead of wait for it" in line
    assert "cannot reach" in line


def test_shown_agreement_is_reported_apart_and_level3_ignores_shown(tmp_path):
    au, sd, cfg = _mod("autonomy"), str(tmp_path / ".sdlc"), _cfg()
    for i in range(3):
        _mod("decision_store").append(sd, {"how": "human", "qkind": "dependency", "area": "docs",
                                           "choice": "wait for it", "answer": "wait for it"}, cfg)
    for i in range(3):
        au.record_watch(sd, "dependency", "docs", "wait for it", "wait for it", cfg, now=NOW, machine="m",
                        shown=True)
    row = au._state(sd, "dependency", "docs", cfg)
    assert row["agreement_shown"] == 1.0 and row["agreement_not_shown"] is None
    assert row["level"] == 2                                  # shown matches never reach level 3
    for i in range(3):
        au.record_watch(sd, "dependency", "docs", "wait for it", "wait for it", cfg, now=NOW, machine="m")
    assert au._state(sd, "dependency", "docs", cfg)["level"] >= 3


def test_list_why_and_doctor_acceptance_and_due(tmp_path):
    rd, rb, sd, cfg = _mod("rule_draft"), _mod("rulebook"), str(tmp_path / ".sdlc"), _cfg()
    ds = _mod("decision_store")
    ds.append(sd, {"how": "human", "qkind": "dependency", "area": "docs", "choice": "drop this",
                   "answer": "drop this"}, cfg)
    why = rd.skipped(sd, cfg)
    assert why and why[0]["qkind"] == "dependency" and why[0]["why"].startswith("level 1")
    rid = _rule(sd, cfg, when=NOW)
    ds.append(sd, {"how": "human", "qkind": "dependency", "area": "docs", "choice": "wait for it",
                   "answer": "wait for it", "rule_id": rid, "shown": True, "accepted": True}, cfg)
    row = rb.acceptance(sd, cfg)[0]
    assert (row["answers"], row["accepted"], row["never_matches"]) == (1, 1, False)
    assert rb.acceptance_line(row).startswith("rule %s: 1 of 1 live answers accepted since 2026-01-01" % rid)
    lines = rd.due_lines(sd, cfg, now=NOW + datetime.timedelta(days=400))
    assert lines == [rb.acceptance_line(row)]
    # an option nobody ever chose is flagged
    cfg2 = _cfg()
    sd2 = str(tmp_path / "b" / ".sdlc")
    ds.append(sd2, {"how": "human", "qkind": "dependency", "area": "docs", "choice": "drop this",
                    "answer": "drop this"}, cfg2)
    _rule(sd2, cfg2)
    assert rb.acceptance(sd2, cfg2)[0]["never_matches"] is True


def _tick_setup(tmp_path, own):
    ap, au, sd, cfg = _mod("applier"), _mod("autonomy"), str(tmp_path / ".sdlc"), _cfg()
    cfg["rulebook"]["autonomy"]["applier"] = {"enabled": True}
    _rule(sd, cfg, option="wait for it", area="docs")
    ap.level_fn = lambda *a: 3
    # the pair's older comparisons chose another option; the new rule has `own` of its own
    for _ in range(5):
        au.record_watch(sd, "dependency", "docs", "drop this", "drop this", cfg, now=NOW - datetime.timedelta(days=1),
                        machine="m")
    for _ in range(own):
        au.record_watch(sd, "dependency", "docs", "wait for it", "wait for it", cfg,
                        now=NOW + datetime.timedelta(days=1), machine="m")
    ap.open_ask(sd, 5, "dependency", "docs", ["wait for it", "drop this"], cfg, NOW)
    return ap, sd, cfg


def test_new_rule_is_not_applied_at_level_3_until_its_own_comparisons_exist(tmp_path):
    ap, sd, cfg = _tick_setup(tmp_path, own=0)
    later = NOW + datetime.timedelta(days=3)
    try:
        assert ap.tick(sd, cfg, later, apply_actions=False, source=object()) == []
        ap2, sd2, cfg2 = _tick_setup(tmp_path / "b", own=2)
        acts = ap2.tick(sd2, cfg2, later, apply_actions=False, source=object())
        assert [a["option"] for a in acts] == ["wait for it"]
    finally:
        ap.level_fn = None


def test_closed_gates_leave_brief_and_resolve_unchanged(tmp_path):
    u, sd = _mod("unpark"), str(tmp_path / ".sdlc")
    cfg = _on(tu._config())                                  # rules and autonomy closed
    b = u.brief(sd, cfg, 5, run=_run())
    assert "recommendation" not in b and "pair" not in b
    assert "recommends" not in u.render_brief(b) and "pair:" not in u.render_brief(b)
    u.resolve(sd, cfg, 5, {"route": "wait for it"}, "keep-parked", run=_run(), rule_id=None)
    rec = _mod("decision_store").list_by(sd, cfg)[0]
    assert not {"rule_id", "shown", "accepted"} & set(rec)
    # a rule_id passed with the rules part closed still names no rule: nothing is accepted
    assert "pair" not in u.brief(sd, _cfg(rules=True, autonomy=False), 5, run=_run())


def test_cli_scenario_round_trip(tmp_path):
    root = tmp_path / "repo"
    (root / ".sdlc").mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    u, sd, cfg = _mod("unpark"), str(root / ".sdlc"), _cfg()
    rid = _rule(sd, cfg)
    b = u.brief(sd, cfg, 5, run=_run())
    assert b["recommendation"]["rule_id"] == rid
    run = _run()
    for _ in range(3):
        u.resolve(sd, cfg, 5, {"route": b["recommendation"]["option"]}, "keep-parked", run=run,
                  rule_id=b["recommendation"]["rule_id"])
    recs = _mod("decision_store").list_by(sd, cfg)
    assert len(recs) == 3 and all(r["accepted"] for r in recs)
    assert _mod("rulebook").acceptance(sd, cfg)[0]["answers"] >= 1
    (root / ".sdlc" / "config.json").write_text(json.dumps(cfg))
    # rule_draft CLI on the same temp repo: list --why and due run and exit 0
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "skills", "sigma-loop", "scripts", "rule_draft.py")
    for verb in (["list", sd, "--why"], ["due", sd]):
        r = subprocess.run([sys.executable, script] + verb, capture_output=True, text=True, cwd=str(root))
        assert r.returncode == 0, r.stderr
