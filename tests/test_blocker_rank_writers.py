"""#1407: every writer that assigns a blocked/blocking pair raises the blocker to the blocked
goal's rank in the same batch (`GitHubSource.raise_blocker_priority`, the #1406 rule)."""
import json, pathlib, importlib.util

import gqlfake

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
REPO = ("--repo", "acme/widget")


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _config(mode=None):
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget"}}}
    if mode:
        cfg["discovery"]["blocker_promotion"] = {"mode": mode}
    return cfg


def _runner(views, lists=None, fail_reads=()):
    """views: {n: {"labels": [...], "state": "OPEN", "comments": [...]}}; lists: {label: [issues]}."""
    calls, labels = [], set()

    def run(args):
        t = gqlfake.rest_issue_target(args)
        if t is not None and t[0] in fail_reads:
            raise RuntimeError("simulated gh failure: read")
        gql = gqlfake.swap(args, labels=labels, calls=calls, repo_args=REPO)
        if gql is not None:
            return gql
        if gqlfake.is_issue_write(args):
            return gqlfake.rest_write(args, calls=calls, repo_args=REPO)
        calls.append(list(args))
        params = gqlfake.rest_list_params(args)
        if params is not None:
            return json.dumps((lists or {}).get(params.get("labels"), []))
        def view(n, fields):
            v = views.get(n, {})
            return {"state": v.get("state", "OPEN"), "stateReason": "",
                    "labels": [{"name": x} for x in v.get("labels", [])],
                    "comments": [{"id": "c%d" % i, "author": {"login": "x"}, "body": b,
                                  "createdAt": "2026-01-01T00:00:00Z", "authorAssociation": "OWNER"}
                                 for i, b in enumerate(v.get("comments", []))]}
        rest = gqlfake.rest_issue(args, view)
        if rest is not None:
            return rest
        return ""
    run.calls = calls
    return run


def _writes(run):
    return [c for c in run.calls if c[:2] in (["issue", "edit"], ["issue", "comment"])]


def _raised(run, n, label):
    return any(c[:3] == ["issue", "edit", str(n)] and "--add-label" in c and label in c for c in run.calls)


def _src(run, mode=None):
    return _mod("sources").GitHubSource(_config(mode), run=run)


# ------------------------------------------------------------------ the method

def test_raises_a_lower_blocker_to_the_dependents_rank_with_one_comment():
    run = _runner({"7": {"labels": ["priority:P3"]}, "9": {"labels": ["priority:P0"]}})
    assert _src(run).raise_blocker_priority("7", "9") == "raised"
    assert _raised(run, 7, "priority:P0")
    assert len([c for c in run.calls if c[:3] == ["issue", "comment", "7"]]) == 1


def test_never_lowers_and_a_second_call_writes_nothing():
    run = _runner({"7": {"labels": ["priority:P0"]}, "9": {"labels": ["priority:P3"]}})
    assert _src(run).raise_blocker_priority("7", "9") == "unchanged"
    assert _writes(run) == []
    run2 = _runner({"7": {"labels": ["priority:P1"]}, "9": {"labels": ["priority:P1"]}})
    assert _src(run2).raise_blocker_priority("7", "9") == "unchanged"
    assert _writes(run2) == []


def test_an_unprioritised_blocker_is_raised_and_an_unprioritised_dependent_raises_nothing():
    run = _runner({"7": {"labels": []}, "9": {"labels": ["priority:P2"]}})
    assert _src(run).raise_blocker_priority("7", "9") == "raised"
    assert _raised(run, 7, "priority:P2")
    run2 = _runner({"7": {"labels": ["priority:P4"]}, "9": {"labels": []}})
    assert _src(run2).raise_blocker_priority("7", "9") == "unchanged"
    assert _writes(run2) == []


def test_mode_off_makes_no_call_at_all():
    run = _runner({"7": {"labels": ["priority:P3"]}, "9": {"labels": ["priority:P0"]}})
    assert _src(run, "off").raise_blocker_priority("7", "9") == "skipped: off"
    assert run.calls == []


def test_a_confirmation_labelled_or_closed_blocker_is_never_raised():
    run = _runner({"7": {"labels": ["priority:P3", "sdlc:needs-confirmation"]},
                   "9": {"labels": ["priority:P0"]}})
    src = _src(run)
    assert src.raise_blocker_priority("7", "9").startswith("skipped")
    run2 = _runner({"7": {"labels": ["priority:P3"], "state": "CLOSED"}, "9": {"labels": ["priority:P0"]}})
    assert _src(run2).raise_blocker_priority("7", "9").startswith("skipped")
    assert _writes(run) == [] and _writes(run2) == []


