"""#1620: "biggest chain first" on both pick queues, from the stored dependency map.

Controls, each run red once on its own and restored (the test each one turns red):
* the term dropped from the label key (`_pick_key`) only -> `test_label_path_biggest_chain_first`
* the term dropped from the board sort key only -> `test_board_path_biggest_chain_first`
* the term moved BEFORE the unit rank (position swap) -> `test_unit_rank_outranks_the_chain_term`
* the depth element dropped (dependents only) -> `test_ties_run_dependents_then_depth_then_existing_key`
* the `created` branch returns a shorter tuple -> `test_created_order_ignores_the_map_and_keeps_tuple_length`
* the change-check clause dropped from `dag_pick._entry` -> `test_an_unusable_map_or_order_off_changes_nothing[no change check-kw1-None]`
* the `order: off` clause dropped from `dag_order.chain_ranks` -> `test_an_unusable_map_or_order_off_changes_nothing[order off-kw3-config3]`
* a `comment` edge counted -> `test_a_comment_edge_never_boosts`
* the gate's mode clause dropped from `loop._dag_order_pick` -> `test_loop_gate_closed_never_loads_dag_order[mode off-kw0]`
No test reaches the network: sources run over in-memory fakes and the map is built with `dag_store.save`.
Issue numbers here are fixture issues, not real ones.
"""
import importlib.util
import json
import pathlib
import sys
import time
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_blocking_tier_order as tbo  # noqa: E402  (label and board source fixtures)
import test_dag_pick_1619 as t19  # noqa: E402  (the stored-map writer)
import test_github_project as tgp  # noqa: E402
import test_pick_dependency_gate as pg  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
spec = importlib.util.spec_from_file_location("dag_order", S / "dag_order.py")
do = importlib.util.module_from_spec(spec)
spec.loader.exec_module(do)

NOW = time.time()
FREE = 90                                   # oldest, nothing waits on it
BIG, SMALL = 100, 110                       # 5 and 2 open dependents (flat)
BIG_DEPS = [101, 102, 103, 104, 105]
SMALL_DEPS = [111, 112]
LONG, WIDE = 130, 140                       # 2 dependents each: a chain of 2 versus two leaves
LONG_DEPS, WIDE_DEPS = [131, 132], [141, 142]


def _map(base, extra_edges=(), **kw):
    edges = ([(BIG, d) for d in BIG_DEPS] + [(SMALL, d) for d in SMALL_DEPS]
             + [(LONG, 131), (131, 132)] + [(WIDE, d) for d in WIDE_DEPS] + list(extra_edges))
    nums = {FREE, BIG, SMALL, LONG, WIDE, *BIG_DEPS, *SMALL_DEPS, *LONG_DEPS, *WIDE_DEPS}
    for e in extra_edges:
        nums.update(e[:2])
    t19._store(base, [t19._node(n) for n in sorted(nums)], edges, **kw)


def _ranks(tmp_path, extra_edges=(), config=None, **kw):
    base = pg._base(str(tmp_path), gh_mode=True)
    _map(base, extra_edges, **kw)
    return base, do.chain_ranks(base, config or {}, {}, NOW)


def _label(issues, ranks, override=False):
    src = tbo._label_src(issues, override=override)
    src.dag_chain_ranks = ranks
    return src


def _board(cards, ranks):
    src = tbo._board(cards, override=True)
    src.dag_chain_ranks = ranks
    return src


def _iss(n, prio="P1"):
    return tbo._issue(n, prio)


def _order_by_key(src, numbers, prio="P1"):
    return [i["number"] for i in sorted((_iss(n, prio) for n in numbers), key=src._pick_key)]


# ------------------------------------------------------------------ 1 and 2. the two queues

def test_label_path_biggest_chain_first(tmp_path):
    _, ranks = _ranks(tmp_path)
    issues = [_iss(FREE), _iss(SMALL), _iss(BIG)]
    assert _label(issues, ranks).next_pending() == str(BIG)
    assert _label(issues, None).next_pending() == str(FREE)           # control: no term, oldest first
    assert _order_by_key(_label(issues, ranks), [FREE, SMALL, BIG]) == [BIG, SMALL, FREE]


def test_board_path_biggest_chain_first(tmp_path):
    _, ranks = _ranks(tmp_path)
    cards = [tgp._ready(FREE, "P1"), tgp._ready(SMALL, "P1"), tgp._ready(BIG, "P1")]
    assert _board(cards, ranks).next_pending() == str(BIG)
    assert _board(cards, None).next_pending() == str(FREE)            # board position decides without it


# ------------------------------------------------------------------ 3. what it must never cross

def test_never_crosses_a_priority_tier(tmp_path):
    _, ranks = _ranks(tmp_path)
    issues = [_iss(BIG, "P2"), _iss(FREE, "P0")]
    assert _label(issues, ranks).next_pending() == str(FREE)
    cards = [tgp._ready(BIG, "P2"), tgp._ready(FREE, "P0")]
    assert _board(cards, ranks).next_pending() == str(FREE)


def test_never_crosses_the_blocking_split(tmp_path):
    _, ranks = _ranks(tmp_path)
    issues = [_iss(BIG), tbo._issue(FREE, "P1", tbo.BLOCK)]
    assert _label(issues, ranks, override=True).next_pending() == str(FREE)
    cards = [tgp._ready(BIG, "P1"), tgp._ready(FREE, "P1", ("sdlc:goal", tbo.BLOCK))]
    assert _board(cards, ranks).next_pending() == str(FREE)


