"""#1603: every Sigma-spawned model session takes a lease from resource_guard.session_lease and gives it back.

The three spawn sites (the judge, the supervisor, the upkeep launcher) share ONE helper; a ratchet fails when a
file that builds a host-CLI spawn is neither gated nor listed with a reason. No network, no real model call:
every child is a stub.
"""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

TOTAL = 16 << 30


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def leases(state):
    root = pathlib.Path(state) / "leases"
    return sorted(root.glob("*.json")) if root.is_dir() else []


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / "resource-state"
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(path))
    monkeypatch.delenv("SIGMA_RESOURCE_MODE", raising=False)
    return path


def with_sample(guard, available=TOTAL // 2):
    """Give the helper's lease module a fixed machine sample so admission does not depend on this host."""
    lease = guard._lease()
    lease.take_sample = lambda: lease.guard.Sample(
        total=TOTAL, available=available, swap_used=0, swap_total=0, swapouts=0, swapouts_at=0.0, pressure=1,
        cpu_count=8, load1=0.0)
    lease.take_footprint = lambda pid: None
    return lease


class FakeLease:
    """Stands in for resource_lease: holds `holds` times, then grants."""
    def __init__(self, holds=0):
        self.holds, self.calls, self.released = holds, 0, []

    def admit(self, kind, want, repo, goal, **kw):
        self.calls += 1
        if self.calls <= self.holds:
            return type("Hold", (), {"reason": "pressure: new work is held", "retry_after": 0.0})()
        return type("Grant", (), {"n": 1, "id": "fake%d" % self.calls})()

    def release(self, lease_id, env=None):
        self.released.append(lease_id)
        return True


def is_hold(value):
    return hasattr(value, "reason")


# ------------------------------------------------------------------------------------------ the helper
def test_mode_off_takes_no_lease_and_writes_nothing(state):
    guard = _load("resource_guard", "rg_t")
    with guard.session_lease("judge", "1", config={"mode": "off"}) as got:
        assert got is None
    assert not state.exists()


def test_unset_mode_is_off(state):
    guard = _load("resource_guard", "rg_t")
    with guard.session_lease("judge", "1") as got:
        assert got is None
    assert not state.exists()


def test_lease_is_held_inside_and_gone_after_even_on_exception(state):
    guard = _load("resource_guard", "rg_t")
    with_sample(guard)
    with guard.session_lease("judge", "1", config={"mode": "enforce"}) as got:
        assert not is_hold(got) and len(leases(state)) == 1
    assert leases(state) == []
    with pytest.raises(RuntimeError):
        with guard.session_lease("judge", "1", config={"mode": "observe"}):
            assert len(leases(state)) == 1
            raise RuntimeError("child blew up")
    assert leases(state) == []


def test_enforce_hold_is_returned_not_raised(state):
    guard = _load("resource_guard", "rg_t")
    with_sample(guard, available=1 << 20)
    with guard.session_lease("judge", "1", config={"mode": "enforce"}) as got:
        assert is_hold(got) and got.reason
    assert leases(state) == []


def test_env_mode_is_read(state, monkeypatch):
    guard = _load("resource_guard", "rg_t")
    with_sample(guard)
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "enforce")
    with guard.session_lease("judge", "1") as got:
        assert not is_hold(got) and got is not None and len(leases(state)) == 1


# ------------------------------------------------------------------------------------------ the judge
@pytest.fixture
def judge():
    return _load("feature_judge", "fj_t")


def test_judge_holds_a_lease_while_the_child_runs(state, judge, monkeypatch):
    with_sample(judge._guard())
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "observe")
    seen = []

    def child(argv, timeout=60, cwd=None, env=None):
        seen.append(len(leases(state)))
        return judge._Done(json.dumps({"result": "{}", "subtype": "success"}), 0)
    monkeypatch.setattr(judge, "_run_group", child)
    judge.ask_claude("p", {}, timeout_s=5)
    assert seen == [1] and leases(state) == []


