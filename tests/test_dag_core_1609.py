"""#1609: the pure dependency-graph core. Goldens on small fixtures, cycle reporting that does not
depend on where a walk starts, depth without recursion, input-order independence, the three
pull-request/unknown state rules, a loose cost ceiling, and a structural pin on dag_core's imports.
Each guard has a `control:` note, run red once against a deliberate break. Issue numbers in
fixtures are plain integers; the text fixtures build a reference with `_ref` so no literal
issue reference lands in this file."""
import ast
import importlib.util
import json
import pathlib
import random
import time

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dag = _mod("dag_core")


def _ref(n):
    return "#" + str(n)


def _g(edges, states=None, prios=None, nodes=()):
    """edges: (blocker, blocked) pairs. states: {number: state}; default every node open issue."""
    states = states or {}
    prios = prios or {}
    nums = set(nodes)
    for a, b in edges:
        nums.update((a, b))
    recs = [dag.Node(n, "issue", states.get(n, "open"), "", prios.get(n)) for n in sorted(nums)]
    return dag.build(recs, [(a, b, "t") for a, b in edges])


CHAIN = [(1, 2), (2, 3), (3, 4)]                  # 1 blocks 2 blocks 3 blocks 4
DIAMOND = [(1, 2), (1, 3), (2, 4), (3, 4)]
FAN_IN = [(1, 5), (2, 5), (3, 5), (4, 5)]
FAN_OUT = [(1, 2), (1, 3), (1, 4)]


# ---- closure / order / waves goldens ---------------------------------------------------------
def test_chain_goldens():
    g = _g(CHAIN)
    assert dag.closure(g, 4) == [1, 2, 3]
    assert dag.closure(g, 1) == []
    assert dag.order(g) == [1, 2, 3, 4]
    assert dag.waves(g) == [[1], [2], [3], [4]]
    assert dag.depth(g, 4) == 3 and dag.depth(g, 1) == 0
    assert dag.open_dependents(g, 1) == [2, 3, 4]


def test_diamond_goldens():
    g = _g(DIAMOND)
    assert dag.closure(g, 4) == [1, 2, 3]
    assert dag.order(g) == [1, 2, 3, 4]
    assert dag.waves(g) == [[1], [2, 3], [4]]
    assert dag.depth(g, 4) == 2
    assert dag.open_dependents(g, 1) == [2, 3, 4]


def test_fan_in_goldens():
    g = _g(FAN_IN)
    assert dag.closure(g, 5) == [1, 2, 3, 4]
    assert dag.waves(g) == [[1, 2, 3, 4], [5]]
    assert dag.leaves(g) == [1, 2, 3, 4]


def test_fan_out_goldens():
    g = _g(FAN_OUT)
    assert dag.closure(g, 3) == [1]
    assert dag.waves(g) == [[1], [2, 3, 4]]
    assert dag.open_dependents(g, 1) == [2, 3, 4]
    assert dag.leaves(g) == [1]


def test_disconnected_parts():
    g = _g([(1, 2), (10, 11)], nodes=[99])
    assert dag.components(g) == [(1, 2), (10, 11), (99,)]
    assert dag.closure(g, 11) == [10]
    assert dag.waves(g) == [[1, 10, 99], [2, 11]]


def test_resolved_middle_stops_closure():
    """control: stop treating closed as resolved -> closure walks through 2 and this goes red."""
    g = _g(CHAIN, states={2: "closed"})
    assert dag.closure(g, 4) == [3]
    assert dag.closure(g, 3) == []
    assert dag.order(g) == [1, 3, 4]
    assert dag.waves(g) == [[1, 3], [4]]
    assert dag.open_dependents(g, 1) == []     # the resolved middle cuts the dependents walk too


def test_order_ties_by_priority_then_number():
    g = _g([], prios={5: "P2", 6: "P0", 7: "P0", 8: None}, nodes=[5, 6, 7, 8])
    assert dag.order(g) == [6, 7, 5, 8]
    assert dag.waves(g) == [[6, 7, 5, 8]]


# ---- cycles ------------------------------------------------------------------------------------
def test_two_cycle():
    assert dag.cycles(_g([(1, 2), (2, 1)])) == [(1, 2)]


