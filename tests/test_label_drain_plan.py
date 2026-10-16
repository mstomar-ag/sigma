"""#1576: `label_drain.py plan|status` -- a read-only view of everything a run label would drain.

A fake `gh` transport serves REST list/GET and the label lookup; every call is recorded so the tests
can assert the module only ever READ. No network. Graphs are built from `_issue(...)` payloads in
REST shape (state lowercase, `pull_request` key for PRs).
"""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

import gqlfake

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = ROOT / "skills" / "sigma-loop" / "scripts"
LBL = "validation-run-1"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _issue(n, *labels, body="", state="open", pr=False, assignees=(), comments=()):
    d = {"number": n, "title": "issue %d" % n, "body": body, "state": state,
         "labels": [{"name": x} for x in labels],
         "assignees": [{"login": a} for a in assignees],
         "user": {"login": "someone"}, "_comments": list(comments)}
    if pr:
        d["pull_request"] = {}
    return d


def _goal(n, *extra, body="## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n", **kw):
    return _issue(n, "sdlc:goal", LBL, *extra, body=body, **kw)


def _repo(tmp_path, **gh):
    base = tmp_path / ".sdlc"
    (base / "state").mkdir(parents=True)
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget", **gh}},
           "budget": {"max_iterations": 10}}
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return str(base)


def _runner(issues, calls=None, unreadable=()):
    """Fake `gh`: REST list (AND-labels, state, paging), REST single GET, comments page, label lookup."""
    calls = calls if calls is not None else []
    by = {i["number"]: i for i in issues}

    def pub(i):
        return {k: v for k, v in i.items() if not k.startswith("_")} | {"comments": len(i["_comments"])}

    def run(args):
        args = [str(a) for a in args]
        calls.append(args)
        g = gqlfake.swap(args)
        if g is not None:
            return g
        if args[0] == "api":
            assert "--method" in args and args[args.index("--method") + 1] == "GET", args
            m = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)(/comments)?", args[1])
            if m:
                n = int(m.group(1))
                if n in unreadable or n not in by:
                    raise RuntimeError("HTTP 404")
                if m.group(2):
                    return json.dumps([{"node_id": "c%d" % k, "body": b, "user": {"login": "bot"},
                                        "created_at": "2026-01-0%dT00:00:00Z" % (k + 1)}
                                       for k, b in enumerate(by[n]["_comments"])])
                return json.dumps(pub(by[n]))
            params = gqlfake.rest_list_params(args)
            assert params is not None, args
            want = [w for w in params.get("labels", "").split(",") if w]
            hits = [i for i in issues if i["state"] == params.get("state", "open") and "pull_request" not in i
                    and all(w in [l["name"] for l in i["labels"]] for w in want)]
            per, page = int(params["per_page"]), int(params["page"])
            return json.dumps([pub(i) for i in hits[(page - 1) * per:page * per]])
        if args[:2] == ["issue", "view"]:
            n = int(args[2])
            return json.dumps({k: v for k, v in pub(by[n]).items()} | {"state": by[n]["state"].upper()})
        return ""

    run.calls = calls
    return run


def _writes(calls):
    out = []
    for a in calls:
        if a[:2] == ["api", "graphql"]:
            if "mutation" in " ".join(a).lower():
                out.append(a)
        elif a[0] == "api":
            if a[a.index("--method") + 1] != "GET":
                out.append(a)
        elif " ".join(a[:2]) not in ("issue view", "issue list", "repo view"):
            out.append(a)
    return out


def _plan(tmp_path, issues, **gh):
    ld = _mod("label_drain")
    base = _repo(tmp_path, **gh)
    calls = []
    p = ld.plan(base, LBL, run=_runner(issues, calls))
    return p, calls


def _by(p, n):
    return next(r for r in p["members"] + p["outside"] if r["number"] == n)


PARK = "Sigma parked this goal: should the export keep the legacy column?"


# --- verdicts, read-only -------------------------------------------------------------------------

