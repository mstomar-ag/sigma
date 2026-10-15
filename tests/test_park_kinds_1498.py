"""#1498: every park declares a kind; one resolver reads it; deny-list parks are hard stops.

Fake gh only, no network. The CLI scenario drives the real verbs (`loop.py record ... parked`,
`unpark.py declare|list --undeclared|brief|resolve`) on a temp repo.
"""
import json
import pathlib
import subprocess
import importlib.util

import gqlfake
from test_unpark import _config, _mod as _umod

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
NEEDLE_TEXT = "add a rate limit to the login endpoint; needs a product call"


def _mod(name):
    return _umod(name)


# --------------------------------------------------------------------------- F-05 / M-08: loop

def _capture(monkeypatch, lp):
    seen = {}
    monkeypatch.setattr(lp, "_record", lambda *a, **k: seen.update(args=a, kw=k) or "parked")
    return seen


def test_pr_closed_unmerged_park_declares_needs_decision(monkeypatch):
    lp = _mod("loop")
    seen = _capture(monkeypatch, lp)
    monkeypatch.setattr(lp.work, "_record", lambda d, g: {"awaiting_merge": {}, "pr": "7"})
    monkeypatch.setattr(lp.work, "pr_landing_state", lambda *a: (lp.work.CLOSED_PR,))
    monkeypatch.setattr(lp.work, "stamp_merge_check", lambda *a, **k: None)
    lp._reconcile_one(".sdlc", {}, None, "5", None, 0, [])
    assert seen["kw"]["qkind"] == "needs_decision"


def test_needle_kind_is_explicit_for_internal_text_only():
    lp = _mod("loop")
    assert lp._needle_kind("hit a rate limit") == "quota"
    assert lp._needle_kind("free text nobody wrote a rule for") is None


def test_suggest_kind_needle_then_keywords_then_unknown():
    lp = _mod("loop")
    assert lp._suggest_kind("changes requested on the API") == "needs_decision"
    assert lp._suggest_kind("we should drop table users") == "irreversible"
    assert lp._suggest_kind("zzz") == "unknown"


def _github_loop(monkeypatch, tmp_path, run):
    lp = _mod("loop")
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    src = _mod("sources")
    monkeypatch.setattr(lp.sources, "get_source",
                        lambda d, c: lp.sources.GitHubSource(c, run=run, sdlc_dir=d))
    return lp, src


class _Repo:
    """A fake gh whose issue 5 keeps the comments written to it (authored as OWNER)."""

    def __init__(self, labels=("sdlc:parked",), comments=()):
        from test_unpark import _runner
        self.comments = [dict(c) for c in comments]
        self.labels = list(labels)
        outer = self

        class Views(dict):
            def get(self, n, default=None):
                return json.dumps({"number": int(n), "title": "A goal", "body": "", "state": "OPEN",
                                   "labels": [{"name": x} for x in outer.labels],
                                   "comments": outer.comments})
        self.inner = _runner(views=Views({"0": ""}), by_label={"sdlc:parked": json.dumps(
            [{"number": 5, "title": "A goal", "body": "", "labels": [{"name": "sdlc:parked"}]}])})

    def __call__(self, args):
        n0 = len(self.inner.calls)
        out = self.inner(args)
        for c in self.inner.calls[n0:]:
            if c[:2] == ["issue", "comment"] and "--body" in c:
                self.comments.append({"body": c[c.index("--body") + 1], "authorAssociation": "OWNER"})
        return out

    def bodies(self):
        return [c["body"] for c in self.comments]


def test_cli_record_parked_with_needle_text_stays_undeclared(monkeypatch, tmp_path, capsys):
    repo = _Repo(labels=("sdlc:goal",))
    lp, _ = _github_loop(monkeypatch, tmp_path, repo)
    assert lp.main(["loop.py", "record", str(tmp_path), "5", "parked", NEEDLE_TEXT]) == 0
    err = capsys.readouterr().err
    assert "no --qkind: this park cannot be learned from; suggested kind: quota" in err
    assert all("sigma-qkind:" not in b for b in repo.bodies())
    assert any(NEEDLE_TEXT in b for b in repo.bodies())


def test_cli_record_parked_with_qkind_writes_the_last_line(monkeypatch, tmp_path, capsys):
    repo = _Repo(labels=("sdlc:goal",))
    lp, _ = _github_loop(monkeypatch, tmp_path, repo)
    assert lp.main(["loop.py", "record", str(tmp_path), "5", "parked", NEEDLE_TEXT,
                    "--qkind", "quota"]) == 0
    assert "no --qkind" not in capsys.readouterr().err
    park = next(b for b in repo.bodies() if NEEDLE_TEXT in b)
    assert park.splitlines()[-1] == "sigma-qkind: quota"


# --------------------------------------------------------------------------- F-06: resolver