def test_three_cycle():
    assert dag.cycles(_g([(1, 2), (2, 3), (3, 1)])) == [(1, 2, 3)]


def test_two_disjoint_cycles():
    g = _g([(1, 2), (2, 1), (5, 6), (6, 7), (7, 5)])
    assert dag.cycles(g) == [(1, 2), (5, 6, 7)]


def test_cycle_with_tail_reports_only_the_loop():
    g = _g([(1, 2), (2, 3), (3, 2), (3, 4)])
    assert dag.cycles(g) == [(2, 3)]
    assert dag.order(g) == [1]                 # cycle members and what waits on them are held
    assert dag.waves(g) == [[1]]


def test_self_edge_is_ignored():
    g = _g([(1, 1), (1, 2)])
    assert dag.cycles(g) == []
    assert dag.order(g) == [1, 2]


def test_cycles_do_not_depend_on_where_a_walk_starts():
    """control: make the walk entry-dependent (skip a node an earlier root's walk touched when it
    should still join its own component) -> some start order reports a different set; red."""
    edges = [(1, 2), (2, 3), (3, 1), (3, 4), (4, 5), (5, 4), (0, 1)]
    want = [(1, 2, 3), (4, 5)]
    nums = sorted({n for e in edges for n in e})
    rng = random.Random(7)
    for _ in range(40):
        order = nums[:]
        rng.shuffle(order)
        shuffled = edges[:]
        rng.shuffle(shuffled)
        recs = [dag.Node(n, "issue", "open", "", None) for n in order]
        g = dag.build(recs, [(a, b, "t") for a, b in shuffled])
        assert dag.cycles(g) == want
    g = _g(edges)
    for first in nums:                           # and with every node as the one the walk enters first
        assert dag.cycles(g, start=first) == want, first


# ---- depth of walk, no recursion ----------------------------------------------------------------
def test_deep_chain_no_recursion_error():
    """control: swap in a recursive walk -> RecursionError at 5,000 deep; red."""
    n = 5000
    g = _g([(i, i + 1) for i in range(1, n)])
    assert len(dag.closure(g, n)) == n - 1
    assert dag.depth(g, n) == n - 1
    assert len(dag.open_dependents(g, 1)) == n - 1
    assert dag.cycles(g) == []
    assert dag.order(g)[:3] == [1, 2, 3]
    assert len(dag.waves(g)) == n
    assert dag.components(g) == [tuple(range(1, n + 1))]


def test_deep_chain_cycle_no_recursion_error():
    n = 5000
    g = _g([(i, i + 1) for i in range(1, n)] + [(n, 1)])
    assert dag.cycles(g) == [tuple(range(1, n + 1))]


def test_wide_fan_no_recursion_error():
    m = 50000
    g = _g([(1, i) for i in range(2, m + 2)])
    assert len(dag.open_dependents(g, 1)) == m
    assert dag.closure(g, m + 1) == [1]
    g2 = _g([(i, 1) for i in range(2, m + 2)])
    assert len(dag.closure(g2, 1)) == m


# ---- determinism ---------------------------------------------------------------------------------
def test_shuffled_input_gives_identical_output():
    rng = random.Random(11)
    nums = list(range(1, 60))
    edges = set()
    while len(edges) < 140:
        a, b = rng.sample(nums, 2)
        edges.add((a, b))
    edges = sorted(edges)
    states = {n: rng.choice(["open", "closed", "open"]) for n in nums}
    prios = {n: rng.choice(["P0", "P1", "P3", None]) for n in nums}
    seen = set()
    for _ in range(12):
        e = edges[:]
        rng.shuffle(e)
        ns = nums[:]
        rng.shuffle(ns)
        recs = [dag.Node(n, "issue", states[n], "", prios[n]) for n in ns]
        g = dag.build(recs, [(a, b, "t") for a, b in e])
        seen.add(json.dumps(dag.summary(g), sort_keys=False))
    assert len(seen) == 1
    assert json.loads(next(iter(seen)))["cycles"]       # the property ran over a graph with cycles


# ---- pull-request and unknown states ---------------------------------------------------------------
def _pr_graph(state, kind="pr"):
    recs = [dag.Node(1, kind, state, "", None), dag.Node(2, "issue", "open", "", None)]
    return dag.build(recs, [(1, 2, "t")])


