"""#1613: the chain query (`dag_chain.py`, `dag.py chain|chains|stuck`), schema `dag-chain/1`.

Every guard below was run red once against its own deliberate break; the `control:` line in a docstring
names the break. The controls: drop the priority/number tie-break from the order; skip the `--verify`
re-read; drop the cycle exclusion; make the walk go behind a resolved node. No test reaches the network:
the live read is the injected `reader`."""
import importlib.util
import json
import os
import pathlib
import random
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dc = _mod("dag_chain")
ds = _mod("dag_store")
dag = _mod("dag")

NOW = 1_800_000_000.0


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _node(number, prio=None, state="open", life="goal", kind="issue", unit=None):
    n = {"number": number, "kind": kind, "state": state, "life": life, "prio": prio, "in_corpus": True}
    if unit:
        n["unit"] = unit
    return n


def _store(tmp_path, nodes, edges, mode=None, **header):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True, exist_ok=True)
    h = {"repo": "owner/repo", "built_at": _iso(NOW - 60), "refreshed_at": _iso(NOW - 60),
         "last_ok_at": _iso(NOW - 60), "last_error": None, "partial": False}
    h.update(header)
    ok, why = ds.save(sdlc, {"header": h, "nodes": nodes,
                             "edges": [{"blocker": a, "blocked": b, "source": "body"} for a, b in edges]})
    assert ok, why
    if mode:                                    # after the save: `off` refuses the write
        (sdlc / "config.json").write_text(json.dumps({"discovery": {"dag": {"mode": mode}}}))
    return sdlc


def _chain(sdlc, goal, **kw):
    return dc.chain(sdlc, goal, now=NOW, **kw)


def _nums(c):
    return [m["number"] for m in c["members"]]


# the golden fixture: a parked root (5) behind a diamond (8, 9 -> 7), an unarmed member (6), a pull
# request member (11), a resolved root behind the parked one (4), the goal 10
GOLDEN_NODES = [
    _node(4, state="closed", life="none"),
    _node(5, "P1", life="parked", unit="alpha"),
    _node(6, None, life="needs-label", unit="beta"),
    _node(7, "P2"), _node(8, "P2"), _node(9, "P1"),
    _node(10, "P1", unit="alpha"),
    _node(11, "P3", kind="pr", life="in-progress"),
]
GOLDEN_EDGES = [(5, 8), (5, 9), (6, 9), (8, 7), (9, 7), (7, 10), (11, 10), (4, 5)]


def _m(number, role, kind, life, prio, level, waits, unit, state="open"):
    return {"number": number, "role": role, "kind": kind, "state": state, "life": life, "prio": prio,
            "level": level, "waits_on": waits, "unit": unit}


def test_golden_schema(tmp_path):
    """The whole `dag-chain/1` object, field for field. control: rename any key; this goes red."""
    got = _chain(_store(tmp_path, GOLDEN_NODES, GOLDEN_EDGES), 10)
    want = {
        "schema": "dag-chain/1", "goal": 10, "mode": "shadow", "source": "map",
        "map_built_at": int(NOW - 60), "map_age_seconds": 60,
        "closure_id": got["closure_id"], "complete": True,
        "members": [
            _m(5, "blocker", "issue", "parked", 1, 0, [], "alpha"),
            _m(8, "blocker", "issue", "goal", 2, 1, [5], None),
            _m(11, "blocker", "pr", "in-progress", 3, 0, [], None),
            _m(6, "blocker", "issue", "followup", 5, 0, [], "beta"),
            _m(9, "blocker", "issue", "goal", 1, 1, [5, 6], None),
            _m(7, "blocker", "issue", "goal", 2, 2, [8, 9], None),
            _m(10, "goal", "issue", "goal", 1, 3, [7, 11], "alpha"),
        ],
        "waves": [[5, 11, 6], [9, 8], [7], [10]],
        "decisions": [{"number": 5, "why": "parked", "hint": "/sigma-unpark 5"},
                      {"number": 6, "why": "unarmed", "hint": "/sigma-promote 6"}],
        "cycles": [],
        "counts": {"open": 7, "parked": 1, "unarmed": 1, "unknown": 0, "resolved_skipped": 1},
    }
    assert got == want
    assert len(got["closure_id"]) == 16


def test_additive_only_rule_is_in_the_module_docstring():
    doc = dc.__doc__
    assert "ADDITIVE ONLY" in doc and "never renamed, removed or re-typed" in doc


def test_order_ties_break_by_priority_rank_then_number(tmp_path):
    """Two blockers ready together: the lower number has the worse priority. control: order by
    number alone (drop the rank); this goes red."""
    sdlc = _store(tmp_path, [_node(1, "P4"), _node(2, "P0"), _node(3, "P1"), _node(4, "P0")],
                  [(1, 3), (2, 3), (4, 3)])
    assert _nums(_chain(sdlc, 3)) == [2, 4, 1, 3]


