"""#1913: doctor reports the plugin install scopes and whether the resource guard is present.

Controls (each clause broken alone, then restored):
  1. the scope comparison: differing versions WARN, same versions do not, and the older scope is
     found by version tuple (1.0.9 is older than 1.0.10), not by plain membership or by text order;
  2. the guard check: a copy of the plugin root without resource_guard.py FAILs through the doctor
     gesture the docs give (`python3 .../doctor.py check .sdlc`);
  3. a missing registry is UNKNOWN, never OK;
  4. the session hook prints its line only when the guard is absent.
Nothing here reads the real home registry: every registry is a tmp file, and every subprocess env
carries the session resource mode through tests/resource_mode.py.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import resource_mode  # noqa: E402
import test_doctor as td  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUARD_REL = pathlib.Path("skills") / "sigma-loop" / "scripts" / "resource_guard.py"


def _registry(tmp_path, entries):
    p = tmp_path / "installed_plugins.json"
    p.write_text(json.dumps({"version": 2, "plugins": {"sigmaloop@sigmaloop": [
        {"scope": s, "version": v, "installPath": ip} for s, v, ip in entries]}}))
    return p


def _rows(path):
    return td._doc()._plugin_scope_rows(path)


def test_one_line_per_scope_with_scope_version_and_path(tmp_path):
    rows = _rows(_registry(tmp_path, [("user", "1.0.17", "/plug/a"), ("project", "1.0.17", "/plug/b")]))
    names = [r["name"] for r in rows]
    assert names == ["plugin scope: user 1.0.17 (/plug/a)", "plugin scope: project 1.0.17 (/plug/b)"]
    assert all(r["ok"] for r in rows)


def test_differing_versions_warn_naming_both_and_the_older_scopes_update(tmp_path):
    rows = _rows(_registry(tmp_path, [("user", "1.0.10", "/a"), ("project", "1.0.9", "/b")]))
    warn = [r for r in rows if r.get("level") == "WARN"]
    assert len(warn) == 1 and warn[0]["ok"] is False
    text = warn[0]["name"] + warn[0]["fix"]
    assert "1.0.9" in text and "1.0.10" in text
    assert "claude plugin update sigmaloop@sigmaloop --scope project" in warn[0]["fix"]


def test_same_versions_do_not_warn(tmp_path):
    rows = _rows(_registry(tmp_path, [("user", "1.0.17", "/a"), ("project", "1.0.17", "/b")]))
    assert not [r for r in rows if r.get("level") == "WARN"]


def test_missing_registry_is_unknown_never_ok(tmp_path):
    rows = _rows(tmp_path / "absent.json")
    assert len(rows) == 1 and rows[0]["level"] == "UNKNOWN" and "OK" not in rows[0]["level"]


@pytest.mark.parametrize("body", ["{not json", "[]", '{"plugins": {}}', '{"plugins": {"other@x": []}}'])
def test_garbled_or_empty_registry_is_unknown(tmp_path, body):
    p = tmp_path / "r.json"
    p.write_text(body)
    rows = _rows(p)
    assert len(rows) == 1 and rows[0]["level"] == "UNKNOWN" and "OK" not in rows[0]["level"]


def test_check_carries_the_scope_rows_and_guard_row(tmp_path):
    d = td._doc()
    base = td._sdlc(tmp_path / "repo", {})
    reg = _registry(tmp_path, [("user", "1.0.10", "/a"), ("project", "1.0.9", "/b")])
    names = [c["name"] for c in d.check(base, run=td._runner(), installed_plugins_path=reg)]
    assert "plugin scope: user 1.0.10 (/a)" in names
    assert "resource guard present" in names


def test_guard_present_ok_in_this_tree():
    row = td._doc()._resource_guard_row()
    assert row["ok"] is True and row["level"] == "OK"


def test_guard_absent_fails_naming_first_version(tmp_path):
    row = td._doc()._resource_guard_row(tmp_path)
    assert row["ok"] is False and row["level"] == "FAIL"
    assert "1.0.16" in row["fix"]


def _copy_plugin(tmp_path, with_guard):
    dest = tmp_path / "plugin"
    for rel in ("skills/sigma-doctor", "skills/sigma-loop", "hooks"):
        shutil.copytree(ROOT / rel, dest / rel, ignore=shutil.ignore_patterns("__pycache__"))
    if not with_guard:
        (dest / GUARD_REL).unlink()
    return dest


def test_documented_doctor_gesture_fails_on_a_copy_without_the_guard(tmp_path):
    """The gesture skills/sigma-doctor/SKILL.md gives: `python3 <skill>/scripts/doctor.py check .sdlc`."""
    plugin = _copy_plugin(tmp_path, with_guard=False)
    project = tmp_path / "proj"
    (project / ".sdlc").mkdir(parents=True)
    (project / ".sdlc" / "config.json").write_text("{}")
    env = resource_mode.carry({"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
                               "CLAUDE_CONFIG_DIR": str(tmp_path / "cc")})
    r = subprocess.run([sys.executable, str(plugin / "skills/sigma-doctor/scripts/doctor.py"),
                        "check", ".sdlc"], cwd=project, env=env, capture_output=True, text=True,
                       timeout=170)
    line = [ln for ln in r.stdout.splitlines() if "resource guard present" in ln]
    assert line and line[0].lstrip().startswith("[FAIL]") and "1.0.16" in line[0], r.stdout[-800:]


def _hook(tmp_path, plugin):
    project = tmp_path / "p2"
    project.mkdir(exist_ok=True)
    env = resource_mode.carry({k: v for k, v in os.environ.items() if k != "SIGMA_RUN_ID"})
    env["CLAUDE_PROJECT_DIR"] = str(project)
    return subprocess.run(["bash", str(plugin / "hooks" / "session_start.sh")], input="{}",
                          capture_output=True, text=True, env=env, timeout=60).stdout


def test_hook_prints_one_line_only_when_guard_absent(tmp_path):
    (tmp_path / "a").mkdir()
    absent = _hook(tmp_path / "a", _copy_plugin(tmp_path / "a", with_guard=False))
    assert "/sigma-doctor" in absent and "resource guard" in absent
    (tmp_path / "b").mkdir()
    present = _hook(tmp_path / "b", _copy_plugin(tmp_path / "b", with_guard=True))
    assert "resource guard" not in present
