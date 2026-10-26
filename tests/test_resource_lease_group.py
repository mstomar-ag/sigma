"""#1917 - does a verify whose wrapper is SIGKILLed keep its lease while its command still runs?

The measurement test starts its OWN wrapper (a new session, so a process group of its own) that takes a verify
lease through the real `resource_verify.run` path and runs a child that ignores SIGTERM. The test SIGKILLs ONLY
that wrapper pid, then asks `admit` for a second verify while the child is alive. Library code sends only
signal 0; the test alone sends SIGKILL, to the wrapper it just started, and cleans up only what it started.

Controls (each alone, then restored): drop the group-liveness clause of `_stale` -> the alive-group tests go red;
drop the ttl override -> the ttl test goes red; make `_stale` always True -> the alive-group tests go red.
"""
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
GIB = 1024 ** 3
unix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")

CHILD = ("import os, signal, sys, time\n"
         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
         "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
         "end = time.time() + 25\n"
         "while time.time() < end:\n"
         "    time.sleep(0.1)\n")
WRAPPER = ("import sys\n"
           "sys.path.insert(0, sys.argv[1])\n"
           "import resource_verify\n"
           "cmd = '%s -c \"%s\" %s' % (sys.executable, sys.argv[3].replace('\"', '\\\\\"'), sys.argv[4])\n"
           "resource_verify.run('verify', cmd, sys.argv[2], 60, 'g1917', term_grace=3.0)\n")


def _fresh(name):
    sys.modules.pop(name, None)
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


@pytest.fixture
def lease(tmp_path, monkeypatch):
    saved = {n: sys.modules.get(n) for n in ("resource_guard", "resource_lease")}
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "enforce")
    for n in saved:
        sys.modules.pop(n, None)
    mod = _fresh("resource_lease")

    def fake_sample():
        return mod.guard.Sample(total=16 * GIB, available=12 * GIB, swap_used=0, swap_total=0, swapouts=0,
                                swapouts_at=0.0, pressure=1, cpu_count=8, load1=0.0)
    monkeypatch.setattr(mod, "take_sample", fake_sample)
    mod.state = tmp_path / "state"
    yield mod
    for n, m in saved.items():
        if m is None:
            sys.modules.pop(n, None)
        else:
            sys.modules[n] = m


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lease_files(lease):
    root = lease.state / "leases"
    return sorted(root.glob("*.json")) if root.exists() else []


@pytest.fixture
def killed_wrapper(lease, tmp_path):
    """Start a wrapper in a session of its own, wait until its lease names the child, SIGKILL the wrapper pid it
    started and nothing else. -> dict(child=pid, wrapper=pid)."""
    pidfile = tmp_path / "child.pid"
    env = resource_mode.carry({k: v for k, v in os.environ.items() if k != "SIGMA_RESOURCE_MODE"})
    env["SIGMA_RESOURCE_DIR"] = str(lease.state)
    work = tmp_path / "work"
    work.mkdir()
    wrapper = subprocess.Popen([sys.executable, "-c", WRAPPER, str(SCRIPTS), str(work), CHILD, str(pidfile)],
                               env=env, start_new_session=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    info = {"wrapper": wrapper.pid, "child": None}
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not (pidfile.exists() and pidfile.read_text().strip()
                                              and _lease_files(lease)):
            time.sleep(0.05)
        assert pidfile.exists() and pidfile.read_text().strip(), "the child never started"
        info["child"] = int(pidfile.read_text())
        time.sleep(0.3)   # one lease-record amend after the child starts
        os.kill(wrapper.pid, signal.SIGKILL)   # the wrapper this test started, no other pid
        wrapper.wait(timeout=10)
        assert _alive(info["child"]), "the child must outlive its wrapper for this test to measure anything"
        yield info
    finally:
        if wrapper.poll() is None:
            os.kill(wrapper.pid, signal.SIGKILL)   # still our own wrapper
            wrapper.wait(timeout=10)
        child = info["child"]
        end = time.time() + 20   # the lifeline sentinel (3s grace) stops the child; the child also ends by itself
        while child and _alive(child) and time.time() < end:
            time.sleep(0.1)
        if child and _alive(child):
            try:
                if os.getsid(child) == child:     # the group the wrapper's run started for it
                    os.killpg(child, signal.SIGKILL)
            except OSError:
                pass


@unix_only
def test_measurement_second_verify_while_the_killed_wrappers_child_lives(lease, killed_wrapper):
    """AC-1/AC-2, the measurement: with the wrapper SIGKILLed and its command alive, a second verify is not admitted."""
    got = lease.admit("verify", 1, "repo", "g-second", ttl=120)
    try:
        assert _alive(killed_wrapper["child"])
        assert isinstance(got, lease.Hold), "ADMITTED while the killed wrapper's command still runs"
    finally:
        if hasattr(got, "id"):
            lease.release(got.id)


@unix_only
def test_empty_group_after_the_command_ends_is_reclaimed(lease, killed_wrapper):
    """AC-3: once the command's group is gone the lease is reclaimed and a second verify is admitted."""
    end = time.time() + 25
    while _alive(killed_wrapper["child"]) and time.time() < end:
        time.sleep(0.2)
    assert not _alive(killed_wrapper["child"])
    got = lease.admit("verify", 1, "repo", "g-second", ttl=120)
    assert isinstance(got, lease.Grant), got
    lease.release(got.id)


def _planted(lease, group_pid, pid_dead, start):
    """A lease file for a dead wrapper pid and a group id the test supplies (its own live group, or none)."""
    path = lease.state / "leases"
    path.mkdir(parents=True, exist_ok=True)
    rec = {"id": "planted", "kind": "verify", "pid": pid_dead, "pgid": None, "group": group_pid, "start": start,
           "pid_start": None, "repo_hash": "x", "goal": "g-old", "est_mem": 1, "workers": 1, "ttl": 100,
           "peak": 0, "procs": 0}
    (path / "planted.json").write_text(json.dumps(rec))
    return rec


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@unix_only
def test_alive_group_is_not_stale_and_ttl_overrides_it(lease):
    """AC-2 + AC-4 at the seam: a live group of this test's own (a session it starts) keeps a dead-pid lease; the
    ttl passing reclaims it whether or not the group is alive."""
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        now = time.time()
        rec = _planted(lease, holder.pid, _dead_pid(), now)
        assert lease._stale(rec, now + 10) is False          # alive group, inside the ttl
        assert lease._stale(rec, now + 101) is True          # ttl overrides the alive group
    finally:
        holder.kill()          # the process this test just started
        holder.wait(timeout=10)


@unix_only
def test_empty_group_is_stale_for_a_dead_pid(lease):
    """AC-3 at the seam."""
    gone = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    gone.wait()
    now = time.time()
    rec = _planted(lease, gone.pid, _dead_pid(), now)
    assert lease._stale(rec, now + 10) is True


def test_a_lease_without_group_info_keeps_the_pid_rule(lease):
    """No group recorded (no process groups on the platform): the old pid behaviour, documented."""
    now = time.time()
    rec = _planted(lease, None, _dead_pid(), now)
    rec.pop("group")
    assert lease._stale(rec, now + 10) is True
    live = dict(rec, pid=os.getpid())
    assert lease._stale(live, now + 10) is False