def test_shared_blocker_once_and_goal_last(tmp_path):
    got = _chain(_store(tmp_path, GOLDEN_NODES, GOLDEN_EDGES), 10)
    assert _nums(got).count(5) == 1 and _nums(got)[-1] == 10
    pos = {n: i for i, n in enumerate(_nums(got))}
    for m in got["members"]:
        assert all(pos[w] < pos[m["number"]] for w in m["waits_on"])


def test_membership_ignores_priority_unit_and_other_labels(tmp_path):
    """A fixture spanning two priorities and two units returns all of them."""
    got = _chain(_store(tmp_path, GOLDEN_NODES, GOLDEN_EDGES), 10)
    assert {m["prio"] for m in got["members"]} >= {1, 2, 3, 5}
    assert {m["unit"] for m in got["members"]} >= {"alpha", "beta"}
    assert set(_nums(got)) == {5, 6, 7, 8, 9, 10, 11}


@pytest.mark.parametrize("node,why,hint", [
    (_node(1, life="parked"), "parked", "/sigma-unpark 1"),
    (_node(1, life="needs-unit"), "unarmed", "/sigma-promote 1"),
    (_node(1, life="none"), "unmanaged", "/sigma-promote 1"),
    (_node(1, state="unknown", life="none"), "unknown", "dag.py refresh --force"),
])
def test_decision_why(tmp_path, node, why, hint):
    got = _chain(_store(tmp_path, [node, _node(2)], [(1, 2)]), 2)
    assert [d["why"] for d in got["decisions"]] == [why]
    assert got["decisions"][0]["number"] == 1 and hint in got["decisions"][0]["hint"]


def test_decision_cross_repo(tmp_path):
    got = _chain(_store(tmp_path, [_node(1), _node(2)], [(1, 2)]), 2, foreign={1})
    assert [(d["number"], d["why"]) for d in got["decisions"]] == [(1, "cross-repo")]


def test_decision_cycle_and_incomplete(tmp_path):
    """A cycle is listed under cycles and decisions, not in the order. control: keep cycle members in
    the order; this goes red."""
    sdlc = _store(tmp_path, [_node(1), _node(2), _node(3), _node(4)], [(1, 2), (2, 1), (2, 3), (3, 4)])
    got = _chain(sdlc, 4)
    assert got["cycles"] == [[1, 2]] and got["complete"] is False
    assert sorted(d["number"] for d in got["decisions"] if d["why"] == "cycle") == [1, 2]
    assert 1 not in _nums(got) and 2 not in _nums(got) and _nums(got) == [3, 4]


def _reach(edges, a):
    out, stack = set(), [a]
    while stack:
        cur = stack.pop()
        for x, y in edges:
            if x == cur and y not in out:
                out.add(y)
                stack.append(y)
    return out


def test_waves_have_no_path_inside_a_wave(tmp_path):
    """Property over seeded random acyclic graphs. control: put every member in one wave; red."""
    rnd = random.Random(1613)
    for trial in range(40):
        n = rnd.randint(3, 14)
        edges = [(a, b) for a in range(1, n + 1) for b in range(a + 1, n + 1) if rnd.random() < 0.25]
        nodes = [_node(i, rnd.choice(["P0", "P1", "P2", None])) for i in range(1, n + 1)]
        sub = tmp_path / ("t%d" % trial)
        sub.mkdir()
        got = _chain(_store(sub, nodes, edges), n)
        in_chain = set(_nums(got))
        for wave in got["waves"]:
            for a in wave:
                assert not (_reach(edges, a) & set(wave))
        flat = [x for w in got["waves"] for x in w]
        assert sorted(flat) == sorted(in_chain)
        for m in got["members"]:
            assert m["level"] == (1 + max(got_l for got_l in
                                          [x["level"] for x in got["members"] if x["number"] in m["waits_on"]])
                                  if m["waits_on"] else 0)


def test_resolved_absent_by_default_and_closure_stops_there(tmp_path):
    """4 is closed and itself blocked by open 3. control: walk behind a resolved node; red."""
    nodes = [_node(3), _node(4, state="closed"), _node(5, state="merged", kind="pr"), _node(6)]
    sdlc = _store(tmp_path, nodes, [(3, 4), (4, 6), (5, 6)])
    got = _chain(sdlc, 6)
    assert _nums(got) == [6] and got["counts"]["resolved_skipped"] == 2
    inc = _chain(sdlc, 6, include_resolved=True)
    assert _nums(inc) == [4, 5, 6] and 3 not in _nums(inc)
    assert [m["state"] for m in inc["members"]] == ["closed", "merged", "open"]
    assert inc["waves"] == [[6]] and inc["counts"]["resolved_skipped"] == 0


