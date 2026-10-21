"""#1986: a goal's page also draws the open work that waits on it (to the right, muted, edges tagged `holds`).

Controls, each broken alone and seen red: the open-only filter on dependents; the cap comparison; the nearest-first
order (replaced by plain number order); the exclusion of dependents from `open`; the left-out notice."""
import importlib.util
import pathlib
import re
import sys

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_dag_html import NOW, _data, _model, _node, _store, dh, dc  # noqa: E402


def _chain_store(tmp_path, extra_nodes, extra_edges):
    # 2 <- 1 (goal 1 waits on 2); 3 waits on 1; 4 waits on 3 (transitive); 5 is closed and waits on 1.
    nodes = [_node(2), _node(1), _node(3), _node(4), _node(5, state="closed")] + extra_nodes
    return _store(tmp_path, nodes, [(2, 1), (1, 3), (3, 4), (1, 5)] + extra_edges)


def test_dependents_are_drawn_right_of_the_goal_tagged_holds(tmp_path):
    m = _model(_chain_store(tmp_path, [], []), 1)
    assert {3, 4} <= set(m["nodes"]) and 5 not in m["nodes"]
    assert m["nodes"][3]["depth"] == m["nodes"][1]["depth"] + 1
    assert m["nodes"][4]["depth"] == m["nodes"][1]["depth"] + 2
    tags = {(a, b): t for a, b, t in m["edges"]}
    assert tags[(1, 3)] == "holds" and tags[(3, 4)] == "holds" and tags[(2, 1)] == "explicit"
    page = dh.render_page(m)
    assert 'class="e holds"' in page and "c-waiting" in page and "wait on it" in page


def test_a_middle_member_shows_blockers_left_and_dependents_right(tmp_path):
    m = _model(_chain_store(tmp_path, [], []), 3)
    assert set(m["nodes"]) == {1, 2, 3, 4}
    assert m["nodes"][2]["depth"] < m["nodes"][1]["depth"] < m["nodes"][3]["depth"] < m["nodes"][4]["depth"]
    assert [m["nodes"][n]["m"] for n in (1, 2)] != ["waiting", "waiting"]
    assert m["nodes"][4]["m"] == "waiting" and m["nodes"][3]["m"] != "waiting"


def test_a_leaf_draws_no_dependents_and_no_heading_fact(tmp_path):
    m = _model(_chain_store(tmp_path, [], []), 4)
    assert not [d for d in m["nodes"].values() if d["m"] == "waiting"]
    assert m["waiting"] == 0 and "wait on it" not in dh.render_page(m)


def test_closed_dependents_are_not_counted_or_drawn(tmp_path):
    m = _model(_chain_store(tmp_path, [], []), 1)
    assert m["waiting"] == 2 and 5 not in m["nodes"]


def _wide(tmp_path, count):
    nodes = [_node(1)] + [_node(100 + i) for i in range(count)]
    return _store(tmp_path, nodes, [(1, 100 + i) for i in range(count)])


def test_cap_is_honoured_and_the_left_out_count_is_written(tmp_path):
    m = _model(_wide(tmp_path, dh.DEPENDENT_CAP + 10), 1)
    assert sum(1 for d in m["nodes"].values() if d["m"] == "waiting") == dh.DEPENDENT_CAP
    assert m["waiting"] == dh.DEPENDENT_CAP + 10 and m["left_out"] == 10
    page = dh.render_page(m)
    assert "10 more open issues wait on #1 and are not drawn (cap %d)" % dh.DEPENDENT_CAP in page
    assert "<b>%d</b> open issues wait on it" % (dh.DEPENDENT_CAP + 10) in page


def test_at_the_cap_nothing_is_left_out(tmp_path):
    m = _model(_wide(tmp_path, dh.DEPENDENT_CAP), 1)
    assert m["left_out"] == 0 and "more open issues wait on" not in dh.render_page(m)


def test_nearest_dependents_are_drawn_first(tmp_path):
    # 1 -> 900..(cap-1) direct children, each also with a far child of a LOWER number; the direct ones must win.
    direct = list(range(500, 500 + dh.DEPENDENT_CAP))
    far = [10 + i for i in range(5)]
    nodes = [_node(1)] + [_node(n) for n in direct + far]
    edges = [(1, n) for n in direct] + [(direct[0], f) for f in far]
    m = _model(_store(tmp_path, nodes, edges), 1)
    assert set(m["nodes"]) - {1} == set(direct) and m["left_out"] == 5


def test_the_prerequisite_half_is_exactly_the_plan_closure(tmp_path):
    sdlc = _chain_store(tmp_path, [], [])
    m = _model(sdlc, 1)
    want = {x["number"] for x in dc.chain(sdlc, 1, include_resolved=True, now=NOW)["members"]}
    assert {n for n, d in m["nodes"].items() if d["m"] != "waiting"} == want
    assert m["open"] == 2 and m["depth"] == 1       # goal 1 and prerequisite 2 only; dependents excluded
    assert m["decisions"] == 0 and 3 not in m["who"]


def test_dependents_are_muted_and_members_are_not(tmp_path):
    page = dh.render_page(_model(_chain_store(tmp_path, [], []), 1))
    cls = {n: c for c, n in re.findall(r'<g class="n c-(\w+)[^"]*" data-n="(\d+)"', page)}
    assert cls["3"] == cls["4"] == "waiting" and cls["1"] != "waiting" and cls["2"] != "waiting"
    assert re.search(r"\.n\.c-waiting\{[^}]*opacity", page)
    assert _data(page)["nodes"]["3"]["mk"] == "waiting"


def test_a_dependent_in_a_cycle_is_drawn_once(tmp_path):
    sdlc = _store(tmp_path, [_node(1), _node(2)], [(1, 2), (2, 1)])
    m = _model(sdlc, 1)
    assert sorted(m["nodes"]) == [1, 2] and m["nodes"][2]["m"] != "waiting"