def test_plan_gives_each_member_its_verdict_and_makes_no_write_call(tmp_path):
    issues = [
        _goal(1, body="Blocked by #9\n\n## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n"),   # armed; blocker closed
        _issue(2, LBL, body="## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n"),             # unarmed (no goal label)
        _goal(3, "sdlc:parked", comments=[PARK]),                                     # parked
        _goal(4, "epic"),                                                             # upper-tier
        _issue(9, "sdlc:goal", state="closed"),
    ]
    p, calls = _plan(tmp_path, issues)
    assert _by(p, 1)["verdict"] == "armed" and _by(p, 1)["ready"]
    assert _by(p, 1)["dropped_closed"] == [9]
    assert _by(p, 2)["verdict"] == "unarmed"
    assert _by(p, 3)["verdict"] == "parked" and "legacy column" in _by(p, 3)["question"]
    assert _by(p, 3)["kind"]
    assert _by(p, 4)["verdict"] == "upper-tier"
    assert _writes(calls) == []
    assert any(a[0] == "api" for a in calls)               # the control: calls were really recorded


def test_the_read_only_guard_refuses_a_write_even_if_asked(tmp_path):
    ld = _mod("label_drain")
    ro = ld._ReadOnly(lambda a: "")
    for bad in (["api", "repos/a/b/issues/1", "--method", "PATCH"],
                ["api", "graphql", "-f", "query=mutation { x }"],
                ["issue", "edit", "1"], ["issue", "comment", "1"]):
        with pytest.raises(RuntimeError):
            ro(bad)
    assert ro.counts["total"] == 0


def test_assigned_elsewhere_and_held_by_gate(tmp_path):
    issues = [
        _goal(1, assignees=["other-person"]),
        _goal(2, body="Feature: billing\n\n## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n"),   # unit in body, no label
        _goal(3, "feature:alpha", "feature:beta"),                                     # contradicts itself
    ]
    p, _ = _plan(tmp_path, issues, assignee="me-login")
    # @me/login resolution: the explicit login is not in the assignee list -> assigned elsewhere
    assert _by(p, 2)["verdict"] == "held-by-gate"
    assert "feature label is missing" in _by(p, 2)["detail"]
    assert _by(p, 3)["verdict"] == "held-by-gate"
    assert _by(p, 1)["verdict"] == "assigned-elsewhere"


def test_assignee_compare_is_case_insensitive_on_both_sides(tmp_path):
    p, _ = _plan(tmp_path, [_goal(1, assignees=["Me-Login"])], assignee="me-login")
    assert _by(p, 1)["verdict"] == "armed", _by(p, 1)


def test_read_only_guard_refuses_every_write_shape():
    ld = _mod("label_drain")
    for bad in (["api", "repos/a/b/issues/1", "-X", "PATCH"],
                ["api", "repos/a/b/issues/1", "-X", "post"],
                ["api", "repos/a/b/issues/1", "--method=DELETE"],
                ["api", "repos/a/b/issues/1", "-XPUT"],
                ["api", "repos/a/b/issues/1", "-f", "state=closed"],
                ["api", "repos/a/b/issues/1", "-F", "x=1"],
                ["api", "repos/a/b/issues/1", "--field", "x=1"],
                ["api", "repos/a/b/issues/1", "--raw-field", "x=1"],
                ["api", "repos/a/b/issues/1", "--input", "f.json"],
                ["api", "repos/a/b/issues/1", "--field=x=1"]):
        ro = ld._ReadOnly(lambda a: "")
        with pytest.raises(RuntimeError):
            ro(bad)
        assert ro.counts["total"] == 0, bad
    ro = ld._ReadOnly(lambda a: "")
    for ok in (["api", "repos/a/b/issues/1", "--method", "GET"], ["api", "-X", "GET", "repos/a/b/issues"],
               ["api", "--method=GET", "repos/a/b/issues", "-f", "state=open"],
               ["api", "repos/a/b/issues/1"], ["api", "graphql", "-f", "query=query { x }"]):
        ro(ok)
    assert ro.counts["total"] == 5


def test_parked_members_are_briefed_up_to_the_cap_then_reported_without_a_question(tmp_path):
    ld = _mod("label_drain")
    assert ld.MAX_PARKED_BRIEFS == 20
    issues = [_goal(k, "sdlc:parked", comments=[PARK]) for k in range(1, 26)]
    p, calls = _plan(tmp_path, issues)
    briefed = [r for r in p["members"] if r["question"]]
    assert len(briefed) == 20 and len(p["members"]) == 25
    bare = [r for r in p["members"] if not r["question"]]
    assert len(bare) == 5 and all(r["verdict"] == "parked" and r["kind"] == "not-read" for r in bare)
    msgs = p["warnings"]["parked_not_briefed"]
    assert len(msgs) == 1 and "20" in msgs[0] and "5" in msgs[0]
    assert p["calls"]["total"] == len(calls)
    assert p["calls"]["total"] <= 1 + 1 + 20 * 4 + 5, p["calls"]     # label + list + bounded briefs


