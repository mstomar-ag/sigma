"""#979: hardening of the shared verify runner (skills/sigma-loop/scripts/bounded_run.py) before its first caller.

Each test names one fix and carries its own control: the same check against a copy of the module with that one
fix undone must fail. Processes are real; waits are bounded; anything a control is meant to leak is reaped."""
import os
import signal
import subprocess
import sys

import pytest

import unattended_support as support
from test_bounded_run import CALLER

#: The repository-location variables, pinned by name: a name dropped from the module's set fails here.
LOCATION_NAMES = ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                  "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX"]


def test_done_is_told_only_after_the_group_is_confirmed_gone(tmp_path, monkeypatch):
    """A second interrupt while the group is being stopped must not tell the lifeline `done`: it stops the group."""
    br = support.load("bounded_run")
    pidfile = tmp_path / "tree.pid"
    grand = None

    def interrupted(proc, selector, deadline, stop_path, clock):
        support.pid_in(pidfile)
        raise KeyboardInterrupt

    def interrupted_again(proc, grace, drain):
        raise KeyboardInterrupt

    monkeypatch.setattr(br, "_supervise", interrupted)
    monkeypatch.setattr(br, "_stop_group", interrupted_again)
    try:
        with pytest.raises(KeyboardInterrupt):
            br.run_group("sleep 60 & echo $! > %s; wait" % pidfile, tmp_path, 60, shell=True, term_grace=1)
        grand = support.pid_in(pidfile)
        assert support.gone(grand, 8.0), "the lifeline was disarmed while the group was still running"
    finally:
        support.reap(grand)


