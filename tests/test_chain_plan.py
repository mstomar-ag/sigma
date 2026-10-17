"""#1584: `chain.py plan <sdlc_dir> <N>` -- the read-only chain plan for one end goal.

A counting fake `gh` serves REST GETs only and FAILS THE TEST on anything that is not a GET. No
network. Controls (each clause alone, then restored): drop the cycle check; raise the bound; let a
non-GET through the read-only gate; drop the closed/external filter. The call counts and local wall
times in `test_measured_calls_for_10_30_50_member_chains` are measured from a counting fake; network
wall time is NOT measured anywhere in this file."""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gqlfake  # noqa: E402
import resource_mode  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = ROOT / "skills" / "sigma-loop" / "scripts"
NOW = 1_800_000_000.0
PARK = "Sigma parked this goal: should the export keep the legacy column?"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _chain():
    return _mod("chain")


def _issue(n, *labels, body="", state="open", pr=False, comments=()):
    d = {"number": n, "title": "SECRET-TITLE %d" % n, "body": body, "state": state,
         "labels": [{"name": x} for x in labels], "assignees": [],
         "user": {"login": "someone"}, "_comments": list(comments)}
    if pr:
        d["pull_request"] = {}
    return d


def _goal(n, *extra, blocked_by=(), body="", **kw):
    lines = ["SECRET-BODY %d" % n] + ["Blocked by #%d" % b for b in blocked_by] + [body]
    return _issue(n, "sdlc:goal", *extra, body="\n".join(lines), **kw)


def _repo(tmp_path, chain_cfg=None, dag_mode=None):
    base = tmp_path / ".sdlc"
    (base / "state").mkdir(parents=True)
    disc = {"source": "github", "github": {"repo": "acme/widget"}}
    if dag_mode:
        disc["dag"] = {"mode": dag_mode}
    cfg = {"discovery": disc, "budget": {"max_iterations": 10}}
    if chain_cfg is not None:
        cfg["chain"] = chain_cfg
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return str(base)


def _fake(issues, calls=None):
    """A fake `gh`: REST list and single GET (and a comments page). Any non-GET fails the test."""
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
        assert args[0] == "api", "non-api gh call: %s" % args
        assert "--method" in args and args[args.index("--method") + 1] == "GET", "non-GET: %s" % args
        m = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)(/comments)?", args[1])
        if m:
            n = int(m.group(1))
            if n not in by:
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

    run.calls = calls
    return run


def _save_map(base, nodes, edges):
    ds = _mod("dag_store")
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60))
    h = {"repo": "acme/widget", "built_at": iso, "refreshed_at": iso, "last_ok_at": iso,
         "last_error": None, "partial": False}
    ok, why = ds.save(pathlib.Path(base), {"header": h, "nodes": nodes, "edges": [
        {"blocker": a, "blocked": b, "source": "body"} for a, b in edges]})
    assert ok, why


def _mnode(n, life="goal", state="open", kind="issue", prio=None, unit=None):
    d = {"number": n, "kind": kind, "state": state, "life": life, "prio": prio, "in_corpus": True}
    if unit:
        d["unit"] = unit
    return d


def _plan(base, goal, issues, calls=None, **kw):
    run = _fake(issues, calls)
    return _chain().plan(base, goal, run=run, now=NOW, **kw)


def _nums(p):
    return [m["number"] for m in p["members"]]


def _row(p, n):
    return next(m for m in p["members"] if m["number"] == n)


def _four_deep():
    return [_goal(1, blocked_by=[2]), _goal(2, blocked_by=[3]), _goal(3, blocked_by=[4]),
            _goal(4, "sdlc:parked", comments=[PARK])]


# --- the five named tests --------------------------------------------------------------------------

