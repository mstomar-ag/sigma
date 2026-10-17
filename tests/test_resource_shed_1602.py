"""#1602 - bounded shed of a Sigma-started verify group on sustained critical pressure.

No model call, no network. Machine readings are faked through the lease module's own seam; the child is a real
process in its own group. Controls (break one clause, see red) are named in each docstring."""
import json
import os
import pathlib
import signal
import sys
import time

import pytest

import test_resource_verify_1601 as base
from test_resource_verify_1601 import GIB, SCRIPTS, env, unix_only  # noqa: F401  (env is a fixture)

CHILD = ("import os,signal,sys,time\n"
         "open(sys.argv[1],'w').write(str(os.getpid()))\n"
         "def term(*a):\n open(sys.argv[2],'a').write('term\\n'); sys.exit(0)\n"
         "signal.signal(signal.SIGTERM, term)\n"
         "time.sleep(60)\n")


def _cmd(tmp_path):
    (tmp_path / "child.py").write_text(CHILD)
    return "%s %s %s %s" % (sys.executable, tmp_path / "child.py", tmp_path / "pid", tmp_path / "term")


def _critical_after_admission(env, seconds=0.0):
    """Sampler: roomy until `seconds` after the first reading, then critical (below half the reserve)."""
    t0 = []

    def sample():
        t0.append(time.time()) if not t0 else None
        crit = time.time() - t0[0] > seconds
        avail = 1 * GIB if crit else 12 * GIB
        return env.lease.guard.Sample(total=16 * GIB, available=avail, swap_used=0, swap_total=0, swapouts=0,
                                      swapouts_at=0.0, pressure=1, cpu_count=8, load1=0.0)
    env.lease.take_sample = sample


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@unix_only
def test_sustained_critical_stops_the_group_term_before_kill_and_no_worker_survives(env, tmp_path):
    _critical_after_admission(env, 0.3)
    out = env.run("verify", _cmd(tmp_path), str(tmp_path), 60, "g1", sample_gap=0.1, term_grace=3)
    assert out.shed and out.hold is not None and out.hold.reason.endswith("(shed)"), out.hold
    assert (tmp_path / "term").read_text().startswith("term")        # TERM was delivered (and handled) first
    pid = int((tmp_path / "pid").read_text())
    time.sleep(0.3)
    assert not _alive(pid)


@unix_only
def test_one_critical_sample_does_nothing(env, tmp_path):
    """Control: shed after 1 sample instead of 3 and this goes red."""
    readings = iter([False, False, False, True] + [False] * 200)       # admit reads, then ONE critical sample

    def sample():
        crit = next(readings)
        return env.lease.guard.Sample(total=16 * GIB, available=(1 if crit else 12) * GIB, swap_used=0, swap_total=0,
                                      swapouts=0, swapouts_at=0.0, pressure=1, cpu_count=8, load1=0.0)
    env.lease.take_sample = sample
    out = env.run("verify", "sleep 1", str(tmp_path), 30, "g1", sample_gap=0.1)
    assert not out.shed and out.hold is None and out.returncode == 0


@unix_only
def test_shed_off_does_nothing_even_when_critical(env, tmp_path):
    _critical_after_admission(env, 0.2)
    out = env.run("verify", "sleep 1.5", str(tmp_path), 30, "g1", sample_gap=0.1, config={"shed": "off"})
    assert not out.shed and out.hold is None and out.returncode == 0


@unix_only
def test_observe_mode_never_sheds(env, tmp_path):
    """observe logs would-be actions only: sustained critical pressure stops nothing. Control: drop the mode clause."""
    _critical_after_admission(env, 0.2)
    out = env.run("verify", "sleep 1.5", str(tmp_path), 30, "g1", sample_gap=0.1, config={"mode": "observe"})
    assert not out.shed and out.hold is None and out.returncode == 0


@unix_only
def test_a_non_critical_hold_level_does_nothing(env, tmp_path):
    """Pressure level 2 holds new goals but is not critical: the running verify is left alone."""
    def sample():
        return env.lease.guard.Sample(total=16 * GIB, available=12 * GIB, swap_used=0, swap_total=0, swapouts=0,
                                      swapouts_at=0.0, pressure=2, cpu_count=8, load1=0.0)
    env.lease.take_sample = sample
    out = env.run("verify", "sleep 1", str(tmp_path), 30, "g1", sample_gap=0.1)
    assert not out.shed and out.returncode == 0


