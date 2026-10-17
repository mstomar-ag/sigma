"""#1578: `label_drain.py plan` assembles every question a person answers before a drain starts.

Fake `gh` only (the helpers of test_label_drain_plan): no network, no model. The questions come from
the question bank, never from improvised text.
"""
import json

import pytest

import test_label_drain_plan as P
from test_label_drain_plan import LBL, _goal, _issue, _mod, _repo, _runner

DONE = "## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n"
KIND = "Parked by Sigma: waiting on the other issue.\nsigma-qkind: dependency"
NOKIND = "Parked by Sigma: should the export keep the legacy column?"
ME = "me-login"
PAID = "Paid step: runpodctl create pod for the render\n\n" + DONE


def _owner_runner(issues, calls=None):
    """The shared fake `gh`, with every comment attributed to an OWNER (a declared kind is honoured)."""
    inner = _runner(issues, calls)

    def run(args):
        out = inner(args)
        if str(args[0]) == "api" and str(args[1]).endswith("/comments"):
            out = json.dumps([dict(c, author_association="OWNER") for c in json.loads(out)])
        return out
    return run


def _plan(tmp_path, issues, hard_stops=True):
    ld = _mod("label_drain")
    base = _repo(tmp_path, assignee=ME)
    cfgp = tmp_path / ".sdlc" / "config.json"
    cfg = json.loads(cfgp.read_text())
    if hard_stops:
        cfg["hard_stops"] = {"enabled": True}
    cfgp.write_text(json.dumps(cfg))
    return ld.plan(base, LBL, run=_owner_runner(issues))


def _ids(p):
    return [q["id"] for q in p["questions"]]


def _q(p, qid):
    return next(q for q in p["questions"] if q["id"] == qid)


def _g(n, *a, **kw):
    return _goal(n, *a, **{"assignees": (ME,), **kw})


def _scenario():
    return [
        _g(1, "sdlc:parked", comments=[KIND], body="Blocked by #50\n\n" + DONE),   # declared park
        _g(2, "sdlc:parked", comments=[NOKIND]),                                    # undeclared park
        _issue(3, LBL, body=DONE), _issue(4, LBL, body=DONE), _issue(5, LBL, body=DONE),   # unarmed
        _g(6, body=PAID),                                                           # paid, armed
        _g(7, body="Blocked by #60\n\n" + DONE),                                    # waits on elsewhere
        _issue(50, "sdlc:goal", assignees=(ME,), body=DONE),
        _issue(60, "sdlc:goal", assignees=("someone-else",), body=DONE),               # assigned elsewhere
    ]


def test_full_scenario_yields_exactly_the_expected_questions(tmp_path):
    p = _plan(tmp_path, _scenario())
    assert _ids(p) == ["dependency:1", "kind:2", "dependency:60", "label_set:" + LBL, "irreversible:6"]
    assert p["ready"] is False
    u = _mod("unpark")
    for q in p["questions"]:
        assert set(q) >= {"id", "kind", "area", "ask", "options", "unblocks"}
        assert q["area"] == "repo"
    assert _q(p, "dependency:1")["options"] == ["wait for it", "work around it", "drop this"]
    assert _q(p, "dependency:1")["ask"].startswith("#50 is still open")
    k = _q(p, "kind:2")
    assert k["kind"] == "kind" and "label_set" not in k["options"]
    assert set(k["options"]) == set(_mod("qkind").QKINDS) - {"label_set"}
    assert _q(p, "dependency:60")["options"] == ["take it over", "leave it and wait"]
    assert _q(p, "dependency:60")["unblocks"] == [7]
    arm = _q(p, "label_set:" + LBL)
    assert arm["options"] == ["arm them", "arm only the ones that can start", "leave them"]
    assert arm["unblocks"] == [3, 4, 5]
    spend = _q(p, "irreversible:6")
    assert spend["options"] == ["go ahead", "do not", "change the plan"] and spend["unblocks"] == [6]
    assert spend["ask"] == u.route_slot("irreversible")["ask"]


def test_nothing_to_ask_is_ready_with_an_empty_list(tmp_path):
    p = _plan(tmp_path, [_g(1), _g(2, body="Blocked by #1\n\n" + DONE)])
    assert p["questions"] == [] and p["ready"] is True


def test_paid_step_is_not_asked_unless_the_hard_stop_net_recognises_it(tmp_path):
    p = _plan(tmp_path, [_g(6, body=PAID)], hard_stops=False)
    assert p["questions"] == [] and p["ready"] is True
    (tmp_path / "b").mkdir()
    p = _plan(tmp_path / "b", [_g(6, body="Paid step: tidy the docs\n\n" + DONE)])
    assert p["questions"] == []


