"""#1610: the dependency-map store, its schema, and the `discovery.dag.mode` switch.

Every guard here was run red once against its own deliberate break (the `control:` line in each
docstring names the break). The controls that matter: an added title field; the atomic replace
removed; `off` ignored; each `usable` clause deleted on its own."""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
STORE = S / "dag_store.py"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ds = _mod("dag_store")

NOW = 1_800_000_000.0
TTL = 60


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _map(n=3, **header):
    h = {"repo": "owner/repo", "built_at": _iso(NOW - 60), "refreshed_at": _iso(NOW - 60),
         "last_ok_at": _iso(NOW - 60), "last_error": None, "partial": False,
         "cursor": "2026-10-17T00:00:00Z", "etag": "abc123", "checked_at": None}
    h.update(header)
    nodes = [{"number": i, "kind": "issue", "state": "open", "life": "goal", "prio": "P1",
              "in_corpus": True, "updated_at": _iso(NOW - 100), "unit": "dag"} for i in range(1, n + 1)]
    edges = [{"blocker": 1, "blocked": 2, "source": "body"}]
    return {"schema": ds.SCHEMA, "header": h, "nodes": nodes, "edges": edges}


def _sdlc(tmp_path):
    base = tmp_path / ".sdlc"
    base.mkdir()
    return base


def _path(sdlc):
    return pathlib.Path(sdlc) / "state" / "dag.json"


# ---------------------------------------------------------------- round trip + closed vocabulary

def test_round_trip(tmp_path):
    sdlc = _sdlc(tmp_path)
    m = _map()
    assert ds.save(sdlc, m)[0] is True
    got, reason = ds.load(sdlc)
    assert reason == "ok" and got == m


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def test_saved_file_holds_no_string_outside_the_closed_vocabulary(tmp_path):
    """Whatever the caller passes, the file carries only codes, numbers, timestamps, the repo name and
    the unit token. control: let `title` through the node whitelist; this goes red."""
    sdlc = _sdlc(tmp_path)
    m = _map()
    for node in m["nodes"]:
        node["title"] = "Secret customer rollout plan"
        node["labels"] = ["customer-acme"]
        node["author"] = "someone"
    m["edges"][0]["phrase"] = "blocked by the Acme migration"
    m["edges"][0]["source"] = "free text from a comment"
    m["header"]["last_error"] = "boom: token ghp_secret in message"
    assert ds.save(sdlc, m)[0]
    raw = _path(sdlc).read_text()
    for banned in ("Secret", "customer", "someone", "Acme", "ghp_secret", "free text"):
        assert banned not in raw
    doc = json.loads(raw)
    allowed = (set(ds.VOCAB) | {"owner/repo", "dag", ds.SCHEMA})
    ts = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
    for s in _strings(doc):
        assert s in allowed or ts.match(s) or s in {"abc123"}, s


# ---------------------------------------------------------------- bad files

def _good_text(tmp_path):
    sdlc = _sdlc(tmp_path)
    ds.save(sdlc, _map())
    return sdlc, _path(sdlc).read_text()


