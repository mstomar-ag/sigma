"""#1580: `label_drain.py answer` applies a person's answers to the up-front questions.

A stateful fake `gh` (the arm tests' fake, extended with comments and body edits) serves the reads and
APPLIES the writes, so the tests assert on recorded writes and on the stored decision records.
"""
import json
import pathlib
import re

import pytest

import test_decision_record as tdr
import test_unpark as tu
from test_label_drain_arm import DW, LBL, Fake as ArmFake, _issue as _arm_issue, _mod, _repo

PAID = "Paid step: runpodctl create pod for the render\n\n" + DW
KIND_DEP = "Parked by Sigma: waiting on the other issue.\nsigma-qkind: dependency"
KIND_MC = "Parked by Sigma: the branch conflicts.\nsigma-qkind: merge_conflict"
ACTION = "runpodctl create pod for the render"


class Fake(ArmFake):
    """The arm fake plus comments (stored), body edits (stored) and paginated comment reads."""

    def __init__(self, issues, fail_label_writes=False, **kw):
        super().__init__(issues, **kw)
        self.fail_label_writes = fail_label_writes
        for i in issues:
            i.setdefault("_comments", [])

    def __call__(self, args):
        args = [str(a) for a in args]
        if self.fail_label_writes:
            joined = " ".join(args)
            if args[:2] == ["issue", "edit"] or "mutation" in joined \
                    or (args[0] == "api" and args[1].endswith("/labels")
                        and ("POST" in args or "DELETE" in args)):
                self.calls.append(args)
                raise RuntimeError("simulated label write failure")
        if args[0] == "api":
            import re
            path = next((a for a in args[1:] if a.startswith("repos/")), "")
            method = args[args.index("--method") + 1] if "--method" in args else "GET"
            m = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)(/comments)?", path)
            if m and int(m.group(1)) in self.by:
                n, issue = int(m.group(1)), self.by[int(m.group(1))]
                if m.group(2) and method == "GET":
                    self.calls.append(args)
                    return json.dumps(issue["_comments"])
                if m.group(2) and method == "POST":
                    self.calls.append(args)
                    body = [a for a in args if a.startswith("body=")][0][5:]
                    issue["_comments"].append({"body": body, "user": {"login": "alice"},
                                               "author_association": "OWNER",
                                               "created_at": "2026-01-%02dT00:00:00Z" % (len(issue["_comments"]) + 1)})
                    return json.dumps({"id": 1, "html_url": "x"})
                if not m.group(2) and method == "PATCH":
                    self.calls.append(args)
                    issue["body"] = [a for a in args if a.startswith("body=")][0][5:]
                    return json.dumps({"number": n})
                if not m.group(2) and method == "GET":
                    self.calls.append(args)
                    return json.dumps({k: v for k, v in issue.items() if not k.startswith("_")}
                                      | {"comments": len(issue["_comments"])})
        return super().__call__(args)

    def stored_comments(self, n):
        return [c["body"] for c in self.by[n]["_comments"]]

    def body(self, n):
        return self.by[n]["body"]


def issue(n, *labels, author="alice", assoc="OWNER", comments=(), **kw):
    d = _arm_issue(n, *labels, **kw)
    d["user"] = {"login": author}
    d["author_association"] = assoc
    d["_comments"] = [{"body": c, "user": {"login": "alice"}, "author_association": "OWNER",
                       "created_at": "2026-01-%02dT00:00:00Z" % (k + 1)} for k, c in enumerate(comments)]
    return d


def env(tmp_path, issues, records=True, spend=True, **fk_kw):
    base = _repo(tmp_path, "alice")
    cfgp = pathlib.Path(base) / "config.json"
    cfg = json.loads(cfgp.read_text())
    cfg["hard_stops"] = {"enabled": True}
    if spend:
        cfg["spend_approval"] = {"enabled": True, "approvers": ["alice"]}
    if records:
        cfg = tdr._on(cfg)
        cfg["rulebook"]["hard_stops"] = {"enabled": True}      # `hard_stop` reads the block inside `rulebook`
    cfgp.write_text(json.dumps(cfg))
    return _mod("label_drain"), base, Fake(issues, **fk_kw), cfg


def records(base):
    store = pathlib.Path(base) / "decisions"
    return [json.loads(p.read_text()) for p in sorted(store.glob("*.json")) if not p.name.startswith("_")]


def ask(ld, base, fk, answers, dry_run=False):
    return ld.answer(base, LBL, answers, dry_run=dry_run, run=fk)


# 1 -- refused whole ---------------------------------------------------------------------------