def _park_c(text, assoc="OWNER"):
    return {"body": _mod("sources").PARK_COMMENT_PREFIX + text, "authorAssociation": assoc}


def _decl(kind, assoc, login="someone"):
    return {"body": "Question kind set: %s\nsigma-qkind: %s" % (kind, kind),
            "authorAssociation": assoc, "author": {"login": login}}


def test_resolve_newest_trusted_line_wins(capsys):
    q = _mod("qkind")
    first = _park_c("x\n" + q.render_line("quota"))
    assert q.resolve([first]) == "quota"
    assert q.resolve([first, _decl("dependency", "OWNER")]) == "dependency"      # later owner declaration
    assert q.resolve([_decl("dependency", "OWNER"), _park_c("y\n" + q.render_line("review_cap"))]) == "review_cap"
    assert q.resolve([_park_c("no line at all")]) is None


def test_resolve_ignores_an_untrusted_declaration_with_a_note(capsys):
    q = _mod("qkind")
    park = _park_c("x\n" + q.render_line("quota"))
    assert q.resolve([park, _decl("irreversible", "NONE", "stranger")], goal="5") == "quota"
    assert "stranger" in capsys.readouterr().err
    assert q.resolve([_decl("irreversible", None)], goal="5") is None            # fail closed


def test_hardstop_line_round_trip():
    q = _mod("qkind")
    assert q.parse_hardstop("a\nsigma-hardstop: secret-shaped text\n") == "secret-shaped text"
    assert q.parse_hardstop("nothing") is None


# --------------------------------------------------------------------------- F-06: CLI scenario

def test_cli_scenario_park_declare_list_brief_resolve(monkeypatch, tmp_path, capsys):
    repo = _Repo(labels=("sdlc:goal",))
    lp, _ = _github_loop(monkeypatch, tmp_path, repo)
    u = _mod("unpark")
    assert lp.main(["loop.py", "record", str(tmp_path), "5", "parked", NEEDLE_TEXT]) == 0
    repo.labels = ["sdlc:parked"]
    capsys.readouterr()

    assert u.main(["unpark.py", "list", str(tmp_path), "--undeclared"], run=repo) == 0
    out = capsys.readouterr().out
    assert "#5" in out and "suggested: quota" in out

    brief = lambda: (u.main(["unpark.py", "brief", str(tmp_path), "5", "--json"], run=repo),
                     json.loads(capsys.readouterr().out))[1]
    b = brief()
    assert b["kind_declared"] is False and b["questions"][0]["id"] == "kind"
    assert b["questions"][0]["options"][0] == "quota"                            # suggestion first
    assert "needs_decision" in b["questions"][0]["options"]

    n_labels, n_calls = len(repo.inner.gql_calls), len(repo.inner.calls)
    n_events = len(list((tmp_path / "state").glob("**/*"))) if (tmp_path / "state").exists() else 0
    assert u.main(["unpark.py", "declare", str(tmp_path), "5", "needs_decision"], run=repo) == 0
    capsys.readouterr()
    assert repo.bodies()[-1] == "Question kind set: needs_decision\nsigma-qkind: needs_decision"
    assert len(repo.inner.gql_calls) == n_labels            # no label swap
    assert [c[:2] for c in repo.inner.calls[n_calls:] if c[0] == "issue"] == [["issue", "comment"]]   # one comment, nothing else
    assert (len(list((tmp_path / "state").glob("**/*"))) if (tmp_path / "state").exists() else 0) == n_events
    b = brief()
    assert b["kind_declared"] is True and b["reason_class"] == "needs_decision"
    assert all(q["id"] != "kind" for q in b["questions"])

    assert u.main(["unpark.py", "list", str(tmp_path), "--undeclared"], run=repo) == 0
    assert "#5" not in capsys.readouterr().out


def test_cli_declare_refuses_an_unknown_kind_and_posts_nothing(monkeypatch, tmp_path, capsys):
    repo = _Repo()
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    u = _mod("unpark")
    assert u.main(["unpark.py", "declare", str(tmp_path), "5", "approval"], run=repo) == 2
    assert repo.comments == []


def test_resolve_with_qkind_posts_the_line_in_the_decision_comment(monkeypatch, tmp_path, capsys):
    repo = _Repo(comments=[_park_c(NEEDLE_TEXT)])
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    u = _mod("unpark")
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps({"answers": {"decision": "go"}, "questions": []}))
    assert u.main(["unpark.py", "resolve", str(tmp_path), "5", "--answers", str(answers),
                   "--decision", "unpark", "--qkind", "needs_decision"], run=repo) == 0
    assert "sigma-qkind: needs_decision" in repo.bodies()[-1]


# --------------------------------------------------------------------------- F-11

def _issue_plan(title, body, tmp_path, monkeypatch):
    h = _mod("handoff")
    cfg = {}
    return h._triage_plan(str(tmp_path), cfg, None, "k", True, None, title, body, "P3", False)


