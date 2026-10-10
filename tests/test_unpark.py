"""#1392: unpark.py -- the guided interview that turns a park into a decision or a re-pickable goal.

Real `sources.GitHubSource` throughout, driven by an injectable runner; `gqlfake.swap` answers the
GraphQL transport `_swap_labels` uses.
"""
import json, pathlib, importlib.util

import gqlfake
from test_auto_unpark import _label_aware_sweep_runner

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _config(**gh):
    return {"discovery": {"source": "github", "github": {"repo": "acme/widget", **gh}}}


def _view(number=5, title="A goal", body="", labels=("sdlc:parked",), comments=(), state="OPEN"):
    return json.dumps({"number": number, "title": title, "body": body,
                       "labels": [{"name": n} for n in labels],
                       "comments": [{"body": c} for c in comments], "state": state})


def _runner(views=None, by_label=None, states=None, fail_on=()):
    calls, label_state, gql_calls, fallbacks, rest_writes = [], set(), [], [], []
    views, by_label, states = views or {}, by_label or {}, states or {}

    def run(args):
        joined = " ".join(str(a) for a in args)
        for needle in fail_on:
            if needle in joined:
                raise RuntimeError(f"simulated gh failure: {needle}")
        gql = gqlfake.swap(args, labels=label_state, calls=calls,
                           repo_args=("--repo", "acme/widget"))
        if gql is not None:
            gql_calls.append(list(args))
            return gql
        if gqlfake.is_issue_write(args):                # #895 slice 3a: REST writes, legacy-recorded
            rest_writes.append(list(args))
            return gqlfake.rest_write(args, calls=calls, repo_args=("--repo", "acme/widget"))
        calls.append(list(args))
        if args[0] == "project":
            return "{}"
        params = gqlfake.rest_list_params(args)
        if params is not None:      # #895 2c: the list read is REST first
            return by_label.get(params.get("labels"), "[]")
        if len(args) >= 2 and args[0] == "issue" and args[1] == "list":
            fallbacks.append(list(args))      # the one `issue list` fallback; REST-first tests assert this empty
            label = args[args.index("--label") + 1] if "--label" in args else None
            return by_label.get(label, "[]")
        # #895: issue reads are REST first; answered REST-shaped (issue + comments pages) from the same
        # gh-shape `views` / `states`, a `states` entry winning exactly as the old `--json state` branch did.
        def _gh_shape(n, _f):
            if n in states:
                return {"state": states[n], "stateReason": "COMPLETED" if states[n] == "CLOSED" else ""}
            return views.get(n, _view(number=int(n)))
        rest = gqlfake.rest_issue(args, _gh_shape)
        if rest is not None:
            return rest
        if len(args) >= 3 and args[0] == "issue" and args[1] == "view":
            fallbacks.append(list(args))      # the one fallback; REST-first tests assert this empty
            n = str(args[2])
            field = args[args.index("--json") + 1] if "--json" in args else ""
            if field.startswith("state"):
                st = states.get(n, "OPEN")
                return json.dumps({"state": st,
                                   "stateReason": "COMPLETED" if st == "CLOSED" else ""})
            return views.get(n, _view(number=int(n)))
        return ""
    run.calls = calls
    run.gql_calls = gql_calls
    run.fallbacks = fallbacks
    run.rest_writes = rest_writes
    return run


def _park(reason):
    return _mod("sources").PARK_COMMENT_PREFIX + reason


# --------------------------------------------------------------------------- the re-park hazard


def test_recorded_answers_can_never_plant_a_phantom_blocker():
    """THE test this module's body-append depends on. `_BLOCK_RE` triggers on ordinary English
    ("waiting on", "needs", "after", "requires") within 40 chars of a `#N` -- so an answer a human
    would write without a second thought would otherwise plant a blocker in the body of the goal
    that was just unblocked, and `precheck` would re-park it on the very next pick. That is the
    exact failure mode this whole interview exists to prevent, caused by the interview itself."""
    u, bc = _mod("unpark"), _mod("backlog_check")
    block = u.render_block({"next": "waiting on the design sign-off, see #1234; also needs #77"})
    body = "Do the thing.\n\n" + block
    doc = {"ref": "5", "raw": body}
    assert bc._referenced_blocker_refs(doc) == set()
    assert bc._explicit_blockers(doc, [{"ref": "1234", "open": True}]) == []


