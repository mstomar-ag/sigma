"""Goal 1500, part B: the applier waits for evidence that is behind the default branch (M-10), approving
names the approver and refuses an unconfirmed identity, the doctor shows the three new rows, and one
scenario runs the real verbs on a temporary repository with a fake gh."""
import datetime
import importlib.util
import json
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
T0 = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
MIN = datetime.timedelta(minutes=1)


def _mod(name, path=None):
    p = path or SCRIPTS / (name + ".py")
    spec = importlib.util.spec_from_file_location(p.stem + "_t1500b", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


AP, AU, DS, RB, RD = (_mod(n) for n in ("applier", "autonomy", "decision_store", "rulebook", "rule_draft"))
DOC = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py")


def _cfg(owners=("boss",), assignee="boss"):
    return {"discovery": {"github": {"assignee": assignee}},
            "rulebook": {"records": {"enabled": True}, "rules": {"enabled": True},
                         "autonomy": {"enabled": True, "owners": list(owners),
                                      "applier": {"enabled": True, "window_minutes": 60,
                                                  "max_auto_applied_per_day": 100, "spot_check_one_in": 1000000},
                                      "levels": {"l1_min_answers": 2, "l2_min_answers": 4, "l2_agreement": 0.75,
                                                 "l3_min_answers": 8, "l3_agreement": 0.9,
                                                 "l4_min_answers": 16, "l4_agreement": 0.99}}}}


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, check=True).stdout


