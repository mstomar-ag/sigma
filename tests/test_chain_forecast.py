"""#1907: `chain.py plan <N> --forecast` and the sheet's forecast -- how far a run will really get.

The forecast and `run` share their rules (`chain_forecast.ready_members`, `stuck_reason`, and the assignee clause
`label_drain.assignee_gate`), so the tests that matter play a real run (`chain.run`, cycle by cycle, against a
stateful fake `gh` and an independent model of the loop's scoped pick) and compare it with the forecast read from
the same plan record: for the runner who owns most members, for one who owns none, and at width 2.

Controls (each clause alone, then restored): the assignee clause left out of the forecast; the claim rule's
verdict table row for an unclaimable verdict made claimable; the width left out of the ready set; the stuck order
weakened (held before parked); the width plan back to its own copy of the claim rule; the run's stuck rule
changed in `run` only, the run's pick ignoring the assignee filter, and a run at another width (the comparison
must go red each time).
"""
import io
import json
import pathlib
import sys
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_chain_arm as tca  # noqa: E402
import test_chain_run as tcr  # noqa: E402
import test_label_answer as tla  # noqa: E402
from test_label_drain_arm import DW  # noqa: E402

NOW = tca.NOW
KEPT_5 = "#5 is parked and you answered to keep it parked; release it (answer `release it`) to include it"
PARK = "Parked by Sigma: waiting on the other issue.\nsigma-qkind: dependency"


def _backlog():
    """Goal 1 waits on 2, 4, 6, 7 and 8. 2 waits on 3. 7 waits on 5. Alice owns 1, 2, 3, 5, 7, 8; bob owns 4;
    5 is parked; 6 is an upper-tier story."""
    def goal(n, who, waits=(), *labels):
        body = "".join("Blocked by #%d\n" % w for w in waits) + "\n" + DW
        return tla.issue(n, "sdlc:goal", *labels, assignees=(who,), body=body, **(
            {"comments": [PARK]} if "sdlc:parked" in labels else {}))
    return [goal(1, "alice", (2, 4, 6, 7, 8)), goal(2, "alice", (3,)), goal(3, "alice"), goal(4, "bob"),
            goal(5, "alice", (), "sdlc:parked"), tla.issue(6, "story", body=DW), goal(7, "alice", (5,)),
            goal(8, "alice")]


def _free():
    """Everything can be claimed by alice: 1 waits on 2, 2 on 3."""
    return [tla.issue(1, "sdlc:goal", assignees=("alice",), body="Blocked by #2\n\n" + DW),
            tla.issue(2, "sdlc:goal", assignees=("alice",), body="Blocked by #3\n\n" + DW),
            tla.issue(3, "sdlc:goal", assignees=("alice",), body=DW)]


def _scene(tmp_path, issues):
    base, fk = tca._scene(tmp_path, issues=issues)
    ch = tca._chain()
    ch.arm(base, 1, run=fk, now=NOW)
    assert tca._rec(base).get("armed_at"), "the fixture chain was not armed"
    return ch, base, fk


def _cfg(ch, base, runner, width=1):
    cfg = ch._load("state").load_config(base)
    cfg["discovery"]["github"]["assignee"] = runner
    if width > 1:
        cfg["chain"] = {"parallel": width}
        cfg["parallel"] = {"goals": {"enabled": True, "max_concurrent": width}}
        cfg["work"] = {"enabled": True}
    return cfg