def _id(tmp_path, nodes, edges, goal=3, **kw):
    sub = tmp_path / ("c%d" % len(list(tmp_path.iterdir())))
    sub.mkdir()
    return _chain(_store(sub, nodes, edges), goal, **kw)["closure_id"]


def test_closure_id_changes_on_add_resolve_life_and_is_otherwise_stable(tmp_path):
    base_n = [_node(1), _node(2), _node(3)]
    base_e = [(1, 3), (2, 3)]
    base = _id(tmp_path, base_n, base_e)
    assert _id(tmp_path, base_n, base_e) == base                                     # stable
    assert _id(tmp_path, base_n, base_e, include_resolved=True) == base              # flag-independent
    assert _id(tmp_path, [_node(1, "P0"), _node(2, unit="u"), _node(3)], base_e) == base  # prio/unit
    assert _id(tmp_path, base_n + [_node(4)], base_e + [(4, 3)]) != base             # added
    assert _id(tmp_path, [_node(1, state="closed"), _node(2), _node(3)], base_e) != base  # resolved
    assert _id(tmp_path, [_node(1, life="parked"), _node(2), _node(3)], base_e) != base   # life


class Reader:
    def __init__(self, table=None):
        self.table, self.calls = table or {}, []

    def __call__(self, number):
        self.calls.append(number)
        return {"state": self.table.get(number, "open"), "kind": "issue"}


def test_verify_rereads_every_open_member_and_applies_state(tmp_path):
    """control: skip the re-read (no reader call); the call count and the dropped member go red."""
    sdlc = _store(tmp_path, GOLDEN_NODES, GOLDEN_EDGES)
    rd = Reader({5: "closed"})
    got = _chain(sdlc, 10, verify=True, reader=rd)
    assert sorted(rd.calls) == [5, 6, 7, 8, 9, 10, 11]
    assert got["source"] == "map+verified" and 5 not in _nums(got)
    assert got["counts"]["resolved_skipped"] == 1 and got["verify"] == {"read": 7, "failed": [], "capped": False}
    assert got["complete"] is True
    assert _chain(sdlc, 10)["source"] == "map"


def test_verify_cap_is_fifty_and_says_so(tmp_path):
    n = 60
    nodes = [_node(i) for i in range(1, n + 1)]
    sdlc = _store(tmp_path, nodes, [(i, i + 1) for i in range(1, n)])
    rd = Reader()
    got = _chain(sdlc, n, verify=True, reader=rd)
    assert len(rd.calls) == 50 and got["verify"]["capped"] is True and got["complete"] is False


def test_verify_unreadable_member_is_incomplete_not_fatal(tmp_path):
    def boom(number):
        raise RuntimeError("down")
    got = _chain(_store(tmp_path, [_node(1), _node(2)], [(1, 2)]), 2, verify=True, reader=boom)
    assert got["complete"] is False and got["verify"]["failed"] == [1, 2]


def test_default_reader_goes_through_the_rest_helper(monkeypatch):
    seen = []

    class Fake:
        gh_api = type("G", (), {"_default_run": staticmethod(lambda a: "")})
        _gh = staticmethod(lambda cfg: {"repo": "owner/repo"})

        @staticmethod
        def _outside(run, repo, number, now):
            seen.append((run, repo, number))
            return {"state": "closed", "kind": "issue"}, True

    monkeypatch.setattr(dc, "_load", lambda name: Fake)
    assert dc._default_reader({})(9) == {"state": "closed", "kind": "issue"}
    assert seen[0][1:] == ("owner/repo", 9)


# ------------------------------------------------------------------ exit codes and read-only

def _run(argv, capsys):
    code = dag.main(argv)
    return code, capsys.readouterr().out


def test_exit_codes(tmp_path, capsys):
    sdlc = _store(tmp_path, GOLDEN_NODES, GOLDEN_EDGES)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(dc.time, "time", lambda: NOW)
    try:
        code, out = _run(["chain", "10", "--json", "--sdlc", str(sdlc)], capsys)
        assert code == 0 and json.loads(out)["schema"] == "dag-chain/1"
        code, out = _run(["chain", "99", "--sdlc", str(sdlc)], capsys)
        assert code == 2 and "99" in out
        empty = tmp_path / "e" / ".sdlc"
        empty.mkdir(parents=True)
        code, out = _run(["chain", "10", "--sdlc", str(empty)], capsys)
        assert code == 3 and "missing" in out
        stale = _store(tmp_path / "s", GOLDEN_NODES, GOLDEN_EDGES, built_at=_iso(NOW - 99999),
                       refreshed_at=_iso(NOW - 99999), last_ok_at=_iso(NOW - 99999))
        code, out = _run(["chain", "10", "--sdlc", str(stale)], capsys)
        assert code == 3 and "stale" in out
        off = _store(tmp_path / "o", GOLDEN_NODES, GOLDEN_EDGES, mode="off")
        code, out = _run(["chain", "10", "--sdlc", str(off)], capsys)
        assert code == 4 and out.strip() == "dag: off"
    finally:
        monkey.undo()