@pytest.mark.parametrize("case,reason", [
    ("missing", "missing"),
    ("empty", "truncated"),
    ("half", "truncated"),
    ("garbage", "corrupt"),
    ("array", "corrupt"),
    ("bad_nodes", "corrupt"),
    ("other_schema", "schema"),
    ("no_schema", "schema"),
])
def test_bad_files_load_as_none_with_a_distinct_reason(tmp_path, case, reason):
    """control: collapse two reasons into one; the distinctness test below goes red."""
    sdlc, text = _good_text(tmp_path)
    p = _path(sdlc)
    doc = json.loads(text)
    if case == "missing":
        p.unlink()
    elif case == "empty":
        p.write_text("")
    elif case == "half":
        p.write_text(text[: len(text) // 2])
    elif case == "garbage":
        p.write_text("not json at all }")
    elif case == "array":
        p.write_text("[1, 2]")
    elif case == "bad_nodes":
        doc["nodes"] = "nope"
        p.write_text(json.dumps(doc))
    elif case == "other_schema":
        doc["schema"] = "dag/2"
        p.write_text(json.dumps(doc))
    elif case == "no_schema":
        del doc["schema"]
        p.write_text(json.dumps(doc))
    got, why = ds.load(sdlc)
    assert got is None and why == reason


def test_unreadable_path_never_raises(tmp_path):
    sdlc = _sdlc(tmp_path)
    (sdlc / "state").mkdir()
    _path(sdlc).mkdir()          # a directory where the file should be
    got, why = ds.load(sdlc)
    assert got is None and why in ds.LOAD_REASONS


def test_reasons_are_distinct():
    assert len(set(ds.LOAD_REASONS)) == len(ds.LOAD_REASONS) >= 4


# ---------------------------------------------------------------- lock and concurrent save

def test_second_refresher_gets_busy_without_waiting(tmp_path):
    sdlc = _sdlc(tmp_path)
    with ds.refresh_lock(sdlc) as first:
        assert first == "held"
        t0 = time.monotonic()
        with ds.refresh_lock(sdlc) as second:
            assert second == "busy"
        assert time.monotonic() - t0 < 2
    with ds.refresh_lock(sdlc) as again:
        assert again == "held"       # released on exit


def test_lock_fails_open_without_fcntl(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "fcntl", None)
    sdlc = _sdlc(tmp_path)
    with ds.refresh_lock(sdlc) as a, ds.refresh_lock(sdlc) as b:
        assert a == b == "open"


def test_save_goes_through_the_atomic_publish(tmp_path, monkeypatch):
    """The deterministic control at the exact seam. control: write the file directly in `save`
    (no atomic replace); this goes red."""
    sdlc = _sdlc(tmp_path)
    calls = []
    real = ds.state.atomic_write_text
    monkeypatch.setattr(ds.state, "atomic_write_text", lambda p, t: (calls.append(str(p)), real(p, t))[1])
    assert ds.save(sdlc, _map())[0]
    assert calls == [str(_path(sdlc))]


_WRITER = r"""
import importlib.util, sys, json
spec = importlib.util.spec_from_file_location("dag_store", sys.argv[1])
ds = importlib.util.module_from_spec(spec); spec.loader.exec_module(ds)
sdlc, tag, secs = sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
import time
end = time.time() + secs
nodes = [{"number": i, "kind": "issue", "state": "open", "life": "goal", "prio": "P1",
          "in_corpus": True, "updated_at": "2026-10-17T00:00:00Z"} for i in range(1, 4000)]
m = {"schema": ds.SCHEMA, "header": {"repo": "o/r%d" % tag, "built_at": "2026-10-17T00:00:00Z",
     "partial": False}, "nodes": nodes, "edges": []}
while time.time() < end:
    ds.save(sdlc, m)
"""


def test_concurrent_saves_leave_a_valid_file_for_a_concurrent_reader(tmp_path):
    """Smoke test (a race): two writer processes and a reader. The deterministic proof of the atomic
    replace is `test_save_goes_through_the_atomic_publish`. control: direct write; this goes red."""
    sdlc = _sdlc(tmp_path)
    ds.save(sdlc, _map())
    env = resource_mode.carry(dict(os.environ))
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, str(STORE), str(sdlc), str(i), "1.5"],
                              env=env) for i in (1, 2)]
    bad = []
    seen = 0
    try:
        while any(p.poll() is None for p in procs):
            got, why = ds.load(sdlc)
            seen += 1
            if why in ("truncated", "corrupt"):
                bad.append(why)
    finally:
        for p in procs:
            p.wait(timeout=30)
    assert all(p.returncode == 0 for p in procs)
    assert seen > 0 and not bad
    got, why = ds.load(sdlc)
    assert why == "ok" and got["header"]["repo"] in ("o/r1", "o/r2")
    assert list((sdlc / "state").glob(".*.tmp")) == []


# ---------------------------------------------------------------- mode

