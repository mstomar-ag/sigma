"""#1956: the dependency map snapshot on the ledger branch (`dag/map.json`) and DAG.md built from it.

Offline: fake issue numbers, a real local git repo and bare origin, no network, no model. MEASURED: these
tests only; no real multi-machine run. Controls, each run red once on its own clause:
  * drop the `ledger.enabled` gate in `_ledger_root`          -> the ledger-off test red
  * drop the `clean` filter in `publish_snapshot`             -> the filtered-write test red
  * swap newer/older in `load_with_source`                    -> the newer/older test red
  * drop `dag/map.json` from the staged list in `publish`     -> the publish test red
"""
import importlib.util
import json
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sync, ledger, dag_store, dag_render = _mod("sync"), _mod("ledger"), _mod("dag_store"), _mod("dag_render")

OLD, NEW = "2026-03-05T10:00:00Z", "2026-03-06T10:00:00Z"


def _git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr or p.stdout
    return p.stdout.strip()


def _config(enabled=True, mode="shadow"):
    return {"ledger": {"enabled": enabled, "actor": "dana", "host_alias": "box1"}, "discovery": {"dag": {"mode": mode}}}


def _clone(tmp_path, origin, config):
    repo = tmp_path / "a"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / ".gitignore").write_text(".sdlc/\n", encoding="utf-8")
    _git(repo, "add", "--", ".gitignore")
    _git(repo, "commit", "-q", "-m", "base")
    base = repo / ".sdlc"
    (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return base


@pytest.fixture
def origin(tmp_path):
    o = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(o)], check=True)
    return o


def _booted(tmp_path, origin, **kw):
    cfg = _config(**kw)
    base = _clone(tmp_path, origin, cfg)
    if cfg["ledger"]["enabled"]:
        assert "published" in sync.bootstrap(base, cfg)
    return base, cfg


def _map(built=NEW, edges=((1, 2),), states=None, extra=False):
    states = states or {}
    nums = sorted({x for e in edges for x in e})
    nodes = [{"number": n, "kind": "issue", "state": states.get(n, "open"), "life": "goal"} for n in nums]
    m = {"schema": "dag/1", "header": {"repo": "o/r", "built_at": built, "last_ok_at": built},
         "nodes": nodes, "edges": [{"blocker": a, "blocked": b, "source": "body"} for a, b in edges]}
    if extra:
        m["nodes"][0]["title"] = "secret title of fixture issue 8"
        m["header"]["note"] = "free text"
        m["built_by"] = "forged"
    return m


def test_snapshot_written_filtered_with_built_by_and_loads_back(tmp_path, origin):
    base, cfg = _booted(tmp_path, origin)
    ok, _ = dag_store.save(base, _map(extra=True), cfg)
    assert ok
    f = base / "ledger" / "dag" / "map.json"
    text = f.read_text(encoding="utf-8")
    assert "secret title" not in text and "free text" not in text and "forged" not in text
    doc = json.loads(text)
    assert doc["built_by"] == "box1" and doc["schema"] == "dag/1"
    snap, by = dag_store.read_snapshot_at(base / "ledger")
    assert by == "box1" and snap["edges"] == [{"blocker": 1, "blocked": 2, "source": "body"}]


def test_ledger_off_creates_no_dag_dir_and_save_load_unchanged(tmp_path, origin):
    base, cfg = _booted(tmp_path, origin, enabled=False)
    assert dag_store.save(base, _map(), cfg) == (True, "ok")
    assert not (base / "ledger").exists()
    m, why, src = dag_store.load_with_source(base, cfg)
    assert why == "ok" and src == "local" and m == dag_store.load(base, cfg)[0]


def test_ledger_disabled_with_a_worktree_present_writes_no_snapshot(tmp_path, origin):
    base, cfg = _booted(tmp_path, origin)            # worktree exists; the config now turns the ledger off
    assert dag_store.publish_snapshot(base, _map(), _config(enabled=False)) is False
    assert dag_store.read_snapshot(base, _config(enabled=False)) is None
    assert not (base / "ledger" / "dag" / "map.json").exists()


def test_publish_snapshot_filters_a_raw_map_itself(tmp_path, origin):
    base, cfg = _booted(tmp_path, origin)
    assert dag_store.publish_snapshot(base, _map(extra=True), cfg) is True
    text = (base / "ledger" / "dag" / "map.json").read_text(encoding="utf-8")
    assert "secret title" not in text and "forged" not in text and '"box1"' in text


def test_mode_off_writes_no_snapshot(tmp_path, origin):
    base, cfg = _booted(tmp_path, origin)
    off = _config(mode="off")
    assert dag_store.publish_snapshot(base, _map(), off) is False
    assert not (base / "ledger" / "dag" / "map.json").exists()