class RunnerPick:
    """The loop's scoped pick as far as the driver can tell, written out independently of the production rule:
    label scoped, leased, assigned to the runner, armed, not parked, not an upper tier, nothing open behind it."""

    def __init__(self, fk, base, runner, honour_assignee=True):
        self.fk, self.base, self.runner, self.honour = fk, base, runner, honour_assignee
        self.claims = []

    def __call__(self, label, skip, max_concurrent=None):
        rows = {r["number"]: r for r in tca._rec(self.base)["members"]}
        got = []
        for n in rows:
            issue = self.fk.by[n]
            names = {l["name"] for l in issue["labels"]}
            who = {a["login"] for a in issue["assignees"]}
            if issue["state"] == "closed" or label not in names or n in self.claims or "sdlc:goal" not in names \
                    or names & {"story", "sdlc:parked", "sdlc:in-progress"}:
                continue
            if self.honour and self.runner not in who:
                continue
            if any(self.fk.by[w]["state"] != "closed" for w in rows[n].get("waits_on", [])):
                continue
            got.append(n)
            self.claims.append(n)
            if len(got) == (max_concurrent or 1):
                break
        return got


def _drive(ch, base, fk, runner, width=1, **pick_kw):
    """Run cycle after cycle, closing what each cycle claims (every goal merges). -> what the run did."""
    cfg = _cfg(ch, base, runner, width)
    pick = RunnerPick(fk, base, runner, **pick_kw)
    picked, cycles, res = [], 0, None
    for i in range(30):
        res = ch.run(base, 1, pick=pick, now=NOW + 10 + i, run=fk, sweep=tcr._sweep, config=cfg)
        if res["picked"]:
            cycles += 1
            picked += res["picked"]
            tcr._close(fk, *res["picked"])
        if res["state"] in ("stuck", "done", "capped"):
            break
    return {"picked": sorted(picked), "cycles": cycles, "state": res["state"], "reason": res["reason"],
            "detail": res["detail"]}


def _forecast(ch, base, runner, width=1):
    return ch.chain_forecast.forecast(tca._rec(base), _cfg(ch, base, runner, width), runner)


def _diff(fc, act):
    """What the forecast says that the run did not do. Empty when they agree."""
    want = {"picked": sorted(fc["finish"]), "cycles": fc["cycles"], "reason": fc["reason"], "detail": fc["detail"],
            "state": "done" if fc["reason"] is None else "stuck"}
    if fc["reason"] is None:                     # a finished run words its own summary; only a stop is compared
        want.pop("detail")
    return {k: (want[k], act[k]) for k in want if want[k] != act[k]}


CASES = [("alice", 1, [3, 8, 2], 3), ("alice", 2, [3, 8, 2], 2), ("carol", 1, [], 0), ("bob", 1, [4], 1)]


# 1 -- the forecast is what the run does -------------------------------------------------------------------

@pytest.mark.parametrize("runner,width,finish,cycles", CASES)
def test_forecast_equals_run(tmp_path, runner, width, finish, cycles):
    ch, base, fk = _scene(tmp_path, _backlog())
    fc = _forecast(ch, base, runner, width)
    act = _drive(ch, base, fk, runner, width)
    assert _diff(fc, act) == {}, (fc["line"], act)
    # and both are what the fixture says, so two wrong answers cannot agree
    assert sorted(fc["finish"]) == sorted(finish) and fc["cycles"] == cycles, fc
    assert act["picked"] == sorted(finish) and act["state"] == "stuck" and act["reason"] == "decision", act
    assert act["detail"] == KEPT_5 == fc["detail"]      # --defaults kept #5 parked: the stop says so
    assert fc["width"] == width


def test_forecast_equals_run_when_everything_finishes(tmp_path):
    ch, base, fk = _scene(tmp_path, _free())
    fc = _forecast(ch, base, "alice")
    act = _drive(ch, base, fk, "alice")
    assert _diff(fc, act) == {} and act["state"] == "done" and act["picked"] == [1, 2, 3], (fc, act)
    assert fc["line"] == "a run from alice can finish all 3 of 3" and fc["stops"] == []


