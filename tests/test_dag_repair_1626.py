"""#1626: the blocker-priority repair reads the stored dependency map.

Controls, each run red once on its own and restored (the test each one turns red):
* the corpus list read added back on the map path -> `test_a_usable_map_makes_zero_list_reads`
* the recursive walk restored in `discovery.blocker_required_ranks` -> `test_a_5000_deep_chain_*` (both)
* the map path returns a different set (an `other` edge counted) -> `test_golden_old_versus_new_*`
* the gate loosened (any mode but `off` reads the map) -> `test_every_other_gate_state_takes_todays_path_*`
* the mode clause alone, the missing-map clause alone, the stale-check clause alone: one parametrised case each
No test reaches the network; the board is the in-memory fake of `test_blocker_rank_repair`, the map is
built with `dag_store.save`, and every issue number is a fixture issue.
"""
import copy
import importlib.util
import json
import pathlib
import re
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_blocker_rank_repair as br  # noqa: E402  (its board fake and issue builders)

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
GOAL = "sdlc:goal"
NOW = time.time()


def _mod(name, tag="1626"):
    spec = importlib.util.spec_from_file_location(name + "_" + tag, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ds = _mod("dag_store")
core = _mod("dag_core")
disc = _mod("discovery")


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _prio(issue):
    for lab in issue["labels"]:
        if lab["name"].startswith("priority:"):
            return lab["name"].split(":", 1)[1]
    return None


def _map_of(issues, extra_nodes=(), extra_edges=(), checked=NOW - 60):
    """The map the refresh would have written for `issues` (open, goal-labelled ones are the corpus)."""
    nodes, edges = {}, []
    for it in issues:
        n = it["number"]
        names = {l["name"] for l in it["labels"]}
        corpus = it["state"] == "open" and GOAL in names
        nodes[n] = {"number": n, "kind": "issue", "state": it["state"], "life": "goal" if corpus else "none",
                    "prio": _prio(it), "in_corpus": corpus}
        if corpus:
            edges += [(a, b, "body") for a, b, _p in core.edges_from("%s\n%s" % (it["title"], it["body"]), n)]
    for n in extra_nodes:
        nodes[n["number"]] = n
    edges += list(extra_edges)
    stamp = _iso(NOW - 60)
    h = {"repo": "acme/widget", "built_at": stamp, "refreshed_at": stamp, "last_ok_at": stamp,
         "last_error": None, "partial": False}
    if checked is not None:
        h["checked_at"] = _iso(checked)
    return {"header": h, "nodes": [nodes[k] for k in sorted(nodes)],
            "edges": [{"blocker": a, "blocked": b, "source": s} for a, b, s in sorted(set(edges))]}


def _cfg(mode="on"):
    cfg = br._cfg()
    if mode is not None:
        cfg["discovery"]["dag"] = {"mode": mode}
    return cfg


def _setup(tmp_path, issues, mode="on", checked=NOW - 60, save=True, **kw):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True, exist_ok=True)
    if save:
        ok, why = ds.save(str(d), _map_of(issues, checked=checked, **kw))
        assert ok, why
    return d, _cfg(mode)


def _run(issues, d, cfg, handoffs=(), **kw):
    """-> (result, run) for one apply pass of a fresh source."""
    run = br._runner(copy.deepcopy(issues), **kw)
    src = br._mod("sources").GitHubSource(cfg, run=run, sdlc_dir=str(d))
    src.ledger_stub = handoffs
    if handoffs:
        led = type(src).__init__.__globals__["ledger"]
        led.read_all = lambda _d, *a, **k: []
        led.outstanding = lambda _e, _h=list(handoffs): list(_h)
    return src.repair_blocker_ranks(), run


def _writes_of(run):
    return sorted(tuple(c[:4]) for c in br._writes(run))