# --- the four blocker cases ----------------------------------------------------------------------

def test_four_blockers_one_case_each(tmp_path):
    body = ("Blocked by #20\nBlocked by #21\nBlocked by #22\nBlocked by other/repo#7\n\n"
            "## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n")
    issues = [_goal(1, body=body),
              _issue(20, "sdlc:goal", body="open unarmed? no, armed outside"),       # open, outside the label
              _issue(21, state="closed"),                                           # closed -> dropped
              _issue(22, pr=True)]                                                  # pull request
    issues[1]["labels"] = []                                                        # open, unarmed
    p, _ = _plan(tmp_path, issues)
    out = _by(p, 20)
    assert out["display"] == "outside/unarmed" and out["depth"] == 1
    assert p["outside"] == [out]
    m = _by(p, 1)
    assert m["dropped_closed"] == [21]
    assert m["waiting_on"] == [20]
    refs = {u["ref"] for u in p["unresolved"]}
    assert "#22 (pull request)" in refs
    assert any(r.startswith("other/repo#7") for r in refs)
    assert not m["ready"] and m["reason"].startswith("waiting on open #20")
    assert p["pickable"] == 0


def test_an_unresolvable_ref_keeps_its_dependent_out_of_the_ready_set(tmp_path):
    issues = [_goal(1, body="Blocked by #50\n\n## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n")]   # #50 absent: 404
    p, _ = _plan(tmp_path, issues)
    m = _by(p, 1)
    assert m["unresolved"] == ["#50 (unreadable)"]
    assert m["ready"] is False
    assert p["pickable"] == 0
    assert m["reason"].startswith("unresolved blocker")


# --- limits ---------------------------------------------------------------------------------------

def _chain(length):
    """#1 is the only member; #k is blocked by #k+1 for k < length; none carries the label."""
    out = [_goal(1, body="Blocked by #2\n\n## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n")]
    for k in range(2, length + 1):
        body = "Blocked by #%d" % (k + 1) if k < length else ""
        out.append(_issue(k, "sdlc:goal", body=body))
    return out


def test_a_chain_deeper_than_eight_stops_and_names_the_limit_and_where(tmp_path):
    p, _ = _plan(tmp_path, _chain(14))
    depths = sorted(r["depth"] for r in p["outside"])
    assert depths == list(range(1, 9))                    # depth 8 is read, depth 9 is not
    assert any("depth limit 8" in s and "#10" in s for s in p["stops"]), p["stops"]
    assert _by(p, 9)["unresolved"] == ["#10 (not read: depth limit)"]
    assert _by(p, 1)["ready"] is False


def test_more_than_fifty_pulled_issues_stops_and_names_the_limit(tmp_path):
    body = "\n".join("Blocked by #%d" % k for k in range(100, 160)) + "\n"   # 60 distinct open blockers
    issues = [_goal(1, body=body)] + [_issue(k, "sdlc:goal") for k in range(100, 160)]
    p, calls = _plan(tmp_path, issues)
    assert len(p["outside"]) == 50 and p["pulled"] == 50
    assert any("pulled-issue limit 50" in s and "#150" in s for s in p["stops"]), p["stops"]
    assert p["calls"]["get"] == 50
    assert _by(p, 1)["unresolved"][0].endswith("(not read: pulled-issue limit)")


def test_a_label_with_more_than_200_members_is_refused_with_its_count(tmp_path, capsys):
    ld = _mod("label_drain")
    issues = [_goal(k, body="") for k in range(1, 202)]
    base = _repo(tmp_path)
    rc = ld.main(["label_drain.py", "plan", base, LBL], run=_runner(issues))
    err = capsys.readouterr().err
    assert rc == 2 and "more than 200" in err and "201" in err


def test_label_rules_are_slice_a_rules(tmp_path, capsys):
    ld = _mod("label_drain")
    base = _repo(tmp_path)
    assert ld.main(["label_drain.py", "plan", base, "sdlc:goal"], run=_runner([])) == 2
    assert "namespace" in capsys.readouterr().err
    assert ld.main(["label_drain.py", "status", base, "feature:x"], run=_runner([])) == 2