def test_four_deep_parked_deepest(tmp_path):
    """Live fallback over a fixture backlog: deepest member first, parked with its kind."""
    base = _repo(tmp_path, dag_mode="off")
    p = _plan(base, 1, _four_deep())
    assert p["source"] == "live"
    assert _nums(p) == [4, 3, 2, 1]
    assert _row(p, 4)["verdict"] == "parked" and _row(p, 4)["kind"]
    assert [_row(p, n)["depth"] for n in (4, 3, 2, 1)] == [0, 1, 2, 3]
    assert _row(p, 1)["verdict"] == "armed"
    assert p["complete"] is True


def test_four_deep_parked_deepest_from_the_map(tmp_path):
    base = _repo(tmp_path)
    _save_map(base, [_mnode(1), _mnode(2), _mnode(3), _mnode(4, life="parked")],
              [(2, 1), (3, 2), (4, 3)])
    p = _plan(base, 1, _four_deep())
    assert p["source"] == "map" and p["map_age_seconds"] == 60
    assert _nums(p) == [4, 3, 2, 1]
    assert _row(p, 4)["verdict"] == "parked" and _row(p, 4)["kind"]
    assert [m["depth"] for m in p["members"]] == [0, 1, 2, 3]


def test_external_and_closed(tmp_path):
    base = _repo(tmp_path, dag_mode="off")
    issues = [
        _goal(1, blocked_by=[2, 3, 4], body="Blocked by acme/other#7\nBlocked by lib-org/tool#9"),
        _issue(2, "sdlc:goal", state="closed"),
        _issue(3, "sdlc:goal", pr=True),
        _goal(4),
    ]
    p = _plan(base, 1, issues)
    assert _nums(p) == [4, 1]
    assert p["done"] == [2]
    kinds = {e["ref"]: e["kind"] for e in p["externals"]}
    assert kinds["#3"] == "pull-request"
    assert kinds["acme/other#7"] == "other-repository"
    assert kinds["lib-org/tool#9"] == "third-party"
    assert 2 not in _nums(p) and 3 not in _nums(p)


def test_external_pull_request_from_the_map(tmp_path):
    base = _repo(tmp_path)
    _save_map(base, [_mnode(1), _mnode(2, state="closed"), _mnode(3, kind="pr"), _mnode(4)],
              [(2, 1), (3, 1), (4, 1)])
    p = _plan(base, 1, [_goal(1), _goal(4)])
    assert _nums(p) == [4, 1] and p["done"] == [2]
    assert [(e["ref"], e["kind"]) for e in p["externals"]] == [("#3", "pull-request")]


@pytest.mark.parametrize("via", ["live", "map"])
def test_cycle_refused(tmp_path, via):
    base = _repo(tmp_path, dag_mode="off" if via == "live" else None)
    if via == "map":
        _save_map(base, [_mnode(1), _mnode(2), _mnode(3), _mnode(4)], [(2, 1), (3, 2), (1, 3), (4, 3)])
    issues = [_goal(1, blocked_by=[2]), _goal(2, blocked_by=[3]), _goal(3, blocked_by=[1, 4]), _goal(4)]
    ch = _chain()
    with pytest.raises(ch.ChainRefused) as ei:
        ch.plan(base, 1, run=_fake(issues), now=NOW)
    assert ei.value.members == [1, 2, 3]
    assert all("#%d" % n in str(ei.value) for n in (1, 2, 3)) and "#4" not in str(ei.value)
    assert not (pathlib.Path(base) / "state" / "chains").exists()


def test_bound_refuses(tmp_path):
    # live: bound 50, a 52-member chain is refused with the count read, never truncated
    base = _repo(tmp_path, dag_mode="off")
    issues = [_goal(1)] + [_goal(k, blocked_by=[k - 1]) for k in range(2, 53)]
    ch = _chain()
    with pytest.raises(ch.ChainRefused) as ei:
        ch.plan(base, 52, run=_fake(issues), now=NOW)
    assert "50" in str(ei.value) and re.search(r"\b5[12]\b", str(ei.value))
    assert not (pathlib.Path(base) / "state" / "chains").exists()
    # exactly at the bound is planned
    p = _chain().plan(base, 50, run=_fake(issues), now=NOW)
    assert len(p["members"]) == 50


