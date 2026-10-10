"""#959: both delete checks must see a Python HTTP-client DELETE in its three shapes."""
import importlib.util
import itertools
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_n = itertools.count()


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHAPES = {
    "urllib": 'import urllib.request\ndef f(u):\n    return urllib.request.Request(u, method="DELETE")\n',
    "urllib-lower": "import urllib.request\ndef f(u):\n    return urllib.request.Request(u, method='delete')\n",
    "requests": 'import requests\ndef f(u):\n    return requests.delete(u)\n',
    "connection": 'def f(conn, p):\n    conn.request("DELETE", p)\n',
}
QUIET = {
    "get": 'import urllib.request\ndef f(u):\n    return urllib.request.Request(u, method="GET")\n',
    "conn-get": 'def f(conn, p):\n    conn.request("GET", p)\n',
    "docstring": 'def f():\n    """requests.delete(u) and conn.request("DELETE", p)"""\n    return 1\n',
}


def _tree(tmp_path, code):
    sub = tmp_path / ("c%d" % next(_n))
    sub.mkdir()
    rel = sub / "skills" / "x" / "scripts" / "y.py"
    rel.parent.mkdir(parents=True)
    rel.write_text(code, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(sub), check=True, capture_output=True, timeout=120)
    subprocess.run(["git", "add", "-A"], cwd=str(sub), check=True, capture_output=True, timeout=120)
    return sub


def _guard_kinds(tmp_path, code):
    guard = _load("guard_959", "tests/test_no_autonomous_feature_branch_deletion.py")
    hits = guard._delete_call_sites(_tree(tmp_path, code))
    assert hits is not None
    return {h[2] for h in hits}


def _scan_rules(tmp_path, code):
    ws = _load("ws_959", "tools/readiness/write_surface.py")
    sub = _tree(tmp_path, code)
    path = sub / "skills" / "x" / "scripts" / "y.py"
    return {r["rule"] for r in ws.scan_paths(sub, [path])}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_guard_sees_http_client_delete(tmp_path, shape):
    assert "http_delete" in _guard_kinds(tmp_path, SHAPES[shape])


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_scanner_sees_http_client_delete(tmp_path, shape):
    assert "network-delete" in _scan_rules(tmp_path, SHAPES[shape])


@pytest.mark.parametrize("shape", sorted(QUIET))
def test_quiet_shapes_stay_quiet(tmp_path, shape):
    assert "http_delete" not in _guard_kinds(tmp_path, QUIET[shape])
    assert "network-delete" not in _scan_rules(tmp_path, QUIET[shape])