def test_a_real_blocker_outside_the_block_is_still_detected():
    """The scrub must narrow the haystack by a fixed, code-known SPAN and nothing else -- a genuine
    dependency written anywhere else in the body is still a genuine dependency."""
    u, bc = _mod("unpark"), _mod("backlog_check")
    body = ("Blocked by #99\n\n"
            + u.render_block({"next": "waiting on the design sign-off, see #1234"}))
    doc = {"ref": "5", "raw": body}
    assert bc._referenced_blocker_refs(doc) == {"99"}


def test_an_unterminated_block_is_still_stripped():
    """A truncated write must fail toward silence, not toward a phantom blocker."""
    bc = _mod("backlog_check")
    body = "Do the thing.\n" + bc.UNPARK_QA_START + "\n- **next** — waiting on #1234"
    assert bc._referenced_blocker_refs({"ref": "5", "raw": body}) == set()


# --------------------------------------------------------------------------- brief


def test_brief_derives_the_kind_from_the_issue_not_the_ledger():
    """PR-1, the blocking plan-review finding: `reason_class` and `decision_tier` are recorded ONLY
    into the ledger, and `ledger.enabled` ships FALSE -- while `decision_tier.resolve()` is gated on
    a key the template ships as "off". Reading either would make this module inert on a stock
    adopter. Both are re-derived from the park comment, which is always on the issue."""
    u = _mod("unpark")
    run = _runner(views={"5": _view(comments=[_park("changes requested on the API shape")])})
    b = u.brief(".sdlc", _config(), 5, run=run)
    assert b["reason_class"] == "needs_decision"
    assert b["park_reason"] == "changes requested on the API shape"
    assert b["park_reason_is_sigmas"] is True
    # no ledger anywhere in the config, and `decision_tier` is not "auto" -- yet a tier is available
    assert b["decision_tier"]


def test_brief_asks_about_the_concrete_open_blocker_by_number():
    u = _mod("unpark")
    run = _runner(views={"5": _view(body="Blocked by #7",
                                    comments=[_park("no pr for this goal")])},
                  states={"7": "OPEN"})
    b = u.brief(".sdlc", _config(), 5, run=run)
    assert b["reason_class"] == "dependency"
    assert b["blockers"] == [{"ref": "7", "open": True}]
    assert any("#7 is still open" in q["ask"] for q in b["questions"])


def test_brief_drops_a_ref_slot_when_there_is_no_concrete_ref_to_name():
    """A question that would read "#{ref} is still open" with nothing to substitute is noise, not a
    question -- naming the concrete thing is the first rule of the bank."""
    u = _mod("unpark")
    run = _runner(views={"5": _view(body="Blocked by #7",
                                    comments=[_park("no pr for this goal")])},
                  states={"7": "CLOSED"})
    b = u.brief(".sdlc", _config(), 5, run=run)
    assert not any("{ref}" in q["ask"] for q in b["questions"])
    assert not any("is still open" in q["ask"] for q in b["questions"])


def test_brief_never_re_asks_something_a_previous_round_answered():
    """An interrupted interview resumes rather than restarting -- a human who answered three
    questions and lost the session must not be asked them again."""
    u = _mod("unpark")
    body = "Do it.\n\n" + u.render_block({"resume": "start it again", "why": "the API settled"})
    run = _runner(views={"5": _view(body=body, comments=[_park("changes requested")])})
    b = u.brief(".sdlc", _config(), 5, run=run)
    assert b["prior_answers"] == {"resume": "start it again", "why": "the API settled"}
    assert "why" not in [q["id"] for q in b["questions"]]


def test_the_closing_question_is_always_last():
    u = _mod("unpark")
    run = _runner(views={"5": _view(comments=[_park("some free prose nobody classified")])})
    b = u.brief(".sdlc", _config(), 5, run=run)
    assert b["questions"][-1]["id"] == "resume"
    assert b["questions"][-1]["options"] == ["start it again", "leave it parked"]