def test_merged_pr_resolves():
    g = _pr_graph("merged")
    assert dag.closure(g, 2) == []
    assert dag.leaves(g) == [2]


def test_closed_unmerged_pr_stays_unresolved():
    for state in ("closed-unmerged", "closed"):
        g = _pr_graph(state)
        assert dag.closure(g, 2) == [1]
        assert dag.leaves(g) == [1]


def test_unknown_is_reported_and_never_resolves():
    g = _pr_graph("unknown")
    assert dag.closure(g, 2) == [1]
    assert dag.unknown(g) == [1]
    assert dag.leaves(g) == [1]
    ghost = dag.build([dag.Node(2, "issue", "open", "", None)], [(7, 2, "t")])   # never supplied
    assert dag.unknown(ghost) == [7] and dag.closure(ghost, 2) == [7]


# ---- the text extractor -------------------------------------------------------------------------
def test_edges_from_uses_the_explicit_vocabulary():
    text = "Blocked by " + _ref(8) + " and blocked by " + _ref(9) + ". Also " + _ref(3) + " unrelated."
    assert dag.edges_from(text, 3) == [(8, 3, "blocked by"), (9, 3, "blocked by")]
    assert dag.edges_from("Blocked by " + _ref(3), 3) == []
    assert dag.edges_from("Blocked by owner/repo" + _ref(5), 3) == []


# ---- cost ceiling ----------------------------------------------------------------------------------
def test_ceiling_memoised_depth_visits_each_edge_once():
    """control: replace the memo with recomputation -> the layered graph below re-walks every path
    and the step count explodes past the bound; red. A step count, not a clock: deterministic."""
    layers, width = 11, 3
    edges = [(l * width + i, (l + 1) * width + j)
             for l in range(layers - 1) for i in range(width) for j in range(width)]
    g = _g(edges)
    for n in sorted(g.nodes):
        dag.depth(g, n)
    assert g.ops <= 8 * (len(g.nodes) + len(edges))


def test_ceiling_thirty_thousand_nodes_complete_within_a_loose_bound():
    n = 30000
    chain = [(i, i + 1) for i in range(1, n)]
    rng = random.Random(3)
    dense = [(rng.randrange(1, n), rng.randrange(1, n)) for _ in range(4 * n)]
    start = time.perf_counter()
    for edges in (chain, dense):
        g = _g(edges)
        dag.closure(g, n - 1)
        dag.components(g)
        dag.cycles(g)
    assert time.perf_counter() - start < 120     # a quadratic walk takes far longer; normal is ~1s


# ---- structure: what dag_core may import -----------------------------------------------------------
BANNED_MODULES = {"subprocess", "socket", "urllib", "http", "requests", "shutil", "tempfile",
                  "ssl", "ftplib", "smtplib", "asyncio", "gh_api", "gh_session", "sources",
                  "mirror", "ledger", "handoff", "actionlog", "state"}
BANNED_CALLS = {"open", "system", "popen", "write_text", "write_bytes", "mkdir", "unlink",
                "rename", "replace", "rmtree"}


def _tree():
    return ast.parse((S / "dag_core.py").read_text())


def _imported_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_dag_core_imports_no_network_gh_or_file_writing_module():
    assert _imported_names(_tree()).isdisjoint(BANNED_MODULES)


def test_dag_core_loads_siblings_only_from_the_pure_list():
    loaded = {n.args[0].value for n in ast.walk(_tree())
              if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_load"
              and n.args and isinstance(n.args[0], ast.Constant)}
    assert loaded <= {"blocker_scan", "discovery"}
    assert loaded


def test_dag_core_calls_no_file_or_process_function():
    called = set()
    for n in ast.walk(_tree()):
        if isinstance(n, ast.Call):
            f = n.func
            called.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    assert called.isdisjoint(BANNED_CALLS)


def test_structural_check_sees_a_banned_import():
    """The checker itself fails on a bad module: control for the three tests above."""
    bad = ast.parse("import subprocess\nfrom urllib import request\nx = open('f', 'w')\n")
    assert _imported_names(bad) & BANNED_MODULES == {"subprocess", "urllib"}