@pytest.mark.parametrize("answers", [
    {"bogus:9": "arm them", "label_set:" + LBL: "arm them"},
    {"label_set:" + LBL: "arm everything twice"},
    {"label_set:" + LBL: "arm them", "irreversible:99": "go ahead"},
    {"label_set:other-label": "arm them"},
])
def test_an_unknown_id_or_option_outside_the_bank_is_refused_whole_with_zero_writes(tmp_path, answers, capsys):
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL), issue(2, LBL)])
    f = tmp_path / "a.json"
    f.write_text(json.dumps(answers))
    assert ld.main(["label_drain.py", "answer", base, LBL, "--answers", str(f)], run=fk) == 2
    assert fk.writes() == [] and records(base) == []
    assert "nothing was written" in capsys.readouterr().err


# 2 -- arm route -------------------------------------------------------------------------------

def _two_unarmed():
    return [issue(1, LBL), issue(2, LBL, body="Blocked by #7\n\n" + DW), issue(7, "sdlc:blocked")]


def test_arm_only_the_ones_that_can_start_arms_exactly_that_subset_and_writes_one_record(tmp_path):
    ld, base, fk, _ = env(tmp_path, _two_unarmed())
    res = ask(ld, base, fk, {"label_set:" + LBL: "arm only the ones that can start"})
    assert res["errors"] == [] and res["results"][0]["outcome"] == "applied"
    assert "sdlc:goal" in fk._names(1) and "sdlc:goal" not in fk._names(2) and "sdlc:goal" not in fk._names(7)
    recs = records(base)
    assert len(recs) == 1
    r = recs[0]
    assert (r["qkind"], r["area"], r["choice"], r["scope"]) == ("label_set", "label-kind:" + ld.label_kind(LBL), "arm only the ones that can start",
                                                                "label:" + LBL)
    assert len(fk.comments()) == 1


def test_the_record_is_written_after_the_label_write(tmp_path):
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL)])
    ask(ld, base, fk, {"label_set:" + LBL: "arm them"})
    label_at = [i for i, a in enumerate(fk.calls) if a[:2] == ["api", "graphql"] and "mutation" in " ".join(a)]
    comment_at = [i for i, a in enumerate(fk.calls) if a[0] == "api" and a[1].endswith("/comments") and "POST" in a]
    assert label_at and comment_at and max(label_at) < min(comment_at)
    assert len(records(base)) == 1


def test_a_failed_label_swap_writes_no_record_and_no_comment(tmp_path):
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL)], fail_label_writes=True)
    res = ask(ld, base, fk, {"label_set:" + LBL: "arm them"})
    assert res["results"][0]["outcome"] == "failed"
    assert records(base) == [] and fk.comments() == []


def test_a_crash_between_the_label_write_and_the_record_writes_only_the_record(tmp_path):
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL)])
    ld.arm(base, LBL, apply=True, run=fk)                  # the effect landed, the record did not
    before = len(fk.writes())
    res = ask(ld, base, fk, {"label_set:" + LBL: "arm them"})
    assert res["errors"] == [] and len(records(base)) == 1
    assert not [a for a in fk.writes()[before:] if a[:2] == ["issue", "edit"]]


def test_a_recovered_label_question_records_a_goal_and_posts_its_comment_once(tmp_path):
    """#1725: the recovery lookup rebuilds the question with no members; the record must still carry a goal."""
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL), issue(2, LBL)])
    ld.arm(base, LBL, apply=True, run=fk)
    res = ask(ld, base, fk, {"label_set:" + LBL: "arm them"})
    recs = records(base)
    assert res["errors"] == [] and len(recs) == 1 and recs[0]["issue"] == 1
    assert len(fk.comments()) == 1
    ask(ld, base, fk, {"label_set:" + LBL: "arm them"})
    assert len(records(base)) == 1 and len(fk.comments()) == 1


def test_a_recovered_label_question_never_lands_on_an_outside_blocker(tmp_path):
    """#1725 review: the lowest number overall is the unlabelled blocker #5; the record must use label member #10."""
    ld, base, fk, _ = env(tmp_path, [issue(10, LBL, body="Blocked by #5\n\n" + DW), issue(12, LBL), issue(5, "sdlc:goal", assignees=("bob",))])
    ld.arm(base, LBL, apply=True, run=fk)
    ask(ld, base, fk, {"label_set:" + LBL: "arm them"})
    recs = records(base)
    assert len(recs) == 1 and recs[0]["issue"] == 10


def test_a_recovered_answer_with_every_member_closed_records_once_and_posts_no_comment(tmp_path):
    """#1751: all members close after arming; the record is stored with no issue and no comment is attempted."""
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL), issue(2, LBL)])
    ld.arm(base, LBL, apply=True, run=fk)
    for n in (1, 2):
        fk.by[n]["state"] = "closed"
    res = ask(ld, base, fk, {"label_set:" + LBL: "arm them"})
    assert len(records(base)) == 1 and fk.comments() == []
    assert not [l for r in res["results"] for l in r["lines"] if "not posted" in l]
    assert any("no open member" in l for r in res["results"] for l in r["lines"])
    ask(ld, base, fk, {"label_set:" + LBL: "arm them"})
    assert len(records(base)) == 1 and fk.comments() == []