def test_a_conditional_slot_is_not_asked_speculatively():
    u = _mod("unpark")
    assert [q["id"] for q in u._questions_for("dependency", "r", ["7"], {})] == ["route", "resume"]
    followed = [q["id"] for q in u._questions_for("dependency", "r", ["7"],
                                                  {"route": "work around it"})]
    assert "workaround" in followed


def test_every_question_in_the_bank_obeys_the_phrasing_rules():
    """The complaint that produced this module was that agent questions are hard to understand. The
    rules are enforceable, so they are enforced: one sentence, no Sigma vocabulary."""
    u = _mod("unpark")
    jargon = ("reason_class", "decision_tier", "offboard", "sdlc:", "slot", "lifecycle",
              "predicate", "idempotent")
    slots = [s for group in u.QUESTIONS.values() for s in group] + [u.CLOSING_QUESTION]
    for slot in slots:
        ask = slot["ask"]
        assert ask.rstrip().endswith("?"), ask
        assert not any(word in ask.lower() for word in jargon), ask
        assert ask.count("?") == 1, ask


def test_every_reason_class_the_loop_can_record_has_questions():
    """A park whose class has no entry falls through to `unknown`, which is a real answer -- but a
    class the loop can actually produce and that we simply forgot is not."""
    u, ledger = _mod("unpark"), _mod("ledger")
    covered = set(u.QUESTIONS)
    for cls in ledger.REASON_CLASSES:
        assert cls in covered or cls in ("backlog-empty",), cls


# --------------------------------------------------------------------------- resolve


def test_unpark_is_one_atomic_swap_then_the_record():
    u = _mod("unpark")
    run = _runner(views={"5": _view(labels=("sdlc:parked",), comments=[_park("needs a call")])})
    result = u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run)
    assert result["outcome"] == "unparked"
    assert result["detail"] == "+sdlc:goal -sdlc:parked"
    docs = [a for c in run.gql_calls for a in c if str(a).startswith("query=mutation")]
    assert len(docs) == 1


def test_unpark_clears_the_blocked_label_too_not_just_parked():
    u = _mod("unpark")
    run = _runner(views={"5": _view(labels=("sdlc:blocked",), comments=[_park("dep")])})
    result = u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run)
    assert result["detail"] == "+sdlc:goal -sdlc:blocked"


def test_unpark_writes_the_answers_into_the_body_and_as_a_comment():
    """The comment is the audit trail; the BODY is the copy the next agent actually reads. Both."""
    u = _mod("unpark")
    run = _runner(views={"5": _view(body="Do it.", comments=[_park("needs a call")])})
    u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run)
    edits = [c for c in run.calls if c[:2] == ["issue", "edit"] and "--body" in c]
    comments = [c for c in run.calls if c[:2] == ["issue", "comment"]]
    assert len(edits) == 1 and len(comments) == 1
    new_body = edits[0][edits[0].index("--body") + 1]
    assert new_body.startswith("Do it.")
    assert "**decision** — start it again" in new_body


def test_a_second_round_replaces_the_record_rather_than_stacking_another_copy():
    u = _mod("unpark")
    first = u.render_block({"decision": "leave it parked"})
    run = _runner(views={"5": _view(body="Do it.\n\n" + first, comments=[_park("needs a call")])})
    u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run)
    body = next(c[c.index("--body") + 1] for c in run.calls
                if c[:2] == ["issue", "edit"] and "--body" in c)
    assert body.count(_mod("backlog_check").UNPARK_QA_START) == 1
    assert "leave it parked" not in body and "start it again" in body


def test_keep_parked_changes_no_labels_and_opts_out_of_the_automatic_sweep():
    """A human deciding "leave it parked" is exactly the decision `auto_unpark`'s own opt-out marker
    exists to record -- reused live, so the sweep never re-litigates it."""
    u, au = _mod("unpark"), _mod("auto_unpark")
    run = _runner(views={"5": _view(comments=[_park("needs a call")])})
    result = u.resolve(".sdlc", _config(), 5, {"decision": "leave it parked"}, "keep-parked",
                       run=run)
    assert result["outcome"] == "kept-parked"
    assert not run.gql_calls or not any(str(a).startswith("query=mutation")
                                        for c in run.gql_calls for a in c)
    comment = next(c[c.index("--body") + 1] for c in run.calls if c[:2] == ["issue", "comment"])
    assert au.KEEP_PARKED_MARKER in comment