def test_bound_refuses_from_the_map_with_the_configured_bound(tmp_path):
    base = _repo(tmp_path, chain_cfg={"max_members": 4})
    _save_map(base, [_mnode(k) for k in range(1, 7)], [(k, k + 1) for k in range(1, 6)])
    ch = _chain()
    with pytest.raises(ch.ChainRefused) as ei:
        ch.plan(base, 6, run=_fake([]), now=NOW)
    assert "6" in str(ei.value) and "4" in str(ei.value)
    assert len(ch.plan(base, 4, run=_fake([]), now=NOW)["members"]) == 4


def test_plan_is_read_only(tmp_path):
    base = _repo(tmp_path, dag_mode="off")
    calls = []
    p = _plan(base, 1, _four_deep(), calls)
    assert calls and all(a[0] == "api" and a[a.index("--method") + 1] == "GET" for a in calls)
    rec = pathlib.Path(base) / "state" / "chains" / "c1.json"
    text = rec.read_text()
    assert "SECRET" not in text and "legacy column" not in text
    doc = json.loads(text)
    assert doc["goal"] == 1 and [m["number"] for m in doc["members"]] == [4, 3, 2, 1]
    assert doc["calls"]["total"] == p["calls"]["total"] and 1 <= doc["calls"]["total"] <= len(calls)
    keys = set()

    def walk(x):
        if isinstance(x, dict):
            keys.update(x)
            [walk(v) for v in x.values()]
        elif isinstance(x, list):
            [walk(v) for v in x]
    walk(doc)
    assert not keys & {"title", "body", "question", "comments"}


def test_the_read_only_gate_refuses_anything_but_a_get():
    ch = _chain()
    gate = ch._GetOnly(lambda args: "{}")
    for bad in (["api", "repos/a/b/issues/1", "--method", "POST"],
                ["api", "repos/a/b/issues/1", "--method", "PATCH", "-f", "state=closed"],
                ["api", "repos/a/b/issues/1", "-f", "state=closed"],
                ["api", "graphql", "-f", "query=query{viewer{login}}"],
                ["issue", "close", "1"], ["issue", "view", "1"]):
        with pytest.raises(RuntimeError):
            gate(bad)
    assert gate(["api", "repos/a/b/issues/1", "--method", "GET"]) == "{}"


# --- the rest of the report ------------------------------------------------------------------------

def test_inversions_ahead_of_weak_mentions_and_cross_unit(tmp_path):
    base = _repo(tmp_path, dag_mode="off")
    issues = [
        _goal(1, "priority:P0", "feature:alpha", blocked_by=[2], body="needs #5 maybe"),
        _goal(2, "priority:P3", "feature:beta", blocked_by=[3]),
        _goal(3, "priority:P1"),
        _goal(5, "priority:P1"),                    # outside, weakly mentioned only
        _goal(6, "priority:P0"),                    # outside, more urgent than the P3 member
        _goal(7, "priority:P4"),                    # outside, less urgent than every member
    ]
    p = _plan(base, 1, issues)
    assert _nums(p) == [3, 2, 1]
    assert [(i["blocker"], i["dependent"]) for i in p["priority_inversions"]] == [(2, 1)]
    assert p["higher_priority_ahead"] == 2          # goals 5 (P1) and 6 (P0) outrank the P3 step; 7 does not
    assert p["weak_mentions"] == 1
    assert [(c["from"], c["to"]) for c in p["cross_unit"]] == [(2, 1)]    # blocker -> dependent
    assert {(e["from"], e["to"], e["tag"]) for e in p["edges"]} == {(2, 1, "explicit"), (3, 2, "explicit")}
    rec = json.loads((pathlib.Path(base) / "state" / "chains" / "c1.json").read_text())
    assert rec["priority_inversions"] == p["priority_inversions"]
    # the chain never writes a priority: nothing but GETs (the fake asserts it)