def test_publish_pushes_a_map_only_change(tmp_path, origin):
    base, cfg = _booted(tmp_path, origin)
    dag_store.save(base, _map(), cfg)
    out = sync.publish(base, cfg)
    assert "nothing" not in out, out
    shown = json.loads(_git(origin, "show", "sdlc-ledger:dag/map.json"))  # the ledger branch
    assert shown["edges"][0]["blocked"] == 2
    assert "Built by box1 at " + NEW in _git(origin, "show", "sdlc-ledger:DAG.md")  # the ledger branch


def test_dag_md_golden_from_snapshot_and_history():
    snap = _map(edges=((1, 2), (2, 3), (5, 6)), states={3: "merged"})
    hist = [{"t": NEW, "ev": "chain-completed", "chain": 9, "n": 2}, {"t": NEW, "ev": "edge-added", "a": 40, "b": 41}]
    text = dag_render.render(hist, snap, "box1", now=1772791200 + 2 * 3600 + 5 * 60)   # built 10:00:00Z + 2h05m
    assert text == (
        "# DAG\n\nBuilt by box1 at 2026-03-06T10:00:00Z\nSnapshot age: 0 day(s)\n\n"
        "## Stuck chains\n\n- #1 holds 1\n- #5 holds 1\n\n"
        "## Active chains\n\n- #1: 2 open of 3: #1, #2\n- #5: 2 open of 2: #5, #6\n\n"
        "## Cycles\n\n- none\n\n## Completed this month\n\n- #9 (2 goals)\n\n"
        "## Counts\n\nopen 4, resolved 1, edges 3, chains 2, stuck 2, cycles 0, completed 1\n")
    assert "#40" not in text                              # history edges are not folded when a snapshot is given


def test_no_snapshot_render_is_the_old_render():
    hist = [{"t": OLD, "ev": "edge-added", "a": 1, "b": 2}]
    assert dag_render.render(hist) == dag_render.render(hist, None, None, None)
    assert "Built by" not in dag_render.render(hist)


def _write_snapshot(base, built):
    root = base / "ledger"
    dag_store.snapshot_path(root).parent.mkdir(parents=True, exist_ok=True)
    doc = dag_store.clean(_map(built=built, edges=((7, 8),)))
    doc["schema"], doc["built_by"] = "dag/1", "box2"
    dag_store.snapshot_path(root).write_text(json.dumps(doc), encoding="utf-8")


def test_missing_local_uses_ledger_snapshot(tmp_path, origin):
    base, cfg = _booted(tmp_path, origin)
    _write_snapshot(base, NEW)
    m, why, src = dag_store.load_with_source(base, cfg)
    assert (why, src) == ("ok", "ledger") and m["edges"][0]["blocker"] == 7
    assert dag_store.load(base, cfg)[0] == m


def test_local_newer_stays_local_and_local_older_yields_to_ledger(tmp_path, origin):
    base, cfg = _booted(tmp_path, origin)
    local = _map(built=NEW)
    (base / "state" / "dag.json").write_text(json.dumps(local), encoding="utf-8")
    _write_snapshot(base, OLD)
    assert dag_store.load_with_source(base, cfg)[2] == "local"
    _write_snapshot(base, "2026-03-07T10:00:00Z")
    assert dag_store.load_with_source(base, cfg)[2] == "ledger"


@pytest.mark.parametrize("body", ["{\"schema\":\"dag/1\",\"hea", "[1]", "{\"schema\":\"dag/9\",\"header\":{},\"nodes\":[],\"edges\":[]}", "not json"])
def test_corrupt_local_or_bad_snapshot(tmp_path, origin, body):
    base, cfg = _booted(tmp_path, origin)
    (base / "state" / "dag.json").write_text(body, encoding="utf-8")
    _write_snapshot(base, NEW)
    assert dag_store.load_with_source(base, cfg)[2] == "ledger"
    dag_store.snapshot_path(base / "ledger").write_text(body, encoding="utf-8")   # untrusted snapshot ignored
    assert dag_store.load_with_source(base, cfg)[1:] != ("ok", "ledger")


def test_dag_md_bytes_do_not_change_within_a_day():
    """A finer age would make every publish tick commit a new DAG.md."""
    snap = _map()
    t0 = 1772791200
    assert dag_render.render([], snap, "box1", now=t0 + 60) == dag_render.render([], snap, "box1", now=t0 + 3600 * 5)
    assert dag_render.render([], snap, "box1", now=t0 + 60) != dag_render.render([], snap, "box1", now=t0 + 86400 * 3)
