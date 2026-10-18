"""One stored-map load and one auto-unpark sweep per batch, not one per slot.

Controls, each run red once on its own and restored (the test each one turns red):
* the sweep hoist reverted (every slot call sweeps again) -> `test_a_three_slot_batch_sweeps_and_loads_once`
* the map-cache sharing dropped (the prologue order pick gets its own dict) -> `test_a_three_slot_batch_sweeps_and_loads_once`
* the swept goals no longer folded into the slot skip -> `test_a_goal_the_sweep_just_unparked_is_not_picked_in_the_same_batch`
* the per-call `_dag_order_pick` put back in `_next_untraced` -> `test_a_three_slot_batch_sweeps_and_loads_once`
No test reaches the network: the board is the in-memory fake of `test_pick_dependency_gate` and the map
is written with `dag_store.save`. Issue numbers here are fixture issues, not real ones.
"""
import importlib.util
import json
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_dag_order_1620 as t20  # noqa: E402  (its gate fixture)
import test_dag_pick_1619 as t19  # noqa: E402  (the stored-map writer)
import test_pick_dependency_gate as pg  # noqa: E402  (the board fake)

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
NOW = time.time()
ROOT_A, DEP_A, ROOT_B, DEP_B, ROOT_C = 10, 11, 20, 21, 30     # two chains and a free goal


def _setup(tmp_path, edges, nums, mode="on", extra=None):
    cfg = {"discovery": {"dag": {"mode": mode}}}
    cfg.update(extra or {})
    base = pg._base(str(tmp_path), mirror=None, gh_mode=True, extra=cfg)
    t19._store(base, [t19._node(n) for n in sorted(nums)], edges)
    lp = pg._loop()
    return base, lp, pg._cfg(lp, base)


def _counting(lp, sweeps):
    """Counts `_auto_unpark_sweep` calls and every `dag_store.load` made through the order and hold
    readers (`dag_order`, `dag_pick`); the change check (`dag_check`) reads the file on its own."""
    loads = []
    real_load = lp._load

    def load(name):
        m = real_load(name)
        store = getattr(m, "dag_store", None)
        if name in ("dag_order", "dag_pick") and store is not None and not getattr(store, "_counted_1621", False):
            inner = store.load
            store.load = lambda *a, **k: loads.append(1) or inner(*a, **k)
            store._counted_1621 = True
        return m

    lp._load = load
    lp._auto_unpark_sweep = lambda sdlc_dir, config, run=None: sweeps.append(1) or frozenset()
    return loads


def _board(order):
    return pg._Board([str(n) for n in order], live=[])


def test_a_three_slot_batch_sweeps_and_loads_once(tmp_path):
    base, lp, cfg = _setup(tmp_path, [(ROOT_A, DEP_A), (ROOT_B, DEP_B)],
                           {ROOT_A, DEP_A, ROOT_B, DEP_B, ROOT_C})
    sweeps = []
    loads = _counting(lp, sweeps)
    src = _board([DEP_A, ROOT_A, DEP_B, ROOT_B, ROOT_C])
    picks = lp.next_batch(base, src, cfg, max_concurrent=3)
    assert [p[1] for p in picks] == ["10", "20", "30"]
    assert len(sweeps) == 1
    assert len(loads) == 1


def test_next_alone_still_sweeps_once(tmp_path):
    base, lp, cfg = _setup(tmp_path, [(ROOT_A, DEP_A)], {ROOT_A, DEP_A})
    sweeps = []
    _counting(lp, sweeps)
    assert lp._next(base, _board([DEP_A, ROOT_A]), cfg) == ("goal", "10")
    assert len(sweeps) == 1


def test_two_independent_chains_fill_two_slots_with_roots(tmp_path):
    base, lp, cfg = _setup(tmp_path, [(ROOT_A, DEP_A), (ROOT_B, DEP_B)],
                           {ROOT_A, DEP_A, ROOT_B, DEP_B})
    src = _board([DEP_A, DEP_B, ROOT_A, ROOT_B])
    picks = lp.next_batch(base, src, cfg, max_concurrent=4)
    assert picks == [("goal", "10"), ("goal", "20"), ("DONE", None)]
    assert src.marked == ["10", "20"]               # no dependent of an in-flight root was offered


def test_a_goal_the_sweep_just_unparked_is_not_picked_in_the_same_batch(tmp_path):
    base, lp, cfg = _setup(tmp_path, [], {ROOT_A, ROOT_B, ROOT_C})
    lp._auto_unpark_sweep = lambda sdlc_dir, config, run=None: frozenset({"10"})
    src = _board([ROOT_A, ROOT_B, ROOT_C])
    picks = lp.next_batch(base, src, cfg, max_concurrent=3)
    assert picks == [("goal", "20"), ("goal", "30"), ("DONE", None)]


def test_parallel_goals_off_is_a_single_slot(tmp_path):
    base, lp, cfg = _setup(tmp_path, [], {ROOT_A, ROOT_B})
    sweeps = []
    _counting(lp, sweeps)
    picks = lp.next_batch(base, _board([ROOT_A, ROOT_B]), cfg)
    assert picks == [("goal", "10")]
    assert len(sweeps) == 1


def test_only_wave_zero_is_picked_when_every_other_member_is_skipped(tmp_path):
    edges = [(1, 3), (2, 3), (3, 4)]
    base, lp, cfg = _setup(tmp_path, edges, {1, 2, 3, 4})
    spec = importlib.util.spec_from_file_location("dag_chain", S / "dag_chain.py")
    dc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dc)
    body = dc.chain(base, 4, now=NOW)
    wave0 = set(body["waves"][0])
    others = {m["number"] for m in body["members"]} - wave0
    assert wave0 == {1, 2} and others
    src = _board([4, 3, 2, 1])
    picks = lp.next_batch(base, src, cfg, max_concurrent=4, extra_skip={str(n) for n in others})
    assert {p[1] for p in picks if p[0] == "goal"} == {str(n) for n in wave0}
    assert len([p for p in picks if p[0] == "goal"]) == len(wave0)


@pytest.mark.parametrize("case,kw", [
    ("mode off", {"mode": "off"}),
    ("local source", {"source": "local-goals"}),
])
def test_a_closed_gate_batch_never_loads_dag_order(tmp_path, case, kw):
    disc = {"source": kw.get("source", "github")}
    base = pg._base(str(tmp_path), mirror=None, gh_mode=False, extra={"discovery": disc})
    t20._map(base)
    if kw.get("mode"):
        p = pathlib.Path(base) / "config.json"
        c = json.loads(p.read_text())
        c["discovery"]["dag"] = {"mode": kw["mode"]}
        p.write_text(json.dumps(c))
    lp = pg._loop()
    loaded = []
    real = lp._load
    lp._load = lambda name: loaded.append(name) or real(name)
    lp._auto_unpark_sweep = lambda sdlc_dir, config, run=None: frozenset()
    picks = lp.next_batch(base, _board([1, 2]), pg._cfg(lp, base), max_concurrent=2)
    assert [p[0] for p in picks] == ["goal", "goal"]
    assert "dag_order" not in loaded