def test_stop_reason_follows_the_runner(tmp_path):
    """A runner who owns none of the work stops at the first thing a person must do (the parked goal), never
    on a goal merely assigned to someone else; with nothing parked it stops on the assignment."""
    issues = [tla.issue(1, "sdlc:goal", assignees=("alice",), body="Blocked by #2\n\n" + DW),
              tla.issue(2, "sdlc:goal", assignees=("alice",), body=DW)]
    ch, base, fk = _scene(tmp_path, issues)
    fc = _forecast(ch, base, "carol")
    act = _drive(ch, base, fk, "carol")
    assert _diff(fc, act) == {} and fc["finish"] == [] and act["reason"] == "claimed", (fc, act)
    assert [(s["number"], s["marker"]) for s in fc["stops"]] == [(2, "someone else's")], fc["stops"]


# 2 -- the markers and the one line --------------------------------------------------------------------------

def test_markers_and_line(tmp_path):
    ch, base, fk = _scene(tmp_path, _backlog())
    got = {m["number"]: (m["marker"], m["after"]) for m in _forecast(ch, base, "alice")["members"]}
    assert got == {3: ("ready now", []), 8: ("ready now", []), 2: ("after", [3]), 4: ("someone else's", []),
                   5: ("needs decision", []), 6: ("needs decision", []), 7: ("after", [5]),
                   1: ("after", [2, 4, 6, 7, 8])}, got
    fc = _forecast(ch, base, "alice")
    assert fc["line"] == ("a run from alice can finish 3 of 8 before it stops on decision: "
                          "#4 someone else's (assigned to someone else), #5 needs decision (parked; you answered to keep it parked), "
                          "#6 needs decision (an upper-tier issue, plan its slices)"), fc["line"]
    got = {m["number"]: m["marker"] for m in _forecast(ch, base, "bob")["members"]}
    assert got[4] == "ready now" and got[3] == got[8] == "someone else's"
    assert set(ch.chain_forecast.MARKERS) >= set(got.values())


# 3 -- the verbs ------------------------------------------------------------------------------------------

def _say(ch, argv, **kw):
    out = io.StringIO()
    with redirect_stdout(out):
        code = ch.main(argv, **kw)
    return code, out.getvalue()


def test_plan_forecast_verb(tmp_path):
    base, fk = tca._scene(tmp_path, issues=_backlog(), approve=False)
    ch = tca._chain()
    code, out = _say(ch, ["chain.py", "plan", base, "1", "--forecast"], run=fk, now=NOW)
    assert code == 0, out
    assert "Forecast, a run from alice (width 1):" in out and "  #3 ready now" in out
    assert "  #2 after #3" in out and "  #1 after #2, #4, #6, #7, #8" in out
    assert "Forecast: a run from alice can finish 3 of 8 before it stops on decision:" in out
    code, out = _say(ch, ["chain.py", "plan", base, "1", "--forecast", "--as", "carol"], run=fk, now=NOW)
    assert code == 0 and "Forecast: a run from carol can finish 0 of 8 before it stops on decision:" in out, out
    code, out = _say(ch, ["chain.py", "plan", base, "1", "--forecast", "--json"], run=fk, now=NOW)
    assert code == 0 and json.loads(out)["forecast"]["finish"] == [3, 8, 2]
    assert "forecast" not in tca._rec(base), "the record keeps the plan, not the forecast"
    # no forecast unless asked, and --as means nothing without it
    assert "Forecast" not in _say(ch, ["chain.py", "plan", base, "1"], run=fk, now=NOW)[1]
    for bad in (["--as", "carol"], ["--forecast", "--as"], ["--forecast", "--as", ""]):
        assert _say(ch, ["chain.py", "plan", base, "1"] + bad, run=fk, now=NOW)[0] == 2, bad