def test_resolve_refuses_a_closed_issue():
    u = _mod("unpark")
    run = _runner(views={"5": _view(state="CLOSED", comments=[_park("x")])})
    result = u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run)
    assert result["outcome"] == "failed" and "closed" in result["detail"]


def test_resolve_refuses_an_unknown_decision():
    u = _mod("unpark")
    run = _runner(views={"5": _view()})
    result = u.resolve(".sdlc", _config(), 5, {}, "maybe", run=run)
    assert result["outcome"] == "failed"


def test_dry_run_writes_nothing():
    u = _mod("unpark")
    run = _runner(views={"5": _view(comments=[_park("x")])})
    result = u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run,
                       apply=False)
    assert result["outcome"] == "would"
    assert not any(c[:2] == ["issue", "edit"] for c in run.calls)
    assert not any(c[:2] == ["issue", "comment"] for c in run.calls)


def test_a_failed_body_write_still_leaves_the_answers_on_the_issue(capsys):
    """The comment carries the same text, so nothing is lost -- but the body is the copy that gets
    read, and silently not having it is how a goal is re-parked for the reason just answered."""
    u = _mod("unpark")
    run = _runner(views={"5": _view(comments=[_park("x")])}, fail_on=["issues/5 --method PATCH"])
    result = u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run)
    assert result["outcome"] == "unparked"
    assert any(c[:2] == ["issue", "comment"] for c in run.calls)
    assert "could not write them into the body" in capsys.readouterr().err


def test_a_failed_label_write_is_reported_not_swallowed():
    u = _mod("unpark")
    run = _runner(views={"5": _view(comments=[_park("x")])}, fail_on=["addLabelsToLabelable"])
    result = u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run)
    assert result["outcome"] == "failed" and "did not land" in result["detail"]


# --------------------------------------------------------------------------- list + CLI


def test_list_is_oldest_first_and_names_the_scope():
    u = _mod("unpark")
    run = _runner(by_label={"sdlc:parked": json.dumps([
        {"number": 30, "title": "newer", "body": "", "labels": [{"name": "sdlc:parked"}]},
        {"number": 4, "title": "older", "body": "", "labels": [{"name": "sdlc:parked"}]}])})
    result = u.list_parked(".sdlc", _config(), run=run)
    assert [r["number"] for r in result["issues"]] == ["4", "30"]
    assert "discovery.github.assignee is not set" in u.render_list(result)


def test_list_reports_incomplete_rather_than_empty_when_a_query_fails():
    u = _mod("unpark")
    run = _runner(fail_on=["labels=sdlc:parked"])
    result = u.list_parked(".sdlc", _config(), run=run)
    assert result["complete"] is False
    assert any("incomplete" in n for n in result["degraded"])


def test_cli_resolve_requires_both_flags(tmp_path, capsys):
    u = _mod("unpark")
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    assert u.main(["unpark.py", "resolve", str(tmp_path), "5"]) == 2
    assert "--decision" in capsys.readouterr().err


def test_cli_resolve_rejects_an_answers_file_of_the_wrong_shape(tmp_path, capsys):
    u = _mod("unpark")
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    bad = tmp_path / "a.json"
    bad.write_text(json.dumps(["not", "an", "object"]))
    assert u.main(["unpark.py", "resolve", str(tmp_path), "5", "--answers", str(bad),
                   "--decision", "unpark"]) == 2
    assert "answers" in capsys.readouterr().err


def test_cli_usage_on_a_bogus_verb(capsys):
    u = _mod("unpark")
    assert u.main(["unpark.py", "bogus"]) == 2
    assert "unpark.py list" in capsys.readouterr().err


def test_render_brief_reads_as_a_briefing_not_a_dump(capsys):
    """The rendered form is what a human sees when they are not driving this through --json; it has
    to answer why/what-kind/what-blocks without them opening the issue."""
    u = _mod("unpark")
    run = _runner(views={"5": _view(body="Blocked by #7", title="Fix the retry wrapper",
                                    comments=[_park("no pr for this goal")])},
                  states={"7": "OPEN"})
    text = u.render_brief(u.brief(".sdlc", _config(), 5, run=run))
    assert "#5 Fix the retry wrapper" in text
    assert "parked because: no pr for this goal." in text
    assert "kind: dependency" in text
    assert "blocker #7: still open" in text
    assert "#7 is still open" in text