@pytest.mark.parametrize("cfg,expected", [
    ({}, "shadow"),
    (None, "shadow"),
    ({"discovery": {}}, "shadow"),
    ({"discovery": {"dag": {}}}, "shadow"),
    ({"discovery": {"dag": {"mode": "off"}}}, "off"),
    ({"discovery": {"dag": {"mode": "shadow"}}}, "shadow"),
    ({"discovery": {"dag": {"mode": "on"}}}, "on"),
    ({"discovery": {"dag": {"mode": "OFF"}}}, "shadow"),
    ({"discovery": {"dag": {"mode": "On"}}}, "shadow"),
    ({"discovery": {"dag": {"mode": "of"}}}, "shadow"),
    ({"discovery": {"dag": {"mode": False}}}, "shadow"),
    ({"discovery": {"dag": {"mode": None}}}, "shadow"),
    ({"discovery": {"dag": "off"}}, "shadow"),
    ({"discovery": "off"}, "shadow"),
])
def test_mode_table(cfg, expected):
    assert ds.mode(cfg) == expected


def test_other_keys_default_and_bad_values_read_as_default():
    assert ds.ttl_minutes({}) == 60
    assert ds.ttl_minutes({"discovery": {"dag": {"ttl_minutes": 15}}}) == 15
    for bad in (0, -5, "x", True, None, 1.5):
        assert ds.ttl_minutes({"discovery": {"dag": {"ttl_minutes": bad}}}) == 60
    assert ds.order({}) == "chain"
    assert ds.order({"discovery": {"dag": {"order": "off"}}}) == "off"
    assert ds.order({"discovery": {"dag": {"order": "CHAIN"}}}) == "chain"
    assert ds.cycle_comment({}) is True
    assert ds.cycle_comment({"discovery": {"dag": {"cycle_comment": False}}}) is False
    assert ds.cycle_comment({"discovery": {"dag": {"cycle_comment": "no"}}}) is True


def _off_config(sdlc):
    (sdlc / "config.json").write_text(json.dumps({"discovery": {"dag": {"mode": "off"}}}))


def test_off_creates_no_file_and_reads_none(tmp_path, monkeypatch):
    """With mode off: no read, no write, no lock file, no state directory. A counting stub sits on
    every I/O seam. control: ignore `off` in the gate; this goes red."""
    sdlc = _sdlc(tmp_path)
    _off_config(sdlc)
    hits = []
    monkeypatch.setattr(ds, "_read_text", lambda p: hits.append(("read", p)) or "")
    monkeypatch.setattr(ds.state, "atomic_write_text", lambda p, t: hits.append(("write", p)))
    cfg = {"discovery": {"dag": {"mode": "off"}}}
    assert ds.save(sdlc, _map(), cfg) == (False, "off")
    assert ds.load(sdlc, cfg) == (None, "off")
    with ds.refresh_lock(sdlc, cfg) as got:
        assert got == "off"
    # the same, with the config read from disk
    assert ds.save(sdlc, _map()) == (False, "off")
    assert ds.load(sdlc) == (None, "off")
    assert hits == []
    assert not (sdlc / "state").exists()


