"""#1935: `take it over` assigns the runner named by `plan --forecast --as`, not the login that planned.

A stateful fake `gh`; no network. The plan is made as alice (the configured assignee) for carol.

Controls (each clause alone, then restored; every one seen red): enact assigning the source login again; the
planner not recorded; the `Planned by` suffix dropped; the suffix written when planner and runner are the same.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402,F401
import test_chain_arm as tca  # noqa: E402
from test_chain_asked_up_front import Fk, answer_all, goal  # noqa: E402

NOW = tca.NOW


def _issues():
    return [goal(1, 2, 3, who=None), goal(2, who="bob"), goal(3, who="bob")]


def _plan(tmp_path, as_login=None, issues=None):
    ch = tca._chain()
    base = tca._repo(tmp_path, "alice")
    import json
    cp = pathlib.Path(base) / "config.json"
    cfg = json.loads(cp.read_text())
    cfg["discovery"]["dag"] = {"mode": "off"}
    cp.write_text(json.dumps(cfg))
    fk = Fk(issues if issues is not None else _issues())
    ch.plan(base, 1, run=fk, now=NOW, as_login=as_login)
    fk.calls.clear()
    fk.write_count = 0
    return ch, base, fk


def test_take_over_assigns_the_runner_named_by_as_not_the_planner(tmp_path):
    ch, base, fk = _plan(tmp_path, "carol")
    rec = tca._rec(base)
    assert rec["runner"] == "carol" and rec["planner"] == "alice"
    answer_all(ch, base, fk, **{"elsewhere:2": "take it over"})
    ch.arm(base, 1, run=fk, now=NOW)
    assert fk.assignees(2) == ["carol"]                      # old removed, planner not assigned
    assert fk.assignees(3) == ["bob"]


def test_no_as_assigns_the_planner_as_before(tmp_path):
    ch, base, fk = _plan(tmp_path)
    assert "planner" not in tca._rec(base)
    answer_all(ch, base, fk, **{"elsewhere:2": "take it over"})
    ch.arm(base, 1, run=fk, now=NOW)
    assert fk.assignees(2) == ["alice"]


def test_as_equal_to_the_planner_records_no_planner(tmp_path):
    ch, base, fk = _plan(tmp_path, "alice")
    assert tca._rec(base)["runner"] == "alice" and "planner" not in tca._rec(base)


def test_every_elsewhere_ask_says_who_planned_for_whom_only_when_they_differ(tmp_path):
    ch, base, fk = _plan(tmp_path, "carol")
    asks = [q["ask"] for q in ch.chain_asks.elsewhere_questions(tca._rec(base))]
    assert len(asks) == 2 and all("Planned by alice for carol." in a for a in asks), asks
    ch, base, fk = _plan(tmp_path / "same")
    asks = [q["ask"] for q in ch.chain_asks.elsewhere_questions(tca._rec(base))]
    assert asks and not any("Planned by" in a for a in asks), asks


def test_a_record_without_a_runner_falls_back_to_the_source_login(tmp_path):
    ch, base, fk = _plan(tmp_path)
    answer_all(ch, base, fk, **{"elsewhere:2": "take it over"})
    rec = tca._rec(base)
    rec["runner"] = None
    tca._put(base, rec)
    ch.arm(base, 1, run=fk, now=NOW)
    assert fk.assignees(2) == ["alice"]


def test_main_passes_as_through_to_plan(tmp_path, capsys):
    ch = tca._chain()
    base = tca._repo(tmp_path, "alice")
    fk = Fk(_issues())
    assert ch.main(["chain.py", "plan", "--forecast", "--as", "carol", base, "1"], run=fk, now=NOW) == 0
    assert tca._rec(base)["runner"] == "carol"
