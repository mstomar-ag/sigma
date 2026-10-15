"""#1495: one comparison key for answers, rules, comparisons and drafts; every option declares its meaning."""
import json
import os
import pathlib
import subprocess
import sys

import test_unpark as tu
import test_decision_record as tdr

_mod = tu._mod
SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
U, DR, AP, RB, DS, AU, RD = (_mod(n) for n in ("unpark", "decision_record", "applier", "rulebook",
                                                "decision_store", "autonomy", "rule_draft"))

RULES25 = [("quota", "wait it out"), ("merge_conflict", "rebase it"), ("no_evidence", "add a test"),
           ("dependency", "wait for it"), ("reviewer_down", "fix and re-run the reviewer")]


def _cfg(**over):
    cfg = tdr._on(tu._config())
    cfg["rulebook"]["rules"] = {"enabled": True, "draft": {"min_held_out": 1}}
    cfg["rulebook"]["autonomy"] = {"enabled": True, "owners": ["boss"],
                                   "levels": {"l1_min_answers": 2, "l2_min_answers": 4, "l2_agreement": 0.75}}
    cfg["rulebook"].update(over)
    return cfg


def _parked(kind, extra=()):
    return tu._view(comments=[tu._park("hit a wall\nsigma-qkind: " + kind), *extra])


# ---- the key ---------------------------------------------------------------------------------

def test_normalise_and_route_answer_table():
    for raw, want in [("Wait it out", "wait it out"), ("  WAIT   it out (recommended)", "wait it out"),
                      ("retry now (Recommended)", "retry now"), ("sleep on it", "free")]:
        assert U.route_answer("quota", {"route": raw}) == want
    assert U.route_answer("quota", {}) is None and U.route_answer("needs_decision", {"decision": "x"}) is None


def test_key_of_prefers_answer_then_bank_reason_then_choice():
    assert DR.key_of({"answer": "wait it out", "choice": "start it again"}) == "wait it out"
    # M-07: a record made before `answer` existed: the reason is the route answer
    assert DR.key_of({"qkind": "quota", "reason": "Wait it out", "choice": "start it again"}) == "wait it out"
    assert DR.key_of({"qkind": "quota", "reason": "because", "choice": "start it again"}) == "start it again"
    assert DR.key_of({"qkind": "needs_decision", "reason": "merge", "choice": "leave it parked"}) == "leave it parked"
    assert DR.key_of({}) is None


def test_validate_refuses_a_non_bank_option_and_unknown_kind():
    def rule(kind, opt):
        return {"id": "x", "qkind": kind, "area": "repo", "option": opt, "status": "proposed",
                "created": "2026-10-01T00:00:00+00:00"}
    ok, why = RB.validate(rule("quota", "merge"))
    assert not ok and "not an option" in why
    ok, why = RB.validate(rule("unknown", "start it again"))
    assert not ok and "not learnable" in why
    assert RB.validate(rule("needs_decision", "merge"))[0] is False


def test_the_25_rule_shape_stays_valid_and_sealed(tmp_path):
    cfg = _cfg()
    sd = str(tmp_path / ".sdlc")
    for kind, opt in RULES25:
        path = RB.propose(sd, {"qkind": kind, "area": "repo", "option": opt}, cfg)
        assert path, (kind, opt)
    rid = [r["id"] for r in RB.load(sd, cfg)]
    assert len(rid) == 5
    for r in rid:
        RB.approve(sd, r, "boss", cfg)
    assert all(r["status"] == "approved" for r in RB.load(sd, cfg))   # sealed: an unsealed one reads proposed


def test_option_gesture_table_covers_every_bank_option():
    for kind in U.QUESTIONS:
        gestures = U.option_gestures(kind)
        assert set(gestures) == set(U.valid_options(kind)), kind
        for opt, g in gestures.items():
            assert AP.option_gesture(kind, opt) == g and g in ("unpark", "keep-parked", "none")
    assert AP.option_gesture("needs_decision", "start it again") == "none"
    assert AP.option_gesture("needs_decision", "leave it parked") == "keep-parked"
    assert AP.option_gesture("dependency", "wait for it") == "keep-parked"
    assert AP.option_gesture("quota", "merge") is None
    assert AP.option_gesture("reviewer_down", "fix and re-run the reviewer") == "none"


