"""#1622: the dependency map's local history, append-only and monthly (`dag_history.py`).

`dag_store.save` is the single writer of the stored map, so it records what changed between the map it is
replacing and the one it writes: an edge added or removed, a node resolved or deleted, a chain completed
or superseded. Each record is one line of numbers, an event name and a timestamp in
`.sdlc/state/dag-history/YYYY-MM.jsonl`. No network, no model; fixture numbers are plain numbers.

Each guard was run red once against its own deliberate break (one clause at a time); the `control:` line
in a docstring names the break:
  * omit one record kind from `diff`, each kind on its own          -> that kind's table row red
  * a merge or a split not recorded as superseding                  -> the merge / split tests red
  * a growing one-to-one chain recorded as superseded               -> the growth test red
  * add a delete / truncate / rewrite of a history path             -> the structural test red
  * `read_month` opens another month                                -> the open-count test red
  * drop the mode gate in `append`                                  -> the mode-off test red
  * append even when the diff is empty                              -> the same-save-twice test red
"""
import builtins
import importlib.util
import json
import pathlib
import re
import sys
import tempfile
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name, base=S):
    spec = importlib.util.spec_from_file_location(name, base / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dag_history = _mod("dag_history")
dag_store = _mod("dag_store")
dag = _mod("dag")
doctor = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts")

T = "2026-03-05T10:00:00Z"
OPEN, DONE = "open", "merged"


def _node(n, state=OPEN):
    return {"number": n, "kind": "issue", "state": state, "life": "goal", "prio": None, "in_corpus": True}


def _edge(a, b):
    return {"blocker": a, "blocked": b, "source": "body"}


def _map(nodes, edges):
    return {"header": {"repo": "o/r", "built_at": T, "refreshed_at": T, "last_ok_at": T,
                       "last_error": None, "partial": False},
            "nodes": [_node(n, s) for n, s in nodes], "edges": [_edge(a, b) for a, b in edges]}


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _ev(records, kind):
    return [r for r in records if r["ev"] == kind]


@pytest.fixture
def sdlc(tmp_path):
    d = tmp_path / ".sdlc"
    d.mkdir()
    return str(d)


# ------------------------------------------------------------------ 1. the record table

OLD = _map([(1, OPEN), (2, OPEN), (3, OPEN), (4, OPEN), (9, OPEN)], [(1, 2), (2, 3)])
NEW = _map([(1, OPEN), (2, DONE), (3, OPEN), (4, OPEN), (7, OPEN)], [(1, 2), (3, 4)])
# 2 resolved; 9 is gone; 7 is new; edge (2,3) removed; edge (3,4) added.

TABLE = [
    ("edge-added", [{"ev": "edge-added", "a": 3, "b": 4}]),
    ("edge-removed", [{"ev": "edge-removed", "a": 2, "b": 3}]),
    ("node-resolved", [{"ev": "node-resolved", "n": 2}]),
    ("node-deleted", [{"ev": "node-deleted", "n": 9}]),
]


@pytest.mark.parametrize("kind,expected", TABLE)
def test_diff_yields_the_expected_records_for_each_kind(kind, expected):
    """control: omit this kind from `diff` -> exactly its row goes red (each kind on its own)."""
    got = [{k: v for k, v in r.items() if k != "t"} for r in _ev(dag_history.diff(OLD, NEW, T), kind)]
    assert got == expected


def test_diff_has_no_other_kind_and_stamps_every_record():
    recs = dag_history.diff(OLD, NEW, T)
    kinds = {r["ev"] for r in recs}
    assert kinds <= set(dag_history.EVENTS)
    assert {k for k, _ in TABLE} <= kinds
    assert all(r["t"] == T for r in recs)


def test_first_map_and_identical_maps_yield_nothing():
    assert dag_history.diff(None, NEW, T) == []
    assert dag_history.diff(NEW, NEW, T) == []


# ------------------------------------------------------------------ 2. chain lifecycle

def test_chain_completes_when_all_members_resolve():
    old = _map([(1, DONE), (2, OPEN), (3, DONE)], [(1, 2), (2, 3)])
    new = _map([(1, DONE), (2, DONE), (3, DONE)], [(1, 2), (2, 3)])
    recs = _ev(dag_history.diff(old, new, T), "chain-completed")
    assert [{k: v for k, v in r.items() if k != "t"} for r in recs] == [{"ev": "chain-completed", "chain": 1, "n": 3}]
    assert _ev(dag_history.diff(old, old, T), "chain-completed") == []
    assert _ev(dag_history.diff(new, new, T), "chain-completed") == []     # already complete: once only


def test_a_chain_with_one_open_member_is_not_completed():
    old = _map([(1, OPEN), (2, OPEN), (3, OPEN)], [(1, 2), (2, 3)])
    new = _map([(1, DONE), (2, DONE), (3, OPEN)], [(1, 2), (2, 3)])
    assert _ev(dag_history.diff(old, new, T), "chain-completed") == []


def test_a_merge_supersedes_both_chains_and_names_the_successor():
    old = _map([(i, OPEN) for i in (1, 2, 5, 6)], [(1, 2), (5, 6)])
    new = _map([(i, OPEN) for i in (1, 2, 5, 6)], [(1, 2), (2, 5), (5, 6)])
    got = sorted((r["chain"], r["to"]) for r in _ev(dag_history.diff(old, new, T), "chain-superseded"))
    assert got == [(1, [1]), (5, [1])]


def test_a_split_supersedes_the_chain_and_names_both_successors():
    old = _map([(i, OPEN) for i in (1, 2, 3, 4)], [(1, 2), (2, 3), (3, 4)])
    new = _map([(i, OPEN) for i in (1, 2, 3, 4)], [(1, 2), (3, 4)])
    got = [(r["chain"], r["to"]) for r in _ev(dag_history.diff(old, new, T), "chain-superseded")]
    assert got == [(1, [1, 3])]


def test_a_growing_or_shrinking_one_to_one_chain_is_not_superseded():
    """control: record every changed membership as superseded -> red."""
    old = _map([(i, OPEN) for i in (1, 2, 3)], [(1, 2)])
    grown = _map([(i, OPEN) for i in (1, 2, 3)], [(1, 2), (2, 3)])
    assert _ev(dag_history.diff(old, grown, T), "chain-superseded") == []
    assert _ev(dag_history.diff(grown, old, T), "chain-superseded") == []


# ------------------------------------------------------------------ 3. nothing deletes a history path

BANNED_ATTRS = {"unlink", "remove", "rmtree", "truncate", "write_text", "write_bytes", "replace", "rename",
                "removedirs", "rmdir", "ftruncate"}


def _violations(src):
    import ast
    bad = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        if name in BANNED_ATTRS:
            bad.append(name)
        if name in ("open", "fdopen"):
            mode = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if isinstance(mode, str) and any(c in mode for c in "wx+"):
                bad.append("open(%r)" % mode)
    return bad


def test_the_module_never_deletes_truncates_or_rewrites_a_history_path():
    """control: add `p.unlink()`, `.truncate()`, `.write_text()` or `open(p, "w")` to a copy -> red,
    each one on its own (see the next test)."""
    assert _violations((S / "dag_history.py").read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("extra", ["p.unlink()", "p.write_text('')", "os.truncate(p, 0)", "shutil.rmtree(p)",
                                   "open(p, 'w')", "open(p, mode='r+')", "os.replace(a, p)", "os.remove(p)"])
def test_the_structural_scan_goes_red_on_each_forbidden_shape(extra):
    src = (S / "dag_history.py").read_text(encoding="utf-8") + "\n\ndef _x(p, a):\n    %s\n" % extra
    assert _violations(src), extra


def test_the_scan_allows_append():
    assert _violations("def f(p):\n    open(p, 'a')\n") == []


# ------------------------------------------------------------------ 4. through save: the same save twice

def _history_files(sdlc):
    d = pathlib.Path(sdlc) / "state" / "dag-history"
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def _lines(sdlc):
    return [json.loads(x) for f in _history_files(sdlc) for x in f.read_text().splitlines()]


def test_the_same_save_twice_appends_nothing_the_second_time(sdlc):
    """control: append even when the diff is empty (one line per call) -> red."""
    a = _map([(1, OPEN), (2, OPEN)], [(1, 2)])
    b = _map([(1, OPEN), (2, DONE)], [(1, 2)])
    assert dag_store.save(sdlc, a, config={}) == (True, "ok")
    assert _lines(sdlc) == []                       # the first map records nothing
    assert dag_store.save(sdlc, b, config={}) == (True, "ok")
    first = _history_files(sdlc)[0].read_bytes()
    assert [r["ev"] for r in _lines(sdlc)] == ["node-resolved"]
    b2 = json.loads(json.dumps(b))
    b2["header"]["refreshed_at"] = "2026-03-06T10:00:00Z"     # a later refresh of an unchanged map
    assert dag_store.save(sdlc, b2, config={}) == (True, "ok")
    assert _history_files(sdlc)[0].read_bytes() == first


def test_records_land_in_the_month_of_their_own_timestamp(sdlc):
    a = _map([(1, OPEN)], [])
    b = _map([(1, DONE)], [])
    dag_store.save(sdlc, a, config={})
    dag_history.record_save(sdlc, {}, a, b, 1772359200)             # 2026-03-01T10:00:00Z
    dag_history.record_save(sdlc, {}, b, _map([], []), 1775037600)  # 2026-04-01T10:00:00Z
    assert [f.name for f in _history_files(sdlc)] == ["2026-03.jsonl", "2026-04.jsonl"]
    assert _iso(1772359200) == "2026-03-01T10:00:00Z"


# ------------------------------------------------------------------ 5. a synthetic year

def test_a_synthetic_year_at_fifty_changes_a_day_stays_within_the_stated_size(sdlc):
    base = 1767225600                        # 2026-01-01T00:00:00Z
    wrote = 0
    for day in range(365):
        t = base + day * 86400
        recs = [{"t": _iso(t + i), "ev": "edge-added", "a": 10000 + i, "b": 20000 + day} for i in range(50)]
        dag_history.append(sdlc, {}, recs)
        wrote += len(recs)
    total = sum(f.stat().st_size for f in _history_files(sdlc))
    assert len(_history_files(sdlc)) == 12
    assert wrote == 50 * 365
    assert total <= dag_history.YEAR_BOUND_BYTES      # the docstring's 50 * 365 * 100 = 1.8 MB, with margin
    assert max(f.stat().st_size for f in _history_files(sdlc)) < dag_history.WARN_BYTES


def test_health_and_the_doctor_row_warn_above_one_megabyte_in_one_month(sdlc):
    cfg = {}
    assert doctor._dag_history_row(cfg, sdlc) is None                       # no history yet: absent
    dag_history.append(sdlc, cfg, [{"t": "2026-03-05T10:00:00Z", "ev": "edge-added", "a": 1, "b": 2}])
    now = 1772712000                                                         # 2026-03-05T12:00:00Z
    h = dag_history.health(sdlc, now, cfg)
    assert h["month"] == "2026-03" and h["warn"] is False
    ok_row = doctor._dag_history_row(cfg, sdlc, now=now)
    assert ok_row["ok"] is True and ok_row["name"].startswith("dag history: ") and "2026-03" in ok_row["name"]
    p = pathlib.Path(sdlc) / "state" / "dag-history" / "2026-03.jsonl"
    with open(p, "a") as fh:
        fh.write(("x" * 99 + "\n") * 10_500)                               # past 1 MB
    assert dag_history.health(sdlc, now, cfg)["warn"] is True
    bad = doctor._dag_history_row(cfg, sdlc, now=now)
    assert bad["ok"] is False and bad["fix"]
    assert doctor._dag_history_row({"discovery": {"dag": {"mode": "off"}}}, sdlc, now=now) is None


def test_the_warning_clause_is_strictly_above_the_limit(sdlc):
    now = 1772712000
    p = pathlib.Path(sdlc) / "state" / "dag-history"
    p.mkdir(parents=True)
    (p / "2026-03.jsonl").write_text("x" * dag_history.WARN_BYTES)
    assert dag_history.health(sdlc, now, {})["warn"] is False
    (p / "2026-03.jsonl").write_text("x" * (dag_history.WARN_BYTES + 1))
    assert dag_history.health(sdlc, now, {})["warn"] is True


def test_diff_of_thirty_thousand_nodes_is_linear_and_measured(sdlc):
    n = 30000
    old = _map([(i, OPEN) for i in range(1, n + 1)], [(i, i + 1) for i in range(1, n)])
    new = _map([(i, DONE if i % 100 == 0 else OPEN) for i in range(1, n + 1)], [(i, i + 1) for i in range(1, n)])
    t0 = time.perf_counter()
    recs = dag_history.diff(old, new, T)
    took = time.perf_counter() - t0
    print("diff of %d nodes: %.2fs" % (n, took))
    assert len(_ev(recs, "node-resolved")) == n // 100
    assert took < 30


# ------------------------------------------------------------------ 6. numbers, event names, timestamps

NUM_KEYS = {"a", "b", "n", "chain"}


def _all_kinds_records():
    recs = list(dag_history.diff(OLD, NEW, T))
    recs += dag_history.diff(_map([(1, OPEN), (2, OPEN)], [(1, 2)]), _map([(1, DONE), (2, DONE)], [(1, 2)]), T)
    recs += dag_history.diff(_map([(i, OPEN) for i in (1, 2, 5, 6)], [(1, 2), (5, 6)]),
                             _map([(i, OPEN) for i in (1, 2, 5, 6)], [(1, 2), (2, 5), (5, 6)]), T)
    return recs


def test_every_record_holds_only_numbers_event_names_and_timestamps():
    recs = _all_kinds_records()
    assert {r["ev"] for r in recs} == set(dag_history.EVENTS)       # every kind is in the scan
    ts = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
    for r in recs:
        for k, v in r.items():
            if k == "ev":
                assert v in dag_history.EVENTS
            elif k == "t":
                assert ts.match(v)
            elif k == "to":
                assert isinstance(v, list) and all(type(x) is int for x in v)
            else:
                assert k in NUM_KEYS and type(v) is int, (k, v)


def test_a_title_in_a_node_never_reaches_a_record(sdlc):
    a = _map([(1, OPEN)], [])
    b = _map([(1, DONE)], [])
    b["nodes"][0]["title"] = "SECRET-TITLE"
    dag_store.save(sdlc, a, config={})
    dag_store.save(sdlc, b, config={})
    assert "SECRET" not in _history_files(sdlc)[0].read_text()


# ------------------------------------------------------------------ 7. the verb opens one month

def test_history_verb_prints_that_month_only_and_opens_no_other_file(sdlc, capsys, monkeypatch):
    """control: make `read_month` also open the neighbouring month -> red."""
    dag_history.append(sdlc, {}, [{"t": "2026-02-03T10:00:00Z", "ev": "node-resolved", "n": 11}])
    dag_history.append(sdlc, {}, [{"t": "2026-03-03T10:00:00Z", "ev": "node-resolved", "n": 22}])
    dag_history.append(sdlc, {}, [{"t": "2026-04-03T10:00:00Z", "ev": "node-resolved", "n": 33}])
    opened = []
    real = builtins.open

    def counting(file, *a, **k):
        if "dag-history" in str(file):
            opened.append(pathlib.Path(str(file)).name)
        return real(file, *a, **k)

    monkeypatch.setattr(builtins, "open", counting)
    code = dag.main(["history", "--month", "2026-03", "--sdlc", sdlc])
    monkeypatch.setattr(builtins, "open", real)
    out = capsys.readouterr().out
    assert code == 0
    assert '"n":22' in out.replace(" ", "") and "11" not in out and "33" not in out
    assert opened == ["2026-03.jsonl"]


@pytest.mark.parametrize("bad", ["2026-3", "../2026-03", "2026-13", "2026-03.jsonl", "", "2026-03/x", "latest"])
def test_read_month_refuses_anything_but_a_strict_month(sdlc, bad):
    with pytest.raises(ValueError):
        dag_history.read_month(sdlc, bad)


def test_a_missing_month_reads_empty(sdlc):
    assert dag_history.read_month(sdlc, "2026-03") == []


def test_history_verb_refuses_a_bad_month_with_exit_two(sdlc, capsys):
    assert dag.main(["history", "--month", "../x", "--sdlc", sdlc]) == 2
    assert "month" in capsys.readouterr().out


# ------------------------------------------------------------------ 8. mode off

OFF = {"discovery": {"dag": {"mode": "off"}}}


def test_mode_off_creates_no_history_and_never_loads_the_module_on_save(sdlc, monkeypatch):
    """control: drop the mode gate in `append` -> the direct-append assertion goes red."""
    dag_history.append(sdlc, OFF, [{"t": T, "ev": "node-resolved", "n": 1}])
    assert not (pathlib.Path(sdlc) / "state").exists()
    loaded = []
    real = dag_store._load
    monkeypatch.setattr(dag_store, "_load", lambda name: (loaded.append(name), real(name))[1])
    assert dag_store.save(sdlc, _map([(1, OPEN)], []), config=OFF) == (False, "off")
    assert loaded == [] and not (pathlib.Path(sdlc) / "state").exists()


def test_mode_on_save_does_load_the_module(sdlc, monkeypatch):
    loaded = []
    real = dag_store._load
    monkeypatch.setattr(dag_store, "_load", lambda name: (loaded.append(name), real(name))[1])
    assert dag_store.save(sdlc, _map([(1, OPEN)], []), config={}) == (True, "ok")
    assert dag_store.save(sdlc, _map([(1, DONE)], []), config={}) == (True, "ok")
    assert "dag_history" in loaded


def test_history_verb_with_mode_off_prints_off_and_touches_nothing(sdlc, capsys):
    cfgdir = pathlib.Path(sdlc)
    (cfgdir / "config.json").write_text(json.dumps(OFF))
    assert dag.main(["history", "--month", "2026-03", "--sdlc", sdlc]) == 0
    assert capsys.readouterr().out.strip() == "dag: off"
    assert not (cfgdir / "state").exists()


# ------------------------------------------------------------------ 9. a history failure never fails the save

def test_a_history_failure_never_fails_the_save(sdlc, capsys, monkeypatch):
    dag_store.save(sdlc, _map([(1, OPEN)], []), config={})
    hist = pathlib.Path(sdlc) / "state" / "dag-history"
    hist.write_text("a file where the directory should be")       # makedirs / open fail
    assert dag_store.save(sdlc, _map([(1, DONE)], []), config={}) == (True, "ok")
    assert dag_store.load(sdlc, {})[0]["nodes"][0]["state"] == DONE
    assert "dag history" in capsys.readouterr().err


def test_a_diff_that_raises_never_fails_the_save(sdlc, monkeypatch):
    dag_store.save(sdlc, _map([(1, OPEN)], []), config={})
    monkeypatch.setattr(dag_store, "_load", lambda name: (_ for _ in ()).throw(RuntimeError("boom")))
    assert dag_store.save(sdlc, _map([(1, DONE)], []), config={}) == (True, "ok")
    assert dag_store.load(sdlc, {})[0]['nodes'][0]['state'] == DONE


def test_a_subprocess_run_carries_the_resource_mode(sdlc):
    import os
    import subprocess
    env = resource_mode.carry(dict(os.environ))
    r = subprocess.run([sys.executable, str(S / "dag.py"), "history", "--month", "2026-03", "--sdlc", sdlc],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# ----------------------------------------------------------------- a partial map proves no deletion (review)

def _partial(m):
    m["header"]["partial"] = True
    return m


def test_a_partial_new_map_records_no_deletion_removal_or_chain_change():
    """A map cut short by a read cap lacks nodes that still exist; recording them as deleted would put a false,
    never-erasable record in an append-only file. Control: drop the partial guard and this goes red."""
    old = _map([(1, OPEN), (2, OPEN), (3, OPEN)], [(1, 2), (2, 3)])
    new = _partial(_map([(1, OPEN)], []))
    recs = dag_history.diff(old, new, T)
    assert not _ev(recs, "node-deleted") and not _ev(recs, "edge-removed")
    assert not _ev(recs, "chain-superseded") and not _ev(recs, "chain-completed")


def test_a_partial_new_map_still_records_additions_and_resolutions():
    old = _map([(1, OPEN), (2, OPEN)], [(1, 2)])
    new = _partial(_map([(1, DONE), (2, OPEN), (3, OPEN)], [(1, 2), (2, 3)]))
    recs = dag_history.diff(old, new, T)
    assert _ev(recs, "edge-added") == [{"t": T, "ev": "edge-added", "a": 2, "b": 3}]
    assert _ev(recs, "node-resolved") == [{"t": T, "ev": "node-resolved", "n": 1}]


def test_a_complete_map_after_a_partial_one_records_nothing_false():
    partial = _partial(_map([(1, OPEN)], []))
    full = _map([(1, OPEN), (2, OPEN)], [(1, 2)])
    recs = dag_history.diff(partial, full, T)
    assert not _ev(recs, "node-deleted") and not _ev(recs, "edge-removed")
    assert not _ev(recs, "edge-added")        # the edge the cap had dropped is not new; control: drop the old_short guard


def test_a_partial_old_map_cannot_make_a_false_merge_record():
    """Control: drop `old_short` from the chain guard and the merge below is recorded as superseding."""
    old = _partial(_map([(1, OPEN), (2, OPEN), (5, OPEN), (6, OPEN)], [(1, 2), (5, 6)]))
    new = _map([(n, OPEN) for n in (1, 2, 3, 5, 6)], [(1, 2), (2, 3), (3, 5), (5, 6)])
    assert dag_history.diff(old, new, T) != []                                  # additions are recorded
    assert not _ev(dag_history.diff(old, new, T), "chain-superseded")
    assert _ev(dag_history.diff(_map([(n, OPEN) for n in (1, 2, 5, 6)], [(1, 2), (5, 6)]), new, T), "chain-superseded")