def test_sheet_carries_the_forecast(tmp_path):
    base, fk = tca._scene(tmp_path, issues=_backlog(), approve=False)
    ch = tca._chain()
    code, out = _say(ch, ["chain.py", "sheet", base, "1"], now=NOW)
    assert code == 0 and "  #2 after #3" in out        # the per-member markers ride in the sheet's blocks (#1908)
    assert out.index("Forecast: a run from alice can finish 3 of 8") < out.index("Ready and waiting") \
        < out.index("Decisions (")
    code, out = _say(ch, ["chain.py", "sheet", base, "1", "--as", "carol"], now=NOW)
    assert "Forecast: a run from carol can finish 0 of 8" in out
    assert _say(ch, ["chain.py", "sheet", base, "1", "--as"], now=NOW)[0] == 2
    assert "Nothing has been changed yet." in out


def test_forecast_makes_no_gh_call(tmp_path):
    base, fk = tca._scene(tmp_path, issues=_backlog(), approve=False)
    fk.calls.clear()
    _say(tca._chain(), ["chain.py", "sheet", base, "1", "--as", "bob"], now=NOW)
    assert fk.calls == []


# 4 -- the record carries what a forecast for another runner needs -----------------------------------------

def test_record_holds_logins_not_text(tmp_path):
    base, fk = tca._scene(tmp_path, issues=_backlog(), approve=False)
    rec = tca._rec(base)
    rows = {r["number"]: r for r in rec["members"]}
    assert rec["runner"] == "alice" and rec["assignee_filter"] is True
    assert rows[4]["assignees"] == ["bob"] and rows[3]["assignees"] == ["alice"]
    assert "issue " not in json.dumps(rec), "a title reached the record"
    # planned by carol, 3 reads as someone else's and the record remembers what it was before that clause
    ch = tca._chain()
    cfg = _cfg(ch, base, "carol")
    p = ch.plan(base, 1, config=cfg, run=fk, now=NOW)
    r3 = next(r for r in p["members"] if r["number"] == 3)
    assert r3["verdict"] == "assigned-elsewhere" and r3["base_verdict"] == "armed"
    assert ch.chain_forecast.forecast(p, cfg, "alice")["finish"] == [3, 8, 2]


def test_at_me_is_resolved_with_an_explicit_get(tmp_path):
    """`_GetOnly` refuses `gh api user` without a method; the planner used to lose the login that way."""
    ch = tca._chain()
    seen = []

    def gh(args):
        seen.append(list(args))
        return "Carol\n"
    ro = ch._GetOnly(gh)

    class Src:
        assignee = "@me"
    assert ch._runner_login(ro, Src()) == "carol" and "GET" in seen[0]
    assert ch._runner_login(ro, type("S", (), {"assignee": None})()) is None


def test_plan_resolves_at_me_through_the_read_only_guard(tmp_path, capfd):
    """#1934: every `gh api user` a plan makes carries `--method GET`, so the guard lets the assignee check run."""
    base, fk = tca._scene(tmp_path, issues=_backlog(), approve=False)
    fk.calls.clear()
    ch = tca._chain()
    ch.plan(base, 1, config=_cfg(ch, base, "@me"), run=fk, now=NOW)
    users = [c for c in fk.calls if c[:2] == ["api", "user"]]
    assert users and all("GET" in c for c in users), users
    assert tca._rec(base)["assignee_filter"] is True
    # the backlog fetch's own resolution (the source method, not the planner's) also runs under the guard
    ro = ch._GetOnly(lambda args: "alice\n" if list(args[:2]) == ["api", "user"] else fk(args))
    src = ch._load("sources").GitHubSource(_cfg(ch, base, "@me"), run=ro)
    assert src._assignee_login() == "alice"
    assert "refused" not in capfd.readouterr().err


def test_guard_still_refuses_write_shaped_api_calls():
    ch = tca._chain()
    gate = ch._GetOnly(lambda args: "{}")
    for bad in (["api", "user", "-f", "name=x"], ["api", "user", "-F", "a=b"], ["api", "user", "--input", "f"],
                ["api", "user", "--method", "PATCH", "-f", "name=x"], ["api", "user", "--jq", ".login"]):
        with pytest.raises(RuntimeError):
            gate(bad)
    assert gate(["api", "user", "--method", "GET", "--jq", ".login"]) == "{}"