def test_render_brief_says_what_a_previous_round_already_answered():
    u = _mod("unpark")
    body = "Do it.\n\n" + u.render_block({"why": "the API settled"})
    run = _runner(views={"5": _view(body=body, comments=[_park("changes requested")])})
    assert "already answered: why" in u.render_brief(u.brief(".sdlc", _config(), 5, run=run))


def test_render_brief_on_a_park_with_no_recorded_reason():
    """A goal parked by hand in the GitHub UI has no Sigma park comment at all -- it must still
    brief cleanly rather than rendering an empty reason as a blank line."""
    u = _mod("unpark")
    run = _runner(views={"5": _view(comments=[])})
    b = u.brief(".sdlc", _config(), 5, run=run)
    assert b["reason_class"] == "unknown"
    assert "(no reason was recorded)" in u.render_brief(b)


def test_a_human_written_park_comment_is_used_even_though_sigma_did_not_write_it():
    u = _mod("unpark")
    run = _runner(views={"5": _view(comments=["holding this until the pricing call"])})
    b = u.brief(".sdlc", _config(), 5, run=run)
    assert b["park_reason"] == "holding this until the pricing call"
    assert b["park_reason_is_sigmas"] is False


def test_the_newest_park_comment_wins_when_a_goal_was_parked_twice():
    u = _mod("unpark")
    run = _runner(views={"5": _view(comments=[_park("first reason"), "chatter",
                                              _park("changes requested")])})
    b = u.brief(".sdlc", _config(), 5, run=run)
    assert b["park_reason"] == "changes requested"
    assert b["reason_class"] == "needs_decision"


def test_cli_list_and_brief_render_and_exit_zero(tmp_path, capsys):
    u = _mod("unpark")
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    run = _runner(by_label={"sdlc:parked": json.dumps(
                      [{"number": 5, "title": "t", "body": "", "labels": [{"name": "sdlc:parked"}]}])},
                  views={"5": _view(comments=[_park("changes requested")])})
    assert u.main(["unpark.py", "list", str(tmp_path)], run=run) == 0
    assert "#5" in capsys.readouterr().out
    assert u.main(["unpark.py", "brief", str(tmp_path), "5"], run=run) == 0
    assert "kind: needs_decision" in capsys.readouterr().out
    assert u.main(["unpark.py", "brief", str(tmp_path), "5", "--json"], run=run) == 0
    assert json.loads(capsys.readouterr().out)["number"] == "5"


def test_cli_brief_refuses_an_unreadable_issue_without_a_traceback(tmp_path, capsys):
    u = _mod("unpark")
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    run = _runner(fail_on=["issues/5", "issue view 5"])
    assert u.main(["unpark.py", "brief", str(tmp_path), "5"], run=run) == 1
    assert "could not read #5" in capsys.readouterr().err


def test_resolve_refuses_when_the_issue_cannot_be_read():
    """GhApiError (REST and the one fallback fail) reaches resolve()'s own arm: failed, nothing written."""
    u = _mod("unpark")
    run = _runner(fail_on=["issues/5", "issue view 5"])
    out = u.resolve(".sdlc", _config(), 5, {}, "unpark", run=run)
    assert out["outcome"] == "failed" and "could not read the current state of #5" in out["detail"]
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


def test_fetch_issue_reads_rest_first_issue_and_comments_and_nothing_else():
    """#895: `_fetch_issue` has no except of its own; it asks `api .../issues/5` then its comments page."""
    u = _mod("unpark")
    run = _runner(views={"5": _view(comments=[_park("changes requested")])})
    source = _mod("sources").GitHubSource(_config(), run=run)
    data = u._fetch_issue(source, 5)
    assert data["number"] == 5 and data["state"] == "OPEN" and len(data["comments"]) == 1
    reads = [c for c in run.calls if c[0] == "api"]
    assert [c[1] for c in reads] == ["repos/acme/widget/issues/5", "repos/acme/widget/issues/5/comments"]
    assert run.fallbacks == []


