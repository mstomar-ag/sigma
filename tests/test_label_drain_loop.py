"""#1579: label-drain behaviour inside a `--label` run -- pull blockers on hold, refuse to start on open
questions, and stop `label-held` rather than `backlog-empty`.

The loop wiring is tested against the dependency-gate fake (`_Board`) with a recording stand-in for
`label_drain`, so every assertion is on what the loop asked for. The real `pull` is tested against the
stateful fake `gh` of the arm tests. Nothing here touches a network or a model.
"""
import os
import tempfile
import time

import pytest

from journal_events import journal_events
from test_pick_dependency_gate import _Board, _base, _blocked_by, _cfg, _issue, _loop

LBL = "validation-run-1"
CHAIN_OPEN = [_blocked_by(1, 7), _issue(7)]
CHAIN_DONE = [_blocked_by(1, 7), _issue(7, state="CLOSED")]


class Scoped(_Board):
    label_scope = LBL
    repo = "acme/widget"
    goal_label = "sdlc:goal"

    def scope_label_current(self, goal):
        return True


class Stub:
    """Stands in for the label_drain module: records pulls, serves a canned plan."""

    def __init__(self, members=(), results=None, on_pull=None):
        self.pulls, self.plans = [], 0
        self.members, self.results, self.on_pull = list(members), results or {}, on_pull

    def pull(self, sdlc_dir, label, ref, for_goal, config=None, run=None):
        self.pulls.append(str(ref))
        if self.on_pull:
            self.on_pull(ref)
        return self.results.get(str(ref), {"ref": str(ref), "result": "pulled", "reason": "", "actions": []})

    def plan(self, sdlc_dir, label, config=None, run=None):
        self.plans += 1
        return {"label": label, "members": self.members}


def _run(lp, stub, base, src, **kw):
    lp._label_drain = lambda: stub
    lp._LABEL_PULLED.clear()
    lp._LABEL_CAP_SAID.clear()
    return lp._next(base, src, _cfg(lp, base), **kw)


def _ev(base):
    lp = _loop()
    return [e for e in journal_events(lp.ledger, base) if e["kind"] == "run_stop"]


# --- pull on hold ------------------------------------------------------------------------------

def test_a_held_member_pulls_its_outside_blocker_then_is_picked_after_it_closes(capsys):
    lp, stub = _loop(), Stub()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        src = Scoped(["1"], live=CHAIN_OPEN)
        # the blocker, once pulled, is part of the label and offered by the next backlog read
        stub.on_pull = lambda ref: src.order.append(ref)
        kind, goal = _run(lp, stub, base, src)
        assert (kind, goal) == ("goal", "7")             # member 1 held, #7 pulled and picked
        assert stub.pulls == ["7"]
        src.live = list(CHAIN_DONE)                      # #7 closes
        lp._MIRROR_FORCED.clear()
        src.order.remove("7")
        again = lp._next(base, src, _cfg(lp, base))
    assert again == ("goal", "1") and stub.pulls == ["7"]  # picked, and not pulled a second time


@pytest.mark.parametrize("why", ["parked: carries sdlc:parked", "assigned-elsewhere: assigned to bob",
                                 "upper-tier: labelled sdlc:epic", "in another repository (o/r#7)"])
def test_a_refused_blocker_stays_held_and_the_stop_note_names_it(why, capsys):
    lp = _loop()
    res = {"7": {"ref": "7", "result": "refused", "reason": why, "actions": []}}
    stub = Stub(members=[{"number": 1, "verdict": "armed", "reason": "waiting on open #7"}], results=res)
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        src = Scoped(["1"], live=CHAIN_OPEN)
        assert _run(lp, stub, base, src)[0] == "DONE"
    err = capsys.readouterr().err
    assert src.marked == [] and why in err and "#7" in err
    assert "prerequisite #7 left out of the label" in err