# 3 -- parked member == the unpark path ----------------------------------------------------------

@pytest.mark.parametrize("kind_comment,qid,option,decision,slot", [
    (KIND_DEP, "dependency:1", "wait for it", "keep-parked", "route"),
    (KIND_MC, "merge_conflict:1", "rebase it", "unpark", "route"),
])
def test_a_parked_member_answer_equals_the_unpark_resolve_path(tmp_path, kind_comment, qid, option, decision, slot):
    mem = lambda: [issue(1, "sdlc:parked", LBL, comments=[kind_comment], assignees=("alice",))]
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(), b_dir.mkdir()
    ld, base_a, fk_a, cfg = env(a_dir, mem())
    ask(ld, base_a, fk_a, {qid: option})
    ld2, base_b, fk_b, cfg_b = env(b_dir, mem())
    u = _mod("unpark")
    r = u.resolve(base_b, cfg_b, 1, {slot: option}, decision, run=fk_b)
    assert r["outcome"] != "failed"
    assert fk_a._names(1) == fk_b._names(1)
    norm = lambda cs: [re.sub(r"`\d{8}T\d{6}-[0-9a-f]{8}`", "`ID`", c) for c in cs[1:]]   # [0] is the park comment
    assert norm(fk_a.stored_comments(1)) == norm(fk_b.stored_comments(1)) and len(fk_a.stored_comments(1)) == 2
    strip = lambda rec: {k: v for k, v in rec.items() if k not in ("id", "time", "scope", "qid")}
    ra, rb = records(base_a), records(base_b)
    assert len(ra) == len(rb) == 1 and strip(ra[0]) == strip(rb[0])
    assert ra[0]["scope"] == "label:" + LBL and ra[0]["qid"] == qid


# 4 -- spend -----------------------------------------------------------------------------------

def _paid(n, **kw):
    return issue(n, "sdlc:goal", LBL, assignees=("alice",), body=PAID, **kw)


def test_go_ahead_marks_only_approver_authored_members_and_names_the_rest(tmp_path):
    ld, base, fk, cfg = env(tmp_path, [_paid(6), _paid(8, author="mallory", assoc="CONTRIBUTOR")])
    res = ask(ld, base, fk, {"irreversible:6": "go ahead", "irreversible:8": "go ahead"})
    assert res["errors"] == []
    assert fk.body(6).splitlines()[0] == "sigma:spend-approved=" + LBL and fk.body(6).endswith(PAID)
    assert fk.body(8) == PAID                                   # not an approver: left as it is
    assert any("#8" in l and "not marked" in l for r in res["results"] for l in r["lines"])
    assert "#8" in ld.render_answer(res)
    sa, sources = _mod("spend_approval"), _mod("sources")
    src = sources.GitHubSource(cfg, run=fk)
    assert sa.check(base, "6", ACTION, cfg, src).startswith("APPROVED")
    assert sa.check(base, "6", ACTION, cfg, src).startswith("DENIED")
    assert sa.check(base, "8", ACTION, cfg, src).startswith("DENIED")


def test_do_not_and_change_the_plan_record_and_write_no_marker(tmp_path):
    ld, base, fk, _ = env(tmp_path, [_paid(6), _paid(8)])
    ask(ld, base, fk, {"irreversible:6": "do not", "irreversible:8": "change the plan"})
    assert fk.body(6) == PAID and fk.body(8) == PAID and fk.body_patches() == []
    assert sorted(r["choice"] for r in records(base)) == ["change the plan", "do not"]


def test_the_marker_token_is_derived_from_the_label():
    ld = _mod("label_drain")
    assert ld._token("run 1/ü") == "run-1--" or ld._token("run 1/ü").startswith("run-1")
    assert len(ld._token("a" * 100)) == 64 and ld._token("---") == "approved"


# 5 -- omitted stays open -----------------------------------------------------------------------

def test_a_question_left_out_stays_open_and_ready_flips_only_when_none_is_open(tmp_path):
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL), _paid(6)])
    first = ask(ld, base, fk, {"label_set:" + LBL: "arm them"})
    assert [q["id"] for q in first["open"]] == ["irreversible:6"] and first["ready"] is False
    assert "irreversible:6" in ld.render_answer(first) and "ready: false" in ld.render_answer(first)
    second = ask(ld, base, fk, {"irreversible:6": "go ahead"})
    assert second["open"] == [] and second["ready"] is True