def test_source_line_says_map_or_live_and_why(tmp_path, capsys):
    ch = _chain()
    live = _repo(tmp_path / "a", dag_mode="off")
    assert ch.main(["chain.py", "plan", live, "1"], run=_fake(_four_deep()), now=NOW) == 0
    assert re.search(r"(?m)^source: live \(map: off\)", capsys.readouterr().out)
    mp = _repo(tmp_path / "b")
    _save_map(mp, [_mnode(1)], [])
    assert ch.main(["chain.py", "plan", mp, "1"], run=_fake([_goal(1)]), now=NOW) == 0
    assert re.search(r"(?m)^source: map, age 1m", capsys.readouterr().out)
    # a map that lacks the goal falls back to the live read
    assert ch.main(["chain.py", "plan", mp, "9"], run=_fake([_goal(9)]), now=NOW) == 0
    assert re.search(r"(?m)^source: live \(map: goal not in map\)", capsys.readouterr().out)


def test_json_flag_and_usage(tmp_path, capsys):
    ch = _chain()
    base = _repo(tmp_path, dag_mode="off")
    assert ch.main(["chain.py", "plan", base, "1", "--json"], run=_fake(_four_deep()), now=NOW) == 0
    assert json.loads(capsys.readouterr().out)["goal"] == 1
    assert ch.main(["chain.py", "plan", base], run=_fake([])) == 2
    assert ch.main(["chain.py", "plan", base, "x"], run=_fake([])) == 2
    assert ch.main(["chain.py", "plan", base, "77"], run=_fake([])) == 2      # unreadable goal
    assert "#77" in capsys.readouterr().err


def test_help_has_no_side_effects(tmp_path):
    env = resource_mode.carry({"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    r = subprocess.run([sys.executable, str(S / "chain.py"), "--help"], capture_output=True, text=True,
                       env=env, cwd=str(tmp_path), timeout=60)
    assert r.returncode == 0 and "chain.py plan" in r.stdout
    assert list(tmp_path.iterdir()) == []


# --- measured, not asserted ------------------------------------------------------------------------

def _line_issues(k):
    return [_goal(1)] + [_goal(i, blocked_by=[i - 1]) for i in range(2, k + 1)]


def test_measured_calls_for_10_30_50_member_chains(tmp_path, capsys):
    """Prints the table the PR text quotes. Calls come from the counting fake; the wall time is LOCAL
    computation against the fake only. Network wall time is not measured."""
    rows = []
    for k in (10, 30, 50):
        base = _repo(tmp_path / ("l%d" % k), dag_mode="off")
        calls = []
        t = time.perf_counter()
        p = _plan(base, k, _line_issues(k), calls)
        live_ms = (time.perf_counter() - t) * 1000
        assert len(p["members"]) == k and p["source"] == "live"
        assert p["calls"]["total"] == len(calls) == k + 1       # k single GETs + one list read
        mbase = _repo(tmp_path / ("m%d" % k))
        _save_map(mbase, [_mnode(i) for i in range(1, k + 1)], [(i, i + 1) for i in range(1, k)])
        mcalls = []
        t = time.perf_counter()
        mp = _plan(mbase, k, [], mcalls)
        map_ms = (time.perf_counter() - t) * 1000
        assert len(mp["members"]) == k and mp["source"] == "map"
        assert mp["calls"]["total"] == len(mcalls) == 0
        rows.append((k, 0, map_ms, len(calls), live_ms))
    with capsys.disabled():
        print("\nchain plan measured (counting fake; local wall time only, network not measured)")
        print("members | map calls | map local ms | live calls | live local ms")
        for k, mc, mms, lc, lms in rows:
            print("%7d | %9d | %12.1f | %10d | %13.1f" % (k, mc, mms, lc, lms))