def test_sixty_blockers_pull_fifty_once_each_and_name_the_cap(capsys):
    lp = _loop()
    refs = list(range(100, 160))
    body = "".join("**Blocked by:** #%d\n" % r for r in refs)
    issues = [_issue(1, body=body)] + [_issue(r) for r in refs]
    stub = Stub()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=issues)
        src = Scoped(["1"], live=issues)
        _run(lp, stub, base, src)
        before = len(stub.pulls)
        lp._next(base, src, _cfg(lp, base))              # a second pick in the same run
    assert before == 50 and len(stub.pulls) == 50 and len(set(stub.pulls)) == 50
    assert "pull cap of 50" in capsys.readouterr().err


# --- refuse to start ---------------------------------------------------------------------------

def test_open_questions_refuse_the_start_and_claim_nothing():
    lp = _loop()
    stub = Stub(members=[{"number": 3, "verdict": "parked", "reason": "parked (needs_decision)"},
                         {"number": 4, "verdict": "unarmed", "reason": "unarmed"}])
    calls = []
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        src = Scoped(["1"], live=CHAIN_OPEN, calls=calls)
        kind, payload = _run(lp, stub, base, src)
    assert kind == "DONE" and "label_drain.py arm" in payload and "1 unarmed" in payload
    assert src.marked == [] and src.released == [] and stub.pulls == [] and calls == []


# --- stop classes ------------------------------------------------------------------------------

def _stop(monkeypatch, tmp, members):
    lp = _loop()
    monkeypatch.setenv("SIGMA_RUN_ID", "r-1579")
    stub = Stub(members=members)
    base = _base(tmp, mirror=[_issue(9)], extra={"ledger": {"enabled": True, "actor": "me"}, "journal": {"enabled": True}})
    src = Scoped([], live=[_issue(9)])
    assert _run(lp, stub, base, src)[0] == "DONE"
    return [e.get("reason_class") for e in _ev(base)]