@unix_only
def test_the_stop_file_is_removed_after_a_shed_and_after_an_exception(env, tmp_path):
    _critical_after_admission(env, 0.2)
    env.run("verify", _cmd(tmp_path), str(tmp_path), 60, "g1", sample_gap=0.1, term_grace=3)
    shed_dir = env.state / "shed"
    assert not shed_dir.exists() or not list(shed_dir.iterdir())

    env.lease.take_sample = lambda: env.lease.guard.Sample(
        total=16 * GIB, available=12 * GIB, swap_used=0, swap_total=0, swapouts=0, swapouts_at=0.0, pressure=1,
        cpu_count=8, load1=0.0)

    def boom(*a, stop_path=None, **k):
        os.makedirs(os.path.dirname(stop_path), exist_ok=True)
        open(stop_path, "w").close()                  # the file exists when the runner blows up
        assert os.path.exists(stop_path)
        raise RuntimeError("runner exploded")
    with pytest.raises(RuntimeError):
        env.run("verify", "sleep 1", str(tmp_path), 30, "g2", sample_gap=0.1, run_group=boom)
    assert not shed_dir.exists() or not list(shed_dir.iterdir())
    assert not list((env.state / "leases").glob("*.json"))


@unix_only
def test_a_registered_foreign_session_is_never_signalled(env, tmp_path, monkeypatch):
    """A foreign lease (a registered session pid) sits in the table; the shed signals only the run's own group."""
    foreign = os.getpid() + 7777
    (env.state / "leases").mkdir(parents=True, exist_ok=True)
    (env.state / "leases" / "foreign.json").write_text(json.dumps(
        {"id": "foreign", "kind": "session", "pid": foreign, "pgid": foreign, "start": time.time(), "ttl": 3600,
         "workers": 1, "est_mem": 0, "peak": 0}))
    sent = []
    real_kill, real_killpg = os.kill, os.killpg
    monkeypatch.setattr(os, "kill", lambda pid, sig: (sent.append((pid, sig)), real_kill(pid, sig))[1])
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (sent.append((pgid, sig)), real_killpg(pgid, sig))[1])
    _critical_after_admission(env, 0.2)
    out = env.run("verify", _cmd(tmp_path), str(tmp_path), 60, "g1", sample_gap=0.1, term_grace=3)
    assert out.shed
    assert all(target not in (foreign, -foreign) for target, sig in sent if sig != 0), sent


def test_the_guard_modules_never_pause_or_resume_a_process():
    for name in ("resource_verify.py", "resource_guard.py", "resource_lease.py", "bounded_run.py"):
        text = (SCRIPTS / name).read_text()
        assert "SIGSTOP" not in text and "SIGCONT" not in text, name


@unix_only
def test_end_to_end_shed_gives_exit_5_shed_no_evidence_and_the_rerun_records_evidence(tmp_path, monkeypatch, capsys):
    sdlc, goal = base._repo(tmp_path, {"resource": {"mode": "enforce"}},
                            command="%s -c 'import time; time.sleep(60)'" % sys.executable)
    loop = base._loop(monkeypatch, tmp_path, available=12 * GIB)
    lease = sys.modules["resource_lease"]
    t0 = time.time()

    def sample():
        crit = time.time() - t0 > 1.0
        return lease.guard.Sample(total=16 * GIB, available=(1 if crit else 12) * GIB, swap_used=0, swap_total=0,
                                  swapouts=0, swapouts_at=0.0, pressure=1, cpu_count=8, load1=0.0)
    monkeypatch.setattr(lease, "take_sample", sample)
    assert loop.verify_goal(sdlc, goal) == 5
    err = capsys.readouterr().err
    assert "RESOURCE-HOLD" in err and "(shed)" in err
    assert not (pathlib.Path(sdlc) / "state" / "verify" / "0001.json").exists()
    # pressure clears: the same goal's verify now runs and records evidence
    monkeypatch.setattr(lease, "take_sample", lambda: lease.guard.Sample(
        total=16 * GIB, available=12 * GIB, swap_used=0, swap_total=0, swapouts=0, swapouts_at=0.0, pressure=1,
        cpu_count=8, load1=0.0))
    cfg = json.loads((pathlib.Path(sdlc) / "config.json").read_text())
    cfg["verify"]["command"] = "echo ok"
    (pathlib.Path(sdlc) / "config.json").write_text(json.dumps(cfg))
    assert loop.verify_goal(sdlc, goal) == 0
    assert (pathlib.Path(sdlc) / "state" / "verify" / "0001.json").exists()


@unix_only
def test_configured_shed_after_sets_the_critical_samples_before_a_shed(env, tmp_path):
    """#1769: `resource.shed_after` is read from the config; unset keeps 3. Control: ignore the key and this goes red."""
    readings = iter([False, False, False] + [True] * 5 + [False] * 300)   # admit reads, then five critical samples

    def sample():
        crit = next(readings)
        return env.lease.guard.Sample(total=16 * GIB, available=(1 if crit else 12) * GIB, swap_used=0, swap_total=0,
                                      swapouts=0, swapouts_at=0.0, pressure=1, cpu_count=8, load1=0.0)
    env.lease.take_sample = sample
    out = env.run("verify", "sleep 1.5", str(tmp_path), 30, "g1", sample_gap=0.1, term_grace=1,
                  config={"shed_after": 9})
    assert not out.shed and out.hold is None        # five critical samples are fewer than the configured nine