def test_a_not_unparkable_option_opens_no_ask(tmp_path):
    cfg = _cfg()
    cfg["rulebook"]["applier"] = {"enabled": True}
    sd = str(tmp_path / ".sdlc")
    for kind, opt in (("dependency", "wait for it"), ("reviewer_down", "fix and re-run the reviewer")):
        RB.approve(sd, RB.propose(sd, {"qkind": kind, "area": "repo", "option": opt}, cfg) and
                   [r["id"] for r in RB.load(sd, cfg) if r["qkind"] == kind][0], "boss", cfg)
        assert AP.open_for_park(sd, cfg, 5, kind, source=tu._mod("sources")) is None


# ---- the scenario through the real verbs --------------------------------------------------------

def _resolve(tmp_path, number, kind, route, decision="unpark", extra=(), capsys=None):
    answers = tmp_path / ("a%s.json" % number)
    answers.write_text(json.dumps({"answers": {"route": route, "resume": "start it again"}}))
    run = tu._runner(views={str(number): tu._view(number=number, comments=[
        tu._park("hit a wall\nsigma-qkind: " + kind), *extra])})
    code = U.main(["unpark.py", "resolve", str(tmp_path), str(number), "--answers", str(answers),
                   "--decision", decision], run=run)
    return code, run


def _cfg_file(tmp_path, cfg):
    (tmp_path / "config.json").write_text(json.dumps(cfg))


def test_fourteen_resolves_agree_reach_level_two_then_draft_and_show(tmp_path, capsys):
    tmp_path = tmp_path / "repo" / ".sdlc"   # the rulebook file sits beside the state dir: keep it private
    tmp_path.mkdir(parents=True)
    cfg = _cfg()
    _cfg_file(tmp_path, cfg)
    for n in range(1, 15):
        code, _ = _resolve(tmp_path, n, "merge_conflict", "Rebase it (recommended)")
        assert code == 0
    recs = [r for r in DS.list_by(str(tmp_path), cfg, qkind="merge_conflict")]
    assert len(recs) == 14 and all(r["answer"] == "rebase it" for r in recs)
    watch = AU._facts(str(tmp_path), "watch-")
    assert watch and all(w["would"] == w["actual"] == "rebase it" for w in watch)
    assert AU.level(str(tmp_path), "merge_conflict", "repo", cfg) >= 2
    drafted = RD.draft(str(tmp_path), "merge_conflict", "repo", cfg)
    assert drafted["status"] == "drafted" and drafted["option"] == "rebase it"
    assert "UNPARK" in drafted["meaning"]
    rid = os.path.basename(drafted["path"]).rsplit(".", 1)[0]
    r = subprocess.run([sys.executable, str(SCRIPTS / "rule_draft.py"), "show", str(tmp_path), RB.load(
        str(tmp_path), cfg)[0]["id"]], capture_output=True, text=True, cwd=str(tmp_path))
    assert r.returncode == 0 and "UNPARK" in r.stdout, (r.stdout, r.stderr, rid)


def test_the_same_issue_parked_again_asks_the_route_question_again(tmp_path):
    cfg = _cfg()
    u = U
    first = u.render_block({"route": "rebase it", "resume": "start it again"}, config=cfg, park="c0")
    again = tu._view(body="Do it.\n\n" + first, comments=[tu._park("a\nsigma-qkind: merge_conflict"),
                                                         tu._park("b\nsigma-qkind: merge_conflict")])
    run = tu._runner(views={"5": again})
    brief = u.brief(str(tmp_path), cfg, 5, run=run)
    assert [q["id"] for q in brief["questions"]] == ["route", "resume"]
    same = tu._view(body="Do it.\n\n" + first, comments=[tu._park("a\nsigma-qkind: merge_conflict")])
    brief = u.brief(str(tmp_path), cfg, 5, run=tu._runner(views={"5": same}))
    assert brief["questions"] == []


def test_resolving_the_same_park_twice_counts_once(tmp_path):
    cfg = _cfg()
    _cfg_file(tmp_path, cfg)
    for _ in range(2):
        _resolve(tmp_path, 5, "merge_conflict", "rebase it", decision="keep-parked")
    recs = DS.list_by(str(tmp_path), cfg, qkind="merge_conflict")
    assert len(recs) == 2 and [bool(r.get("repeat")) for r in recs].count(True) == 1
    assert AU._state(str(tmp_path), "merge_conflict", "repo", cfg)["answers"] == 1