def test_open_but_unpickable_members_stop_label_held(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        classes = _stop(monkeypatch, d, [{"number": 5, "verdict": "armed", "reason": "in progress (claimed by a run)"}])
    assert classes == ["label-held"]


def test_no_open_members_stop_backlog_empty(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        classes = _stop(monkeypatch, d, [])
    assert classes == ["backlog-empty"]


# --- unscoped and --feature runs are untouched --------------------------------------------------

#: recorded from the commit before #1579 (the call list of the dependency-gate trial backlog).
GOLDEN_CALLS = [["api", "repos/acme/widget/issues", "--method", "GET", "-f", "labels=sdlc:goal", "-f", "state=open", "-f", "sort=created", "-f", "direction=asc", "-f", "per_page=100", "-f", "page=1"], ["api", "repos/acme/widget/issues", "--method", "GET", "-f", "state=closed", "-f", "sort=updated", "-f", "direction=desc", "-f", "per_page=100", "-f", "page=1"], ["api", "repos/acme/widget/issues", "--method", "GET", "-f", "labels=sdlc:goal", "-f", "state=open", "-f", "sort=created", "-f", "direction=asc", "-f", "per_page=100", "-f", "page=1"], ["api", "repos/acme/widget/issues", "--method", "GET", "-f", "state=closed", "-f", "sort=updated", "-f", "direction=desc", "-f", "per_page=100", "-f", "page=1"]]

GOLDEN_FEATURE = [[["DONE", None]], [["api", "repos/acme/widget/issues", "--method", "GET", "-f", "labels=sdlc:goal", "-f", "state=open", "-f", "sort=created", "-f", "direction=asc", "-f", "per_page=100", "-f", "page=1"], ["api", "repos/acme/widget/issues", "--method", "GET", "-f", "state=closed", "-f", "sort=updated", "-f", "direction=desc", "-f", "per_page=100", "-f", "page=1"]], []]


def _trial_calls(feature=False):
    lp = _loop()
    lp._label_drain = lambda: (_ for _ in ()).throw(AssertionError("label_drain used outside --label"))
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        src = _Board(["1595", "1596", "1594"])
        if feature:
            src.feature = "some-unit"
        out = lp.next_batch(base, src, _cfg(lp, base), max_concurrent=4)
    return out, src.calls, src.marked


def test_unscoped_and_feature_runs_make_the_same_calls_as_before():
    out, calls, marked = _trial_calls()
    assert out == [("goal", "1594"), ("DONE", None)] and marked == ["1594"]
    assert calls == GOLDEN_CALLS
    out2, calls2, marked2 = _trial_calls(feature=True)
    assert [[list(x) for x in out2], calls2, marked2] == GOLDEN_FEATURE


# --- the real `pull`, against the stateful fake gh of the arm tests ---------------------------------

import test_label_drain_arm as A


def _pull(tmp_path, issues, ref=7, extra=None):
    ld = A._mod("label_drain")
    base = A._repo(tmp_path, "alice", extra)
    fk = A.Fake(issues)
    return fk, ld.pull(base, A.LBL, ref, 1, run=fk)


def test_pull_arms_an_unarmed_outside_blocker_like_arm_does(tmp_path):
    fk, res = _pull(tmp_path, [A._issue(7, "sdlc:blocked")])
    assert res["result"] == "pulled"
    assert {"sdlc:goal", "priority:P2", A.LBL} <= fk._names(7) and "sdlc:blocked" not in fk._names(7)
    assert len(fk.comments()) == 1 and "as a prerequisite of #1." in fk.comments()[0][-1]


def test_pull_of_an_already_armed_blocker_adds_only_the_label_and_the_comment(tmp_path):
    fk, res = _pull(tmp_path, [A._issue(7, "sdlc:goal", assignees=("alice",))])
    assert res["result"] == "pulled" and fk._names(7) == {"sdlc:goal", A.LBL}
    assert len(fk.comments()) == 1


@pytest.mark.parametrize("issue, ref, word", [
    (lambda: A._issue(7, "sdlc:parked"), 7, "parked"),
    (lambda: A._issue(7, "sdlc:goal", assignees=("bob",)), 7, "assigned"),
    (lambda: A._issue(7, "epic"), 7, "upper-tier"),
    (lambda: A._issue(7, "sdlc:goal"), "other/repo#9", "another repository"),
])
def test_pull_refuses_without_writing_anything(tmp_path, issue, ref, word):
    fk, res = _pull(tmp_path, [issue()], ref=ref)
    assert res["result"] == "refused" and word in res["reason"]
    assert fk.writes() == []


def test_pull_labels_comments_and_assigns_an_outside_armed_unassigned_blocker(tmp_path):
    """#1824: an armed blocker with no assignee is `unassigned`; pull gives it the label, the comment and the assignee."""
    fk, res = _pull(tmp_path, [A._issue(7, "sdlc:goal")])
    assert res["result"] == "pulled"
    assert A.LBL in fk._names(7) and len(fk.comments()) == 1
    assert [a["login"] for a in fk.by[7]["assignees"]] == ["alice"]


def test_pull_never_takes_an_outside_issue_assigned_to_someone_else(tmp_path):
    fk, res = _pull(tmp_path, [A._issue(7, "sdlc:goal", assignees=("bob",))])
    assert res["result"] == "refused" and "assigned" in res["reason"]
    assert fk.writes() == [] and fk.by[7]["assignees"] == [{"login": "bob"}]


def test_pull_leaves_closed_and_already_labelled_issues_alone(tmp_path):
    fk, res = _pull(tmp_path, [A._issue(7, "sdlc:goal", state="closed")])
    assert res["result"] == "closed" and fk.writes() == []
    (tmp_path / "b").mkdir()
    fk, res = _pull(tmp_path / "b", [A._issue(7, "sdlc:goal", A.LBL, assignees=("alice",))])
    assert res["result"] == "already" and fk.writes() == []


# --- the CLI gesture: `loop.py next --label L` ------------------------------------------------------

def test_next_label_refuses_to_start_on_an_unarmed_member(tmp_path, monkeypatch, capsys):
    lp = _loop()
    base = A._repo(tmp_path, "alice")
    fk = A.Fake([A._issue(1, "sdlc:blocked", A.LBL)])
    cfg = lp.state.load_config(base)
    monkeypatch.setattr(lp.sources, "get_source",
                        lambda d, c: lp.sources.GitHubSource(c, run=fk, sdlc_dir=d))
    for hook in ("_ensure_watcher", "_ensure_ledger_delivery", "_surface_inbox", "_warn_knowledge_graph"):
        monkeypatch.setattr(lp, hook, lambda *a, **k: None)
    assert lp.main(["loop.py", "next", base, "--label", A.LBL]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "DONE"
    assert "label_drain.py arm" in out.err and "1 unarmed" in out.err
    assert fk.writes() == []


# --- #1730: the controls for the memo, the start check and the unscoped no-op -------------------

def _set_run_iteration(base, n):
    p = os.path.join(base, "state", "STATE.md")
    text = open(p).read() if os.path.exists(p) else "iteration: 0\nlast_run: none\n"
    open(p, "w").write(_loop().state._set_line(text, "run_iteration", n))   # keeps every other line


def test_same_ref_held_by_two_goals_is_pulled_once():
    lp, stub = _loop(), Stub()
    issues = [_blocked_by(1, 7), _blocked_by(2, 7), _issue(7)]
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=issues)
        src = Scoped(["1", "2"], live=issues)
        _run(lp, stub, base, src)
    assert stub.pulls == ["7"]                       # held twice, pulled once: the memo, not the cap


def test_start_check_runs_only_before_the_first_claim():
    lp, stub = _loop(), Stub()
    lp._label_drain = lambda: stub
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        src = Scoped(["1"], live=CHAIN_OPEN)
        assert lp._label_not_ready(base, src, _cfg(lp, base)) is None and stub.plans == 1
        _set_run_iteration(base, 1)                  # a claim has been made in this run
        assert lp._label_not_ready(base, src, _cfg(lp, base)) is None and stub.plans == 1


def _scoped_to(label):
    class S(Scoped):
        label_scope = label
    return S


def test_a_second_label_is_checked_after_the_first_label_run_started():
    """#1805: run_iteration is per checkout, so a label change must re-run the start check."""
    lp, stub = _loop(), Stub(members=[{"number": 1, "verdict": "unarmed", "reason": "unarmed"}])
    lp._label_drain = lambda: stub
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        a = _scoped_to("label-a")(["1"], live=CHAIN_OPEN)
        stub.members = []                            # label A is ready
        assert lp._label_not_ready(base, a, _cfg(lp, base)) is None and stub.plans == 1
        _set_run_iteration(base, 1)                  # A's first pick claimed a goal
        stub.members = [{"number": 1, "verdict": "unarmed", "reason": "unarmed"}]
        b = _scoped_to("label-b")(["1"], live=CHAIN_OPEN)
        refusal = lp._label_not_ready(base, b, _cfg(lp, base))
    assert refusal and "label-b is not ready" in refusal and "nothing was claimed or written" in refusal
    assert stub.plans == 2


def test_a_run_started_with_no_recorded_label_is_checked_again():
    """#1805: the no-run-started clause: run_iteration alone (no recorded label) never skips."""
    lp, stub = _loop(), Stub()
    lp._label_drain = lambda: stub
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        src = Scoped(["1"], live=CHAIN_OPEN)
        _set_run_iteration(base, 3)                  # a run is on, but no check ever passed here
        assert lp._label_not_ready(base, src, _cfg(lp, base)) is None
    assert stub.plans == 1


def test_a_recorded_label_with_no_run_started_is_checked_again():
    """#1805: the recorded label alone (run_iteration 0, e.g. after start-run) never skips."""
    lp, stub = _loop(), Stub()
    lp._label_drain = lambda: stub
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        src = Scoped(["1"], live=CHAIN_OPEN)
        assert lp._label_not_ready(base, src, _cfg(lp, base)) is None and stub.plans == 1
        assert lp._label_not_ready(base, src, _cfg(lp, base)) is None and stub.plans == 2


def test_start_run_clears_the_recorded_label():
    """#1823: a new run starts with no check passed, so start-run drops `label_checked`."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        lp.state.set_label_checked(base, "label-a")
        assert lp.state.load_label_checked(base) == "label-a"
        lp.state.start_run(base, now=5.0)
        assert lp.state.load_label_checked(base) == ""


def test_start_run_leaves_every_other_line_byte_identical():
    """#1823: only `label_checked` goes; with or without it, the rest of STATE.md matches a run that never had it."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        with_label, without = _base(d + "/a", mirror=CHAIN_OPEN), _base(d + "/b", mirror=CHAIN_OPEN)
        lp.state.set_label_checked(with_label, "label-a")
        lp.state.start_run(with_label, now=5.0)
        lp.state.start_run(without, now=5.0)
        read = lambda b: open(os.path.join(b, "state", "STATE.md")).read()
        assert "label_checked" not in read(with_label)
        assert read(with_label) == read(without)


def test_next_label_refuses_after_start_run_even_when_a_pick_raised_the_iteration():
    """#1823: A passes, start-run, an unscoped pick raises run_iteration, A is no longer ready -> refused."""
    lp, stub = _loop(), Stub()
    lp._label_drain = lambda: stub
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        a = _scoped_to("label-a")(["1"], live=CHAIN_OPEN)
        assert lp._label_not_ready(base, a, _cfg(lp, base)) is None and stub.plans == 1
        lp.state.start_run(base, now=5.0)
        _set_run_iteration(base, 1)                  # an unscoped pick in the new run
        stub.members = [{"number": 1, "verdict": "unarmed", "reason": "unarmed"}]
        refusal = lp._label_not_ready(base, a, _cfg(lp, base))
    assert refusal and "label-a is not ready" in refusal
    assert stub.plans == 2


def test_a_refusal_does_not_record_the_label():
    lp, stub = _loop(), Stub(members=[{"number": 1, "verdict": "unarmed", "reason": "unarmed"}])
    lp._label_drain = lambda: stub
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        src = Scoped(["1"], live=CHAIN_OPEN)
        assert lp._label_not_ready(base, src, _cfg(lp, base))
        _set_run_iteration(base, 1)
        assert lp._label_not_ready(base, src, _cfg(lp, base))   # still refused: nothing was recorded
    assert stub.plans == 2


def test_next_batch_label_b_refuses_after_label_a_ran(tmp_path, monkeypatch, capsys):
    lp = _loop()
    base = A._repo(tmp_path, "alice")
    lp.state.set_label_checked(base, "other-label")
    _set_run_iteration(base, 2)
    fk = A.Fake([A._issue(1, "sdlc:blocked", A.LBL)])
    monkeypatch.setattr(lp.sources, "get_source",
                        lambda d, c: lp.sources.GitHubSource(c, run=fk, sdlc_dir=d))
    for hook in ("_ensure_watcher", "_ensure_ledger_delivery", "_surface_inbox", "_warn_knowledge_graph"):
        monkeypatch.setattr(lp, hook, lambda *a, **k: None)
    assert lp.main(["loop.py", "next-batch", base, "--label", A.LBL]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "DONE" and "1 unarmed member(s)" in out.err
    assert fk.writes() == []


def test_unscoped_next_never_calls_label_drain():
    lp, stub = _loop(), Stub(members=[{"number": 1, "verdict": "unarmed", "reason": "unarmed"}])
    lp._label_drain = lambda: stub
    lp._LABEL_PULLED.clear()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        src = _Board(["1"], live=CHAIN_OPEN)         # no label_scope: the real guards must decide
        assert lp._label_not_ready(base, src, _cfg(lp, base)) is None
        lp._label_pull_holds(base, src, _cfg(lp, base), "1", "#7")
        lp._next(base, src, _cfg(lp, base))
    assert stub.plans == 0 and stub.pulls == []      # the stub records before anything can swallow it


def _next_batch_label(tmp_path, monkeypatch, capsys, issue):
    lp = _loop()
    base = A._repo(tmp_path, "alice")
    fk = A.Fake([issue])
    cfg = lp.state.load_config(base)
    monkeypatch.setattr(lp.sources, "get_source",
                        lambda d, c: lp.sources.GitHubSource(c, run=fk, sdlc_dir=d))
    for hook in ("_ensure_watcher", "_ensure_ledger_delivery", "_surface_inbox", "_warn_knowledge_graph"):
        monkeypatch.setattr(lp, hook, lambda *a, **k: None)
    assert lp.main(["loop.py", "next-batch", base, "--label", A.LBL]) == 0
    return base, fk, capsys.readouterr()


def test_next_batch_label_refuses_to_start(tmp_path, monkeypatch, capsys):
    base, fk, out = _next_batch_label(tmp_path, monkeypatch, capsys, A._issue(1, "sdlc:blocked", A.LBL))
    assert out.out.strip() == "DONE"
    assert "First run `label_drain.py arm %s %s --apply`, then start again" % (base, A.LBL) in out.err
    assert "1 unarmed member(s)" in out.err and "nothing was claimed or written" in out.err
    assert fk.writes() == []


def test_parked_only_label_refuses_to_start(tmp_path, monkeypatch, capsys):
    base, fk, out = _next_batch_label(tmp_path, monkeypatch, capsys, A._issue(1, "sdlc:parked", A.LBL))
    assert out.out.strip() == "DONE"
    assert "0 unarmed member(s), 1 open question(s)" in out.err
    assert "First run `label_drain.py plan %s %s` and answer the open questions with /sigma-unpark" % (
        base, A.LBL) in out.err
    assert fk.writes() == []


# --- #1732: a recorded decision is honoured by the start check ------------------------------------

import test_label_answer as LA


def _next_label(tmp_path, monkeypatch, capsys, answers=None):
    lp = _loop()
    ld, base, fk, _ = LA.env(tmp_path, [LA.issue(1, "sdlc:blocked", A.LBL)])
    if answers:
        assert not ld.answer(base, A.LBL, answers, run=fk)["errors"]
    cfg = lp.state.load_config(base)
    monkeypatch.setattr(lp.sources, "get_source",
                        lambda d, c: lp.sources.GitHubSource(c, run=fk, sdlc_dir=d))
    for hook in ("_ensure_watcher", "_ensure_ledger_delivery", "_surface_inbox", "_warn_knowledge_graph"):
        monkeypatch.setattr(lp, hook, lambda *a, **k: None)
    assert lp.main(["loop.py", "next", base, "--label", A.LBL]) == 0
    return capsys.readouterr(), ld, base, fk


def test_next_label_starts_after_leave_them(tmp_path, monkeypatch, capsys):
    out, *_ = _next_label(tmp_path, monkeypatch, capsys, {"label_set:" + A.LBL: "leave them"})
    assert "is not ready" not in out.err and "label_drain.py arm" not in out.err


def test_next_label_still_refuses_unarmed_without_a_decision(tmp_path, monkeypatch, capsys):
    out, *_ = _next_label(tmp_path, monkeypatch, capsys)
    assert "is not ready" in out.err and "label_drain.py arm" in out.err and "1 unarmed" in out.err


def test_refusal_text_counts_real_open_questions(tmp_path, monkeypatch, capsys):
    out, ld, base, fk = _next_label(tmp_path, monkeypatch, capsys)
    n = len(ld.plan(base, A.LBL, run=fk)["questions"])
    assert n >= 1 and "%d open question(s)" % n in out.err


# --- #1763: the unscoped terminal path never enters label drain -------------------------------------

def test_unscoped_terminal_done_never_plans_a_label():
    lp, stub = _loop(), Stub(members=[{"number": 1, "verdict": "unarmed", "reason": "unarmed"}])
    lp._label_drain = lambda: stub
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=[_issue(9)])
        src = _Board([], live=[_issue(9)])           # nothing pickable, no label_scope
        assert lp._next(base, src, _cfg(lp, base))[0] == "DONE"
        assert lp._label_drained_note(base, src, _cfg(lp, base), set()) == "backlog-empty"
    assert stub.plans == 0 and stub.pulls == []


# --- the "filed during the drain" line (#1809) ---------------------------------------------------

def _note(monkeypatch, capsys, started_at, created):
    lp = _loop()
    stub = Stub(members=[{"number": 5, "verdict": "armed", "reason": "in progress (claimed by a run)"}])
    lp._label_drain = lambda: stub
    lp._LABEL_PULLED.clear()
    outside = dict(_issue(7), created_at=created)
    monkeypatch.setattr(lp.state, "load_cursor", lambda sdlc_dir: {"run_started_at": started_at})
    monkeypatch.setattr(lp.sources, "fetch_issues_rest", lambda *a, **k: [outside])
    src = Scoped([], live=[])
    src._run = object()
    with tempfile.TemporaryDirectory() as d:
        assert lp._label_drained_note(d, src, {}, ()) == "label-held"
    return capsys.readouterr().err


def test_a_goal_filed_before_the_drain_is_not_listed_when_no_run_start_is_recorded(monkeypatch, capsys):
    err = _note(monkeypatch, capsys, 0.0, "2026-10-01T00:00:00Z")
    assert "has 1 open member" in err and "filed during the drain" not in err


def test_a_goal_filed_after_the_recorded_drain_start_is_listed(monkeypatch, capsys):
    err = _note(monkeypatch, capsys, 1791590400.0, "2026-10-17T00:00:00Z")
    assert "filed during the drain, not in the label: #7" in err


def test_a_goal_filed_before_the_recorded_drain_start_is_not_listed(monkeypatch, capsys):
    err = _note(monkeypatch, capsys, 1791590400.0, "2026-10-01T00:00:00Z")
    assert "filed during the drain" not in err


# --- the drain start under the documented gesture (#1832) ----------------------------------------

def _stamp(offset):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


def _documented_gesture_stop(monkeypatch, capsys, made):
    """No `start-run`: the start is recorded only by the start check, which `_label_not_ready` runs."""
    lp = _loop()
    stub = Stub(members=[{"number": 5, "verdict": "armed", "reason": "in progress (claimed by a run)"}])
    lp._label_drain = lambda: stub
    lp._LABEL_PULLED.clear()
    monkeypatch.setattr(lp.sources, "fetch_issues_rest",
                        lambda *a, **k: [dict(_issue(7), created_at=made)])
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        src = Scoped(["1"], live=CHAIN_OPEN)
        src._run = object()
        stub.members = []
        assert lp._label_not_ready(base, src, _cfg(lp, base)) is None       # the start check passes
        assert lp.state.load_cursor(base)["run_started_at"] == 0.0           # and no run start exists
        stub.members = [{"number": 5, "verdict": "armed", "reason": "in progress (claimed by a run)"}]
        assert lp._label_drained_note(base, src, _cfg(lp, base), ()) == "label-held"
    return capsys.readouterr().err


def test_documented_gesture_lists_a_goal_filed_after_the_first_pick(monkeypatch, capsys):
    err = _documented_gesture_stop(monkeypatch, capsys, _stamp(+3600))
    assert "filed during the drain, not in the label: #7" in err


def test_documented_gesture_does_not_list_a_goal_filed_before_the_first_pick(monkeypatch, capsys):
    err = _documented_gesture_stop(monkeypatch, capsys, _stamp(-3600))
    assert "has 1 open member" in err and "filed during the drain" not in err


def test_a_repeated_pass_keeps_the_first_start_and_a_new_label_gets_its_own():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CHAIN_OPEN)
        lp.state.set_label_checked(base, "label-a", now=1000.0)
        lp.state.set_label_checked(base, "label-a", now=2000.0)
        assert lp.state.load_label_checked_at(base) == 1000.0
        lp.state.set_label_checked(base, "label-b", now=3000.0)
        assert lp.state.load_label_checked_at(base) == 3000.0
        lp.state.set_label_checked(base, "label-b", now=4000.0)
        lp.state.start_run(base, now=5000.0)
        assert lp.state.load_label_checked_at(base) == 0.0 and lp.state.load_label_checked(base) == ""
def test_readiness_refusal_names_unassigned_members_in_its_count(tmp_path, monkeypatch, capsys):
    """#1824: an armed member with no assignee is `unassigned`, counted beside the unarmed ones."""
    base, fk, out = _next_batch_label(tmp_path, monkeypatch, capsys, A._issue(1, "sdlc:goal", A.LBL))
    assert out.out.strip() == "DONE"
    assert "0 unarmed member(s), 1 unassigned member(s), 1 open question(s)" in out.err
    assert "First run `label_drain.py arm %s %s --apply`, then start again" % (base, A.LBL) in out.err
    assert fk.writes() == []