def test_a_read_failure_is_reported_and_writes_nothing(capsys):
    run = _runner({"7": {"labels": ["priority:P3"]}, "9": {"labels": ["priority:P0"]}}, fail_reads=("9",))
    assert _src(run).raise_blocker_priority("7", "9") == "skipped: read failed"
    assert _writes(run) == []
    assert "#7 could not be raised" in capsys.readouterr().err


# ------------------------------------------------------------------ the real verbs

def test_blockers_resolve_raises_the_blocker_it_resolves():
    b = _mod("blockers")
    run = _runner({"7": {"labels": ["sdlc:goal", "priority:P3"]}, "9": {"labels": ["priority:P0"]}})
    res = b.resolve(".sdlc", _config(), _src(run), "9", ["7"], run=run)
    assert res["results"][0]["verdict"] == b.PICKABLE
    assert _raised(run, 7, "priority:P0")


def test_blockers_resolve_off_is_byte_identical_and_dry_run_writes_nothing():
    b = _mod("blockers")
    views = {"7": {"labels": ["sdlc:goal", "priority:P3"]}, "9": {"labels": ["priority:P0"]}}
    off = _runner(views)
    b.resolve(".sdlc", _config("off"), _src(off, "off"), "9", ["7"], run=off)
    assert not _raised(off, 7, "priority:P0")
    dry = _runner(views)
    b.resolve(".sdlc", _config(), _src(dry), "9", ["7"], run=dry, apply=False)
    assert _writes(dry) == []


def test_the_sweep_raises_the_live_blocker_of_a_blocked_goal():
    au = _mod("auto_unpark")
    blocked = {"number": 9, "title": "", "body": "blocked by #7 until the base lands",
               "labels": [{"name": "sdlc:blocked"}, {"name": "priority:P0"}]}
    run = _runner({"7": {"labels": ["sdlc:goal", "priority:P3"]}, "9": {"labels": ["sdlc:blocked", "priority:P0"]}},
                  lists={"sdlc:blocked": [dict(blocked, labels=blocked["labels"])]})
    au.sweep_unpark(".sdlc", _config(), apply=True, run=run)
    assert _raised(run, 7, "priority:P0")


def test_the_sweep_dry_run_and_mode_off_raise_nothing():
    au = _mod("auto_unpark")
    blocked = {"number": 9, "title": "", "body": "blocked by #7 until the base lands",
               "labels": [{"name": "sdlc:blocked"}, {"name": "priority:P0"}]}
    views = {"7": {"labels": ["sdlc:goal", "priority:P3"]}, "9": {"labels": ["sdlc:blocked", "priority:P0"]}}
    lists = {"sdlc:blocked": [blocked]}
    dry = _runner(views, lists)
    au.sweep_unpark(".sdlc", _config(), apply=False, run=dry)
    assert not _raised(dry, 7, "priority:P0")
    off = _runner(views, lists)
    au.sweep_unpark(".sdlc", _config("off"), apply=True, run=off)
    assert not _raised(off, 7, "priority:P0")


def test_mode_smart_makes_no_call_at_all():
    run = _runner({"7": {"labels": ["priority:P3"]}, "9": {"labels": ["priority:P0"]}})
    assert _src(run, "smart").raise_blocker_priority("7", "9") == "skipped: smart"
    assert run.calls == []


def _handoff(tmp_path, views, goal="9", **kw):
    import test_handoff as th
    run = _runner(views)
    real = _src(run)
    class Src(th.FakeSource):
        def raise_blocker_priority(self, blocker, dependent):
            return real.raise_blocker_priority(blocker, dependent)
    src = Src()
    if kw:
        th.handoff.create_tracked_issue(th._project(tmp_path), th.ON, goal, "engine", "related",
                                        same_area=True, immediately_actionable=False, source=src, **kw)
    else:
        th.handoff.hand_off(th._project(tmp_path), th.ON, goal, "engine", "needs a flag", source=src)
    return run


def test_handoff_filing_raises_the_new_blocker_for_the_blocked_goal(tmp_path):
    for i, blocker_labels in enumerate(([], ["priority:P3"])):
        run = _handoff(tmp_path / str(i), {"61": {"labels": blocker_labels}, "9": {"labels": ["priority:P0"]}})
        assert _raised(run, 61, "priority:P0")
    run = _handoff(tmp_path / "n", {"61": {"labels": ["priority:P3"]}, "9": {"labels": ["priority:P0"]}},
                   blocks_goal=False)
    assert not _raised(run, 61, "priority:P0") and _writes(run) == []