def test_fetch_issue_lets_a_ghapierror_reach_the_callers_except():
    """No except in `_fetch_issue`: the CALLER's arm (brief/resolve) must see the typed error."""
    u = _mod("unpark")
    src = _mod("sources")
    gh_api = src.gh_api
    run = _runner(fail_on=["issues/5", "issue view 5"])
    source = src.GitHubSource(_config(), run=run)
    try:
        u._fetch_issue(source, 5)
    except gh_api.GhApiError:
        pass
    else:
        raise AssertionError("expected GhApiError to propagate")


def test_fetch_issue_rest_5xx_falls_back_once(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)
    u = _mod("unpark")
    inner = _runner(views={"5": _view(comments=[_park("x")])})

    def run(args):
        if gqlfake.rest_issue_target(args):
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return inner(args)
    data = u._fetch_issue(_mod("sources").GitHubSource(_config(), run=run), 5)
    assert data["state"] == "OPEN"
    assert [c[:3] for c in inner.fallbacks] == [["issue", "view", "5"]]


def test_cli_resolve_end_to_end(tmp_path, capsys):
    u = _mod("unpark")
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    answers = tmp_path / "a.json"
    answers.write_text(json.dumps({"answers": {"decision": "start it again"},
                                   "questions": [{"id": "decision", "ask": "Start again?"}]}))
    run = _runner(views={"5": _view(comments=[_park("changes requested")])})
    assert u.main(["unpark.py", "resolve", str(tmp_path), "5", "--answers", str(answers),
                   "--decision", "unpark"], run=run) == 0
    out = capsys.readouterr().out
    assert "[unparked] #5" in out
    # the question TEXT rides along with the answer -- a bare key is not a record
    body = next(c[c.index("--body") + 1] for c in run.calls
                if c[:2] == ["issue", "edit"] and "--body" in c)
    assert "_Start again?_" in body


def test_resolve_records_the_answers_even_when_there_is_nothing_left_to_swap():
    """A goal whose labels were already fixed by hand still deserves its context recorded -- and
    that is not a failure."""
    u = _mod("unpark")
    run = _runner(views={"5": _view(labels=("sdlc:goal",), comments=[_park("x")])})
    result = u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run)
    assert result["outcome"] == "unparked" and "already pickable" in result["detail"]
    assert any(c[:2] == ["issue", "comment"] for c in run.calls)


def test_a_local_source_degrades_instead_of_raising():
    u = _mod("unpark")

    class Local:
        pass
    assert u.list_parked(".sdlc", {}, source=Local())["complete"] is False
    assert u.brief(".sdlc", {}, 5, source=Local())["questions"] == []
    assert u.resolve(".sdlc", {}, 5, {}, "unpark", source=Local())["outcome"] == "failed"


def test_unpark_also_settles_the_approval_gate():
    """#1393: an issue can be BOTH awaiting approval and parked (a proposal a human parked rather
    than ruled on). An unpark that only dropped `sdlc:parked` would leave `sdlc:goal` +
    `sdlc:needs-confirmation` -- picked by nothing, and /sigma-promote's job to repair. Unparking IS
    the human decision; it settles the approval question in the same gesture."""
    u = _mod("unpark")
    run = _runner(views={"5": _view(labels=("sdlc:parked", "sdlc:needs-confirmation"),
                                    comments=[_park("needs a call")])})
    result = u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run)
    assert result["outcome"] == "unparked"
    assert result["detail"] == "+sdlc:goal -sdlc:parked -sdlc:needs-confirmation"


def test_unpark_clears_a_stale_in_progress_claim_too():
    """Review bug_003, and a straight divergence from the sister sweep: `auto_unpark`'s own unpark
    clears `sdlc:in-progress` (pinned by `test_an_unpark_clears_a_stale_in_progress_claim_too`) and
    this one did not. An issue in the half-applied {parked, in-progress} shape would post-swap to
    {goal, in-progress}, which `not_eligible_labels()` refuses on BOTH queue paths -- so the human
    is told "unparked", the card moves to Ready, and the goal is pickable by nothing until the
    stale-claim reclaimer eventually times the lease out. An unpark that unparks nothing, on the
    command the README names as the sanctioned way to re-queue a goal."""
    u = _mod("unpark")
    run = _runner(views={"5": _view(labels=("sdlc:parked", "sdlc:in-progress"),
                                    comments=[_park("needs a call")])})
    result = u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run)
    assert result["outcome"] == "unparked"
    assert result["detail"] == "+sdlc:goal -sdlc:parked -sdlc:in-progress"


