# SPDX-License-Identifier: MIT
"""A stopped writer's ledger entries file is published, and never leaves a dirty file that defers a pull (#1932).

`entries/<actor>-<host>.<pid>.jsonl` is written by one process. When that process stops (a stopped
watcher, a finished CLI call) with an entry still unpublished, only the SAME pid could stage it, so the
file sat untracked and the next `pull` was deferred. `publish()` now also stages files that carry THIS
machine's host token and a dead pid, and the watcher publishes once on a clean shutdown.

Real git throughout (a real bare origin and a real ledger worktree); nothing in git is stubbed.
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

import resource_mode

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sync = _mod("sync")
ledger = _mod("ledger")

ON = {"ledger": {"enabled": True, "actor": "dana"}}


def _git(cwd, *args):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr or proc.stdout}"
    return proc.stdout.strip()


@pytest.fixture
def clone(tmp_path):
    """A real repo, a real bare origin, a real ledger worktree on the ops branch."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / ".gitignore").write_text(".sdlc/\n", encoding="utf-8")
    _git(repo, "add", "--", ".gitignore")
    _git(repo, "commit", "-q", "-m", "base")
    base = repo / ".sdlc"
    (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps(ON), encoding="utf-8")
    out = sync.bootstrap(base, ON)
    assert "published" in out, out
    return repo, base, origin


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _plant(base, actor, host, pid, why="left behind"):
    """An unpublished entries file for `<actor>-<host>.<pid>`, written the real way then renamed."""
    src = ledger.entry_file(base, "dana", config=ON)
    existed = src.exists()
    before = src.read_text(encoding="utf-8") if existed else ""
    ledger.append(base, ON, "note", "g.md", why=why)
    dest = src.parent / f"{actor}-{host}.{pid}.jsonl"
    dest.write_text(src.read_text(encoding="utf-8")[len(before):], encoding="utf-8")
    if existed:
        src.write_text(before, encoding="utf-8")      # the writer's own tracked file goes back as it was
    else:
        src.unlink()
    return dest


def _host():
    return ledger._host_token(ON)


def _on_origin(origin, name):
    out = subprocess.run(["git", "-C", str(origin), "ls-tree", "-r", "--name-only", sync.branch(ON)],
                         capture_output=True, text=True).stdout
    return any(line.endswith(name) for line in out.splitlines())


def test_dead_pid_file_is_published_and_pull_is_not_deferred(clone):
    _repo, base, origin = clone
    wt = sync.worktree(base)
    dead = _plant(base, "dana", _host(), _dead_pid())
    assert dead.name in _git(wt, "status", "--porcelain")     # the dirty file the pull used to trip on
    assert sync.publish(base, ON) == "published"
    assert _on_origin(origin, dead.name)
    assert dead.name not in _git(wt, "status", "--porcelain")
    assert sync.pull(base, ON) == "pulled", _git(wt, "status", "--porcelain")


def test_dead_pid_file_of_a_different_actor_on_this_host_is_swept(clone):
    _repo, base, origin = clone
    dead = _plant(base, "someone-else", _host(), _dead_pid())
    assert sync.publish(base, ON) == "published"
    assert _on_origin(origin, dead.name)


def test_a_live_foreign_pid_file_is_left_alone(clone):
    _repo, base, origin = clone
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        live = _plant(base, "zed", _host(), child.pid)
        own = _plant_own(base)
        assert sync.publish(base, ON) == "published"
        assert not _on_origin(origin, live.name)
        assert live.name in _git(sync.worktree(base), "status", "--porcelain")
        assert _on_origin(origin, own.name)
    finally:
        child.kill()
        child.wait()


def _plant_own(base):
    ledger.append(base, ON, "note", "g.md", why="mine")
    return ledger.entry_file(base, "dana", config=ON)


def test_another_hosts_dead_pid_file_is_left_alone(clone):
    _repo, base, origin = clone
    other = _plant(base, "zed", "0" * len(_host()) if _host() != "0" * len(_host()) else "1" * len(_host()),
                   _dead_pid())
    own = _plant_own(base)
    assert sync.publish(base, ON) == "published"
    assert not _on_origin(origin, other.name)
    assert _on_origin(origin, own.name)


def test_a_legacy_pid_only_name_is_not_swept(clone):
    _repo, base, origin = clone
    src = ledger.entry_file(base, "dana", config=ON)
    legacy = src.parent / f"zed-{_dead_pid()}.jsonl"
    legacy.write_text('{"kind": "note"}\n', encoding="utf-8")
    own = _plant_own(base)
    assert sync.publish(base, ON) == "published"
    assert not _on_origin(origin, legacy.name)
    assert _on_origin(origin, own.name)


def test_publish_with_only_a_swept_file_is_not_nothing_to_publish(clone):
    _repo, base, origin = clone
    dead = _plant(base, "dana", _host(), _dead_pid())
    stranger = {"ledger": {"enabled": True, "actor": "erin"}}     # owns no entries file of its own
    out = sync.publish(base, stranger)
    assert out != "nothing to publish", out
    assert _on_origin(origin, dead.name)


# ---------------------------------------------------------------- the watcher's clean-shutdown publish


def _children(pid):
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return out.split()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM handling is POSIX")
def test_a_terminated_watcher_publishes_before_it_exits(clone):
    _repo, base, origin = clone
    env = resource_mode.carry({**os.environ, "SIGMA_WATCH_INTERVAL": "600", "SIGMA_WATCH_SLEEP_SCALE": "1",
                               "SIGMA_WATCH_MAX_TICKS": "0", "SIGMA_WATCH_CALL_TIMEOUT": "60"})
    proc = subprocess.Popen([sys.executable, str(S / "watch_daemon.py"), str(base)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    log = base / "state" / "watch.log"
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not (log.exists() and "tick #1" in log.read_text()):
            time.sleep(0.05)
        quiet = 0                                   # tick 1 is over once the watcher has no child
        while time.time() < deadline and quiet < 5:
            quiet = quiet + 1 if not _children(proc.pid) else 0
            time.sleep(0.2)
        assert quiet >= 5, "tick 1 never finished"
        dead = _plant(base, "dana", _host(), _dead_pid())
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode == -signal.SIGTERM
    assert _on_origin(origin, dead.name), "the shutdown did not publish"


def test_the_shutdown_publish_is_bounded_and_runs_once(tmp_path):
    """A hung publish is killed at the call timeout, never raises, and the second cleanup is a no-op."""
    wd = _mod("watch_daemon")
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    p = wd.paths(str(sdlc))
    p.pid.write_text(str(os.getpid()))
    calls = []
    wd.run_call = lambda *a: calls.append(a[2])
    wd.cleanup(p, os.getpid(), str(sdlc), "0.5")
    p.pid.write_text(str(os.getpid()))
    wd.cleanup(p, os.getpid(), str(sdlc), "0.5")
    assert calls == ["0.5"]                              # the same bound a tick call gets, once
    assert not p.pid.exists()
    wd._SHUTDOWN_PUBLISHED = False
    wd.run_call = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    p.pid.write_text(str(os.getpid()))
    wd.cleanup(p, os.getpid(), str(sdlc), "0.5")         # must not raise
    assert not p.pid.exists()
