"""#1759 - one resource mode resolver for every surface, and the documented default is the resolver's.

Hermetic: no model, network or gh call. Control: replace `resolve_mode`'s body (or one surface's own rule) with a
different precedence and the table goes red on that surface.
"""
import importlib.util
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import resource_guard as guard  # noqa: E402
import resource_liveness as live  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: (env value or None, config value or None, expected mode)
TABLE = [
    (None, None, "enforce"),            # the true default
    (None, "off", "off"),
    (None, "observe", "observe"),
    (None, "enforce", "enforce"),
    ("off", None, "off"),
    ("observe", "enforce", "observe"),  # the environment wins over the config key
    ("enforce", "off", "enforce"),
    ("bogus", "observe", "observe"),    # an invalid environment value falls through to the config key
    ("bogus", None, "enforce"),
    (None, "bogus", "enforce"),
    ("", "off", "off"),
]
SURFACES = ("resolver", "sessions", "liveness", "picker", "verify", "shed", "watcher", "doctor", "log")


def _config(value):
    return {} if value is None else {"resource": {"mode": value}, "parallel": {"goals": {"enabled": True}}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "hold.json").write_text(json.dumps({"state": "hold", "since": 1.0, "reason": "memory"}))
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(state))
    monkeypatch.delenv("SIGMA_RESOURCE_MODE", raising=False)
    return tmp_path


def _shed_mode(config, home, monkeypatch):
    """#1794: the mode the verify shed acts on: enforce when it creates its stop file, else off (observe/off)."""
    rv = _load(SCRIPTS / "resource_verify.py", "resource_verify_1794")
    lease = rv._sibling("resource_lease")
    made = []
    real = os.path.join
    monkeypatch.setattr(lease, "take_sample", lambda: guard.Sample(
        total=32000, available=20000, swap_used=0, swap_total=0, swapouts=0, swapouts_at=0.0, pressure=1,
        cpu_count=8, load1=0.0))
    monkeypatch.setattr(rv, "_Meter", lambda lease_, id_, gap, shed: made.append(shed) or type(
        "M", (), {"start": lambda s: None, "stop": lambda s, *a, **k: None, "__getattr__": lambda s, n: (lambda *a, **k: None)})())
    try:
        rv.run("verify", "true", str(home), 30, "g1", config=config)
    except Exception:                                    # noqa: BLE001 - only whether a shed was armed matters
        pass
    return "enforce" if made and made[0] is not None else "off"


def _probe(surface, env_value, cfg_value, home, monkeypatch):
    """-> the mode the surface acts on, read the way that surface exposes it."""
    config = _config(cfg_value)
    env = {} if env_value is None else {"SIGMA_RESOURCE_MODE": env_value}
    if env_value is not None:
        monkeypatch.setenv("SIGMA_RESOURCE_MODE", env_value)
    if surface == "resolver":
        return guard.resolve_mode(config, env)
    if surface == "sessions":
        return guard.session_mode(config, env)
    if surface == "liveness":
        return live.effective_mode(config, env)
    if surface == "verify":
        loop = _load(SCRIPTS / "loop.py", "loop_1759")
        return "enforce" if loop._resource_verify_on(config) else "off"
    if surface == "shed":
        return _shed_mode(config, home, monkeypatch)
    if surface == "picker":
        loop = _load(SCRIPTS / "loop.py", "loop_1759p")
        seen = []
        real = guard.resolve_mode
        monkeypatch.setattr(guard, "resolve_mode", lambda *a, **k: seen.append(real(*a, **k)) or seen[-1])
        try:
            loop._resource_gate(config)
        except Exception:                                 # noqa: BLE001 - only the mode it resolved matters here
            pass
        return seen[0] if seen else "off"
    if surface == "watcher":
        wd = _load(SCRIPTS / "watch_daemon.py", "watch_daemon_1759")
        calls = []
        wd.run_call = lambda *a, **k: calls.append(a) or ""
        wd.touch_heartbeat = lambda p: None
        wd.resource_step(None, str(home / ".sdlc"), "120", config=config)
        return "enforce" if calls else "off"
    if surface == "doctor":
        doctor = _load(ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py", "doctor_1759")
        sample = guard.Sample(total=32000, available=20000, swap_used=0, swap_total=0, swapouts=0, swapouts_at=0.0,
                              pressure=1, cpu_count=8, load1=0.0)
        base = home / ".sdlc"
        base.mkdir(exist_ok=True)
        cfg = dict(config, parallel={"goals": {"enabled": True}})
        rows = doctor._resource_rows(cfg, str(base), now=1000.0, sampler=lambda: sample, env=env)
        names = [r["name"] for r in rows if r["name"].startswith("resource mode: ")]
        return names[0].split(": ", 1)[1] if names else "?"
    if surface == "log":
        log = _load(ROOT / "skills" / "sigma-log" / "scripts" / "log.py", "log_1759")
        tail = log.admission_tail(config, now=10000.0, env=dict(env, SIGMA_RESOURCE_DIR=os.environ["SIGMA_RESOURCE_DIR"]))
        if tail == "":
            return "off"
        return "observe" if "observe: not enforced" in tail else "enforce"
    raise AssertionError(surface)


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("env_value,cfg_value,expected", TABLE)
def test_every_surface_resolves_the_same_mode(surface, env_value, cfg_value, expected, home, monkeypatch):
    if surface == "verify" and expected == "observe":
        expected = "enforce"                              # verify's gate is on/off: observe is "on"
    if surface == "shed" and expected == "observe":
        expected = "off"                                  # #1794: observe only logs, the shed must not act
    if surface == "watcher" and expected == "observe":
        expected = "enforce"
    assert _probe(surface, env_value, cfg_value, home, monkeypatch) == expected


def test_the_documented_default_is_the_resolvers_default():
    default = guard.resolve_mode({}, {})
    assert default == guard.DEFAULT_MODE
    enforcement = (ROOT / "docs" / "enforcement.md").read_text(encoding="utf-8")
    row = next(line for line in enforcement.splitlines() if line.startswith("| Resource hold at pick"))
    assert "resolves to `%s` on every surface" % default in row
    assert "| %s — " % default in row, "the default column must lead with the resolver default"
    assert "resource_guard.resolve_mode" in row


def test_configuration_doc_names_every_surface_and_drops_the_stale_sentence():
    text = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| env `SIGMA_RESOURCE_MODE`"))
    for surface in ("picker", "verify", "sessions", "watcher", "doctor", "log"):
        assert surface in row
    assert "resolves to `%s`" % guard.DEFAULT_MODE in row
    assert "nothing calls" not in text.lower()
    assert "yet" not in next(line for line in text.splitlines() if "resource_lease.py" in line).split("POSIX only")[1][:200]