def test_the_two_unpark_paths_clear_the_same_labels():
    """The human path and the automatic sweep must agree on what an unpark MEANS. They had drifted
    by one label (review bug_003); this pins them together behaviourally, on the same fixture, so a
    future edit to either is caught here rather than by another review."""
    u, au = _mod("unpark"), _mod("auto_unpark")
    labels = ("sdlc:parked", "sdlc:in-progress")

    run_h = _runner(views={"5": _view(labels=labels, comments=[_park("x")])})
    human = u.resolve(".sdlc", _config(), 5, {"decision": "start it again"}, "unpark", run=run_h)
    # the human path acts on a PARKED goal, the sweep on a BLOCKED one -- the labels differ by that
    # one state marker, so compare the OVERLAP: both must clear the stale in-progress claim.
    human_removed = set(human["detail"].split()[1:])          # "+sdlc:goal -a -b" -> {"-a", "-b"}

    # #1394: the sweep resumes MACHINE-blocked goals only -- a parked one is skipped outright now,
    # so the comparable fixture is a blocked issue carrying the same stale claim.
    issue = {"number": 5, "title": "", "body": "blocked by #7",
             "labels": [{"name": n} for n in ("sdlc:blocked", "sdlc:in-progress")]}
    run_s = _label_aware_sweep_runner(
        by_label={"sdlc:blocked": json.dumps([issue]), "sdlc:blocking": "[]"},
        states={"7": "CLOSED"})
    src = au.sources.GitHubSource(_config(), run=run_s)
    acts, _ = au.compute_unpark_actions(".sdlc", _config(), src, [issue], run=run_s)
    swap = next(a for a in acts if a["action"] == "swap-label")

    assert "-sdlc:in-progress" in human_removed, "the human path leaves a stale claim behind"
    assert "sdlc:in-progress" in swap["remove"], "the sweep leaves a stale claim behind"
    assert swap["add"] == ["sdlc:goal"]


# --- cloud-review findings on the fixes (bug_001, bug_002, bug_003) ------------------------------


def test_no_bank_slot_id_collides_with_the_closing_question():
    """Review bug_003, as a STRUCTURAL guard rather than a single case.

    `needs_decision`'s own first slot was `decision`, byte-identical to the closing question's id,
    and both failure modes were silent: on round one the two answers share a key and one overwrites
    the other, so the record loses either the product decision or the routing one; on resume,
    `_questions_for`'s `not in answered` test sees the class answer and drops the closing question
    entirely -- the interview reads as complete having never asked the one question SKILL.md says
    must always be asked. `needs_decision` is the class `loop._reason_class` produces most often, so
    this was the common path.

    Checking the whole bank means a future entry cannot reintroduce it."""
    u = _mod("unpark")
    closing = u.CLOSING_QUESTION["id"]
    for cls, slots in u.QUESTIONS.items():
        for slot in slots:
            assert slot["id"] != closing, (
                "%r's slot %r collides with the closing question's id -- one answer will overwrite "
                "the other, and on resume the closing question is dropped" % (cls, slot["id"]))


def test_the_closing_question_survives_a_needs_decision_answer():
    """The concrete case: answering the class question must not suppress the closing one."""
    u = _mod("unpark")
    ids = [q["id"] for q in u._questions_for("needs_decision", "changes requested", [],
                                             {"decision": "go with option A", "why": "settled"})]
    assert ids == [u.CLOSING_QUESTION["id"]]


def test_a_needs_decision_interview_keeps_both_answers():
    """Round one must record the product decision AND the routing decision, not one on top of the
    other. Proven through the body round-trip, since that is what a later session reads back."""
    u = _mod("unpark")
    answers = {"decision": "go with option A", "resume": "start it again"}
    recovered = u._recorded_answers("x\n\n" + u.render_block(answers))
    assert recovered == answers