def _golden_issues():
    return [
        br._issue(1, [GOAL, "priority:P0"], body="Blocked by #2. Also blocked by #3."),   # 2, 3 are blockers
        br._issue(2, [GOAL, "priority:P3"], body="Blocked by #4"),                          # chain: 4 outside
        br._issue(3, [GOAL, "priority:P2", "sdlc:needs-confirmation"]),                      # legacy label
        br._issue(4, [GOAL, "priority:P4"]),
        br._issue(5, [GOAL, "priority:P1"], body="Blocked by other/repo#77 and blocked by #6"),  # other repo note
        br._issue(6, [GOAL, "priority:P3"]),
        br._issue(7, [GOAL, "priority:P1"], body="Blocked by #8"),                           # 8 is closed
        br._issue(8, [GOAL, "priority:P4"], state="closed"),
        br._issue(9, [GOAL, "priority:P0"]),                                                  # hand-off target
        br._issue(10, ["priority:P3"]),                                                       # outside blocker (hand-off)
        br._issue(11, [GOAL, "priority:P1", "sdlc:blocking"], state="closed"),                # stale mark
    ]


HANDOFFS = [{"issue": "10", "goal": "9"}]


def _view(r):
    return {k: sorted(r[k]) for k in ("raised", "stripped", "unmarked")} | {"violations": r["violations"]}


# ------------------------------------------------------------------ the map answers: no list reads

def test_a_usable_map_makes_zero_list_reads(tmp_path):
    issues = [br._issue(9, [GOAL, "priority:P0"], body="blocked by #7"), br._issue(7, [GOAL, "priority:P3"])]
    d, cfg = _setup(tmp_path, issues)
    res, run = _run(issues, d, cfg)
    assert res["raised"] == ["7"] and res["violations"] == 1
    corpus_reads = [c for c in run.calls if any("sdlc:goal" in str(a) or "sdlc:blocked" in str(a) for a in c)
                    and "--add-label" not in c and c[:2] != ["issue", "edit"]]
    assert corpus_reads == [], corpus_reads
    assert br._raised(run, 7, "priority:P0")


def test_the_old_path_does_read_the_lists(tmp_path):
    """Calibration: the counter above can see a list read when one is made."""
    issues = [br._issue(9, [GOAL, "priority:P0"], body="blocked by #7"), br._issue(7, [GOAL, "priority:P3"])]
    d, cfg = _setup(tmp_path, issues, mode="off", save=False)
    _res, run = _run(issues, d, cfg)
    assert any("sdlc:goal" in str(a) for c in run.calls for a in c if c[:2] != ["issue", "edit"])


# ------------------------------------------------------------------ old versus new

def test_golden_old_versus_new_raises_strips_and_unmarks_are_identical(tmp_path):
    issues = _golden_issues()
    d_old, cfg_old = _setup(tmp_path / "old", issues, mode="off", save=False)
    old, run_old = _run(issues, d_old, cfg_old, handoffs=HANDOFFS)
    d_new, cfg_new = _setup(tmp_path / "new", issues, extra_nodes=[
        {"number": 10, "kind": "issue", "state": "open", "life": "none", "prio": "P3", "in_corpus": False}])
    new, run_new = _run(issues, d_new, cfg_new, handoffs=HANDOFFS)
    assert _view(old) == _view(new)
    assert sorted(old["raised"]) == ["10", "2", "4", "6"]            # the golden is not vacuous
    assert old["stripped"] == ["3"] and old["unmarked"] == ["11"] and old["unwritable"]
    assert _writes_of(run_old) == _writes_of(run_new)


def test_other_repository_note_and_closed_blocker_do_not_raise_on_the_map_path(tmp_path):
    issues = _golden_issues()
    d, cfg = _setup(tmp_path, issues)
    res, run = _run(issues, d, cfg, handoffs=HANDOFFS)
    assert "77" not in res["raised"] and "8" not in res["raised"]


def test_a_blocker_the_map_knows_closed_or_a_pull_request_is_never_read(tmp_path):
    issues = [br._issue(9, [GOAL, "priority:P0"], body="blocked by #8, blocked by #6"),
              br._issue(8, [GOAL, "priority:P4"], state="closed")]
    pr = {"number": 6, "kind": "pr", "state": "open", "life": "none", "prio": "P4", "in_corpus": False}
    d, cfg = _setup(tmp_path, issues, extra_nodes=[pr])
    res, run = _run(issues, d, cfg)
    assert res["raised"] == [] and br._writes(run) == []
    assert not any(str(c[1]).endswith(("/issues/8", "/issues/6")) for c in run.calls if len(c) > 1), run.calls