def test_off_read_from_disk_config_also_holds_for_the_cli(tmp_path):
    sdlc = _sdlc(tmp_path)
    _off_config(sdlc)
    env = resource_mode.carry(dict(os.environ))
    out = subprocess.run([sys.executable, str(S / "dag.py"), "show", "--sdlc", str(sdlc)],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "dag: off"
    assert not (sdlc / "state").exists()


def test_shadow_writes_and_loads(tmp_path):
    sdlc = _sdlc(tmp_path)
    (sdlc / "config.json").write_text("{}")
    assert ds.save(sdlc, _map())[0] is True
    assert ds.load(sdlc)[1] == "ok"


# ---------------------------------------------------------------- usable: one test per clause

def test_a_fresh_built_map_is_usable():
    assert ds.usable(_map(), NOW, TTL) == (True, "ok")


def test_clause_stale():
    """control: delete the age clause; this and only this goes red."""
    m = _map(last_ok_at=_iso(NOW - 2 * TTL * 60 - 120), built_at=_iso(NOW - 3 * TTL * 60))
    ok, why = ds.usable(m, NOW, TTL)
    assert (ok, why) == (False, "stale")
    old = _iso(NOW - 5 * TTL * 60)
    assert ds.usable(_map(last_ok_at=old, checked_at=_iso(NOW - 601)), NOW, TTL) == (False, "stale")


def test_age_boundary_is_inclusive_at_twice_the_ttl():
    m = _map(last_ok_at=_iso(NOW - 2 * TTL * 60))
    assert ds.usable(m, NOW, TTL)[0] is True


def test_a_recent_change_check_rescues_an_old_map():
    old = _iso(NOW - 5 * TTL * 60)
    assert ds.usable(_map(last_ok_at=old, checked_at=_iso(NOW - 300)), NOW, TTL) == (True, "ok")


def test_clause_partial():
    """control: delete the partial clause; this and only this goes red."""
    assert ds.usable(_map(partial=True), NOW, TTL) == (False, "partial")


def test_clause_schema():
    """control: delete the schema clause; this and only this goes red."""
    m = _map()
    m["schema"] = "dag/2"
    assert ds.usable(m, NOW, TTL) == (False, "schema")


def test_clause_built():
    """control: delete the built clause; this and only this goes red."""
    m = _map()
    del m["header"]["built_at"]
    assert ds.usable(m, NOW, TTL) == (False, "not built")
    m["header"]["built_at"] = "yesterday-ish"
    assert ds.usable(m, NOW, TTL) == (False, "not built")


def test_clause_future_stamp():
    assert ds.usable(_map(last_ok_at=_iso(NOW + 7200)), NOW, TTL) == (False, "future")


@pytest.mark.parametrize("m", [None, [], "x", {}, {"schema": "dag/1"}, {"schema": "dag/1", "header": 3}])
def test_usable_never_raises_on_junk(m):
    ok, why = ds.usable(m, NOW, TTL)
    assert ok is False and isinstance(why, str)


# ---------------------------------------------------------------- config keys

TMPL = ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl"
DOC = ROOT / "docs" / "configuration.md"
_KEY_READ = re.compile(r'_key\(\s*\w+\s*,\s*"([a-z_]+)"')


def _keys_read_in_code():
    found = set()
    for p in (STORE, S / "dag.py"):
        found.update(_KEY_READ.findall(p.read_text(encoding="utf-8")))
    return found


def test_the_key_idiom_still_matches():
    assert _keys_read_in_code() == {"mode", "ttl_minutes", "order", "cycle_comment"}


def test_every_dag_key_the_code_reads_is_in_the_template_and_the_docs():
    """control: read a fifth key in code without adding it; this goes red."""
    tmpl = json.loads(TMPL.read_text(encoding="utf-8"))
    block = tmpl["discovery"]["dag"]
    doc = DOC.read_text(encoding="utf-8")
    for key in _keys_read_in_code():
        assert key in block, f"discovery.dag.{key} missing from the config template"
        assert f"discovery.dag.{key}" in doc, f"discovery.dag.{key} missing from docs/configuration.md"
    assert block["mode"] == "shadow" and block["ttl_minutes"] == 60
    assert block["order"] == "chain" and block["cycle_comment"] is True


# ---------------------------------------------------------------- the CLI

def test_show_reports_mode_age_and_counts(tmp_path):
    sdlc = _sdlc(tmp_path)
    (sdlc / "config.json").write_text("{}")
    m = _map(n=3, built_at=_iso(time.time() - 120), last_ok_at=_iso(time.time() - 120))
    ds.save(sdlc, m)
    env = resource_mode.carry(dict(os.environ))
    out = subprocess.run([sys.executable, str(S / "dag.py"), "show", "--sdlc", str(sdlc)],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "mode shadow" in out.stdout and "nodes 3" in out.stdout and "edges 1" in out.stdout
    assert "age 2m" in out.stdout or "age 1m" in out.stdout or "age 3m" in out.stdout


def test_show_with_no_file_says_so_and_creates_nothing(tmp_path):
    sdlc = _sdlc(tmp_path)
    (sdlc / "config.json").write_text("{}")
    env = resource_mode.carry(dict(os.environ))
    out = subprocess.run([sys.executable, str(S / "dag.py"), "show", "--sdlc", str(sdlc)],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0 and "missing" in out.stdout
    assert not (sdlc / "state").exists()