def test_a_failed_label_write_still_records_the_answers():
    """Review bug_001. This path used to return straight out, discarding an interview a human had
    just typed -- and contradicting `resolve`'s own docstring promise that "neither loses the
    answers, because the comment is posted on both paths". The label write is the retryable half;
    a person's answers are not."""
    u = _mod("unpark")
    run = _runner(views={"5": _view(comments=[_park("x")])}, fail_on=["addLabelsToLabelable"])
    result = u.resolve(".sdlc", _config(), 5, {"resume": "start it again", "why": "the API settled"},
                       "unpark", run=run)
    assert result["outcome"] == "failed"
    assert "the answers were recorded" in result["detail"]
    body = next(c[c.index("--body") + 1] for c in run.calls
                if c[:2] == ["issue", "edit"] and "--body" in c)
    assert "the API settled" in body
    # ...so the retry has nothing left to ask
    assert u._recorded_answers(body)["why"] == "the API settled"


def test_a_stray_end_marker_above_the_block_cannot_duplicate_it():
    """Review bug_002. The guard checked for an end marker AFTER the start, but the slice searched
    from position 0 -- so a stray end marker in prose above the real block made `tail` begin inside
    that block, and the new body carried BOTH. That is the one invariant this function exists for."""
    u, bc = _mod("unpark"), _mod("backlog_check")
    body = ("Quoting an earlier comment: " + bc.UNPARK_QA_END + "\n\n"
            + u.render_block({"resume": "leave it parked"}))
    out = u._replace_block(body, u.render_block({"resume": "start it again"}))
    assert out.count(bc.UNPARK_QA_START) == 1
    assert "leave it parked" not in out and "start it again" in out


def test_the_keep_parked_marker_quotes_the_substantive_answer_not_the_routing_one():
    """"leave it parked" is the DECISION, not the REASON -- rendering it produces "this park is a
    deliberate checkpoint, not a stale block — leave it parked", which tells a reader nothing."""
    u, au = _mod("unpark"), _mod("auto_unpark")
    run = _runner(views={"5": _view(comments=[_park("x")])})
    u.resolve(".sdlc", _config(), 5, {"decision": "wait for the pricing call",
                                      "resume": "leave it parked"}, "keep-parked", run=run)
    comment = next(c[c.index("--body") + 1] for c in run.calls if c[:2] == ["issue", "comment"])
    assert au.KEEP_PARKED_MARKER in comment
    assert "wait for the pricing call" in comment


def test_comment_and_body_append_are_rest_writes_with_swallow_and_false_semantics():
    """#895 slice 3a: the comment is a REST POST, the body append a REST PATCH (raw `-f body=`); a failing
    comment is swallowed, a failing PATCH writes ONE stderr line and returns False."""
    u = _mod("unpark")
    run = _runner()
    src = _mod("sources").GitHubSource(_config(), run=run)
    u._comment(src, 5, "hello")
    assert u._append_block(src, 5, "old", "BLOCK") is True
    assert [(c[1], c[3]) for c in run.rest_writes] == [
        ("repos/acme/widget/issues/5/comments", "POST"), ("repos/acme/widget/issues/5", "PATCH")]
    assert "body=" in " ".join(run.rest_writes[1]) and "-F" not in run.rest_writes[1]
    bad = _runner(fail_on=["issues/5/comments", "issues/5 --method PATCH"])
    bsrc = _mod("sources").GitHubSource(_config(), run=bad)
    u._comment(bsrc, 5, "hello")                                     # swallowed
    import io, contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert u._append_block(bsrc, 5, "old", "BLOCK") is False
    assert err.getvalue().count("\n") == 1 and "could not write them into the body" in err.getvalue()


def test_declared_kind_wins_in_the_brief():
    """#994: a kind declared on the park comment beats the substring guess, and the machine line is
    not shown as part of the reason."""
    u, q = _mod("unpark"), _mod("qkind")
    text = "changes requested on the API shape\n" + q.render_line("owner_hold")
    run = _runner(views={"5": _view(comments=[_park(text)])})
    b = u.brief(".sdlc", _config(), 5, run=run)
    assert b["reason_class"] == "owner_hold"
    assert b["park_reason"] == "changes requested on the API shape"