def test_a_route_kind_with_no_route_answer_warns(tmp_path, capsys):
    cfg = _cfg()
    _cfg_file(tmp_path, cfg)
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps({"answers": {"resume": "start it again"}}))
    run = tu._runner(views={"5": _parked("merge_conflict")})
    U.main(["unpark.py", "resolve", str(tmp_path), "5", "--answers", str(answers), "--decision", "unpark"], run=run)
    assert "no route answer" in capsys.readouterr().out


def test_dependency_wait_for_it_posts_no_sweep_opt_out(tmp_path):
    cfg = _cfg()
    _cfg_file(tmp_path, cfg)
    _, run = _resolve(tmp_path, 5, "dependency", "wait for it", decision="keep-parked")
    bodies = tdr._bodies(run, 5)
    assert bodies and not any(_mod("auto_unpark").keep_parked_comment("x")[:20] in b for b in bodies)
    _, run = _resolve(tmp_path, 6, "dependency", "drop this", decision="keep-parked")
    assert any(_mod("auto_unpark").keep_parked_comment("x")[:20] in b for b in tdr._bodies(run, 6))


# ---- rekey, promote/demote, free, public -------------------------------------------------------

def test_rekey_dry_run_then_apply(tmp_path, capsys):
    cfg = _cfg()
    _cfg_file(tmp_path, cfg)
    sd = str(tmp_path)
    for _ in range(3):
        AU.record_watch(sd, "quota", "repo", "wait it out", "start it again", cfg)
    AU.record_watch(sd, "quota", "repo", "wait it out", "wait it out", cfg)
    AU.record_watch(sd, "needs_decision", "repo", "start it again", "start it again", cfg)
    assert AU.main(["autonomy.py", "rekey", sd]) == 0
    out = capsys.readouterr().out
    assert "quota / repo: 3 comparisons would be removed" in out and len(AU._facts(sd, "watch-")) == 5
    assert AU.main(["autonomy.py", "rekey", sd, "--apply"]) == 0
    assert len(AU._facts(sd, "watch-")) == 2


def test_promote_and_demote_records_do_not_feed_needs_decision(tmp_path):
    cfg = _cfg()
    sd = str(tmp_path)
    for g in ("promote", "demote", "promote"):
        DS.append(sd, {"how": "human", "gesture": g, "qkind": "needs_decision", "area": "repo",
                       "choice": g}, cfg)
    DS.append(sd, {"how": "human", "gesture": "unpark", "qkind": "needs_decision", "area": "repo",
                   "choice": "start it again"}, cfg)
    assert AU._state(sd, "needs_decision", "repo", cfg)["answers"] == 1
    assert DR.would_for(sd, {"how": "human", "gesture": "promote", "qkind": "needs_decision",
                             "area": "repo", "choice": "promote"}, cfg) is None


def test_free_answers_never_predict_or_agree(tmp_path):
    cfg = _cfg()
    sd = str(tmp_path)
    for i in range(5):
        DS.append(sd, {"how": "human", "gesture": "unpark", "qkind": "quota", "area": "repo",
                       "choice": "start it again", "answer": "free"}, cfg)
    rec = {"how": "human", "gesture": "unpark", "qkind": "quota", "area": "repo", "choice": "x", "answer": "free"}
    assert DR.would_for(sd, rec, cfg) is None
    assert not AU._agrees({"would": "free", "actual": "free"})
    assert RD._human(sd, "quota", "repo", cfg) == {}


def test_public_visibility_writes_a_comparison_only_for_bank_options(tmp_path):
    cfg = _cfg()
    cfg["rulebook"]["records"]["repo_visibility"] = "public"
    sd = str(tmp_path)
    ok = {"qkind": "quota", "area": "repo", "answer": "wait it out", "choice": "start it again"}
    DR.record_comparison(sd, ok, "wait it out", cfg)
    DR.record_comparison(sd, {**ok, "answer": "free"}, "wait it out", cfg)
    assert len(AU._facts(sd, "watch-")) == 1