def test_deny_word_filing_is_declared_irreversible_with_a_hardstop_class(tmp_path):
    ft = _mod("file_triage")
    d = ft.decide("cleanup", "please drop table users", {}, {})
    assert d.kind == "park" and d.matched
    h = _mod("handoff")
    plan = h._triage_plan(str(tmp_path), {}, None, "k", True, None, "cleanup",
                          "please drop table users", "P3", False)
    assert plan["qkind"] == "irreversible" and plan["hardstop"] == d.matched


def test_spend_secret_and_class_matches_are_hard_stops_too(tmp_path):
    h = _mod("handoff")
    for body in ("needs a paid run on a cloud GPU benchmark", "token = abcdefgh12345678"):
        plan = h._triage_plan(str(tmp_path), {}, None, "k", True, None, "t", body, "P3", False)
        assert plan["qkind"] == "irreversible" and plan["hardstop"], body


def test_ordinary_park_and_arm_keep_their_old_shape(tmp_path):
    h = _mod("handoff")
    plan = h._triage_plan(str(tmp_path), {}, None, "k", True, None, "tidy", "polish the docs", "P3", False)
    assert plan["kind"] == "arm" and plan["qkind"] == "needs_decision" and not plan.get("hardstop")


def test_filing_park_comment_carries_the_hardstop_line_and_record_copies_it(tmp_path):
    h, q, dr = _mod("handoff"), _mod("qkind"), _mod("decision_record")
    posted = []

    class Src:
        def park(self, goal, text, **kw):
            posted.append(text)
    plan = {"kind": "park", "reason": "hard-stop word: drop table", "qkind": "irreversible",
            "hardstop": "hard-stop word", "text": "x", "depth": None}
    try:
        h._triage_finish(str(tmp_path), {}, Src(), plan, 9, "k", {})
    except Exception:
        pass
    assert posted and "sigma-qkind: irreversible" in posted[0] and "sigma-hardstop: hard-stop word" in posted[0]
    rec = dr.build("unpark", "9", "irreversible", "go", "", "how", hardstop_class=q.parse_hardstop(posted[0]))
    assert rec["hardstop_class"] == "hard-stop word"


def test_promote_plan_for_declares_irreversible_for_a_deny_word():
    p = _mod("promote")
    plan = p.plan_for({"title": "t", "body": "rotate the credentials", "labels": []}, {})
    assert getattr(plan, "qkind", None) == "irreversible"


def test_bank_option_rules_stay_valid_and_sealed():
    """A rule whose option is a question-bank option (quota "wait it out") stays valid, sealed, and
    matched; the kind list gained nothing (no `approval`)."""
    rb, q = _mod("rulebook"), _mod("qkind")
    rule = {"id": "r1", "qkind": "quota", "area": "repo", "option": "wait it out", "reason": "free window",
            "status": "approved", "created": "2026-10-01", "confirmed": "2026-10-02"}
    assert rb.validate(rule) == (True, None)
    assert len(rb._seal(rule)) == 64
    assert "approval" not in q.QKINDS


def test_doctor_lists_a_deny_list_park_that_is_not_a_hard_stop_kind():
    from test_unpark import _runner, _view
    spec = importlib.util.spec_from_file_location("doctor_1498", S.parent.parent / "sigma-doctor" / "scripts" / "doctor.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    pk = _mod("sources").PARK_COMMENT_PREFIX
    rows = [{"number": 5, "title": "wipe", "body": "please drop table users", "labels": [{"name": "sdlc:parked"}]},
            {"number": 6, "title": "wipe", "body": "drop table users", "labels": [{"name": "sdlc:parked"}]},
            {"number": 7, "title": "tidy", "body": "polish docs", "labels": [{"name": "sdlc:parked"}]}]
    def owned(number, text):
        view = json.loads(_view(number=number))
        view["comments"] = [{"body": text, "authorAssociation": "OWNER"}]
        return json.dumps(view)

    run = _runner(by_label={"sdlc:parked": json.dumps(rows)},
                  views={"5": owned(5, pk + "x\nsigma-qkind: needs_decision"),
                         "6": owned(6, pk + "x\nsigma-qkind: irreversible"),
                         "7": owned(7, pk + "x")})
    assert d._mis_kinded_hardstop_parks(_config(), {"repo": "acme/widget"}, lambda argv: run(argv[1:])) == [5]


def test_sigma_opener_without_association_cannot_declare_a_kind(capsys):
    """Fail closed: a comment with no authorAssociation is untrusted even if its body opens like Sigma's."""
    q = _mod("qkind")
    spoof = {"body": "Parked by Sigma - x\nsigma-qkind: quota", "author": {"login": "someone"}}
    assert q.resolve([spoof], 5) is None
    assert "ignoring a sigma-qkind line" in capsys.readouterr().err
