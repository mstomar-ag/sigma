"""#1224: the session-start report of units upkeep will rebase. Read-only, fail-silent, host-agnostic."""
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


def due(m, cfg, sdlc, units, behind, **kw):
    deps = {"list_units": lambda _s: units, "measure": lambda n, _d: T.drift_of(m, behind[n])}
    return m.due_units(cfg, sdlc, now=NOW, deps=deps, **kw)


def test_due_unit_carries_arrivals_age_and_reason(tmp_path):
    m, sdlc = T.sched(), T.project(tmp_path)
    got = due(m, T.open_config(), sdlc, ["a", "b"], {"a": 4, "b": 9})
    assert [d["unit"] for d in got["due"]] == ["b", "a"]
    assert got["due"][0]["behind"] == 9 and got["due"][0]["base"] == "main"
    assert got["due"][0]["last_success_age"] is None and got["due"][0]["reason"] == "drift"
    assert got["unknown"] == []


def test_age_of_last_success_is_reported(tmp_path):
    m, sdlc = T.sched(), T.project(tmp_path)
    state = m._sibling("feature_upkeep_state")
    state.record_outcome(sdlc, "a", NOW - 10_000_000, "rebased")
    got = due(m, T.open_config(), sdlc, ["a"], {"a": 5})
    assert got["due"] and got["due"][0]["last_success_age"] == 10_000_000


def test_not_behind_is_not_due(tmp_path):
    m, sdlc = T.sched(), T.project(tmp_path)
    assert due(m, T.open_config(), sdlc, ["a"], {"a": 0})["due"] == []


def test_cooldown_unit_is_not_measured_or_reported(tmp_path):
    m, sdlc = T.sched(), T.project(tmp_path)
    assert m._sibling("feature_upkeep_state").record_attempt(sdlc, "a", NOW - 5, "backstop").ok
    asked = []
    deps = {"list_units": lambda _s: ["a"], "measure": lambda n, d: asked.append(n)}
    got = m.due_units(T.open_config(), sdlc, now=NOW, deps=deps)
    assert asked == [] and got["due"] == []


def test_gate_closed_measures_nothing(tmp_path):
    m, sdlc = T.sched(), T.project(tmp_path)
    cfg = T.open_config()
    cfg["upkeep"]["enabled"] = False
    asked = []
    deps = {"list_units": lambda _s: asked.append("list") or ["a"], "measure": lambda n, d: asked.append(n)}
    got = m.due_units(cfg, sdlc, now=NOW, deps=deps)
    assert got == {"due": [], "unknown": [], "out_of_time": False} and asked == []


def test_unmeasurable_unit_is_unknown_not_due(tmp_path):
    m, sdlc = T.sched(), T.project(tmp_path)
    unknown = m._sibling("feature_upkeep_drift").UNKNOWN
    got = due(m, T.open_config(), sdlc, ["a", "b"], {"a": unknown, "b": 3})
    assert got["unknown"] == ["a"] and [d["unit"] for d in got["due"]] == ["b"]


def test_the_budget_is_honoured(tmp_path):
    m, sdlc = T.sched(), T.project(tmp_path)
    clock = {"t": 0.0}

    def measure(n, _d):
        clock["t"] += 10
        return T.drift_of(m, 5)

    deps = {"list_units": lambda _s: ["a", "b", "c"], "measure": measure}
    got = m.due_units(T.open_config(), sdlc, now=NOW, budget=15, clock=lambda: clock["t"], deps=deps)
    assert got["out_of_time"] is True and [d["unit"] for d in got["due"]] == ["a", "b"]


def test_it_writes_nothing(tmp_path):
    m, sdlc = T.sched(), T.project(tmp_path)
    before = tree(sdlc)
    due(m, T.open_config(), sdlc, ["a"], {"a": 5})
    assert tree(sdlc) == before


def _session_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("upkeep_session_1224", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hookless_call_prints_three_lines_then_a_count(tmp_path):
    mod, m, sdlc = _session_module(), T.sched(), T.project(tmp_path)
    deps = {"list_units": lambda _s: list("abcde"), "measure": lambda n, _d: T.drift_of(m, 6)}
    real = mod._load
    mod._load = lambda name: m if name == "feature_upkeep_sched" else real(name)
    lines = mod.session_lines(sdlc, now=NOW, deps=deps)
    assert len(lines) == 4 and lines[0] == "unit a is 6 merges behind main; upkeep will rebase it", lines
    assert lines[3] == "5 units due for upkeep."


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
    stub = "print('unit x is 6 merges behind main; upkeep will rebase it')\nprint('1 unit due for upkeep.')\n"
    for session_start in (False, True):
        proj = _project(tmp_path / str(session_start), session_start)
        out = _run(_plugin(tmp_path / str(session_start), stub), proj)
        doc = json.loads(out)
        assert "unit x is 6 merges behind main; upkeep will rebase it" in doc["hookSpecificOutput"]["additionalContext"]
