"""#1958: every map history record carries an `id`, so a change two writers both saw is folded once.

Offline: fake issue numbers, fake writers (plain files under a fake ledger root), a real local git ledger for the
snapshot test. No network, no model. MEASURED: these tests only. Controls, each clause broken once on its own:
  * `fold` stops dropping a repeated id                    -> the two-writers test red
  * the id ignores the source `updated_at` (`u`)           -> the real-flip test red
  * the id includes the writer's own stamp `t` with a `u` -> the two-writers test red
  * a record without `u` hashes without its own `t`        -> the old-record re-add test red
  * `fold` ignores `after`, or drops it when unknown       -> the after-cut tests red
  * the snapshot stops recording `history_through`         -> the snapshot test red
Gesture: `python -m pytest tests/test_dag_change_id_1958.py -q`."""
import importlib.util
import json
import pathlib

import pytest

import test_dag_snapshot_1956 as SNAP   # fixtures: origin, _booted, _map (same ledger rig)
from test_dag_snapshot_1956 import origin  # noqa: F401  (a fixture)

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


H, R = _mod("dag_history"), _mod("dag_render")
U1, U2, U3 = "2026-03-01T00:00:00Z", "2026-03-02T00:00:00Z", "2026-03-03T00:00:00Z"
T1, T2, T3 = "2026-03-05T10:00:00Z", "2026-03-05T11:00:00Z", "2026-03-06T10:00:00Z"


def writer(root, name, records, month="2026-03"):
    d = pathlib.Path(root) / "dag" / month
    d.mkdir(parents=True, exist_ok=True)
    (d / (name + ".jsonl")).write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def rec(ev, t, u=None, **nums):
    r = {"t": t, "ev": ev, **nums}
    if u:
        r["u"] = u
    return H.with_id(r)


def test_the_same_change_written_by_two_writers_is_counted_once(tmp_path):
    a = rec("chain-completed", T1, U1, chain=4, n=3)
    b = rec("chain-completed", T2, U1, chain=4, n=3)         # the other machine saw it an hour later: same change
    assert a["id"] == b["id"]
    writer(tmp_path, "dana-box1.1", [a])
    writer(tmp_path, "lee-box2.2", [b])
    got = H.read_all(tmp_path)
    assert len(got) == 1
    text = R.render([a, b])
    assert "completed 1" in text and text == R.render([a])      # DAG.md folds duplicates by construction: edges are a map, completions a set
    every = [rec("edge-added", T1, U1, a=1, b=2), rec("node-resolved", T1, U1, n=2)]
    assert R.render(every + [dict(every[0], t=T3), dict(every[1], t=T3), a, b]) == R.render(every + [a])


def test_an_old_record_without_an_id_folds_to_the_same_id(tmp_path):
    old = {"t": T1, "ev": "edge-added", "a": 1, "b": 2}
    twin = dict(old)
    writer(tmp_path, "old-box.1", [old, twin])
    got = H.read_all(tmp_path)
    assert len(got) == 1 and got[0]["id"] == H.change_id(old)
    assert H.change_id(old) == H.change_id(twin) == H.with_id(old)["id"]


def test_a_readded_edge_without_a_source_stamp_is_not_folded_away(tmp_path):
    """Old records have no `u`: the same edge added, removed, added later must keep the later add."""
    recs = [{"t": T1, "ev": "edge-added", "a": 1, "b": 2}, {"t": T2, "ev": "edge-removed", "a": 1, "b": 2},
            {"t": T3, "ev": "edge-added", "a": 1, "b": 2}]
    writer(tmp_path, "old-box.1", recs)
    assert [r["ev"] for r in H.read_all(tmp_path)] == ["edge-added", "edge-removed", "edge-added"]
    assert "edges 1" in R.render(H.read_all(tmp_path))


def test_a_real_flip_keeps_all_three_records(tmp_path):
    added = rec("edge-added", T1, U1, a=1, b=2)
    removed = rec("edge-removed", T2, U2, a=1, b=2)
    again = rec("edge-added", T3, U3, a=1, b=2)
    assert len({added["id"], removed["id"], again["id"]}) == 3
    writer(tmp_path, "dana-box1.1", [added, removed])
    writer(tmp_path, "lee-box2.2", [again])
    got = H.read_all(tmp_path)
    assert [r["ev"] for r in got] == ["edge-added", "edge-removed", "edge-added"]
    assert "edges 1" in R.render(got)


def test_the_id_depends_on_the_source_stamp_and_not_on_the_writers_clock():
    assert rec("edge-added", T1, U1, a=1, b=2)["id"] == rec("edge-added", T3, U1, a=1, b=2)["id"]
    assert rec("edge-added", T1, U1, a=1, b=2)["id"] != rec("edge-added", T1, U2, a=1, b=2)["id"]


def test_diff_stamps_every_record_with_u_and_id():
    old = SNAP._map(edges=((1, 2),))
    new = SNAP._map(edges=((1, 2), (2, 3)))
    for m in (old, new):
        for n in m["nodes"]:
            n["updated_at"] = U2 if n["number"] == 3 else U1
    recs = H.diff(old, new, T1)
    added = [r for r in recs if r["ev"] == "edge-added"]
    assert len(added) == 1 and added[0]["u"] == U2 and added[0]["id"] == H.change_id(added[0])


def test_fold_applies_only_entries_after_the_snapshots_newest_id(tmp_path):
    first, second, third = rec("edge-added", T1, U1, a=1, b=2), rec("edge-added", T2, U2, a=2, b=3), \
        rec("edge-added", T3, U3, a=3, b=4)
    writer(tmp_path, "w.1", [third, first, second])
    assert H.newest_id([first, second, third]) == third["id"]
    assert [r["id"] for r in H.read_all(tmp_path, after=first["id"])] == [second["id"], third["id"]]
    assert H.read_all(tmp_path, after=third["id"]) == []
    assert len(H.read_all(tmp_path, after="0" * 12)) == 3          # an unknown id applies everything
    assert len(H.read_all(tmp_path, after=None)) == 3


def test_the_snapshot_records_the_newest_id_it_includes(tmp_path, origin):
    base, cfg = SNAP._booted(tmp_path, origin)
    first = SNAP._map(edges=((1, 2),))
    for n in first["nodes"]:
        n["updated_at"] = U1
    second = SNAP._map(edges=((1, 2), (2, 3)))
    for n in second["nodes"]:
        n["updated_at"] = U2
    assert SNAP.dag_store.save(base, first, cfg)[0]
    assert SNAP.dag_store.save(base, second, cfg)[0]
    root = base / "ledger"
    records = H.read_all(root)
    through = SNAP.dag_store.snapshot_through(root)
    assert records and through == records[-1]["id"] == H.newest_id(records)
    assert H.read_all(root, after=through) == []


def test_snapshot_through_ignores_a_malformed_value(tmp_path):
    (tmp_path / "dag").mkdir()
    (tmp_path / "dag" / "map.json").write_text(json.dumps({"history_through": "../etc"}), encoding="utf-8")
    assert SNAP.dag_store.snapshot_through(tmp_path) is None
