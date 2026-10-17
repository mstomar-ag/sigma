"""#1759: one resource mode resolver, the same answer on every surface, and the documented default."""
import importlib.util
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(LOOP))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import resource_guard as guard  # noqa: E402
import resource_liveness as live  # noqa: E402

loop = _load(LOOP / "loop.py", "loop_for_1759")
daemon = _load(LOOP / "watch_daemon.py", "daemon_for_1759")
logmod = _load(ROOT / "skills" / "sigma-log" / "scripts" / "log.py", "log_for_1759")


def _config(mode):
    return {} if mode == "absent" else {"resource": {"mode": mode}}


def _verify(config, env):
    old = os.environ.get("SIGMA_RESOURCE_MODE")
    os.environ.pop("SIGMA_RESOURCE_MODE", None)
    if "SIGMA_RESOURCE_MODE" in env:
        os.environ["SIGMA_RESOURCE_MODE"] = env["SIGMA_RESOURCE_MODE"]
    try:
        return "on" if loop._resource_verify_on(config) else "off"
    finally:
        os.environ.pop("SIGMA_RESOURCE_MODE", None)
        if old is not None:
            os.environ["SIGMA_RESOURCE_MODE"] = old


def _watcher(config, env, monkeypatch):
    monkeypatch.delenv("SIGMA_RESOURCE_MODE", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    calls = []
    daemon.run_call = lambda *a, **k: calls.append(a) or ""
    daemon.touch_heartbeat = lambda p: None
    daemon.resource_step(None, None, 1, config=config)
    return "on" if calls else "off"


def _log_open(config, env, tmp_path):
    e = dict(env, SIGMA_RESOURCE_DIR=str(tmp_path))
    (tmp_path / "hold.json").write_text('{"state": "hold", "since": 0, "reason": "memory"}')
    tail = logmod.admission_tail(config, now=600, env=e)
    return "off" if tail == "" else ("observe" if "observe" in tail else "held")


MODES = ["absent", None, "off", "observe", "enforce", "bogus"]
ENVS = [{}, {"SIGMA_RESOURCE_MODE": "off"}, {"SIGMA_RESOURCE_MODE": "observe"},
        {"SIGMA_RESOURCE_MODE": "enforce"}, {"SIGMA_RESOURCE_MODE": "junk"}]


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("mode", MODES)
def test_every_surface_agrees(mode, env, tmp_path, monkeypatch):
    config = _config(mode)
    want = guard.resolve_mode(config, env)
    assert live.effective_mode(config, env) == want
    assert guard.session_mode(config.get("resource"), env) == want
    assert _verify(config, env) == ("off" if want == "off" else "on")
    assert _watcher(config, env, monkeypatch) == ("off" if want == "off" else "on")
    seen = _log_open(config, env, tmp_path)
    assert seen == {"off": "off", "observe": "observe", "enforce": "held"}[want]


def test_precedence_env_over_config_over_default():
    assert guard.resolve_mode({"resource": {"mode": "off"}}, {"SIGMA_RESOURCE_MODE": "observe"}) == "observe"
    assert guard.resolve_mode({"resource": {"mode": "observe"}}, {}) == "observe"
    assert guard.resolve_mode({}, {}) == guard.DEFAULT_MODE


def test_picker_follows_resolver(monkeypatch):
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "off")
    assert loop._resource_gate({"resource": {"mode": "enforce"}}) is None
    assert loop._resource_verify_on({"resource": {"mode": "enforce"}}) is False


def test_enforcement_doc_states_resolver_default():
    text = (ROOT / "docs" / "enforcement.md").read_text()
    row = next(line for line in text.splitlines() if line.startswith("| Resource hold at pick"))
    assert ("`resource.mode: null` resolves to `%s`" % guard.DEFAULT_MODE) in row


def test_configuration_doc_covers_all_surfaces_and_not_stale():
    text = (ROOT / "docs" / "configuration.md").read_text()
    row = next(line for line in text.splitlines() if line.startswith("| env `SIGMA_RESOURCE_MODE`"))
    for surface in ("picker", "verify", "sessions", "watcher", "doctor", "log"):
        assert surface in row
    assert guard.DEFAULT_MODE in row
    assert "nothing calls it yet" not in text
