"""#1224, #1387: the session-start window line (read-only, fail-silent, host-agnostic) and the sweep start."""
import copy
import json
import os
import pathlib
import shutil
import subprocess
import sys

import test_upkeep_sched as T
import upkeep_support as S

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "session_start.sh"
SCRIPT = ROOT / "skills" / "sigma-loop" / "scripts" / "upkeep_session.py"
NOW = 100_000_000


def tree(sdlc):
    return sorted((str(p.relative_to(sdlc)), p.read_bytes() if p.is_file() else None) for p in pathlib.Path(sdlc).rglob("*"))


def _session_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("upkeep_session_1224", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hookless_call_prints_one_window_line_and_reads_only(tmp_path):
    mod, sdlc = _session_module(), T.project(tmp_path)
    assert mod.session_lines(sdlc, now=NOW) == ["upkeep: no sweep yet on this machine; the window is open"]
    window = mod._load("feature_upkeep_window")
    assert window.stamp(sdlc, NOW - 3600, "swept")
    before = tree(sdlc)
    assert mod.session_lines(sdlc, now=NOW) == ["upkeep: last sweep 1h00m ago, next window opens in 11h00m"]
    assert tree(sdlc) == before


def test_entry_point_is_silent_when_gate_closed_or_broken(tmp_path):
    cfg = T.open_config()
    cfg["upkeep"]["enabled"] = False
    sdlc = T.project(tmp_path, cfg)
    for target in (sdlc, str(tmp_path / "missing")):
        p = subprocess.run([sys.executable, str(SCRIPT), "lines", target], capture_output=True, text=True, timeout=60)
        assert p.returncode == 0 and p.stdout == "" and p.stderr == "", (p.stdout, p.stderr)
    assert "usage" in subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True).stdout


def test_entry_point_failure_prints_nothing(tmp_path):
    mod, sdlc = _session_module(), T.project(tmp_path)
    mod._load = lambda name: (_ for _ in ()).throw(RuntimeError("boom"))
    assert mod.session_lines(sdlc) == []


# ------------------------------------------------------------------------------------------ the hook

def _plugin(tmp_path, stub):
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    shutil.copy(HOOK, root / "hooks" / "session_start.sh")
    for rel in ("skills/sigma-loop/scripts",):
        shutil.copytree(ROOT / rel, root / rel)
    if stub is not None:
        (root / "skills/sigma-loop/scripts/upkeep_session.py").write_text(stub)
    return root


def _run(root, project):
    env = {k: v for k, v in os.environ.items() if k != "SIGMA_RUN_ID"}
    env.update(CLAUDE_PROJECT_DIR=str(project), SIGMA_ALLOW_COEXIST="1")
    p = subprocess.run(["bash", str(root / "hooks" / "session_start.sh")], cwd=project, capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    return p.stdout


def _project(tmp_path, session_start):
    proj = tmp_path / "proj"
    (proj / ".sdlc").mkdir(parents=True)
    cfg = {"session_start": {"enabled": True}} if session_start else {}
    (proj / ".sdlc" / "config.json").write_text(json.dumps(cfg))
    return proj


def test_hook_output_is_byte_identical_when_nothing_is_due(tmp_path):
    silent = "import sys\nsys.exit(0)\n"
    for session_start in (False, True):
        proj = _project(tmp_path / str(session_start), session_start)
        with_tier = _run(_plugin(tmp_path / str(session_start), silent), proj)
        without = _run(_plugin(tmp_path / ("x%s" % session_start), "raise SystemExit(3)\n"), proj)
        assert with_tier == without


def test_hook_failure_in_the_script_is_silent(tmp_path):
    proj = _project(tmp_path, False)
    assert _run(_plugin(tmp_path, "raise SystemExit(3)\n"), proj) == ""


def test_hook_adds_the_lines_in_one_blob(tmp_path):
    stub = "print('upkeep: last sweep 1h00m ago, next window opens in 11h00m')\n\n"
    for session_start in (False, True):
        proj = _project(tmp_path / str(session_start), session_start)
        out = _run(_plugin(tmp_path / str(session_start), stub), proj)
        doc = json.loads(out)
        assert "next window opens in 11h00m" in doc["hookSpecificOutput"]["additionalContext"]
