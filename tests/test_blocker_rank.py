"""#1406: the shared blocker-priority rule, `discovery.blocker_required_ranks`.

A blocker's priority must never be below what it blocks. Raise only, transitive, a cycle reported
once, an unknown priority is the lowest. Pure: the inputs are never mutated."""
import copy
import importlib.util
import pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _disc():
    spec = importlib.util.spec_from_file_location("discovery", S / "discovery.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


D = _disc()
P0, P1, P2, P3 = 0, 1, 2, 3
LOW = D.UNPRIORITISED


def test_a_p0_blocked_by_a_p3_raises_the_p3_to_p0():
    required, cycles = D.blocker_required_ranks([("b", "a")], {"a": P0, "b": P3})
    assert required == {"b": (P0, "a")}
    assert cycles == []


def test_a_p3_blocked_by_p0_p1_p2_changes_nothing():
    for blocker_rank in (P0, P1, P2):
        required, _ = D.blocker_required_ranks([("b", "a")], {"a": P3, "b": blocker_rank})
        assert required == {}


def test_equal_priority_stays():
    required, _ = D.blocker_required_ranks([("b", "a")], {"a": P1, "b": P1})
    assert required == {}


def test_a_chain_raises_the_root_to_the_deepest_urgent_rank():
    edges = [("b", "a"), ("c", "b")]            # c blocks b blocks a
    required, _ = D.blocker_required_ranks(edges, {"a": P0, "b": P2, "c": P3})
    assert required == {"b": (P0, "a"), "c": (P0, "b")}


def test_the_most_urgent_direct_dependent_is_the_cause():
    edges = [("x", "a"), ("x", "b")]
    required, _ = D.blocker_required_ranks(edges, {"a": P2, "b": P1, "x": P3})
    assert required == {"x": (P1, "b")}


def test_an_unknown_priority_is_the_lowest():
    required, _ = D.blocker_required_ranks([("b", "a")], {"a": P3})     # b has no entry
    assert required == {"b": (P3, "a")}
    required, _ = D.blocker_required_ranks([("b", "a")], {"a": None, "b": P3})
    assert required == {}                    # an unknown dependent asks for nothing
    required, _ = D.blocker_required_ranks([("b", "a")], {"a": P3, "b": LOW})
    assert required == {"b": (P3, "a")}


def test_a_cycle_is_reported_once_and_terminates():
    edges = [("b", "a"), ("c", "b"), ("a", "c")]
    required, cycles = D.blocker_required_ranks(edges, {"a": P0, "b": P3, "c": P3})
    assert cycles == ["a", "b", "c"]
    assert isinstance(required, dict)


def test_a_self_edge_and_a_duplicate_edge_are_ignored():
    required, cycles = D.blocker_required_ranks([("a", "a"), ("b", "a"), ("b", "a")], {"a": P0, "b": P2})
    assert required == {"b": (P0, "a")}
    assert cycles == []


def test_inputs_are_not_mutated():
    edges = [("b", "a"), ("c", "b")]
    ranks = {"a": P0, "b": P2, "c": P3}
    before = (copy.deepcopy(edges), copy.deepcopy(ranks))
    D.blocker_required_ranks(iter(edges), ranks)
    assert (edges, ranks) == before


def test_the_default_mode_is_the_full_rule_and_smart_is_still_accepted():
    assert D.DEFAULT_BLOCKER_PROMOTION_MODE == "always"
    assert "smart" in D.BLOCKER_PROMOTION_MODES and "off" in D.BLOCKER_PROMOTION_MODES
