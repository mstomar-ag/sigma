"""#1410: the reconcile tick finds every open blocking edge whose blocker ranks below what it blocks
and repairs it (`GitHubSource.repair_blocker_ranks`), and doctor shows one row."""
import importlib.util
import json
import pathlib

import gqlfake

S = pathlib.Path(__file__).resolve().parent.parent / "skills"
SL = S / "sigma-loop" / "scripts"
REPO = ("--repo", "acme/widget")


def _mod(name, base=SL):
    spec = importlib.util.spec_from_file_location(name + "_1410", base / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cfg(mode=None):
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}}
    if mode:
        cfg["discovery"]["blocker_promotion"] = {"mode": mode}
    return cfg


def _issue(n, labels=(), body="", state="open"):
    return {"number": n, "title": "t%d" % n, "body": body, "state": state,
            "labels": [{"name": x} for x in labels], "assignees": []}


def _runner(issues, views=None, graphql_remaining=5000, fail_graphql=False):
    """issues: REST-shaped issues returned by label/state list reads (goal, blocked, blocking)."""
    calls = []

    def run(args):
        args = [str(a) for a in args]
        gql = gqlfake.swap(args, labels=set(), calls=calls, repo_args=REPO)
        if gql is not None:
            return gql
        if gqlfake.is_issue_write(args):
            return gqlfake.rest_write(args, calls=calls, repo_args=REPO)
        calls.append(list(args))
        if args[:2] == ["api", "rate_limit"]:
            if fail_graphql:
                raise RuntimeError("graphql refused")
            return json.dumps({"resources": {"graphql": {"remaining": graphql_remaining}}})
        params = gqlfake.rest_list_params(args)
        if params is not None:
            want = set(filter(None, params.get("labels", "").split(",")))
            state = params.get("state", "open")
            return json.dumps([i for i in issues if i["state"] == state
                               and want <= {l["name"] for l in i["labels"]}])
        def view(n, fields):
            v = (views or {}).get(n) or next((i for i in issues if str(i["number"]) == n), None) or {}
            return {"state": (v.get("state") or "open").upper(), "stateReason": "",
                    "labels": v.get("labels", []), "comments": []}
        rest = gqlfake.rest_issue(args, view)
        return rest if rest is not None else ""
    run.calls = calls
    return run


def _src(run, mode=None, sdlc_dir=None):
    return _mod("sources").GitHubSource(_cfg(mode), run=run, sdlc_dir=sdlc_dir)


def _raised(run, n, label):
    return any(c[:3] == ["issue", "edit", str(n)] and "--add-label" in c and label in c for c in run.calls)


def _writes(run):
    return [c for c in run.calls if c[:2] in (["issue", "edit"], ["issue", "comment"])]


GOAL = "sdlc:goal"


def _p0_blocked_by_p3():
    return [_issue(9, [GOAL, "priority:P0"], body="blocked by #7"), _issue(7, [GOAL, "priority:P3"])]


def test_p3_blocker_of_p0_is_raised_once_and_a_second_run_writes_nothing():
    issues = _p0_blocked_by_p3()
    run = _runner(issues)
    res = _src(run).repair_blocker_ranks()
    assert res["violations"] == 1 and res["raised"] == ["7"]
    assert _raised(run, 7, "priority:P0")
    assert len([c for c in run.calls if c[:3] == ["issue", "comment", "7"]]) == 1
    issues[1] = _issue(7, [GOAL, "priority:P0"])           # what the first run wrote
    run2 = _runner(issues)
    assert _src(run2).repair_blocker_ranks()["violations"] == 0
    assert _writes(run2) == []


def test_p0_blocker_of_p3_is_untouched():
    run = _runner([_issue(9, [GOAL, "priority:P3"], body="blocked by #7"), _issue(7, [GOAL, "priority:P0"])])
    res = _src(run).repair_blocker_ranks()
    assert res["violations"] == 0 and _writes(run) == []


def test_a_chain_raises_the_far_end_through_the_middle():
    run = _runner([_issue(9, [GOAL, "priority:P0"], body="blocked by #8"),
                   _issue(8, [GOAL, "priority:P3"], body="blocked by #7"),
                   _issue(7, [GOAL, "priority:P4"])])
    res = _src(run).repair_blocker_ranks()
    assert sorted(res["raised"]) == ["7", "8"]
    assert _raised(run, 7, "priority:P0") and _raised(run, 8, "priority:P0")


def test_a_cycle_is_reported_once_and_does_not_loop(capsys):
    run = _runner([_issue(9, [GOAL, "priority:P0"], body="blocked by #8"),
                   _issue(8, [GOAL, "priority:P3"], body="blocked by #9")])
    _src(run).repair_blocker_ranks()
    assert capsys.readouterr().err.count("cycle was detected") == 1


def test_a_legacy_confirmation_label_is_stripped_from_a_blocker_not_raised():
    run = _runner([_issue(9, [GOAL, "priority:P0"], body="blocked by #7"),
                   _issue(7, [GOAL, "priority:P3", "sdlc:needs-confirmation"])])
    res = _src(run).repair_blocker_ranks()
    assert res["stripped"] == ["7"] and res["raised"] == []
    assert any(c[:3] == ["issue", "edit", "7"] and "--remove-label" in c and "sdlc:needs-confirmation" in c
               for c in run.calls)