def test_map_plan_says_what_it_cannot_know():
    ch = tca._chain()
    rec = {"verdict_basis": "map-life", "runner": None, "assignee_filter": None,
           "members": [{"number": 1, "verdict": "armed", "waits_on": []}], "externals": []}
    fc = ch.chain_forecast.forecast(rec, {}, "carol")
    assert fc["finish"] == [1] and any("map" in n for n in fc["notes"]), fc


# 5 -- the table and the shared rules --------------------------------------------------------------------

def _rows(*specs):
    return [dict({"waits_on": [], "unresolved": [], "in_progress": False}, **s) for s in specs]


def test_table_is_one_row_per_verdict(tmp_path, monkeypatch):
    ch = tca._chain()
    cf = ch.chain_forecast
    rec = {"members": [{"number": 1, "verdict": "held-by-gate", "waits_on": []},
                       {"number": 2, "verdict": "checkpoint", "waits_on": []},
                       {"number": 3, "verdict": "something-new", "waits_on": []},
                       {"number": 4, "verdict": "armed", "in_progress": True, "waits_on": []},
                       {"number": 5, "verdict": "armed", "blocked": True, "waits_on": []},
                       {"number": 6, "verdict": "armed", "waits_on": []}]}
    got = {m["number"]: (m["marker"], m["why"]) for m in cf.forecast(rec)["members"]}
    assert got[1] == ("not workable", "held by a gate") and got[2][0] == "checkpoint"
    assert got[3] == ("not workable", "an unrecognised state"), "an unknown verdict must never be promised"
    assert got[4][0] == "someone else's" and got[5][0] == "not workable" and got[6][0] == "ready now"
    assert cf.forecast(rec)["finish"] == [6]
    # a new verdict is one row, and nothing else changes
    monkeypatch.setitem(cf.VERDICTS, "something-new", ("checkpoint", False, "a stop on purpose"))
    assert {m["number"]: m["marker"] for m in cf.forecast(rec)["members"]}[3] == "checkpoint"
    assert cf.forecast(rec)["finish"] == [6]


def test_unresolved_prerequisite_is_a_stop():
    cf = tca._chain().chain_forecast
    rec = {"members": [{"number": 1, "verdict": "armed", "waits_on": [2]}, {"number": 2, "verdict": "armed", "waits_on": []}],
           "externals": [{"ref": "x/y#9", "kind": "other-repository", "needed_by": [2]}]}
    fc = cf.forecast(rec)
    assert fc["finish"] == [] and fc["reason"] == "external" and fc["stops"][0]["number"] == 2, fc
    assert fc["members"][1]["marker"] == "not workable" and fc["members"][0]["marker"] == "after"


def test_stuck_order_is_a_persons_order():
    cf = tca._chain().chain_forecast
    p = {"members": _rows({"number": 1, "verdict": "held-by-gate"}, {"number": 2, "verdict": "parked"}), "outside": []}
    assert cf.stuck_reason(p)[0] == "decision", "a parked goal is acted on before a held one"
    p["members"][1]["verdict"] = "armed"
    assert cf.stuck_reason(p)[0] == "held-gate"


def test_width_plan_offers_only_what_can_be_claimed():
    """The run's width plan and the forecast share one claim rule: with a goal in flight and only members
    nobody may claim unblocked, the cycle waits instead of asking the pick for a goal it cannot get."""
    ch = tca._chain()
    p = {"members": _rows({"number": 2, "verdict": "armed", "in_progress": True},
                          {"number": 3, "verdict": "assigned-elsewhere"}, {"number": 4, "verdict": "parked"})}
    rec = {"members": [{"number": n, "waits_on": []} for n in (2, 3, 4)]}
    kw, waiting, _note = ch._width_plan(".", rec, {}, p, {2, 3, 4}, [2, 3, 4], 2)
    assert waiting == "1 in flight, none ready (width 2)" and kw == {}, (kw, waiting)
    p["members"][1]["verdict"] = "armed"
    kw, waiting, _note = ch._width_plan(".", rec, {}, p, {2, 3, 4}, [2, 3, 4], 2)
    assert waiting is None and kw == {"max_concurrent": 1}, (kw, waiting)