def _repo(tmp_path):
    """-> (repo, other_clone): origin has main; both clones track it."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo, other = tmp_path / "repo", tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    (repo / "README").write_text("x")
    git(repo, "checkout", "-q", "-B", "main")
    git(repo, "add", "README")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True)
    git(repo, "remote", "set-head", "origin", "main")
    return repo, other


class Src:
    goal_label, parked_label, goal_blocked_label, in_progress_label = "sdlc:goal", "sdlc:parked", "sdlc:blocked", "sdlc:in-progress"
    project_enabled = False

    def __init__(self):
        self.swaps, self.notes = [], []

    def _swap_labels(self, issue, add, remove):
        self.swaps.append((issue, add, remove))

    def _read_issue(self, goal, fields, comment_limit=None):
        return {"labels": [self.parked_label]}

    def note(self, issue, text):
        self.notes.append((issue, text))


def _train(sd, cfg, qkind="merge_conflict", area="docs", option="rebase it", n=8):
    for _ in range(n):
        DS.append(sd, {"how": "human", "qkind": qkind, "area": area, "choice": option}, cfg)
        AU.record_watch(sd, qkind, area, option, option, cfg, now=T0, machine="m1")


def _approved_rule(sd, cfg, qkind="merge_conflict", area="docs", option="rebase it"):
    RB.propose(sd, {"qkind": qkind, "area": area, "option": option, "reason": "low risk"}, cfg, now=T0)
    rid = RB.load(sd, cfg)[-1]["id"]
    RB.approve(sd, rid, "boss", cfg, now=T0)
    return rid


def _applier_ready(tmp_path):
    repo, other = _repo(tmp_path)
    sd, cfg = str(repo / ".sdlc"), _cfg()
    _train(sd, cfg)
    _approved_rule(sd, cfg)
    AP.open_ask(sd, 7, "merge_conflict", "docs", ["rebase it", "start fresh"], cfg, T0)
    git(repo, "add", ".sdlc")
    git(repo, "commit", "-q", "-m", "evidence")
    git(repo, "push", "-q", "origin", "main")
    git(other, "pull", "-q", "origin", "main")
    return repo, other, sd, cfg


def test_applier_acts_when_evidence_is_current(tmp_path):
    repo, other, sd, cfg = _applier_ready(tmp_path)
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 61 * MIN, source=Src())] == ["done"]


def test_applier_refuses_while_evidence_is_behind_default_branch(tmp_path):
    repo, other, sd, cfg = _applier_ready(tmp_path)
    veto = other / ".sdlc" / "autonomy"
    veto.mkdir(parents=True, exist_ok=True)
    (veto / "veto-x.json").write_text(json.dumps({"rid": "r", "qkind": "merge_conflict", "area": "elsewhere"}))
    git(other, "add", ".sdlc")
    git(other, "commit", "-q", "-m", "a veto from another machine")
    git(other, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    src = Src()
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    assert src.swaps == [] and src.notes == []
    assert [a["kind"] for a in acts] == ["behind"] and acts[0]["result"] == "skipped"
    assert "autonomy" in acts[0]["reason"]
    git(repo, "merge", "-q", "--ff-only", "origin/main")       # pulled: it acts again
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 62 * MIN, source=src)] == ["done"]


def test_not_a_git_repository_does_not_block(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg()
    _train(sd, cfg)
    _approved_rule(sd, cfg)
    AP.open_ask(sd, 7, "merge_conflict", "docs", ["rebase it", "start fresh"], cfg, T0)
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 61 * MIN, source=Src())] == ["done"]


def _setup_rule(tmp_path, monkeypatch, ident):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg()
    (tmp_path / ".sdlc").mkdir()
    RB.propose(sd, {"qkind": "merge_conflict", "area": "docs", "option": "rebase it", "reason": "r"}, cfg, now=T0)
    monkeypatch.setattr(RD, "_identity", lambda config: ident)
    return sd, cfg, RB.load(sd, cfg)[0]["id"]


def test_approve_prints_the_approving_login(tmp_path, monkeypatch):
    sd, cfg, rid = _setup_rule(tmp_path, monkeypatch, {"login": "boss", "confirmed": True})
    ok, line = RD.approve(sd, rid, cfg)
    assert ok and "boss" in line and rid in line


def test_approve_refuses_unconfirmed_identity(tmp_path, monkeypatch):  # NEGATIVE
    sd, cfg, rid = _setup_rule(tmp_path, monkeypatch, {"login": "boss", "confirmed": False, "detail": "wrong account"})
    ok, line = RD.approve(sd, rid, cfg)
    assert not ok and "wrong account" in line
    assert RB.load(sd, cfg)[0]["status"] == "proposed"


def test_doctor_applier_row_warns_when_owners_resolve_to_nothing():
    empty = DOC._applier_state(_cfg(owners=()), None, T0)
    assert "WARNING" in empty and "owners" in empty
    assert "owners" not in DOC._applier_state(_cfg(), None, T0)


def test_doctor_rulebook_row_shows_local_commits_not_on_default_branch(tmp_path):
    repo, other = _repo(tmp_path)
    sd, cfg = repo / ".sdlc", _cfg()
    assert "not on the default branch" not in DOC._rulebook_state(cfg, sd)
    _train(str(sd), cfg, n=2)
    git(repo, "add", ".sdlc")
    git(repo, "commit", "-q", "-m", "local evidence")
    out = DOC._rulebook_state(cfg, sd)
    assert "1 local commit" in out and "not on the default branch" in out


def test_doctor_shows_unreachable_level(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg()
    for _ in range(4):
        DS.append(sd, {"how": "human", "qkind": "merge_conflict", "area": "docs", "choice": "rebase it"}, cfg)
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=200)
    for _ in range(3):
        AU.record_watch(sd, "merge_conflict", "docs", "rebase it", "rebase it", cfg, now=old, machine="m1")
    out = DOC._autonomy_state(cfg, sd)
    assert "unreachable" in out and "needs 4 comparisons, 0 recorded in the last" in out


def _fake_gh(monkeypatch, login="boss"):
    """The real identity check, fed by a fake `gh api user` (the suite refuses a live gh call)."""
    def run(args):
        return (0, login + "\n", "") if args[:2] == ["api", "user"] else (1, "", "unexpected " + " ".join(args))
    monkeypatch.setattr(RD, "_identity", lambda config: RD._load("cross_repo").identity(config, run=run))


SHAPE = {"quota": "wait it out", "merge_conflict": "rebase it", "no_evidence": "add a test",
         "dependency": "wait for it", "reviewer_down": "fix and re-run the reviewer"}


def test_scenario_real_verbs_on_a_temp_repository(tmp_path, monkeypatch, capsys):
    _fake_gh(monkeypatch)
    repo, other, sd, cfg = _applier_ready(tmp_path)
    (repo / ".sdlc" / "config.json").write_text(json.dumps(cfg))
    RB.propose(sd, {"qkind": "quota", "area": "docs", "option": "wait it out", "reason": "r"}, cfg, now=T0)
    rid = [r for r in RB.load(sd, cfg) if r["qkind"] == "quota"][0]["id"]
    assert RD.main(["approve", sd, rid]) == 0
    assert "boss" in capsys.readouterr().out
    for q, opt in SHAPE.items():                      # the 25-rule shape: 5 kinds x 5 areas
        for i in range(5):
            if (q, i) != ("quota", 0) and not (q == "merge_conflict" and i == 0):
                RB.propose(sd, {"qkind": q, "area": "area%d" % i, "option": opt, "reason": "r"}, cfg, now=T0)
    for r in RB.load(sd, cfg):
        if r["status"] != "approved":
            assert RD.approve(sd, r["id"], cfg)[0], r
    rules = RB.load(sd, cfg)
    assert len(rules) >= 25 and all(RB._acts(r) for r in rules)
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 61 * MIN, source=Src())] == ["done"]
    assert "last tick" in DOC._applier_state(cfg, sd, T0 + 62 * MIN)
    git(repo, "add", ".sdlc")
    git(repo, "commit", "-q", "-m", "approved rules, not pushed")
    assert "not on the default branch" in DOC._rulebook_state(cfg, repo / ".sdlc")