def test_unknown_is_capped_at_level_one(tmp_path):
    cfg = _cfg()
    sd = str(tmp_path)
    for i in range(8):
        DS.append(sd, {"how": "human", "gesture": "unpark", "qkind": "unknown", "area": "repo", "choice": "a"}, cfg)
        AU.record_watch(sd, "unknown", "repo", "a", "a", cfg)
    row = AU._state(sd, "unknown", "repo", cfg)
    assert row["level"] <= 1 and "not learnable" in AU._describe(row)


def test_gates_closed_resolve_is_unchanged(tmp_path):
    """Records/autonomy off: the comment is exactly the old one and no record or answer is written."""
    cfg = tu._config()
    u = U
    run = tu._runner(views={"5": _parked("merge_conflict")})
    out = u.resolve(str(tmp_path), cfg, 5, {"route": "rebase it", "resume": "start it again"}, "unpark", run=run)
    assert out["outcome"] == "unparked"
    assert not (tmp_path / "decisions").exists() and DS.list_by(str(tmp_path), cfg) == []
    body = next(c[c.index("--body") + 1] for c in run.calls if c[:2] == ["issue", "edit"] and "--body" in c)
    assert "sigma-park" not in body


# ---- the same scenario as a person runs it: real CLI processes, a temp git repo, a fake gh ----------

_FAKE_GH = '''#!/usr/bin/env python3
import json, os, re, sys
state = os.environ["FAKE_GH_STATE"]
args = sys.argv[1:]
open(state + ".log", "a").write(json.dumps(args) + "\\n")
def load():
    return json.load(open(state))
sys.path.insert(0, os.environ["FAKE_GH_TESTS"])
import gqlfake
swapped = gqlfake.swap(args, labels=set())
if swapped is not None:
    print(swapped)
    sys.exit(0)
if args[:1] == ["api"] and "graphql" not in args:
    m = re.search(r"issues/(\\d+)(/comments)?$", args[1])
    if m and "GET" in args:
        s = load()
        n = m.group(1)
        if m.group(2):
            print(json.dumps(s["comments"].get(n, [])) if "page=1" in args else "[]")
        else:
            print(json.dumps({"number": int(n), "title": "g", "body": s["bodies"].get(n, ""), "state": "open",
                              "labels": [{"name": "sdlc:parked"}], "comments": len(s["comments"].get(n, [])),
                              "user": {"login": "boss"}}))
        sys.exit(0)
    if m and "PATCH" in args:
        s = load()
        for i, a in enumerate(args):
            if a.startswith("body="):
                s["bodies"][m.group(1)] = a[5:]
        json.dump(s, open(state, "w"))
print("{}")
'''


def test_cli_scenario_fourteen_resolves_then_draft_and_show(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    sd = repo / ".sdlc"
    sd.mkdir()
    cfg = _cfg()
    cfg["discovery"] = {"source": "github", "github": {"repo": "acme/widget"}}
    (sd / "config.json").write_text(json.dumps(cfg))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(_FAKE_GH)
    gh.chmod(0o755)
    state = tmp_path / "state.json"
    park = {"body": "<!-- sigma-park -->\nhit a wall\nsigma-qkind: merge_conflict",
            "created_at": "2026-10-01T00:00:00Z", "id": 11, "author_association": "OWNER"}
    state.write_text(json.dumps({"bodies": {}, "comments": {str(n): [park] for n in range(1, 15)}}))
    env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
           "FAKE_GH_STATE": str(state), "FAKE_GH_TESTS": str(pathlib.Path(__file__).resolve().parent), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("SIGMA_LEAK_PATTERNS", None)

    def cli(script, *a):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *a], capture_output=True, text=True,
                              cwd=str(repo), env=env)

    for n in range(1, 15):
        ans = tmp_path / ("a%d.json" % n)
        ans.write_text(json.dumps({"answers": {"route": "Rebase it (recommended)", "resume": "start it again"}}))
        r = cli("unpark.py", "resolve", str(sd), str(n), "--answers", str(ans), "--decision", "unpark")
        assert r.returncode == 0, (r.stdout, r.stderr)
    recs = DS.list_by(str(sd), cfg, qkind="merge_conflict")
    assert len(recs) == 14 and all(x["answer"] == "rebase it" for x in recs)
    assert AU.level(str(sd), "merge_conflict", "repo", cfg) >= 2
    r = cli("rule_draft.py", "draft", str(sd), "merge_conflict", "repo")
    assert r.returncode == 0 and "UNPARK" in r.stdout, (r.stdout, r.stderr)