def test_assignee_clause_each_branch():
    """Each clause alone: nobody assigned, someone else assigned, the runner's own (any casing), no login."""
    gate = tca._chain().label_drain.assignee_gate
    assert gate([], "carol")[0] == "unassigned"
    assert gate(["bob"], "carol")[0] == "assigned-elsewhere"
    assert gate(["Carol", "bob"], "carol") is None and gate(["carol"], "Carol") is None
    assert gate([], None) is None and gate(["bob"], None) is None, "an unresolved login fails open, like the pick"


def test_forecast_applies_the_filter_only_where_it_applies():
    cf = tca._chain().chain_forecast
    rows = [{"number": 1, "verdict": "armed", "waits_on": [], "assignees": ["bob"]},
            {"number": 2, "verdict": "armed", "waits_on": [], "assignees": []},
            {"number": 3, "verdict": "parked", "waits_on": [], "assignees": ["bob"]},
            {"number": 4, "verdict": "held-by-gate", "waits_on": [], "assignees": ["bob"]}]

    def marks(**extra):
        return {m["number"]: m["marker"] for m in cf.forecast(dict(members=rows, **extra), {}, "carol")["members"]}
    off = marks(assignee_filter=False)
    assert off[1] == off[2] == "ready now" and off[3] == "needs decision", "no filter configured: no one is excluded"
    on = marks(assignee_filter=True)
    assert on[1] == "someone else's" and on[2] == "not workable", on
    assert on[3] == "needs decision", "a parked goal stays parked whoever it is assigned to"
    assert on[4] == "someone else's", "the assignee clause comes before the gate"
    fc = cf.forecast(dict(members=rows[:2], assignee_filter=True), {}, None)
    assert fc["finish"] == [1, 2] and any("login is unknown" in n for n in fc["notes"]), fc
    fc = cf.forecast(dict(members=rows[:2], assignee_filter=False), {}, "carol")
    assert any("--as changes nothing" in n for n in fc["notes"]), fc
    assert cf.forecast(dict(members=rows[:1], assignee_filter=True, runner="bob"), {})["finish"] == [1]
    assert cf.forecast(dict(members=rows[:1], assignee_filter=True), {}, " @BOB ")["finish"] == [1]


# 6 -- the comparison itself must be able to fail ----------------------------------------------------------

def test_comparison_goes_red_when_the_rules_diverge(tmp_path, monkeypatch):
    # (a) the run's stuck rule changed in `run` only
    ch, base, fk = _scene(tmp_path / "a", _backlog())
    fc = _forecast(ch, base, "alice")
    monkeypatch.setattr(ch, "_stuck", lambda p, failure, unconfirmed, held=(), declined=(), kept=(), waiting=None: ("held-gate", "#1 is held by a gate"))
    assert "reason" in _diff(fc, _drive(ch, base, fk, "alice"))
    monkeypatch.undo()
    # (b) the run's pick ignoring the assignee filter
    ch, base, fk = _scene(tmp_path / "b", _backlog())
    fc = _forecast(ch, base, "carol")
    assert "picked" in _diff(fc, _drive(ch, base, fk, "carol", honour_assignee=False))
    # (c) a run that reads the width differently from the forecast: a different number of cycles
    ch, base, fk = _scene(tmp_path / "c", _backlog())
    fc = _forecast(ch, base, "alice", 2)
    assert "cycles" in _diff(fc, _drive(ch, base, fk, "alice", 1))