@pytest.mark.parametrize("failure", [RuntimeError("boom"), subprocess.TimeoutExpired("claude", 1)])
def test_judge_releases_after_exception_and_timeout(state, judge, monkeypatch, failure):
    with_sample(judge._guard())
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "observe")

    def child(*a, **k):
        assert len(leases(state)) == 1
        raise failure
    monkeypatch.setattr(judge, "_run_group", child)
    assert judge.ask_claude("p", {}, timeout_s=5) == (None, 0.0)
    assert leases(state) == []


def test_judge_hold_returns_the_refusal_with_the_reason(state, judge, monkeypatch):
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "enforce")
    with_sample(judge._guard(), available=1 << 20)
    monkeypatch.setattr(judge, "_run_group", lambda *a, **k: pytest.fail("child must not start while held"))
    reasons = []
    assert judge.ask_claude("p", {}, timeout_s=5, reason_out=reasons) == (None, 0.0)
    assert len(reasons) == 1 and "critical" in reasons[0]


def test_judge_mode_off_creates_no_state(state, judge, monkeypatch):
    monkeypatch.setattr(judge, "_run_group", lambda *a, **k: judge._Done("{}", 0))
    judge.ask_claude("p", {}, timeout_s=5)
    assert not state.exists()


# ------------------------------------------------------------------------------------------ the supervisor
@pytest.fixture
def daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGMA_HOST", "claude")
    monkeypatch.setenv("SIGMA_SUPERVISE_SLEEP_SCALE", "0")
    return _load("supervise_daemon", "sd_t")


def stub_child(tmp_path, seen_file, state):
    script = tmp_path / "fake-claude.py"
    script.write_text(
        "import pathlib, sys\n"
        "root = pathlib.Path(%r) / 'leases'\n"
        "n = len(list(root.glob('*.json'))) if root.is_dir() else 0\n"
        "pathlib.Path(%r).write_text(str(n))\n"
        "print('LOOP STOP: backlog-empty')\n" % (str(state), str(seen_file)))
    return "%s %s" % (sys.executable, script)


def test_supervisor_holds_a_lease_while_the_child_runs(state, daemon, monkeypatch, tmp_path):
    with_sample(daemon._guard())
    seen = tmp_path / "seen"
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "observe")
    monkeypatch.setenv("SIGMA_CLAUDE_CMD", stub_child(tmp_path, seen, state))
    monkeypatch.setenv("SIGMA_SUPERVISE_MAX_RUNS", "1")
    (tmp_path / ".sdlc").mkdir()
    assert daemon.main(["x", str(tmp_path / ".sdlc")]) == 0
    assert seen.read_text() == "1" and leases(state) == []


def test_supervisor_releases_when_the_launch_fails(state, daemon, monkeypatch, tmp_path):
    with_sample(daemon._guard())
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "observe")
    monkeypatch.setenv("SIGMA_CLAUDE_CMD", "definitely-not-a-real-binary-xyz")
    monkeypatch.setenv("SIGMA_SUPERVISE_MAX_RUNS", "1")
    (tmp_path / ".sdlc").mkdir()
    daemon.main(["x", str(tmp_path / ".sdlc")])
    assert leases(state) == []


def test_supervisor_hold_pauses_then_relaunches(state, daemon, monkeypatch, tmp_path):
    fake = FakeLease(holds=1)
    monkeypatch.setattr(daemon._guard(), "_lease", lambda: fake)
    seen = tmp_path / "seen"
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "enforce")
    monkeypatch.setenv("SIGMA_CLAUDE_CMD", stub_child(tmp_path, seen, state))
    monkeypatch.setenv("SIGMA_SUPERVISE_MAX_RUNS", "3")
    (tmp_path / ".sdlc").mkdir()
    assert daemon.main(["x", str(tmp_path / ".sdlc")]) == 0
    log = (tmp_path / ".sdlc" / "state" / "supervisor.log").read_text()
    assert "session held" in log and "pressure: new work is held" in log
    assert fake.calls == 2 and fake.released == ["fake2"] and seen.exists()