def test_a_timeline_edge_is_not_an_input(tmp_path):
    issues = [br._issue(9, [GOAL, "priority:P0"]), br._issue(7, [GOAL, "priority:P3"])]
    d, cfg = _setup(tmp_path, issues, extra_edges=[(7, 9, "timeline"), (7, 9, "other")])
    res, run = _run(issues, d, cfg)
    assert res["raised"] == [] and br._writes(run) == []


# ------------------------------------------------------------------ the gate

def _unusable(tmp_path, clause):
    issues = [br._issue(9, [GOAL, "priority:P0"], body="blocked by #7"), br._issue(7, [GOAL, "priority:P3"])]
    if clause == "mode shadow":
        d, cfg = _setup(tmp_path, issues, mode="shadow")
    elif clause == "mode typo":
        d, cfg = _setup(tmp_path, issues, mode="ON")
    elif clause == "mode absent":
        d, cfg = _setup(tmp_path, issues, mode=None)
    elif clause == "no map":
        d, cfg = _setup(tmp_path, issues, save=False)
    elif clause == "no recent check":
        d, cfg = _setup(tmp_path, issues, checked=NOW - 3600)
    else:                                               # unchecked
        d, cfg = _setup(tmp_path, issues, checked=None)
    return issues, d, cfg


CLAUSES = ("mode shadow", "mode typo", "mode absent", "no map", "no recent check", "unchecked")


@pytest.mark.parametrize("clause", CLAUSES)
def test_every_other_gate_state_takes_todays_path_byte_identical(tmp_path, clause):
    issues, d, cfg = _unusable(tmp_path / "a", clause)
    res, run = _run(issues, d, cfg)
    d2, cfg2 = _setup(tmp_path / "b", issues, mode="off", save=False)
    ref, run2 = _run(issues, d2, cfg2)
    assert res == ref and run.calls == run2.calls
    assert res["raised"] == ["7"]


# ------------------------------------------------------------------ caps

def test_the_write_cap_still_holds_on_the_map_path(tmp_path):
    issues = [br._issue(100, [GOAL, "priority:P0"], body=" ".join("blocked by #%d" % n for n in range(1, 6)))]
    issues += [br._issue(n, [GOAL, "priority:P3"]) for n in range(1, 6)]
    d, cfg = _setup(tmp_path, issues)
    res, _run_ = _run(issues, d, cfg)
    capped = br._mod("sources").GitHubSource(cfg, run=br._runner(copy.deepcopy(issues)), sdlc_dir=str(d))
    r2 = capped.repair_blocker_ranks(cap=2)
    assert len(r2["raised"]) == 2 and r2["violations"] == 5 and len(res["raised"]) == 5


def test_outside_reads_stay_capped_at_ten(tmp_path):
    body = " ".join("blocked by #%d" % n for n in range(200, 215))
    issues = [br._issue(9, [GOAL, "priority:P0"], body=body)] + [br._issue(n, ["priority:P3"]) for n in range(200, 215)]
    nodes = [{"number": n, "kind": "issue", "state": "open", "life": "none", "prio": "P3", "in_corpus": False}
             for n in range(200, 215)]
    d, cfg = _setup(tmp_path, issues, extra_nodes=nodes)
    res, run = _run(issues, d, cfg)
    views = [c for c in run.calls if c[:2] == ["api", "repos/acme/widget/issues/200"] or
             (c and re.search(r"/issues/2\d\d$", str(c[1]) if len(c) > 1 else ""))]
    assert res.get("unread") == 5
    assert len(res["raised"]) <= 10


# ------------------------------------------------------------------ depth

def _chain(n):
    """Fixture issue i blocks fixture issue i-1: a chain n deep, the head most urgent."""
    issues = [br._issue(1, [GOAL, "priority:P0"])]
    issues += [br._issue(i, [GOAL, "priority:P4"], body="blocked by #%d" % (i + 1)) for i in range(2, n)]
    return issues


def test_a_5000_deep_chain_goes_through_the_shared_rule_without_recursion_error():
    edges = [(i, i + 1) for i in range(1, 5000)]          # the walk enters at 1 and goes 5,000 deep
    ranks = {i: 4 for i in range(1, 5001)}
    ranks[5000] = 0
    required, cycles = disc.blocker_required_ranks(edges, ranks)
    assert cycles == [] and len(required) == 4999 and required[1] == (0, 2)