# 6 -- idempotent -------------------------------------------------------------------------------

def test_answering_twice_records_once_and_writes_one_effect(tmp_path):
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL), _paid(6)])
    answers = {"label_set:" + LBL: "arm them", "irreversible:6": "go ahead"}
    ask(ld, base, fk, answers)
    n_writes, n_recs = len(fk.writes()), len(records(base))
    again = ask(ld, base, fk, answers)
    assert n_recs == len(records(base)) == 2 and len(fk.writes()) == n_writes
    assert [r["outcome"] for r in again["results"]] == ["already-recorded"] * 2
    assert fk.body(6).count("sigma:spend-approved") == 1


# 7 -- dry run ----------------------------------------------------------------------------------

def test_dry_run_prints_every_write_and_makes_none(tmp_path):
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL), _paid(6)])
    res = ask(ld, base, fk, {"label_set:" + LBL: "arm them", "irreversible:6": "go ahead"}, dry_run=True)
    text = ld.render_answer(res)
    assert fk.writes() == [] and records(base) == []
    assert "would" in text and "marker" in text and "would record" in text and "dry run" in text


# 8 -- the CLI path ------------------------------------------------------------------------------

def test_scenario_through_main(tmp_path, capsys):
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL), _paid(6)])
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"label_set:" + LBL: "arm them"}))
    assert ld.main(["label_drain.py", "answer", base, LBL, "--answers", str(f)], run=fk) == 0
    out = capsys.readouterr().out
    assert "[applied] label_set:" in out and "irreversible:6" in out and "ready: false" in out
    f.write_text(json.dumps({"irreversible:6": "go ahead"}))
    assert ld.main(["label_drain.py", "answer", base, LBL, "--answers", str(f), "--dry-run"], run=fk) == 0
    assert fk.body(6) == PAID
    assert ld.main(["label_drain.py", "answer", base, LBL], run=fk) == 2          # --answers is required
    assert ld.main(["label_drain.py", "answer", base, LBL, "--answers", str(tmp_path / "none.json")], run=fk) == 2


# 8 -- #1732: a later standalone plan agrees with answer --------------------------------------------

def test_plan_after_answer_has_no_open_questions(tmp_path):
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL), _paid(6)])
    ask(ld, base, fk, {"label_set:" + LBL: "leave them", "irreversible:6": "go ahead"})
    p = ld.plan(base, LBL, run=fk)
    assert p["questions"] == [] and p["ready"] is True


def test_ready_flips_in_plan_not_only_in_answer(tmp_path):
    ld, base, fk, _ = env(tmp_path, [issue(1, LBL), _paid(6)])
    assert ld.plan(base, LBL, run=fk)["ready"] is False
    ask(ld, base, fk, {"label_set:" + LBL: "leave them"})
    mid = ld.plan(base, LBL, run=fk)
    assert [q["id"] for q in mid["questions"]] == ["irreversible:6"] and mid["ready"] is False
    ask(ld, base, fk, {"irreversible:6": "go ahead"})
    assert ld.plan(base, LBL, run=fk)["ready"] is True


# 9 -- #1806: answering "which kind" declares the kind -----------------------------------------------

UNDECLARED_PARK = "Sigma parked this goal: should the export keep the legacy column?"


def _undeclared(tmp_path):
    ld, base, fk, cfg = env(tmp_path, [issue(1, "sdlc:parked", LBL, comments=[UNDECLARED_PARK],
                                             assignees=("alice",))])
    q = next(q for q in ld.plan(base, LBL, run=fk)["questions"] if q["id"] == "kind:1")   # the plan's own question
    return ld, base, fk, cfg, q


def _declared(fk, n=1):
    qk = _mod("qkind")
    return qk.resolve([{"body": b, "authorAssociation": "OWNER"} for b in fk.stored_comments(n)], goal=n)


def test_answering_which_kind_declares_that_kind_on_the_issue(tmp_path):
    ld, base, fk, _, q = _undeclared(tmp_path)
    assert _declared(fk) is None and "needs_decision" in q["options"]
    res = ask(ld, base, fk, {q["id"]: "needs_decision"})
    assert res["results"][0]["outcome"] == "applied"
    assert _declared(fk) == "needs_decision"
    assert any(c.startswith("Question kind set: needs_decision") for c in fk.stored_comments(1))


def test_dry_run_of_which_kind_says_what_it_would_declare_and_writes_nothing(tmp_path):
    ld, base, fk, _, q = _undeclared(tmp_path)
    res = ask(ld, base, fk, {q["id"]: "needs_decision"}, dry_run=True)
    assert "would declare kind needs_decision on #1" in ld.render_answer(res)
    assert fk.writes() == [] and _declared(fk) is None