def _tree(base):
    return sorted((str(p.relative_to(base)), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in pathlib.Path(base).rglob("*"))


def test_read_only_writes_nothing(tmp_path, monkeypatch):
    sdlc = _store(tmp_path, GOLDEN_NODES, GOLDEN_EDGES)
    before = _tree(tmp_path)
    monkeypatch.setattr(ds.state, "atomic_write_text", lambda *a, **k: pytest.fail("write"))
    _chain(sdlc, 10, verify=True, reader=Reader())
    dc.chains(sdlc, now=NOW)
    dc.stuck(sdlc, now=NOW)
    assert _tree(tmp_path) == before


def test_mode_off_reads_and_writes_nothing(tmp_path, monkeypatch):
    """A counting stub on every I/O seam. control: ignore `off`; this goes red."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text(json.dumps({"discovery": {"dag": {"mode": "off"}}}))
    hits = []
    monkeypatch.setattr(dc.dag_store, "_read_text", lambda p: hits.append(p) or "")
    monkeypatch.setattr(dc.dag_store, "load", lambda *a, **k: hits.append("load") or (None, "off"))
    monkeypatch.setattr(dc.dag_store.state, "atomic_write_text", lambda *a, **k: hits.append("write"))
    for call in (lambda: dc.chain(sdlc, 1, now=NOW), lambda: dc.chains(sdlc, now=NOW),
                 lambda: dc.stuck(sdlc, now=NOW)):
        with pytest.raises(dc.ChainError) as e:
            call()
        assert e.value.code == 4 and str(e.value) == "dag: off"
    assert hits == [] and not (sdlc / "state").exists()


def test_cli_subprocess_gesture(tmp_path):
    sdlc = _store(tmp_path, GOLDEN_NODES, GOLDEN_EDGES, built_at=_iso(time.time() - 60),
                  refreshed_at=_iso(time.time() - 60), last_ok_at=_iso(time.time() - 60))
    env = resource_mode.carry(dict(os.environ))
    r = subprocess.run([sys.executable, str(S / "dag.py"), "chain", "10", "--json", "--sdlc", str(sdlc)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["goal"] == 10
    r = subprocess.run([sys.executable, str(S / "dag.py"), "chain", "99", "--sdlc", str(sdlc)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2


# ------------------------------------------------------------------ chains and stuck

def test_chains_ranks_and_skips_singletons(tmp_path):
    nodes = [_node(i) for i in range(1, 9)]
    edges = [(1, 2), (2, 3), (3, 4), (5, 6), (5, 7), (5, 8)]   # a chain of 4 (deep), a star of 4 (wide)
    sdlc = _store(tmp_path, nodes + [_node(9)], edges)
    big = dc.chains(sdlc, "biggest", now=NOW)
    long_ = dc.chains(sdlc, "longest", now=NOW)
    assert [c["members"] for c in big] == [[1, 2, 3, 4], [5, 6, 7, 8]]      # equal size: deeper first
    assert long_[0]["members"] == [1, 2, 3, 4] and long_[0]["depth"] == 3
    assert all(9 not in c["members"] for c in big)
    wide = dc.chains(_store(tmp_path / "w", nodes + [_node(10), _node(11)], edges + [(5, 10), (5, 11)]), "biggest", now=NOW)
    assert wide[0]["members"] == [5, 6, 7, 8, 10, 11] and wide[0]["ends"] == [6, 7, 8, 10, 11]


def test_stuck_names_the_decision_root_most_freeing_first(tmp_path):
    nodes = [_node(1, life="parked"), _node(2), _node(3), _node(4, life="needs-label"), _node(5)]
    sdlc = _store(tmp_path, nodes, [(1, 2), (2, 3), (4, 5)])
    got = dc.stuck(sdlc, now=NOW)
    assert [(s["root"], s["why"], s["waiting"]) for s in got] == [(1, "parked", [2, 3]), (4, "unarmed", [5])]


def test_stuck_text_line(tmp_path, capsys):
    sdlc = _store(tmp_path, [_node(1, life="parked"), _node(2), _node(3)], [(1, 2), (2, 3)], built_at=_iso(time.time() - 60),
                  refreshed_at=_iso(time.time() - 60), last_ok_at=_iso(time.time() - 60))
    code, out = _run(["stuck", "--sdlc", str(sdlc)], capsys)
    assert code == 0 and out.strip() == "#2, #3 wait on #1's parked; /sigma-unpark 1"
