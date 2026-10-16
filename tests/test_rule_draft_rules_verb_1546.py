"""Goal 1546: `rule_draft.py rules <dir>` lists approved rules; the doctor row counts them per action."""
import datetime
import importlib.util
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
T0 = datetime.datetime(2026, 10, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _mod(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


RD, RB = _mod(SCRIPTS / "rule_draft.py", "rd_t1546"), _mod(SCRIPTS / "rulebook.py", "rb_t1546")
DOC = _mod(ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py", "doc_t1546")
CFG = {"rulebook": {"records": {"enabled": True}, "rules": {"enabled": True},
                    "autonomy": {"enabled": True, "owners": ["boss"]}}}
CLOSED = {"rulebook": {"records": {"enabled": True}, "rules": {"enabled": False}}}


def _repo(tmp_path, cfg=CFG):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    sd = tmp_path / ".sdlc"
    sd.mkdir()
    (sd / "config.json").write_text(json.dumps(cfg))
    return str(sd)


def _rule(sd, qkind, area, option, approve=True):
    path = RB.propose(sd, {"qkind": qkind, "area": area, "option": option, "reason": "r"}, CFG, now=T0)
    if approve:
        rid = [r for r in RB.load(sd, CFG) if r["qkind"] == qkind and r["area"] == area][0]["id"]
        RB.approve(sd, rid, "boss", CFG, now=T0)


def _three(sd):
    _rule(sd, "merge_conflict", "ci", "rebase it")
    _rule(sd, "needs_decision", "docs", "leave it parked")
    _rule(sd, "needs_decision", "api", "start it again")
    _rule(sd, "failing_check", "ci", "fix it here", approve=False)


def test_rules_lists_every_approved_rule_with_action(tmp_path, capsys):
    sd = _repo(tmp_path)
    _three(sd)
    assert RD.main(["rules", sd]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3
    joined = "\n".join(out)
    for want in ("merge_conflict", "ci", "rebase it", "unpark", "keep parked", "only recommends", "2026-10-11"):
        assert want in joined
    assert "fix it here" not in joined


def test_rules_empty_and_closed(tmp_path, capsys):
    sd = _repo(tmp_path)
    assert RD.main(["rules", sd]) == 0
    assert "no approved rules" in capsys.readouterr().out
    sd2 = _repo(tmp_path / "c", CLOSED) if (tmp_path / "c").mkdir() is None else None
    assert RD.main(["rules", sd2]) == 0
    assert "no approved rules" in capsys.readouterr().out


def test_doctor_row_counts_per_action_and_names_verb(tmp_path):
    sd = _repo(tmp_path)
    _three(sd)
    row = DOC._rulebook_state(CFG, sd)
    assert "actions: 1 unpark, 1 keep parked, 1 only recommends" in row
    assert "rule_draft.py rules" in row


def test_doctor_row_unchanged_without_approved_rules(tmp_path):
    sd = _repo(tmp_path)
    _rule(sd, "failing_check", "ci", "fix it here", approve=False)
    row = DOC._rulebook_state(CFG, sd)
    assert "actions:" not in row and "rule_draft.py rules" not in row
    closed = DOC._rulebook_state(CLOSED, sd)
    assert "actions:" not in closed