def test_created_order_ignores_the_map_and_keeps_tuple_length(tmp_path):
    _, ranks = _ranks(tmp_path)
    src = _label([], ranks)
    src.order = "created"
    keys = [src._pick_key(_iss(n)) for n in (BIG, FREE)]
    assert sorted(keys)[0][-1] == FREE                              # strict FIFO
    assert len({len(k) for k in keys}) == 1
    src.order = "priority"
    assert len(src._pick_key(_iss(BIG))) == len(keys[0])


# ------------------------------------------------------------------ 4. an unusable map or order off

@pytest.mark.parametrize("case,kw,config", [
    ("stale", {"built": NOW - 90 * 86400, "checked": NOW - 3600}, None),
    ("no change check", {"checked": None}, None),
    ("old change check", {"checked": NOW - 3600}, None),
    ("order off", {}, {"discovery": {"dag": {"order": "off"}}}),
])
def test_an_unusable_map_or_order_off_changes_nothing(tmp_path, case, kw, config):
    _, ranks = _ranks(tmp_path, config=config, **kw)
    assert ranks is None
    issues = [_iss(FREE), _iss(SMALL), _iss(BIG)]
    assert _label(issues, ranks).next_pending() == _label(issues, None).next_pending() == str(FREE)


def test_a_missing_map_returns_none(tmp_path):
    base = pg._base(str(tmp_path), gh_mode=True)
    assert do.chain_ranks(base, {}, {}, NOW) is None


# ------------------------------------------------------------------ 5. composition and ties

def test_unit_rank_outranks_the_chain_term(tmp_path):
    _, ranks = _ranks(tmp_path)
    src = _label([], ranks)
    src._feature_rank = lambda issue: 1 if issue["number"] == FREE else 5
    assert _order_by_key(src, [BIG, FREE, SMALL]) == [FREE, BIG, SMALL]


def test_ties_run_dependents_then_depth_then_existing_key(tmp_path):
    _, ranks = _ranks(tmp_path)
    src = _label([], ranks)
    assert ranks.get(LONG) == (-2, -2) and ranks.get(WIDE) == (-2, -1) and ranks.get(SMALL) == (-2, -1)
    # dependents first (BIG), then depth (LONG), then the existing key: the bug label, then the number
    assert _order_by_key(src, [WIDE, LONG, SMALL, BIG]) == [BIG, LONG, SMALL, WIDE]
    bug = sorted([tbo._issue(WIDE, "P1"), tbo._issue(SMALL, "P1", "bug")], key=src._pick_key)
    assert [i["number"] for i in bug] == [SMALL, WIDE]
    only_wide_bug = sorted([tbo._issue(SMALL, "P1"), tbo._issue(WIDE, "P1", "bug")], key=src._pick_key)
    assert [i["number"] for i in only_wide_bug] == [WIDE, SMALL]


def test_a_comment_edge_never_boosts(tmp_path):
    _, ranks = _ranks(tmp_path, extra_edges=[(FREE, 900, "comment")])
    assert ranks.get(FREE) == (0, 0)


def test_a_cycle_is_finite_and_a_resolved_dependent_is_not_counted(tmp_path):
    base = pg._base(str(tmp_path), gh_mode=True)
    nodes = [t19._node(n) for n in (1, 2, 3)] + [t19._node(4, state="closed")]
    t19._store(base, nodes, [(1, 2), (2, 3), (3, 2), (1, 4)])
    ranks = do.chain_ranks(base, {}, {}, NOW)
    assert ranks.get(1) == (-2, -1)         # 4 is closed; the 2/3 cycle counts as one step
    assert ranks.get(999) == (0, 0)         # not in the map


# ------------------------------------------------------------------ 6. the loop's gate

def _gate_case(tmp_path, mode=None, order=None, source="github", write_map=True):
    disc = {"source": source}
    if order:
        disc["order"] = order
    base = pg._base(str(tmp_path), gh_mode=False, extra={"discovery": dict(disc)})
    if write_map:
        _map(base)                          # the writer refuses while the mode is off, so write first
    if mode:
        cfg_file = pathlib.Path(base) / "config.json"
        cfg = json.loads(cfg_file.read_text())
        cfg["discovery"]["dag"] = {"mode": mode}
        cfg_file.write_text(json.dumps(cfg))
    lp = pg._loop()
    loaded = []
    real = lp._load
    lp._load = lambda name: loaded.append(name) or real(name)
    src = types.SimpleNamespace(dag_chain_ranks="stale")
    lp._dag_order_pick(base, pg._cfg(lp, base), src)
    return src, loaded


@pytest.mark.parametrize("case,kw", [
    ("mode off", {"mode": "off"}),
    ("order created", {"order": "created"}),
    ("local source", {"source": "local-goals"}),
    ("no map file", {"write_map": False}),
])
def test_loop_gate_closed_never_loads_dag_order(tmp_path, case, kw):
    src, loaded = _gate_case(tmp_path, **kw)
    assert src.dag_chain_ranks is None
    assert "dag_order" not in loaded


def test_loop_gate_open_sets_the_ranks_for_the_batch(tmp_path):
    src, loaded = _gate_case(tmp_path, mode="shadow")
    assert "dag_order" in loaded
    assert src.dag_chain_ranks.get(BIG) == (-5, -1)