def test_spend_sentence_is_printed_and_pinned(tmp_path):
    p = _plan(tmp_path, [_g(6, body=PAID)])
    want = ("Approval is single-use for each approved item, and Sigma does not meter external spend, "
            "so no dollar cap is enforced.")
    assert _q(p, "irreversible:6")["note"] == want
    assert want in _mod("label_drain").render_plan(p)


def test_text_output_numbers_the_questions_and_says_ready(tmp_path):
    ld = _mod("label_drain")
    text = ld.render_plan(_plan(tmp_path, _scenario()))
    assert "questions: 5 open, ready: false" in text
    assert "  2. [kind:2] Which kind of question is this?" in text


def test_text_output_of_a_clean_label_says_ready_true(tmp_path):
    ld = _mod("label_drain")
    assert "questions: 0 open, ready: true" in ld.render_plan(_plan(tmp_path, [_g(1)]))


def test_an_upper_tier_blocker_gets_its_bank_question(tmp_path):
    p = _plan(tmp_path, [_g(1, body="Blocked by #70\n\n" + DONE), _issue(70, "epic", body=DONE)])
    q = _q(p, "dependency:70")
    assert q["options"] == ["close the dependency", "replace the dependency"] and q["unblocks"] == [1]


def test_plan_makes_no_write_call_with_questions(tmp_path):
    ld = _mod("label_drain")
    base = _repo(tmp_path, assignee=ME)
    calls = []
    ld.plan(base, LBL, run=_owner_runner(_scenario(), calls))
    assert P._writes(calls) == []


# --- the closed kind list ------------------------------------------------------------------------

def test_label_set_is_a_known_kind_and_nonsense_is_not():
    q = _mod("qkind")
    assert "label_set" in q.QKINDS
    assert q.parse_line(q.render_line("label_set")) == "label_set"
    with pytest.raises(ValueError):
        q.render_line("nonsense")
    assert q.parse_line("sigma-qkind: nonsense") is None


def test_every_kind_has_a_bank_entry_and_label_set_is_learnable():
    u = _mod("unpark")
    for k in u.qkind.QKINDS:
        assert k in u.QUESTIONS, k
    assert u.route_slot("label_set")["id"] == "route"
    assert u.valid_options("label_set") == ["arm them", "arm only the ones that can start", "leave them"]
    assert set(u.option_gestures("label_set").values()) == {"none"}      # none of them unparks


def test_label_set_bank_entries_obey_the_wording_rules():
    u = _mod("unpark")
    jargon = ("reason_class", "decision_tier", "offboard", "sdlc:", "slot", "lifecycle", "predicate",
              "idempotent", "park", "sigma")
    for slot in u.QUESTIONS["label_set"]:
        ask = slot["ask"]
        assert ask.endswith("?") and ask.count("?") == 1 and ask.count(". ") == 0, ask
        assert not any(w in ask.lower() for w in jargon), ask


def test_a_member_no_gate_can_classify_gets_the_gate_hold_question(tmp_path):
    p = _plan(tmp_path, [_g(8, body="Feature: billing\n\n" + DONE)])
    q = _q(p, "scope_hold:8")
    assert q["options"] == ["add the repository", "drop this"] and q["unblocks"] == [8]


def test_after_a_declare_the_next_plan_asks_the_members_own_question_until_answered(tmp_path):
    """#1806: answer the plan's own "which kind" question, then plan again."""
    import test_label_answer as A
    ld, base, fk, _ = A.env(tmp_path, [A.issue(1, "sdlc:parked", LBL, comments=[A.UNDECLARED_PARK],
                                               assignees=("alice",))])
    first = ld.plan(base, LBL, run=fk)
    assert _ids(first) == ["kind:1"] and first["ready"] is False
    A.ask(ld, base, fk, {"kind:1": "dependency"})
    nxt = ld.plan(base, LBL, run=fk)
    assert _ids(nxt) == ["dependency:1"] and nxt["ready"] is False
    assert _q(nxt, "dependency:1")["kind"] == "dependency"


def test_an_armed_unassigned_member_is_named_by_the_arming_question_and_holds_ready(tmp_path):
    """#1807: it is not ready until the arming answer is recorded, and the question unblocks it."""
    p = _plan(tmp_path, [_issue(1, LBL, "sdlc:goal", body=DONE), _g(2)])
    q = _q(p, "label_set:" + LBL)
    assert q["unblocks"] == [1]
    assert p["ready"] is False
