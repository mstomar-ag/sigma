"""#1989: `dag_insert.remove` takes one edge out of the stored map (the counterpart of `insert`).

Fixture issues only (7, 8, 9). Control: skip the edge removal in `remove` -> the edge test goes red.
"""
import importlib.util
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_dag_insert_1616 as t

dag_insert, dag_store = t.dag_insert, t.dag_store


def _setup(tmp_path, edges):
    sdlc = tmp_path / ".sdlc"
    t._store(sdlc, [t._node(7), t._node(8), t._node(9)], edges)
    return sdlc


def test_remove_drops_the_edge_and_keeps_the_others(tmp_path):
    sdlc = _setup(tmp_path, [t._edge(8, 7), t._edge(9, 7)])
    assert dag_insert.remove(sdlc, {}, 8, 7) == "removed"
    assert t._edges(sdlc) == {(9, 7)}
    assert t._numbers(sdlc) == {7, 8, 9}


def test_remove_with_no_such_edge_is_absent_and_leaves_the_map(tmp_path):
    sdlc = _setup(tmp_path, [t._edge(9, 7)])
    assert dag_insert.remove(sdlc, {}, 8, 7) == "absent"
    assert t._edges(sdlc) == {(9, 7)}


def test_remove_with_no_stored_map_is_nomap(tmp_path):
    assert dag_insert.remove(tmp_path / ".sdlc", {}, 8, 7) == "nomap"


def test_remove_in_mode_off_is_off_and_writes_nothing(tmp_path):
    sdlc = _setup(tmp_path, [t._edge(8, 7)])
    cfg = {"discovery": {"dag": {"mode": "off"}}}
    assert dag_insert.remove(sdlc, cfg, 8, 7) == "off"
    assert t._edges(sdlc) == {(8, 7)}


def test_remove_drops_a_matching_unchecked_pair(tmp_path):
    sdlc = _setup(tmp_path, [t._edge(8, 7)])
    dag_insert.record_unchecked(sdlc, 8, 7, {})
    dag_insert.record_unchecked(sdlc, 9, 7, {})
    dag_insert.remove(sdlc, {}, 8, 7)
    assert dag_insert.unchecked(sdlc) == [[9, 7]]