def test_supervisor_mode_off_creates_no_state(state, daemon, monkeypatch, tmp_path):
    seen = tmp_path / "seen"
    monkeypatch.setenv("SIGMA_CLAUDE_CMD", stub_child(tmp_path, seen, state))
    monkeypatch.setenv("SIGMA_SUPERVISE_MAX_RUNS", "1")
    (tmp_path / ".sdlc").mkdir()
    assert daemon.main(["x", str(tmp_path / ".sdlc")]) == 0
    assert not state.exists()


# ------------------------------------------------------------------------------------------ the upkeep launcher
@pytest.fixture
def rig(tmp_path, monkeypatch):
    import test_upkeep_launcher as tul
    monkeypatch.delenv("SIGMA_UPKEEP_JOB", raising=False)
    return tul.Rig(tmp_path, monkeypatch)


def with_resource(rig, mode):
    request = rig.request()
    config = dict(request.config, resource={"mode": mode})
    return rig.request(config=config)


def spy_child(rig, state, seen, failure=None):
    bounded = rig.launcher._sibling("bounded_run")
    real = bounded.run_group

    def run(*a, **k):
        if "stdin_path" not in k:               # the login probe also goes through run_group; only the session counts
            return real(*a, **k)
        seen.append(len(leases(state)))
        if failure is not None:
            raise failure
        return real(*a, **k)
    bounded.run_group = run


def test_upkeep_holds_a_lease_while_the_child_runs(state, rig):
    with_sample(rig.launcher._guard())
    seen = []
    spy_child(rig, state, seen)
    result = rig.launcher.launch(with_resource(rig, "observe"))
    assert result.outcome == "ok", result
    assert seen == [1] and leases(state) == []


@pytest.mark.parametrize("failure", [RuntimeError("boom"), subprocess.TimeoutExpired("claude", 1)])
def test_upkeep_releases_after_exception_and_timeout(state, rig, failure):
    with_sample(rig.launcher._guard())
    seen = []
    spy_child(rig, state, seen, failure)
    result = rig.launcher.launch(with_resource(rig, "observe"))
    assert result.outcome == "killed" and seen == [1] and leases(state) == []


def test_upkeep_hold_defers_the_unit_without_spend(state, rig, monkeypatch):
    fake = FakeLease(holds=9)
    monkeypatch.setattr(rig.launcher._guard(), "_lease", lambda: fake)
    result = rig.launcher.launch(with_resource(rig, "enforce"))
    assert result.outcome == "refused" and "pressure: new work is held" in result.reason
    assert result.charged_usd == 0.0 and rig.marker() is None


def test_upkeep_mode_off_creates_no_state(state, rig):
    result = rig.launcher.launch(rig.request())
    assert result.outcome == "ok" and not state.exists()


# ------------------------------------------------------------------------------------------ one helper, no re-implementation
GATED = ("feature_judge.py", "supervise_daemon.py", "feature_upkeep_launcher.py")


@pytest.mark.parametrize("name", GATED)
def test_each_site_uses_the_shared_helper_and_never_touches_the_table(name):
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "session_lease(" in text
    assert "resource_lease" not in text
    assert not re.search(r"\.(admit|release)\(", text)


# ------------------------------------------------------------------------------------------ ratchet
#: argv builders of the host CLI. A file containing one is a spawn site (or builds one for a caller).
SPAWN_SHAPES = (
    re.compile(r'"claude",\s*"-p"'),
    re.compile(r'"claude -p /'),
    re.compile(r'BASE_ARGV\s*=\s*\("-p"'),
    re.compile(r'\[str\(executable\),\s*"exec"'),
    re.compile(r'\["cursor-agent",\s*"-p"'),
    re.compile(r'\["codex",\s*"exec"'),
)
#: spawn files that take no lease, each with the reason and the follow-up. The list only goes down.
NOT_GATED = {
    "codex_runtime.py": "builds the Codex argv only; the spawn is supervise_daemon._capture_codex, which is gated",
    "reviewer.py": "advisory reviewer spawn (cursor/codex), outside #1603's three sites; follow-up",
}


