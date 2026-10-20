"""#1936: Chain plans in one session share a short-lived on-disk read cache of each member's body and newest comments.

No network: the counting fake `gh` of test_chain_waits_1902 serves REST GETs only. The cache is keyed by issue number
plus the issue's exact `updated_at`; it is bounded by age, entry count and file size; it fails open.
Controls (each seen red, then restored): the `updated_at` equality dropped; the age check dropped; the empty-stamp
guard dropped; `put` skipped (a cache that never keeps); the `disk` argument not passed down.
Call counts are the fake's. The saving on a live repository is not measured."""
import importlib.util
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_chain_waits_1902 as w  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = ROOT / "skills" / "sigma-loop" / "scripts"
NOW = w.NOW
STAMP = "2026-02-01T00:00:00Z"


def _crc():
    spec = importlib.util.spec_from_file_location("chain_read_cache", S / "chain_read_cache.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _stamped(issues, stamp=STAMP):
    for i in issues:
        if stamp is None:
            i.pop("updated_at", None)
        else:
            i["updated_at"] = stamp
    return issues


def _world(stamp=STAMP):
    # goals 1 and 2 both wait on 3 (a body edge), so 3 is shared by two overlapping chains
    return _stamped([w._goal(1, blocked_by=[3], comments=["note one"]),
                     w._goal(2, blocked_by=[3], comments=["note two"]),
                     w._goal(3, comments=["note three"])], stamp)


def _plan(base, issues, goal, calls, now=NOW):
    ch = w._mod("chain")
    return ch.plan(base, goal, run=w._fake(issues, calls), now=now)


def _reads(calls, number):
    """Comment-page requests for one issue."""
    return sum(1 for a in calls if a[1].endswith("/issues/%d/comments" % number))


def _core(p):
    return {k: v for k, v in p.items() if k not in ("calls", "record")}


def test_overlapping_chains_read_the_shared_member_once(tmp_path):
    base, issues, calls = w._repo(tmp_path, dag_mode="off"), _world(), []
    _plan(base, issues, 1, calls)
    _plan(base, issues, 2, calls)
    assert _reads(calls, 3) == 1
    assert _reads(calls, 1) == 1 and _reads(calls, 2) == 1


def test_the_cache_file_is_written_under_state(tmp_path):
    base = w._repo(tmp_path, dag_mode="off")
    _plan(base, _world(), 1, [])
    data = json.loads((pathlib.Path(base) / "state" / "chain-reads.json").read_text())
    assert {"1", "3"} <= set(data["entries"])


def test_a_member_whose_updated_at_moved_is_read_again(tmp_path):
    base, issues, calls = w._repo(tmp_path, dag_mode="off"), _world(), []
    _plan(base, issues, 1, calls)
    issues[2]["updated_at"] = "2026-02-02T00:00:00Z"
    _plan(base, issues, 2, calls)
    assert _reads(calls, 3) == 2


def test_age_past_the_limit_reads_again_and_the_limit_itself_hits(tmp_path):
    crc = _crc()
    base, issues, calls = w._repo(tmp_path, dag_mode="off"), _world(), []
    _plan(base, issues, 1, calls)
    _plan(base, issues, 2, calls, now=NOW + crc.MAX_AGE_SECONDS)
    assert _reads(calls, 3) == 1
    _plan(base, issues, 1, calls, now=NOW + 2 * crc.MAX_AGE_SECONDS + 5)
    assert _reads(calls, 3) == 2


def test_no_known_updated_at_is_never_a_hit(tmp_path):
    base, issues, calls = w._repo(tmp_path, dag_mode="off"), _world(None), []
    _plan(base, issues, 1, calls)
    _plan(base, issues, 2, calls)
    assert _reads(calls, 3) == 2


def test_get_refuses_an_empty_stamp_even_when_the_entry_holds_one(tmp_path):
    crc = _crc()
    store = crc.load(str(tmp_path), NOW)
    crc.put(store, 7, "", ({"number": 7}, [], False), NOW)
    assert crc.get(store, 7, "") is None
    assert crc.get(store, 7, None) is None


def test_put_then_get_round_trips_through_the_file(tmp_path):
    crc = _crc()
    store = crc.load(str(tmp_path), NOW)
    got = ({"number": 7, "body": "b"}, [{"body": "c"}], True)
    crc.put(store, 7, STAMP, got, NOW)
    crc.save(str(tmp_path), store)
    again = crc.load(str(tmp_path), NOW + 1)
    assert crc.get(again, 7, STAMP) == got
    assert crc.get(again, 7, "2026-02-02T00:00:00Z") is None
    assert crc.get(again, 8, STAMP) is None


def _prime(tmp_path):
    base = w._repo(tmp_path, dag_mode="off")
    cold = _core(_plan(base, _world(), 1, []))
    return base, cold


def test_a_corrupt_cache_file_reads_as_empty_and_the_plan_is_unchanged(tmp_path):
    base, cold = _prime(tmp_path)
    (pathlib.Path(base) / "state" / "chain-reads.json").write_text("{not json")
    calls = []
    assert _core(_plan(base, _world(), 1, calls)) == cold
    assert _reads(calls, 3) == 1


def test_a_wrong_version_cache_file_reads_as_empty(tmp_path):
    base, cold = _prime(tmp_path)
    path = pathlib.Path(base) / "state" / "chain-reads.json"
    data = json.loads(path.read_text())
    data["version"] = 999
    path.write_text(json.dumps(data))
    calls = []
    assert _core(_plan(base, _world(), 1, calls)) == cold
    assert _reads(calls, 3) == 1


def test_an_unwritable_cache_path_never_fails_the_plan(tmp_path):
    base = w._repo(tmp_path, dag_mode="off")
    (pathlib.Path(base) / "state" / "chain-reads.json").mkdir()      # a directory where the file belongs
    assert _core(_plan(base, _world(), 1, [])) == _core(_plan(w._repo(tmp_path / "b", dag_mode="off"), _world(), 1, []))


def test_a_hot_cache_gives_the_same_plan_as_a_cold_one(tmp_path):
    base, issues = w._repo(tmp_path, dag_mode="off"), _world()
    cold = _core(_plan(base, issues, 1, []))
    calls = []
    hot = _core(_plan(base, issues, 1, calls))
    assert hot == cold
    assert _reads(calls, 3) == 0


def test_more_than_the_entry_limit_keeps_the_newest(tmp_path):
    crc = _crc()
    store = crc.load(str(tmp_path), NOW)
    for k in range(crc.MAX_ENTRIES + 50):
        crc.put(store, k, STAMP, ({"number": k}, [], False), NOW + k)
    crc.save(str(tmp_path), store)
    kept = json.loads((tmp_path / "state" / "chain-reads.json").read_text())["entries"]
    assert len(kept) == crc.MAX_ENTRIES
    assert str(crc.MAX_ENTRIES + 49) in kept and "0" not in kept


def test_the_file_stays_under_the_size_limit(tmp_path, monkeypatch):
    crc = _crc()
    monkeypatch.setattr(crc, "MAX_BYTES", 20000)
    store = crc.load(str(tmp_path), NOW)
    for k in range(20):
        crc.put(store, k, STAMP, ({"number": k, "body": "x" * 4000}, [], False), NOW + k)
    crc.save(str(tmp_path), store)
    path = tmp_path / "state" / "chain-reads.json"
    assert path.stat().st_size <= 20000
    kept = json.loads(path.read_text())["entries"]
    assert "19" in kept and "0" not in kept


def test_expired_entries_are_not_written_back(tmp_path):
    crc = _crc()
    store = crc.load(str(tmp_path), NOW)
    crc.put(store, 1, STAMP, ({"number": 1}, [], False), NOW)
    crc.save(str(tmp_path), store)
    later = crc.load(str(tmp_path), NOW + crc.MAX_AGE_SECONDS + 1)
    crc.put(later, 2, STAMP, ({"number": 2}, [], False), NOW + crc.MAX_AGE_SECONDS + 1)
    crc.save(str(tmp_path), later)
    assert set(json.loads((tmp_path / "state" / "chain-reads.json").read_text())["entries"]) == {"2"}


def test_a_map_plan_uses_the_map_nodes_stamp(tmp_path):
    base = w._repo(tmp_path)
    issues = _world()
    nodes = [dict(w._mnode(n), updated_at=STAMP) for n in (1, 2, 3)]
    w._save_map(base, nodes, [(3, 1), (3, 2)])
    calls = []
    _plan(base, issues, 1, calls)
    first = _reads(calls, 3)
    _plan(base, issues, 2, calls)
    assert first == 1 and _reads(calls, 3) == 1


def test_a_plan_without_a_cache_is_unchanged(tmp_path):
    cw = w._mod("chain_waits")
    calls = []
    run = w._fake(_world(), calls)
    a = cw._read(run, "acme/widget", 3, None)
    b = cw._read(run, "acme/widget", 3, None, None)
    assert a[1] == b[1] and _reads(calls, 3) == 2


def _scenario_plan_calls(tmp_path, stamped, repeats):
    """GitHub calls for each of `repeats` plans of the docs' scratch scenario, counted from the fake's call log."""
    sys.path.insert(0, str(ROOT / "tests" / "e2e"))
    import test_chain_scenario as sc_mod
    sc = sc_mod.Scenario(tmp_path)
    if stamped:
        for i in sc.fk.by.values():
            i["updated_at"] = STAMP
    counts = []
    for _ in range(repeats):
        before = len(sc.fk.calls)
        assert sc.step("plan") == 0, sc.out
        counts.append(len(sc.fk.calls) - before)
    return counts


def test_the_docs_scenario_second_plan_costs_fewer_calls(tmp_path):
    first, second = _scenario_plan_calls(tmp_path, True, 2)
    unstamped = _scenario_plan_calls(tmp_path / "u", False, 2)
    print("scenario plan calls: first %d, second (cache hot) %d, second without stamps %d" % (first, second, unstamped[1]))
    assert first == unstamped[0] == unstamped[1]
    assert second < first
    doc = (ROOT / "docs" / "chain.md").read_text(encoding="utf-8")
    assert "second overlapping plan: %d" % second in doc
