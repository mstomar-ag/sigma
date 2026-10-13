"""Goal 1138 (story 988): draft a rule from a learned pattern, with a held-back exam."""
import datetime
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
T0 = datetime.datetime(2026, 10, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _mod(name):
    p = SCRIPTS / (name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_t1138", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


RD, DS, AU, RB = _mod("rule_draft"), _mod("decision_store"), _mod("autonomy"), _mod("rulebook")


def _cfg(rulebook=True, autonomy=True, draft=None):
    rb = {"enabled": rulebook}
    if draft is not None:
        rb["draft"] = draft
    return {"rulebook": {
        "records": {"enabled": True}, "rules": rb,
        "autonomy": {"enabled": autonomy, "owners": ["boss"],
                     "levels": {"l1_min_answers": 2, "l2_min_answers": 4, "l2_agreement": 0.75}}}}


def _sd(tmp_path):
    return str(tmp_path / ".sdlc")


def _train(sd, cfg, choices, qkind="needs_decision", area="docs", watch=True):
    """choices: callable(id, side) -> choice, side is 'draft' or 'held'."""
    ids = ["r%03d" % i for i in range(40)]
    side = RD.split(ids)
    for rid in ids:
        DS.append(sd, {"id": rid, "how": "human", "qkind": qkind, "area": area,
                       "choice": choices(rid, "draft" if rid in side[0] else "held")}, cfg, now=T0)
    if watch:
        for _ in range(8):
            AU.record_watch(sd, qkind, area, "merge", "merge", cfg, now=T0, machine="m1")
    return ids


def test_split_is_deterministic_and_order_free():
    ids = ["r%03d" % i for i in range(40)]
    a = RD.split(ids)
    assert a == RD.split(list(reversed(ids)))
    assert set(a[0]) | set(a[1]) == set(ids) and not set(a[0]) & set(a[1])
    assert len(a[0]) > 5 and len(a[1]) > 5


def test_list_respects_level_and_threshold(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    _train(sd, cfg, lambda r, s: "merge")
    rows = RD.list_patterns(sd, cfg)
    assert [(r["qkind"], r["area"], r["option"]) for r in rows] == [("needs_decision", "docs", "merge")]
    assert rows[0]["level"] >= 2 and rows[0]["agreement"] == 1.0
    # level below 2: no watch comparisons
    sd2 = str(tmp_path / "b" / ".sdlc")
    _train(sd2, cfg, lambda r, s: "merge", watch=False)
    assert RD.list_patterns(sd2, cfg) == []
    # agreement below threshold
    sd3 = str(tmp_path / "c" / ".sdlc")
    _train(sd3, cfg, lambda r, s: "merge" if int(r[1:]) % 2 else "wait")
    assert RD.list_patterns(sd3, cfg) == []


def test_draft_links_sources_and_only_proposes(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    ids = _train(sd, cfg, lambda r, s: "merge")
    out = RD.draft(sd, "needs_decision", "docs", cfg, now=T0)
    assert out["status"] == "drafted"
    rules = RB.load(sd, cfg)
    assert len(rules) == 1 and rules[0]["status"] == "proposed" and rules[0]["option"] == "merge"
    assert set(out["sources"]) <= set(ids) and out["sources"]
    assert all(s in rules[0]["reason"] for s in out["sources"][:3])
    assert RB.match("needs_decision", "docs", None, rules, cfg) is None  # proposed never acts
    assert set(rules[0]) <= set(RB.FIELDS)  # no person field


def test_exam_fails_closed_when_held_out_disagrees(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    _train(sd, cfg, lambda r, s: "merge" if s == "draft" else "wait")
    out = RD.draft(sd, "needs_decision", "docs", cfg, now=T0)
    assert out["status"] == "refused" and "exam" in out["reason"]
    assert RB.load(sd, cfg) == []


def test_exam_fails_closed_when_held_out_too_small(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg(draft={"min_held_out": 500})
    _train(sd, cfg, lambda r, s: "merge")
    assert RD.draft(sd, "needs_decision", "docs", cfg, now=T0)["status"] == "refused"
    assert RB.load(sd, cfg) == []


def test_refuses_low_level_unknown_pair_and_existing_rule(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    _train(sd, cfg, lambda r, s: "merge", watch=False)
    assert RD.draft(sd, "needs_decision", "docs", cfg)["status"] == "refused"
    assert RD.draft(sd, "needs_decision", "nope", cfg)["status"] == "refused"
    sd2 = str(tmp_path / "b" / ".sdlc")
    _train(sd2, cfg, lambda r, s: "merge")
    assert RD.draft(sd2, "needs_decision", "docs", cfg, now=T0)["status"] == "drafted"
    assert RD.draft(sd2, "needs_decision", "docs", cfg, now=T0)["status"] == "refused"
    assert len(RB.load(sd2, cfg)) == 1


def test_hard_stop_kind_never_drafted(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    _train(sd, cfg, lambda r, s: "merge", qkind="irreversible")
    assert RD.list_patterns(sd, cfg) == []
    assert RD.draft(sd, "irreversible", "docs", cfg)["status"] == "refused"


def test_closed_gates_are_inert(tmp_path):
    sd = _sd(tmp_path)
    _train(sd, _cfg(), lambda r, s: "merge")
    for cfg in (_cfg(rulebook=False), _cfg(autonomy=False), {}, None):
        assert RD.list_patterns(sd, cfg) == []
        assert RD.draft(sd, "needs_decision", "docs", cfg)["status"] == "refused"
    assert not os.path.exists(os.path.join(str(tmp_path), ".sdlc", "rulebook.json"))
    assert not os.path.exists(tmp_path / "rulebook.json")


def test_bad_threshold_reads_as_default():
    assert RD.settings(_cfg(draft={"min_agreement": "x", "min_held_out": True})) == RD.settings(_cfg())


def test_cli_list_and_draft(tmp_path):
    sd, cfg = _sd(tmp_path), _cfg()
    _train(sd, cfg, lambda r, s: "merge")
    pathlib.Path(sd, "config.json").write_text(json.dumps(cfg))
    run = lambda *a: subprocess.run([sys.executable, str(SCRIPTS / "rule_draft.py"), *a],
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
    r = run("list", sd)
    assert r.returncode == 0 and json.loads(r.stdout)[0]["option"] == "merge"
    r = run("draft", sd, "needs_decision", "docs")
    assert r.returncode == 0 and json.loads(r.stdout)["status"] == "drafted"
    r = run("draft", sd, "needs_decision", "docs")
    assert r.returncode == 1


def _public(cfg):
    cfg["rulebook"]["records"]["repo_visibility"] = "public"
    return cfg


def test_public_visibility_says_plainly_it_cannot_draft(tmp_path):
    sd = _sd(tmp_path)
    _train(sd, _cfg(), lambda r, s: "merge")  # private store has the choices
    cfg = _public(_cfg())
    out = RD.draft(sd, "needs_decision", "docs", cfg, now=T0)
    assert out["status"] == "refused" and "public" in out["reason"] and "cannot draft" in out["reason"]
    assert not os.path.exists(os.path.join(sd, "rulebook"))
    # a store really written under public visibility has no choice at all
    sd2 = str(tmp_path / "p" / ".sdlc")
    _train(sd2, cfg, lambda r, s: "merge")
    out2 = RD.draft(sd2, "needs_decision", "docs", cfg, now=T0)
    assert out2["status"] == "refused" and "public" in out2["reason"]


def test_public_visibility_cli_is_explicit_not_silent(tmp_path):
    sd, cfg = _sd(tmp_path), _public(_cfg())
    _train(sd, cfg, lambda r, s: "merge")
    pathlib.Path(sd, "config.json").write_text(json.dumps(cfg))
    run = lambda *a: subprocess.run([sys.executable, str(SCRIPTS / "rule_draft.py"), *a],
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
    r = run("list", sd)
    assert r.returncode == 1 and "public" in r.stderr and r.stdout.strip() == ""
    r = run("draft", sd, "needs_decision", "docs")
    assert r.returncode == 1 and "public" in json.loads(r.stdout)["reason"]


def test_gate_closed_public_visibility_stays_inert_and_unchanged(tmp_path):
    sd = _sd(tmp_path)
    cfg = _public(_cfg(rulebook=False))
    assert RD.draft(sd, "needs_decision", "docs", cfg) == {
        "status": "refused", "reason": "closed: rulebook, autonomy and records parts must all be enabled"}
    pathlib.Path(sd).mkdir(parents=True)
    pathlib.Path(sd, "config.json").write_text(json.dumps(cfg))
    r = subprocess.run([sys.executable, str(SCRIPTS / "rule_draft.py"), "list", sd],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and json.loads(r.stdout) == [] and r.stderr == ""


def _train_how(sd, cfg, qkind, how, area="docs"):
    ids = ["r%03d" % i for i in range(40)]
    for rid in ids:
        DS.append(sd, {"id": rid, "how": how, "qkind": qkind, "area": area, "choice": "merge"}, cfg, now=T0)
    for _ in range(8):
        AU.record_watch(sd, qkind, area, "merge", "merge", cfg, now=T0, machine="m1")


def test_imported_unknown_records_never_feed_a_draft(tmp_path):
    """#1239: imported records of kind unknown are skipped; live unknown and imported known are not."""
    sd, cfg = _sd(tmp_path), _cfg()
    _train_how(sd, cfg, "unknown", "imported")
    assert RD._human(sd, "unknown", "docs", cfg) == {}
    assert RD.list_patterns(sd, cfg) == []
    # live unknown records still count as evidence (the kind itself is refused elsewhere)
    sd2 = str(tmp_path / "b" / ".sdlc")
    _train_how(sd2, cfg, "unknown", "human")
    assert len(RD._human(sd2, "unknown", "docs", cfg)) == 40
    # imported records of a known kind still draft
    sd3 = str(tmp_path / "c" / ".sdlc")
    _train_how(sd3, cfg, "needs_decision", "imported")
    assert RD.draft(sd3, "needs_decision", "docs", cfg, now=T0)["status"] == "drafted"
    assert [r["qkind"] for r in RD.list_patterns(sd3, cfg)] == ["needs_decision"]