def spawn_files(directory):
    found = set()
    for path in pathlib.Path(directory).glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(shape.search(text) for shape in SPAWN_SHAPES):
            found.add(path.name)
    return found


def ungated(directory):
    return sorted(n for n in spawn_files(directory)
                  if n not in NOT_GATED and "session_lease(" not in (pathlib.Path(directory) / n).read_text("utf-8"))


def test_every_spawn_file_is_gated_or_listed_with_a_reason():
    assert ungated(SCRIPTS) == []
    assert set(GATED) <= spawn_files(SCRIPTS)
    for name in NOT_GATED:
        assert name in spawn_files(SCRIPTS), "stale NOT_GATED entry: %s" % name


def test_ratchet_goes_red_for_an_ungated_spawn(tmp_path):
    (tmp_path / "newspawn.py").write_text('argv = ["claude", "-p", "hello"]\n')
    assert ungated(tmp_path) == ["newspawn.py"]


# ------------------------------------------------------------------ #1737: the supervisor lease outlives a long session
def _spy_guard(daemon, monkeypatch):
    calls = []

    class Spy:
        def session_lease(self, kind, goal, **kw):
            calls.append(kw)
            import contextlib
            return contextlib.nullcontext(None)
    monkeypatch.setattr(daemon, "_guard", lambda: Spy())
    return calls


def test_supervisor_passes_its_ttl_at_the_claude_site(daemon, monkeypatch, tmp_path):
    calls = _spy_guard(daemon, monkeypatch)
    monkeypatch.setenv("SIGMA_CLAUDE_CMD", "%s -c pass" % sys.executable)
    monkeypatch.setenv("SIGMA_SUPERVISE_MAX_RUNS", "1")
    (tmp_path / ".sdlc").mkdir()
    daemon.main(["x", str(tmp_path / ".sdlc")])
    assert calls and all(c.get("ttl") == daemon.SESSION_LEASE_TTL for c in calls)


def test_supervisor_passes_its_ttl_at_the_codex_site(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGMA_HOST", "codex")
    monkeypatch.setenv("SIGMA_SUPERVISE_SLEEP_SCALE", "0")
    daemon = _load("supervise_daemon", "sd_t2")
    calls = _spy_guard(daemon, monkeypatch)
    import contextlib
    fake = type("Rt", (), {
        "lock_file": staticmethod(lambda path: contextlib.nullcontext()),
        "detect_host": staticmethod(lambda: "codex"),
        "installed_skill": staticmethod(lambda *a: ("codex", pathlib.Path(__file__))),
        "codex_environment": staticmethod(lambda env: dict(env)),
        "build_codex_command": staticmethod(lambda *a: ["codex", "x"])})
    monkeypatch.setattr(daemon, "_codex_runtime", lambda: fake)
    monkeypatch.setattr(daemon, "_capture_codex", lambda *a, **k: ("LOOP STOP: backlog-empty", 0, None))
    monkeypatch.delenv("SIGMA_CODEX_CMD", raising=False)
    free = type("Done", (), {"returncode": 0, "stdout": "FREE", "stderr": ""})()
    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: free)
    monkeypatch.setenv("SIGMA_SUPERVISE_MAX_RUNS", "1")
    (tmp_path / ".sdlc").mkdir()
    try:
        daemon.main(["x", str(tmp_path / ".sdlc")])
    except SystemExit:
        pass
    assert calls and all(c.get("ttl") == daemon.SESSION_LEASE_TTL for c in calls)


def test_supervisor_lease_is_not_reclaimed_after_seven_hours(state, daemon, monkeypatch):
    lease = with_sample(daemon._guard())
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "observe")
    got = lease.admit("session", 1, os.getcwd(), "supervisor:r", ttl=daemon.SESSION_LEASE_TTL, now=1000.0)
    record = json.loads(leases(state)[0].read_text())
    assert hasattr(got, "id") and not lease._stale(record, 1000.0 + 7 * 3600)
    default = dict(record, ttl=lease.DEFAULT_TTL)
    assert lease._stale(default, 1000.0 + 7 * 3600)  # the control: the 6h default would have gone