def _killed_caller_tree(tmp_path, module_file, command, grace):
    caller = subprocess.Popen([sys.executable, "-c", CALLER, str(module_file), command, str(tmp_path), str(grace)],
                              start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        grand = support.pid_in(tmp_path / "tree.pid")
        os.killpg(caller.pid, signal.SIGKILL)
        caller.wait(timeout=10)
        return grand
    finally:
        if caller.poll() is None:
            caller.kill()


def test_lifeline_asks_before_it_kills_and_then_kills_what_ignores_the_ask(tmp_path):
    """After a SIGKILLed caller the lifeline sends SIGTERM first, waits the grace, then SIGKILL."""
    real = support.SCRIPTS / "bounded_run.py"
    polite = "trap 'echo term > %s; exit 0' TERM; sleep 60 & echo $! > %s; wait" % (tmp_path / "mark", tmp_path / "tree.pid")
    grand = None
    try:
        grand = _killed_caller_tree(tmp_path, real, polite, 5)
        assert support.gone(grand, 8.0)
        assert (tmp_path / "mark").read_text().strip() == "term", "the lifeline never sent SIGTERM first"
    finally:
        support.reap(grand)
    (tmp_path / "tree.pid").unlink()
    stubborn = "trap '' TERM; sleep 60 & echo $! > %s; wait" % (tmp_path / "tree.pid")
    try:
        grand = _killed_caller_tree(tmp_path, real, stubborn, 1)
        assert support.gone(grand, 8.0), "a tree that ignores SIGTERM must be killed after the grace"
    finally:
        support.reap(grand)
    # Controls: kill first (no graceful exit seen), and no escalation (the stubborn tree survives).
    (tmp_path / "mark").unlink()
    (tmp_path / "tree.pid").unlink()
    first = tmp_path / "kill-first"
    first.mkdir()
    kill_first = support.variant_file(first, "bounded_run", ("os.killpg(pgid, signal.SIGTERM)\nexcept OSError:",
                                                             "os.killpg(pgid, signal.SIGKILL)\nexcept OSError:"))
    try:
        grand = _killed_caller_tree(tmp_path, kill_first, polite, 5)
        assert support.gone(grand, 8.0)
        assert not (tmp_path / "mark").exists(), "a kill-first lifeline must not give the graceful exit"
    finally:
        support.reap(grand)
    (tmp_path / "tree.pid").unlink()
    never = tmp_path / "no-escalation"
    never.mkdir()
    no_kill = support.variant_file(never, "bounded_run", ("if alive():\n    try:\n        os.killpg(pgid, signal.SIGKILL)",
                                                          "if False:\n    try:\n        os.killpg(pgid, signal.SIGKILL)"))
    try:
        grand = _killed_caller_tree(tmp_path, no_kill, stubborn, 1)
        assert not support.gone(grand, 4.0), "without the escalation the stubborn tree must survive"
    finally:
        support.reap(grand)


def test_location_variable_set_is_pinned_by_name():
    br = support.load("bounded_run")
    assert sorted(br.LOCATION_VARS) == sorted(LOCATION_NAMES)


@pytest.mark.parametrize("name", LOCATION_NAMES)
def test_each_location_variable_is_stripped_and_refused_by_name(name, tmp_path, monkeypatch):
    br = support.load("bounded_run")
    assert name not in br.unattended_env({name: "/nonexistent-979", "KEEP_979": "1"})
    assert br.unattended_env({name: "x", "KEEP_979": "1"})["KEEP_979"] == "1"
    monkeypatch.setenv(name, "/nonexistent-979")
    result = br.run_verify("true", tmp_path, 5)
    assert result.outcome == br.REFUSED and name in result.detail and "/nonexistent-979" not in result.detail


def test_dropping_one_location_name_is_seen(tmp_path):
    """Control: a module that forgets one variable no longer strips it, and the pinned set differs."""
    mutant = support.variant("bounded_run", ('"GIT_ALTERNATE_OBJECT_DIRECTORIES", ', ""))
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" in mutant.unattended_env({"GIT_ALTERNATE_OBJECT_DIRECTORIES": "x"})
    assert sorted(mutant.LOCATION_VARS) != sorted(LOCATION_NAMES)


FLOOD = """
import importlib.util, resource, sys, tempfile
spec = importlib.util.spec_from_file_location("bounded_run", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
result = module.run_group("yes abcdefgh | head -c 200000000", tempfile.gettempdir(), 120, shell=True, merge=False,
                          max_out_bytes=module.MAX_OUT_BYTES)
text = result.out.strip()
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(peak // 1024 if sys.platform == "darwin" else peak, result.truncated)
"""


def _flood_peak_kib(module_file):
    done = subprocess.run([sys.executable, "-c", FLOOD, str(module_file)], capture_output=True, text=True,
                          timeout=120, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr[-300:]
    kib, truncated = done.stdout.split()
    assert truncated == "True"
    return int(kib)


def test_split_mode_peak_memory_under_a_flood_is_bounded(tmp_path):
    """Measured: a 64 MiB cap peaked near 227 MiB (about 3.2 times the cap); 16 MiB peaks near 74 MiB."""
    real = _flood_peak_kib(support.SCRIPTS / "bounded_run.py")
    assert real < 128 * 1024, "peak %d KiB" % real
    old = support.variant_file(tmp_path, "bounded_run", ("MAX_OUT_BYTES = 16 * 1024 * 1024",
                                                         "MAX_OUT_BYTES = 64 * 1024 * 1024"))
    assert _flood_peak_kib(old) > 128 * 1024, "control: the old cap must exceed the bound"


def test_timeout_error_keeps_no_argument_text():
    """GitTimeout.argv is redacted to program and verb: a remote name, a URL or a path never rides the exception."""
    ug = support.load("unattended_git")
    error = ug.GitTimeout(["git", "-c", "k=secret-979", "fetch", "https://" + "user" + ":token-979" + "@host/repo"], 2, False, 2.5)
    assert error.argv == ["git", "fetch"]
    assert "secret-979" not in repr(vars(error)) and "token-979" not in repr(vars(error))
    assert str(error) == "git fetch: timed out after 2s"


def test_lifeline_is_ended_when_the_group_gone_confirmation_raises(tmp_path, monkeypatch):
    """#1065: a confirmation that raises must not skip ending the lifeline; it ends it as not-confirmed (pipe closed,
    no `done`), and the run's own result is not replaced by the confirmation's error."""
    br = support.load("bounded_run")
    ended = []
    real_end = br._end_lifeline

    def spy(sentinel, write_fd, group_gone=True):
        ended.append(group_gone)
        real_end(sentinel, write_fd, group_gone)
        ended.append(sentinel.poll() is not None)

    def boom(proc):
        raise RuntimeError("confirmation failed")

    monkeypatch.setattr(br, "_end_lifeline", spy)
    monkeypatch.setattr(br, "_group_confirmed_gone", boom)
    result = br.run_group([sys.executable, "-c", "pass"], tmp_path, 30, term_grace=1)
    assert result.outcome == br.OK
    assert ended == [False, True], "the lifeline was not ended as unconfirmed and reaped"