def test_a_closed_blocker_loses_the_blocking_mark_and_an_open_one_keeps_it():
    issues = [_issue(5, [GOAL, "sdlc:blocking"], state="closed"),
              _issue(6, [GOAL, "sdlc:blocking"]),
              _issue(9, [GOAL, "priority:P0"], body="blocked by #7"),
              _issue(7, [GOAL, "priority:P0", "sdlc:blocking"])]
    res = _src(_runner(issues)).repair_blocker_ranks()
    assert res["unmarked"] == ["5"]                          # open marks (6, 7) belong to the unpark sweep


def test_the_write_cap_is_respected():
    issues = [_issue(100, [GOAL, "priority:P0"], body=" ".join("blocked by #%d" % n for n in range(1, 6)))]
    issues += [_issue(n, [GOAL, "priority:P3"]) for n in range(1, 6)]
    run = _runner(issues)
    res = _src(run).repair_blocker_ranks(cap=2)
    assert len(res["raised"]) == 2 and res["violations"] == 5


def test_mode_off_makes_zero_calls_and_dry_run_writes_nothing():
    run = _runner(_p0_blocked_by_p3())
    assert _src(run, "off").repair_blocker_ranks()["skipped"] == "off"
    assert run.calls == []
    dry = _runner(_p0_blocked_by_p3())
    assert _src(dry).repair_blocker_ranks(apply=False)["violations"] == 1
    assert _writes(dry) == []


def test_a_cross_repository_blocker_is_reported_not_skipped(capsys):
    run = _runner([_issue(9, [GOAL, "priority:P0"], body="blocked by other/repo#7")])
    res = _src(run).repair_blocker_ranks()
    assert res["unwritable"] and "not enforced" in capsys.readouterr().err


def test_a_refused_graphql_still_repairs_through_rest_without_the_board_field():
    run = _runner(_p0_blocked_by_p3(), fail_graphql=True)
    res = _src(run).repair_blocker_ranks()
    assert res["raised"] == ["7"] and _raised(run, 7, "priority:P0")
    assert not any("graphql" in c for c in run.calls)


def test_the_board_field_is_written_only_when_graphql_budget_allows():
    for kw, expect in (({"fail_graphql": True}, 0), ({"graphql_remaining": 10}, 0), ({}, 1)):
        run = _runner(_p0_blocked_by_p3(), **kw)
        src = _src(run)
        looked = []
        src._item_id = lambda n, _l=looked: _l.append(n)
        src.repair_blocker_ranks()
        assert len(looked) == expect, kw


# ------------------------------------------------------------------ through the real verb

def _sdlc(tmp_path, cfg):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(cfg))
    return d


def test_the_tick_repairs_records_state_and_reports_the_rollout_once(tmp_path, capsys):
    tick = _mod("reconcile_tick")
    d = _sdlc(tmp_path, _cfg())
    run = _runner(_p0_blocked_by_p3())
    summary = tick.tick(str(d), config=_cfg(), run=run, now=1000.0)
    assert "1 blocker(s) raised" in summary and _raised(run, 7, "priority:P0")
    assert "first run: 1 blocker(s)" in capsys.readouterr().err
    st = tick.read_repair_state(str(d))
    assert st["violations"] == 1 and st["last_repair"] == 1000.0 and st["rollout_reported"] is True
    tick.tick(str(d), config=_cfg(), run=_runner(_p0_blocked_by_p3()), now=1100.0)
    assert "first run" not in capsys.readouterr().err


def test_the_tick_with_the_rule_off_makes_no_call_and_writes_no_state(tmp_path):
    tick = _mod("reconcile_tick")
    d = _sdlc(tmp_path, _cfg("off"))
    run = _runner(_p0_blocked_by_p3())
    tick.tick(str(d), config=_cfg("off"), run=run, now=1000.0)
    assert run.calls == []
    assert _writes(run) == []
    assert not (d / "state" / "blocker-repair.meta.json").exists()


# ------------------------------------------------------------------ doctor

def _row(tmp_path, state, now, cfg=None):
    doctor = _mod("doctor", S / "sigma-doctor" / "scripts")
    d = _sdlc(tmp_path, cfg or _cfg())
    if state is not None:
        (d / "state" / "blocker-repair.meta.json").write_text(json.dumps(state))
    return doctor._blocker_rank_row(cfg or _cfg(), str(d), now=now)


def test_doctor_row_zero_fresh_and_stale(tmp_path):
    zero = _row(tmp_path / "a", {"violations": 0, "last_repair": 900.0}, 1000.0)
    assert zero["ok"] and ": 0 " in zero["name"]
    fresh = _row(tmp_path / "b", {"violations": 2, "first_seen_nonzero": 900.0}, 1000.0)
    assert fresh["ok"] and ": 2 " in fresh["name"]
    stale = _row(tmp_path / "c", {"violations": 2, "first_seen_nonzero": 1000.0}, 1000.0 + 2 * 3600 + 1)
    assert not stale["ok"] and "two reconcile intervals" in stale["fix"]


def test_doctor_row_is_absent_when_the_rule_is_off(tmp_path):
    assert _row(tmp_path, None, 1000.0, _cfg("off")) is None
