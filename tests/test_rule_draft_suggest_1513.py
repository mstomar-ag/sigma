"""Goal 1513: `rule_draft.py suggest` ranks (kind, area) pairs by importance and proposes rules for the top ones."""
import datetime
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
T0 = datetime.datetime(2026, 10, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
NOW = T0 + datetime.timedelta(hours=48)
GO, WAIT = "start it again", "leave it parked"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_t1513", SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


RD, DS, RB = _mod("rule_draft"), _mod("decision_store"), _mod("rulebook")


def _cfg(rules=True):
    return {"rulebook": {"records": {"enabled": True}, "rules": {"enabled": rules},
                         "autonomy": {"enabled": True, "owners": ["boss"]}}}


def _rec(sd, cfg, rid, qkind, area, choice, issue, hours=0):
    DS.append(sd, {"id": rid, "how": "human", "qkind": qkind, "area": area, "choice": choice, "answer": choice,
                   "issue": issue}, cfg, now=T0 + datetime.timedelta(hours=hours))


def _fixture(tmp_path, cfg=None):
    """needs_decision/docs: 4 parks, goals 1-4; needs_decision/api: 2 parks; needs_decision/ci: 2 parks, differs;
    merge_conflict/docs: 1 park; irreversible and unknown: parks that must never be suggested."""
    sd, cfg = str(tmp_path / ".sdlc"), cfg or _cfg()
    for i in range(4):
        _rec(sd, cfg, "a%d" % i, "needs_decision", "docs", GO, 1 + i)
    for i in range(2):
        _rec(sd, cfg, "b%d" % i, "needs_decision", "api", GO, 10 + i)
        _rec(sd, cfg, "c%d" % i, "needs_decision", "ci", WAIT, 20 + i)
    _rec(sd, cfg, "d0", "merge_conflict", "docs", "rebase it", 30)
    _rec(sd, cfg, "e0", "irreversible", "docs", GO, 40)
    _rec(sd, cfg, "f0", "unknown", "docs", GO, 41)
    return sd, cfg


def _order(rows):
    return [(r["qkind"], r["area"]) for r in rows]


def test_exact_order_and_measures(tmp_path):
    sd, cfg = _fixture(tmp_path)
    rows = RD.suggest(sd, cfg, edges={"9": ["1"], "8": ["9"], "7": ["20"]}, now=NOW)
    assert _order(rows) == [("needs_decision", "docs"), ("needs_decision", "ci"), ("needs_decision", "api"),
                            ("merge_conflict", "docs")]
    docs = rows[0]
    # goals 8 and 9 are blocked by goal 1 (one directly, one through a chain)
    assert (docs["parked"], docs["blocked"], docs["agreement"], docs["option"]) == (4, 2, 1.0, GO)
    assert docs["hours"] == 4 * 48.0 and docs["score"] == 14.0  # 4 parks + 192 h / 24 + 2 blocked
    ci = rows[1]
    assert (ci["parked"], ci["blocked"], ci["option"], ci["coverage"]) == (2, 1, WAIT, "own-rule")
    assert rows[2]["coverage"] == "any-area" and rows[2]["option"] == GO


def test_hard_stop_and_unknown_never_suggested(tmp_path):
    sd, cfg = _fixture(tmp_path)
    assert {r["qkind"] for r in RD.suggest(sd, cfg, now=NOW)} == {"needs_decision", "merge_conflict"}


def test_same_answer_kind_is_one_any_area_rule_and_differing_area_gets_its_own(tmp_path):
    sd, cfg = _fixture(tmp_path)
    rows = RD.suggest(sd, cfg, now=NOW)
    assert RD.proposals(rows) == [("needs_decision", "*"), ("needs_decision", "ci"), ("merge_conflict", "*")]


def test_closed_gate_suggests_nothing(tmp_path):
    sd, _ = _fixture(tmp_path)
    assert RD.suggest(sd, _cfg(rules=False), now=NOW) == [] and RD.suggest(sd, None) == []


def test_covered_pairs_are_flagged_and_not_proposed(tmp_path):
    sd, cfg = _fixture(tmp_path)
    RB.propose(sd, {"qkind": "needs_decision", "area": "*", "option": GO, "reason": "r"}, cfg, now=T0)
    rows = RD.suggest(sd, cfg, now=NOW)
    assert [r["covered"] for r in rows if r["qkind"] == "needs_decision"] == [True] * 3
    assert RD.proposals(rows) == [("merge_conflict", "*")]


def test_propose_drafts_proposed_only_and_never_approves(tmp_path):
    sd, cfg = _fixture(tmp_path)
    for i in range(6):                                     # enough answers and levels for the exam
        for area in ("docs", "api"):
            _rec(sd, cfg, "x%s%d" % (area, i), "needs_decision", area, GO, 100 + i)
    pathlib.Path(sd, "config.json").write_text(json.dumps(cfg))
    r = subprocess.run([sys.executable, str(SCRIPTS / "rule_draft.py"), "suggest", sd, "--propose", "1"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode == 0
    assert all(x["status"] != "approved" for x in RB.load(sd, cfg))
    assert "propose needs_decision / *:" in r.stdout


def test_doctor_row_when_on_and_nothing_when_off(tmp_path):
    spec = importlib.util.spec_from_file_location("doctor_t1513", ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
    doc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doc)
    sd, cfg = _fixture(tmp_path)
    on = doc._rulebook_state(cfg, sd)
    assert "uncovered rule pairs" in on and "needs_decision / docs" in on
    off = _cfg(rules=False)
    assert "uncovered rule pairs" not in doc._rulebook_state(off, sd)


def _fake_gh_dir(tmp_path):
    """A fake `gh` that answers every issue-list call with two open issues; goal 9 is blocked by goal 1."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    issues = [{"number": 9, "title": "t", "body": "Blocked by: #1", "state": "open", "labels": []},
              {"number": 2, "title": "t", "body": "nothing", "state": "open", "labels": []}]
    gh = bindir / "gh"
    gh.write_text("#!%s\nimport json, sys\nprint(json.dumps(%r))\n" % (sys.executable, issues))
    gh.chmod(0o755)
    return bindir


def test_scenario_cli_suggest_on_a_temp_repo_with_fake_gh(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    sd, cfg = _fixture(repo)
    cfg["discovery"] = {"source": "github"}
    pathlib.Path(sd, "config.json").write_text(json.dumps(cfg))
    env = dict(os.environ, PATH=str(_fake_gh_dir(tmp_path)) + os.pathsep + os.environ["PATH"])
    r = subprocess.run([sys.executable, str(SCRIPTS / "rule_draft.py"), "suggest", sd],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env, cwd=str(repo))
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].startswith("needs_decision / docs: score ")
    assert "blocks 1" in lines[0] and "one any-area rule (*)" in lines[0]
    assert any(l.startswith("needs_decision / ci:") and "needs its own rule" in l for l in lines)
    assert not any("irreversible" in l or "unknown" in l for l in lines)
    assert not (pathlib.Path(sd) / "rulebook").exists() and RB.load(sd, cfg) == []   # suggest alone writes nothing


def test_the_25_rule_shape_stays_valid(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg()
    for q, opt in (("quota", "wait it out"), ("merge_conflict", "rebase it"), ("no_evidence", "add a test"),
                   ("dependency", "wait for it"), ("reviewer_down", "fix and re-run the reviewer")):
        for i in range(5):
            RB.propose(sd, {"qkind": q, "area": "area%d" % i, "option": opt, "reason": "r"}, cfg, now=T0)
    RB.propose(sd, {"qkind": "needs_decision", "area": "*", "option": GO, "reason": "r"}, cfg, now=T0)
    assert len(RB.load(sd, cfg)) == 26 and RB.invalid(sd, cfg) == []
