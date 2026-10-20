"""#1959: the map's source, the snapshot's age and the lease holder on the doctor row and `dag.py report`.

Offline: real ledger worktrees as in `test_dag_lease_1957`, fixture issue 8, no network, no model.
MEASURED: these tests only.

Controls, each run red once on its own clause (see the final report of the goal):
  * drop the expired-lease clause (`h is None`) in `lease_dead`  -> the unexpired-holder test red
  * drop the live clause in `lease_dead`                          -> the not-live (same without live) test red
  * drop the three-TTL age clause in `lease_dead`                 -> the fresh-snapshot test red
  * `source` always `local`                                       -> the ledger-source test red (so does always-true `am_refresher` the foreign-holder test)
  * drop the this-machine comparison (`am_refresher` always true) -> the foreign-holder test red
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402,F401
import test_dag_lease_1957 as L  # noqa: E402  (fixtures only: `two`, `origin`, `_machine`, `_plain_lease`)
from test_dag_lease_1957 import origin, two  # noqa: E402,F401  (pytest fixtures)


def _mod(name, base=S):
    spec = importlib.util.spec_from_file_location(name, base / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dag_source, dag_report = _mod("dag_source"), _mod("dag_report")
doctor = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts")
NOW, TTL = L.NOW, L.TTL


def _live(m, at=NOW):
    hb = doctor._load_loop_script("sync").heartbeat_path(m.base)
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("")
    import os
    os.utime(hb, (at, at))


def _published(a, then=NOW):
    assert a.tick(then)["status"] == "saved"


def test_ledger_source_age_builder_and_this_machine(two):
    a, b = two
    _published(a)
    assert dag_source.facts(a.base, a.cfg, now=NOW)["source"] == "local"       # the builder's own file is as new
    b.tick(NOW + 10)                                    # B folds the snapshot in and uses it
    f = dag_source.facts(b.base, b.cfg, now=NOW + 300)
    assert f["source"] == "ledger" and f["snapshot_age"] == 300 and f["built_by"]
    assert f["holder"] and not f["am_refresher"]
    f = dag_source.facts(a.base, a.cfg, now=NOW + 300)
    assert f["snapshot_age"] == 300 and f["built_by"]
    assert f["holder"] == f["me"] and f["am_refresher"] and f["holder_expires_in"] > 0
    text = dag_source.line(f)
    assert text.startswith("source local, snapshot 5m old built by ")
    assert "lease held by" in text and "(this machine refreshes)" in text
    assert dag_source.line(dag_source.facts(b.base, b.cfg, now=NOW + 300)).startswith("source ledger, snapshot 5m")


def test_foreign_holder_is_named_and_not_this_machine(two):
    a, b = two
    _published(a)
    b.tick(NOW + 10)                                    # folds the snapshot in
    f = dag_source.facts(b.base, b.cfg, now=NOW + 20)
    assert f["holder"] != f["me"] and f["holder"].endswith("boxa") and not f["am_refresher"]
    assert "this machine" not in dag_source.line(f) and "lease held by dana-boxa" in dag_source.line(f)


def test_ledger_off_is_source_local_and_the_row_is_ok(tmp_path, origin):
    m = L._machine(tmp_path, origin, "boxa", enabled=False)
    m.cfg["discovery"]["dag"]["mode"] = "on"
    L.dag_store.save(m.base, {"header": {"repo": "owner/repo", "built_at": "2027-01-15T08:00:00Z",
                                         "last_ok_at": "2027-01-15T08:00:00Z"},
                              "nodes": [{"number": 8, "kind": "issue", "state": "open", "life": "goal"}],
                              "edges": []}, m.cfg)
    t = L.dag_store.parse_ts("2027-01-15T08:00:00Z")
    f = dag_source.facts(m.base, m.cfg, now=t + 60, live=True)
    assert f["source"] == "local" and f["holder"] is None and not f["lease_dead"]
    assert dag_source.line(f) == "source local (ledger off)"
    row = doctor._dag_map_row(m.cfg, str(m.base), now=t + 60)
    assert row["ok"] and "source" not in row["name"]
    assert dag_source.facts(m.base, {"discovery": {"dag": {"mode": "off"}}}, now=t) is None


def _old_dead(a):
    """A published snapshot four TTLs old and a lease long expired."""
    _published(a)
    return NOW + 4 * TTL + 1000


def test_old_snapshot_expired_lease_live_fails_the_row_with_the_fix(two):
    a, b = two
    later = _old_dead(a)
    _live(a, later)
    f = dag_source.facts(a.base, a.cfg, now=later, live=True)
    assert f["lease_dead"] and f["holder"] is None
    row = doctor._dag_map_row(a.cfg, str(a.base), now=later)
    assert not row["ok"] and "no lease holder" in row["name"] and "refresh --force" in row["fix"]


def test_same_without_live_is_ok(two):
    a, b = two
    later = _old_dead(a)
    assert not dag_source.facts(a.base, a.cfg, now=later, live=False)["lease_dead"]
    assert doctor._dag_map_row(a.cfg, str(a.base), now=later)["ok"]


def test_old_snapshot_with_an_unexpired_holder_is_not_failed(two):
    a, b = two
    later = _old_dead(a)
    L._plain_lease(a, "dana-boxc", later - 60)
    f = dag_source.facts(a.base, a.cfg, now=later, live=True)
    assert f["holder"] == "dana-boxc" and not f["lease_dead"]


def test_fresh_snapshot_with_no_lease_is_not_failed(two):
    a, b = two
    _published(a)
    (a.lease_file()).unlink()
    f = dag_source.facts(a.base, a.cfg, now=NOW + TTL, live=True)    # one TTL old: under three
    assert f["holder"] is None and not f["lease_dead"]


def test_report_shows_source_in_markdown_and_json(two):
    a, b = two
    _published(a)
    text, code = dag_report.report(a.base, now=NOW + 60)
    assert code == 0 and "- source: source local, snapshot 1m old built by " in text
    doc = json.loads(dag_report.report(a.base, as_json=True, now=NOW + 60)[0])
    assert doc["source"]["source"] == "local" and doc["source"]["snapshot_age"] == 60