def test_a_5000_deep_chain_goes_through_the_repair_path_without_recursion_error(tmp_path):
    n = 5000                                                # issue i waits on i-1; the walk enters at 1
    issues = [br._issue(1, [GOAL, "priority:P4"])]
    issues += [br._issue(i, [GOAL, "priority:P4" if i < n else "priority:P0"], body="blocked by #%d" % (i - 1))
               for i in range(2, n + 1)]
    d, cfg = _setup(tmp_path, issues)
    src = br._mod("sources").GitHubSource(cfg, run=br._runner(copy.deepcopy(issues)), sdlc_dir=str(d))
    src._REPAIR_EDGE_CAP = 10 ** 6
    res = src.repair_blocker_ranks(apply=False)
    assert res["skipped"] == "" and res["violations"] == n - 1


def test_the_iterative_walk_matches_known_cycle_and_tie_results():
    ranks = {1: 0, 2: 3, 3: 3, 4: 1}
    required, cycles = disc.blocker_required_ranks([(2, 1), (3, 1), (4, 1), (3, 2), (2, 3)], ranks)
    assert cycles == [2, 3]
    assert required[2] == (0, 1) and required[3] == (0, 1) and required[4] == (0, 1)


def _recursive_reference(edges, ranks):
    """The pre-#1626 walk, verbatim in behaviour, as the oracle (and the control: it overflows at depth)."""
    ranks = ranks or {}
    rank_of = lambda r: disc.UNPRIORITISED if ranks.get(r) is None else ranks[r]      # noqa: E731
    blocks = {}
    for by, blocked in edges:
        if by != blocked:
            lst = blocks.setdefault(by, [])
            if blocked not in lst:
                lst.append(blocked)
    resolved, in_progress, stack, cycles = {}, set(), [], set()

    def resolve(ref):
        if ref in resolved:
            return resolved[ref]
        if ref in in_progress:
            cycles.update(stack[stack.index(ref):])
            return rank_of(ref)
        in_progress.add(ref)
        stack.append(ref)
        best = rank_of(ref)
        for dep in blocks.get(ref, ()):
            best = min(best, resolve(dep))
        stack.pop()
        in_progress.discard(ref)
        resolved[ref] = best
        return best
    for ref in list(blocks):
        resolve(ref)
    changed = True
    while changed:
        changed = False
        for ref in blocks:
            best = min([resolved[ref]] + [resolved.get(d, rank_of(d)) for d in blocks[ref]])
            if best < resolved[ref]:
                resolved[ref], changed = best, True
    required = {}
    for ref, new_rank in resolved.items():
        if ref not in blocks or new_rank >= rank_of(ref):
            continue
        required[ref] = (new_rank, next((d for d in blocks[ref] if resolved.get(d, rank_of(d)) == new_rank), None))
    return required, sorted(cycles, key=str)


def test_the_iterative_walk_equals_the_recursive_one_on_random_graphs():
    import random
    rng = random.Random(1626)
    for _ in range(400):
        n = rng.randint(2, 9)
        edges = [(rng.randint(1, n), rng.randint(1, n)) for _ in range(rng.randint(0, 14))]
        ranks = {i: rng.choice([0, 1, 2, 3, 4, None]) for i in range(1, n + 1) if rng.random() < 0.9}
        assert disc.blocker_required_ranks(edges, ranks) == _recursive_reference(edges, ranks), (edges, ranks)


def test_the_reference_walk_does_overflow_at_depth():
    """Calibration: the deep-chain tests can fail, the recursive shape is what they guard against."""
    with pytest.raises(RecursionError):
        _recursive_reference([(i, i + 1) for i in range(1, 5000)], {5000: 0})


# ------------------------------------------------------------------ structure

def _code(path):
    text = re.sub(r'""".*?"""', "", path.read_text(encoding="utf-8"), flags=re.S)
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_dag_modules_call_no_priority_writer():
    writers = re.compile(r"_write_blocker_promotion|_swap_labels|_issue_remove_label|_mirror_priority|\bpromote\w*\(")
    files = sorted(S.glob("dag_*.py"))
    assert S / "dag_repair.py" in files
    for path in files:
        assert not writers.search(_code(path)), path.name


def test_dag_repair_makes_no_gh_call():
    assert not re.search(r"gh_api|\"gh\"|subprocess|\bgh issue|\.edit\(|_run\(", _code(S / "dag_repair.py"))
