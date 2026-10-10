"""Slice 5 of the decision rubric: the committed, repository-based decisions store."""
import ast
import datetime
import fcntl
import importlib.util
import json
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _mod(rel):
    p = ROOT / rel
    spec = importlib.util.spec_from_file_location(p.stem, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _ds():
    return _mod("skills/sigma-loop/scripts/decision_store.py")


def _cfg(vis="private", **records):
    r = {"enabled": True, "repo_visibility": vis}
    r.update(records)
    return {"decision_rubric": {"records": r}}


def _rec(**kw):
    r = {"how": "human", "qkind": "scope", "area": "docs", "choice": "option a",
         "reason": "because of the free text", "status": "answered"}
    r.update(kw)
    return r


def _files(sdlc):
    d = pathlib.Path(sdlc) / "decisions"
    return sorted(p.name for p in d.glob("*.json") if not p.name.startswith("_")) if d.is_dir() else []


def test_parallel_worktrees_never_collide(tmp_path):
    ds = _ds()
    a, b = tmp_path / "a", tmp_path / "b"
    ida = ds.append(a, _rec(), _cfg(), now=NOW)
    idb = ds.append(a, _rec(), _cfg(), now=NOW)
    assert ida and idb and ida != idb
    assert _files(a) == sorted([ida + ".json", idb + ".json"])
    before = (a / "decisions" / (ida + ".json")).read_bytes()
    ds.append(a, _rec(), _cfg(), now=NOW)
    assert (a / "decisions" / (ida + ".json")).read_bytes() == before
    assert not b.exists()


def test_append_refuses_an_existing_id(tmp_path):
    ds = _ds()
    rid = ds.append(tmp_path, _rec(), _cfg(), now=NOW)
    first = (tmp_path / "decisions" / (rid + ".json")).read_bytes()
    with pytest.raises(FileExistsError):
        ds.append(tmp_path, _rec(id=rid, choice="other"), _cfg(), now=NOW)
    assert (tmp_path / "decisions" / (rid + ".json")).read_bytes() == first


def test_closed_writes_zero_bytes(tmp_path):
    ds = _ds()
    for config in ({}, {"decision_rubric": {"records": {"enabled": "true"}}},
                   {"decision_rubric": {"records": {"enabled": False}}}):
        assert ds.append(tmp_path, _rec(), config, now=NOW) is None
    assert list(tmp_path.iterdir()) == []


def test_invalid_record_and_unsafe_id_are_refused(tmp_path):
    ds = _ds()
    with pytest.raises(ValueError):
        ds.append(tmp_path, {"how": "robot", "qkind": "x", "choice": "y"}, _cfg(), now=NOW)
    with pytest.raises(ValueError):
        ds.append(tmp_path, _rec(id="../escape"), _cfg(), now=NOW)
    assert ds.validate(_rec()) == []
    assert list(tmp_path.iterdir()) == []


def test_public_removes_free_text(tmp_path):
    ds = _ds()
    rid = ds.append(tmp_path, _rec(), _cfg("public"), now=NOW)
    text = (tmp_path / "decisions" / (rid + ".json")).read_text()
    assert "free text" not in text and "option a" not in text
    got = ds.get(tmp_path, rid, _cfg())
    assert got["qkind"] == "scope" and got["how"] == "human" and "reason" not in got


def test_private_keeps_free_text(tmp_path):
    ds = _ds()
    rid = ds.append(tmp_path, _rec(), _cfg("private"), now=NOW)
    got = ds.get(tmp_path, rid, _cfg())
    assert got["reason"] == "because of the free text" and got["choice"] == "option a"


def test_lock_timeout_skips_reindex(tmp_path):
    ds = _ds()
    ds.append(tmp_path, _rec(), _cfg(), now=NOW)
    fd = open(tmp_path / "decisions" / "_index.lock", "w")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        out = ds.reindex(tmp_path, _cfg(lock_timeout_s=1))
    finally:
        fd.close()
    assert out.get("skipped") == "lock-timeout" and not (tmp_path / "decisions" / "_index.json").exists()
    assert ds.reindex(tmp_path, _cfg())["indexed"] == 1


def test_list_by_filters_and_survives_a_stale_index(tmp_path):
    ds = _ds()
    ds.append(tmp_path, _rec(area="docs"), _cfg(), now=NOW)
    ds.append(tmp_path, _rec(area="loop", qkind="merge"), _cfg(), now=NOW)
    ds.reindex(tmp_path, _cfg())
    ds.append(tmp_path, _rec(area="docs"), _cfg(), now=NOW)  # index now stale
    assert len(ds.list_by(tmp_path, _cfg(), area="docs")) == 2
    assert len(ds.list_by(tmp_path, _cfg(), qkind="merge")) == 1
    assert len(ds.list_by(tmp_path, _cfg())) == 3


def test_archive_folds_old_records(tmp_path):
    ds = _ds()
    old = NOW - datetime.timedelta(days=400)
    ids = [ds.append(tmp_path, _rec(), _cfg(), now=old) for _ in range(2)]
    new = ds.append(tmp_path, _rec(), _cfg(), now=NOW)
    assert ds.archive(tmp_path, 180, _cfg(), now=NOW) == 2
    assert _files(tmp_path) == sorted([new + ".json", "archive-2025.json"])
    for rid in ids:
        assert ds.get(tmp_path, rid, _cfg())["qkind"] == "scope"
    assert len(ds.list_by(tmp_path, _cfg())) == 3
    assert ds.archive(tmp_path, 180, _cfg(), now=NOW) == 0


def test_archive_closed_touches_nothing(tmp_path):
    ds = _ds()
    ds.append(tmp_path, _rec(), _cfg(), now=NOW - datetime.timedelta(days=400))
    assert ds.archive(tmp_path, 180, {}, now=NOW) == 0
    assert len(_files(tmp_path)) == 1


def test_settings_default_and_bad_values_read_as_default():
    ds = _ds()
    s = ds.settings({})
    assert (s["store_dir"], s["lock_timeout_s"], s["archive_after_days"], s["max_files_warn"]) == ("decisions", 20, 180, 5000)
    bad = ds.settings(_cfg(store_dir="../x", lock_timeout_s=-1, archive_after_days="9", max_files_warn=True))
    assert bad == s
    assert ds.settings(_cfg(max_files_warn=7))["max_files_warn"] == 7


def test_doctor_warns_above_the_file_ceiling(tmp_path):
    ds = _ds()
    for _ in range(3):
        ds.append(tmp_path, _rec(), _cfg(), now=NOW)
    assert ds.ceiling_warning(tmp_path, _cfg(max_files_warn=3)) is None
    assert "3" in ds.ceiling_warning(tmp_path, _cfg(max_files_warn=2))
    doctor = _mod("skills/sigma-doctor/scripts/doctor.py")
    assert "decision records" in doctor._decision_rubric_state(_cfg(max_files_warn=2), tmp_path)
    assert "decision records" not in doctor._decision_rubric_state(_cfg(max_files_warn=3), tmp_path)


def test_untracked_old_records_are_named(tmp_path):
    ds = _ds()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sdlc = tmp_path / ".sdlc"
    rid = ds.append(sdlc, _rec(), _cfg(), now=NOW - datetime.timedelta(days=3))
    f = sdlc / "decisions" / (rid + ".json")
    old = (NOW - datetime.timedelta(days=3)).timestamp()
    import os
    os.utime(f, (old, old))
    assert ds.untracked_old(sdlc, now=NOW) == [rid]
    assert ds.untracked_old(sdlc, now=NOW - datetime.timedelta(days=5)) == []


def test_archive_keys_on_the_file_name_stem(tmp_path):
    ds = _ds()
    old = NOW - datetime.timedelta(days=400)
    a = ds.append(tmp_path, _rec(), _cfg(), now=old)
    b = ds.append(tmp_path, _rec(), _cfg(), now=old)
    d = tmp_path / "decisions"
    for rid, body in ((a, {"how": "human", "qkind": "scope", "choice": "x"}),
                      (b, {"id": a, "how": "human", "qkind": "scope", "choice": "y"})):
        (d / (rid + ".json")).write_text(json.dumps(body))
    assert ds.archive(tmp_path, 180, _cfg(), now=NOW) == 2
    assert not (d / (a + ".json")).exists() and not (d / (b + ".json")).exists()
    assert ds.get(tmp_path, a, _cfg())["choice"] == "x"
    assert ds.get(tmp_path, b, _cfg())["choice"] == "y"
    assert len(ds.list_by(tmp_path, _cfg())) == 2


def test_archive_prefixed_id_is_rejected(tmp_path):
    ds = _ds()
    assert ds.validate(_rec(id="archive-2025"))
    with pytest.raises(ValueError):
        ds.append(tmp_path, _rec(id="archive-x"), _cfg(), now=NOW)
    assert list(tmp_path.iterdir()) == []


def test_readers_require_config_and_honour_the_gate(tmp_path):
    ds = _ds()
    rid = ds.append(tmp_path, _rec(), _cfg(), now=NOW)
    for fn, args in ((ds.get, (tmp_path, rid)), (ds.list_by, (tmp_path,)), (ds.reindex, (tmp_path,))):
        with pytest.raises(TypeError):
            fn(*args)
    closed = {"decision_rubric": {"records": {"enabled": False}}}
    assert ds.get(tmp_path, rid, closed) is None
    assert ds.list_by(tmp_path, closed) == []
    assert ds.reindex(tmp_path, closed) == {"skipped": "closed"}
    assert ds.reindex(tmp_path, None) == {"skipped": "closed"}
    assert not (tmp_path / "decisions" / "_index.json").exists()


LOGIN = "log" + "in"


def _scan(source):
    """Names of every identifier, attribute, argument or string (docstrings excluded) that mentions the login."""
    tree = ast.parse(source)
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
    found = []
    for node in ast.walk(tree):
        vals = []
        if isinstance(node, ast.Name):
            vals = [node.id]
        elif isinstance(node, ast.Attribute):
            vals = [node.attr]
        elif isinstance(node, ast.arg):
            vals = [node.arg]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            vals = [node.value]
        found += [v for v in vals if LOGIN in v.lower()]
    return found


def test_no_behaviour_keys_on_login():
    for name in ("decision_store.py", "decision_rubric_cfg.py"):
        src = (ROOT / "skills" / "sigma-loop" / "scripts" / name).read_text(encoding="utf-8")
        assert _scan(src) == [], name


def test_login_scan_finds_a_planted_read():
    assert _scan('def f(rec):\n    return rec["%s"] == "x"\n' % LOGIN) == [LOGIN]
    assert _scan('def f(rec):\n    return rec.%s\n' % LOGIN) == [LOGIN]
    assert _scan('def f(rec):\n    """%s in a docstring"""\n    return 1\n' % LOGIN) == []
