"""#1472: headless judge calls run away from the caller's project."""
import importlib.util
import json
import os
import pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


fj = _mod("feature_judge")
ji = _mod("judge_isolation")
launcher = _mod("feature_upkeep_launcher")

HELP_ALL = "--setting-sources <s>\n--strict-mcp-config\n--no-session-persistence\n"


class _Proc:
    returncode = 0

    def communicate(self, timeout=None):
        return json.dumps({"total_cost_usd": 0.01, "structured_output": {"a": 1}}), ""


def _capture(monkeypatch, help_text=HELP_ALL):
    seen = {}

    def popen(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        seen["existed"] = os.path.isdir(kw.get("cwd") or "")
        return _Proc()
    monkeypatch.setattr(fj.subprocess, "Popen", popen)
    monkeypatch.setattr(fj.judge_isolation, "cli_help_text", lambda: help_text)
    return seen


def test_cwd_is_a_fresh_clean_temp_dir_removed_after(monkeypatch):
    seen = _capture(monkeypatch)
    payload, _cost = fj.ask_claude("p", {"type": "object"})
    cwd = pathlib.Path(seen["kw"]["cwd"])
    assert payload == {"a": 1} and seen["existed"]
    assert cwd.name.startswith("sigma-judge-")
    assert not cwd.exists()
    for d in [cwd] + list(cwd.parents):
        for name in ji.INSTRUCTION_FILES:
            assert not (d / name).exists()


def test_ancestor_instruction_file_is_refused_without_a_launch(monkeypatch, tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x")
    monkeypatch.setattr(fj.tempfile, "tempdir", str(tmp_path))
    seen = _capture(monkeypatch)
    assert fj.ask_claude("p", {"type": "object"}) == (None, 0.0)
    assert "kw" not in seen
    assert list(tmp_path.iterdir()) == [tmp_path / "CLAUDE.md"]


def test_project_dir_variable_is_dropped_from_the_child_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/some/project")
    seen = _capture(monkeypatch)
    fj.ask_claude("p", {"type": "object"})
    assert "CLAUDE_PROJECT_DIR" not in seen["kw"]["env"]
    assert "PATH" in seen["kw"]["env"]


def test_isolation_flags_only_when_the_help_lists_them(monkeypatch):
    seen = _capture(monkeypatch)
    fj.ask_claude("p", {"type": "object"})
    argv = seen["argv"]
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert "--bare" not in argv
    seen = _capture(monkeypatch, help_text="--strict-mcp-config\n")
    fj.ask_claude("p", {"type": "object"})
    assert "--strict-mcp-config" in seen["argv"]
    assert "--setting-sources" not in seen["argv"] and "--no-session-persistence" not in seen["argv"]
    seen = _capture(monkeypatch, help_text="")
    fj.ask_claude("p", {"type": "object"})
    assert not {"--setting-sources", "--strict-mcp-config", "--no-session-persistence"} & set(seen["argv"])


def test_probe_with_absent_cli_yields_no_flags(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("claude")
    monkeypatch.setattr(ji.subprocess, "run", boom)
    ji._HELP_CACHE.clear()
    try:
        assert ji.isolation_argv() == []
    finally:
        ji._HELP_CACHE.clear()


def test_launcher_refuse_ancestors_is_the_shared_one(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x")
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    assert launcher.refuse_ancestors(child) == ji.refuse_ancestors(child)
    assert "CLAUDE.md" in launcher.refuse_ancestors(child)
    assert launcher.INSTRUCTION_FILES == ji.INSTRUCTION_FILES