# --- warnings -------------------------------------------------------------------------------------

def test_cross_unit_cycle_and_missing_done_when_are_warned_with_numbers(tmp_path):
    ok = "\n\n## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n"
    issues = [
        _goal(1, "feature:alpha", body="Blocked by #2" + ok),                 # alpha -> beta: cross-unit
        _goal(2, "feature:beta", body="Blocked by #3" + ok),
        _goal(3, "feature:beta", body="Blocked by #2" + ok),                  # 2 <-> 3 cycle
        _goal(4, body="no section here\n\nPaid step: render the full video\n"),   # no Done when, paid
    ]
    p, _ = _plan(tmp_path, issues)
    w = p["warnings"]
    assert [(c["from"], c["to"]) for c in w["cross_unit"]] == [(1, 2)]
    assert "will not contain #2's change until #2's unit" in w["cross_unit"][0]["message"]
    assert sorted(sorted(c) for c in w["cycles"]) == [[2, 3]]
    assert w["no_done_when"] == [4]
    assert w["paid_step"] == [4]


# --- status ---------------------------------------------------------------------------------------

def test_status_gives_one_reason_per_open_member_and_is_not_drained(tmp_path, capsys):
    ld = _mod("label_drain")
    issues = [
        _goal(1, body="Blocked by #9\n\n## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n"),    # held by open blocker
        _goal(2, "sdlc:parked", comments=[PARK]),                                        # parked
        _goal(3, "sdlc:in-progress"),                                                    # in progress
        _goal(4),                                                                        # pickable
        _issue(9, "sdlc:goal"),
    ]
    base = _repo(tmp_path)
    rc = ld.main(["label_drain.py", "status", base, LBL, "--json"], run=_runner(issues))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["drained"] is False and out["message"] == ""
    reasons = {r["number"]: r["reason"] for r in out["not_pickable"]}
    assert set(reasons) == {1, 2, 3}
    assert reasons[1] == "waiting on open #9"
    assert reasons[2].startswith("parked (") and "legacy column" in reasons[2]
    assert reasons[3].startswith("in progress")
    assert out["pickable"] == 1
    rc = ld.main(["label_drain.py", "status", base, LBL], run=_runner(issues))
    text = capsys.readouterr().out
    assert "drained" not in text and "#3 not pickable" in text


def test_status_reports_drained_only_when_no_member_is_open(tmp_path, capsys):
    ld = _mod("label_drain")
    base = _repo(tmp_path)
    issues = [_issue(1, LBL, state="closed")]
    assert ld.main(["label_drain.py", "status", base, LBL], run=_runner(issues)) == 0
    out = capsys.readouterr().out
    assert "drained, the label can be deleted by a person" in out


# --- calls ----------------------------------------------------------------------------------------

def test_json_carries_the_exact_call_count_for_six_members_and_two_blockers(tmp_path, capsys):
    ld = _mod("label_drain")
    ok = "\n\n## Done when\n- [ ] a\n- [ ] b\n- [ ] c\n"
    issues = [_goal(1, body="Blocked by #20" + ok), _goal(2, body="Blocked by #21" + ok),
              _goal(3, body="Blocked by #20" + ok), _goal(4), _goal(5), _goal(6),
              _issue(20, "sdlc:goal"), _issue(21, state="closed")]
    base = _repo(tmp_path)
    rec = []
    assert ld.main(["label_drain.py", "plan", base, LBL, "--json"], run=_runner(issues, rec)) == 0
    out = json.loads(capsys.readouterr().out)
    # one label lookup + one list + one GET per distinct blocker (#20 is shared, memoised)
    assert out["calls"] == {"total": 4, "list": 1, "get": 2, "graphql": 1, "other": 0}
    assert len(rec) == 4
    assert len(out["members"]) == 6


def test_the_cli_gesture_from_the_docs_runs_and_prints_usage_on_bad_input(tmp_path):
    r = subprocess.run([sys.executable, str(S / "label_drain.py"), "--help"], capture_output=True,
                       text=True, cwd=str(tmp_path))
    assert r.returncode == 0 and "usage" in r.stdout.lower()
    r = subprocess.run([sys.executable, str(S / "label_drain.py"), "plan"], capture_output=True,
                       text=True, cwd=str(tmp_path))
    assert r.returncode == 2 and "usage" in r.stderr.lower()
