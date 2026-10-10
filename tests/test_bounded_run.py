"""#920: the shared verify runner and the bounded process mechanism under it (skills/sigma-loop/scripts/bounded_run.py).

PLANNED TESTS of the pull-request red-to-green gate. Every test starts by asking `unattended_support` for the module,
which asserts that it exists, so before the module is written each one fails by AssertionError. The file is bound by
its whole-file hash and is not edited after the red verify. The code under test never lives here.

Every behaviour that a kill, a refusal or a pin provides is shown to be needed: the same check is run once against a
copy of the module with that one thing broken, and must fail there (the control sits in the test it controls).
Processes are real; waits are bounded; anything a control is meant to leak is reaped in a `finally`."""
import os
import shlex
import shutil
import signal
import subprocess
import sys

import pytest

import unattended_support as support

#: What a "caller" does in the lifeline test: run a command under the runner and wait for it (the test kills this).
CALLER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("bounded_run", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.run_group(sys.argv[2], sys.argv[3], 120, shell=True, term_grace=float(sys.argv[4]))
"""


def test_outcomes_exit_codes_and_merged_tail(tmp_path):
    """Exit 0 is ok, any other exit is failed with its code; stderr joins stdout; cwd and closed stdin hold."""
    br = support.load("bounded_run")
    rows = [("exit 0", br.OK, 0), ("echo out; echo err 1>&2; exit 3", br.FAILED, 3), ("kill -9 $$", br.FAILED, -9),
            ("nosuchcommand-920", br.FAILED, 127)]
    got = [(command, br.run_group(command, tmp_path, 30, shell=True)) for command, _o, _c in rows]
    assert [(c, r.outcome, r.code) for c, r in got] == [(c, o, k) for c, o, k in rows]
    merged = br.run_group("echo out; echo err 1>&2", tmp_path, 30, shell=True)
    assert "out\n" in merged.out and "err\n" in merged.out and merged.err == "" and not merged.truncated
    assert br.run_group("pwd -P", tmp_path, 30, shell=True).out.strip() == os.path.realpath(str(tmp_path))
    assert br.run_group("read x; echo got:$x", tmp_path, 30, shell=True).out == "got:\n", "stdin must be closed"
    argv = br.run_group([sys.executable, "-c", "print(7)"], tmp_path, 30)
    assert (argv.outcome, argv.out.strip()) == (br.OK, "7")
    split = br.run_group("echo out; echo err 1>&2", tmp_path, 30, shell=True, merge=False)
    assert (split.out, split.err) == ("out\n", "err\n")
    missing_program = br.run_group(["no-such-program-920"], tmp_path, 30)
    missing_cwd = br.run_group("true", tmp_path / "absent", 30, shell=True)
    assert [r.outcome for r in (missing_program, missing_cwd)] == [br.ERROR, br.ERROR]
    assert missing_program.code is None and "could not start" in missing_program.detail


def test_nothing_runs_without_a_valid_request(tmp_path, monkeypatch):
    """A blank command, a bad budget or a host without process groups spawns nothing, not even a trust read."""
    br = support.load("bounded_run")
    support.hermetic(tmp_path, monkeypatch)
    seen = support.spy_popen(monkeypatch)
    marker = tmp_path / "ran"
    command = "touch %s" % marker
    for blank in (None, "", "  \t\n", 5, ["true"]):
        result = br.run_verify(blank, tmp_path, 30)
        assert (result.outcome, result.code) == (br.NO_COMMAND, None), blank
    assert seen == [], "a blank command must not even read the trust setting: %r" % (seen,)
    for budget in (0, -1, True, False, float("nan"), float("inf"), "5", None, br.MAX_SECONDS + 1):
        for call in (lambda: br.run_verify(command, tmp_path, budget),
                     lambda: br.run_group(command, tmp_path, budget, shell=True)):
            with pytest.raises(ValueError):
                call()
    with pytest.raises(ValueError):
        br.run_group(command, tmp_path, 5, shell=True, term_grace=0)
    with monkeypatch.context() as patched:
        patched.setattr(sys, "platform", "win32")
        refused = [br.run_verify(command, tmp_path, 30), br.run_group(command, tmp_path, 30, shell=True)]
    assert [r.outcome for r in refused] == [br.REFUSED, br.REFUSED]
    assert all("process group" in r.detail for r in refused), [r.detail for r in refused]
    assert not marker.exists() and seen == [], "nothing may be spawned: %r" % (seen,)


def test_verify_needs_trust_and_a_scratch_worktree(tmp_path, monkeypatch):
    """Untrusted or main-checkout verify is refused unspawned; a trusted linked worktree runs; controls run both."""
    br = support.load("bounded_run")
    support.hermetic(tmp_path, monkeypatch)
    main = support.make_repo(tmp_path)
    scratch = support.add_worktree(main, tmp_path / "scratch")
    (scratch / "sub").mkdir()
    marker = tmp_path / "marker"
    command = "echo ran > %s" % marker
    text = br.shell_policy.refusal_message()
    wrong = []
    for label, setting in (("unset", None), ("false", "false"), ("maybe", "maybe")):
        if setting is not None:
            support.trust(main, setting)
        result = br.run_verify(command, scratch, 30)
        if (result.outcome, result.detail, marker.exists()) != (br.UNTRUSTED, text, False):
            wrong.append((label, result.outcome))
    for label, path in (("not a repository", tmp_path / "plain"), ("missing path", tmp_path / "absent")):
        (tmp_path / "plain").mkdir(exist_ok=True)
        result = br.run_verify(command, path, 30)
        if (result.outcome, marker.exists()) != (br.UNTRUSTED, False):
            wrong.append((label, result.outcome))
    support.trust(main, "true")
    clone = tmp_path / "clone"
    support.git(tmp_path, "clone", "-q", str(main), str(clone))
    for label, path in (("fresh clone", clone), ("main checkout", main)):
        result = br.run_verify(command, path, 30)
        if marker.exists() or result.outcome not in (br.UNTRUSTED, br.REFUSED):
            wrong.append((label, result.outcome))
    (main / "sub").mkdir()
    sub = br.run_verify(command, main / "sub", 30)
    if (sub.outcome, marker.exists()) != (br.REFUSED, False) or "scratch worktree" not in sub.detail:
        wrong.append(("main sub-directory", sub.outcome))
    assert wrong == [], wrong
    for label, path in (("linked worktree", scratch), ("its sub-directory", scratch / "sub")):
        if marker.exists():
            marker.unlink()
        result = br.run_verify(command, path, 30)
        assert (result.outcome, marker.exists()) == (br.OK, True), (label, tuple(result)[:2], result.detail)
    marker.unlink()
    trustless = support.variant("bounded_run", ("if not shell_policy.repository_shell_commands_allowed(worktree):",
                                                "if False:"))
    support.trust(main, "false")
    assert trustless.run_verify(command, scratch, 30).outcome == br.OK and marker.exists(), "trust check not needed?"
    marker.unlink()
    support.trust(main, "true")
    anywhere = support.variant("bounded_run", ('return "main" if os.path.realpath(lines[0]) == '
                                               'os.path.realpath(lines[1]) else "linked"', 'return "linked"'))
    assert anywhere.run_verify(command, main, 30).outcome == br.OK and marker.exists(), "scratch check not needed?"
    marker.unlink()
    # the documented entry end to end: stderr joins stdout, and an ok run leaves no background child behind
    merged = br.run_verify("echo out; echo err 1>&2", scratch, 30)
    assert merged.outcome == br.OK and "out\n" in merged.out and "err\n" in merged.out and merged.err == "", tuple(merged)
    pidfile, leaver = tmp_path / "background.pid", None
    try:
        swept = br.run_verify("sleep 60 & echo $! > %s; exit 0" % pidfile, scratch, 30)
        leaver = support.pid_in(pidfile)
        assert swept.outcome == br.OK, tuple(swept)
        assert support.gone(leaver), "an ok run must not leave a background child alive"
    finally:
        support.reap(leaver)
    # an exported location variable fails closed (it would lend the trust of the repository it names), nothing spawned
    clone2 = tmp_path / "clone2"
    support.git(tmp_path, "clone", "-q", str(main), str(clone2))
    untrusted = support.add_worktree(clone2, tmp_path / "untrusted-scratch")
    seen = support.spy_popen(monkeypatch)
    with monkeypatch.context() as exported:
        exported.setenv("GIT_DIR", str(main / ".git"))
        refused = br.run_verify(command, untrusted, 30)
        spawned = list(seen)
        under_export = br.checkout_kind(scratch)
        lent = support.variant("bounded_run", ("    if located:\n", "    if False:\n")).run_verify(command, untrusted, 30)
        lent_unstripped = support.variant("bounded_run", ("stdin=subprocess.DEVNULL, env=unattended_env())",
                                                          "stdin=subprocess.DEVNULL)")).checkout_kind(scratch)
    assert (refused.outcome, spawned) == (br.REFUSED, []) and "GIT_DIR" in refused.detail, (tuple(refused), spawned)
    assert str(main) not in refused.detail, "names only, never values"
    assert under_export == "linked", "checkout_kind must not follow an exported GIT_DIR"
    assert lent_unstripped != "linked", "the strip is what keeps checkout_kind honest"
    assert lent.outcome == br.OK and marker.exists(), "without the refusal an exported GIT_DIR lends another repository's trust"
    marker.unlink()
    # git unable to say what the directory is has its own diagnostic, distinct from the main checkout's
    real_git = shutil.which("git")
    support.shim_git(tmp_path, monkeypatch, 'if [ "$3" = "rev-parse" ]; then echo "fatal: simulated" 1>&2; exit 128; fi\n'
                     'exec %s "$@"\n' % shlex.quote(real_git))
    unknown = br.run_verify(command, scratch, 30)
    assert unknown.outcome == br.REFUSED and "could not say" in unknown.detail, tuple(unknown)
    assert "scratch worktree" not in unknown.detail and not marker.exists()
    conflated = support.variant("bounded_run", ('MAIN_CHECKOUT_REFUSAL if kind == "main" else UNKNOWN_CHECKOUT_REFUSAL',
                                                "MAIN_CHECKOUT_REFUSAL"))
    assert "scratch worktree" in conflated.run_verify(command, scratch, 30).detail, "the two diagnostics must differ"


def test_overrun_stops_the_whole_group(tmp_path, monkeypatch):
    """On overrun the shell and its grandchild are both gone; stopping only the direct child leaves the grandchild.

    The budget is 60 s but the runner's clock is injected: it reads 0 until the grandchild has announced itself and
    then jumps past the deadline, so the overrun is deterministic and nothing waits for a real minute."""
    br = support.load("bounded_run")
    _main, scratch = support.verify_scratch(tmp_path, monkeypatch)
    short = br.run_verify("sleep 12", scratch, 2)          # the documented gesture: the budget must reach the runner
    assert short.outcome == br.TIMEOUT and 1.5 <= short.seconds < 8, tuple(short)

    def attempt(module, name):
        pidfile = tmp_path / (name + ".pid")
        clock = lambda: 10 ** 6 if pidfile.exists() else 0.0            # noqa: E731 - a deterministic seam
        result = module.run_group("sleep 60 & echo $! > %s; wait" % pidfile, tmp_path, 60, shell=True,
                                  term_grace=2, clock=clock)
        return result, support.pid_in(pidfile)

    grand = None
    try:
        result, grand = attempt(br, "grandchild")
        assert (result.outcome, result.escalated) == (br.TIMEOUT, False), tuple(result)
        assert result.seconds < 8 and "timed out after 60s" in result.detail, tuple(result)
        assert support.gone(grand), "the grandchild survived the timeout: the group was not stopped"
    finally:
        support.reap(grand)
    direct = support.variant("bounded_run", ("os.killpg(pgid, signum)", "os.kill(pgid, signum)"),
                             ("REAP_SECONDS = 5.0", "REAP_SECONDS = 0.5"))
    grand2 = None
    try:
        _result, grand2 = attempt(direct, "direct")
        assert support.alive(grand2), "a direct-child kill must leave the grandchild: the check cannot see the bug"
    finally:
        support.reap(grand2)


def test_term_comes_before_kill(tmp_path, monkeypatch):
    """The group is asked to stop before it is killed; a tree that ignores the request is killed after the grace.

    The budget is 60 s and the runner's clock is injected: it jumps past the deadline only once the command has
    announced (after installing its signal handling), so the request cannot arrive before the handler exists."""
    br = support.load("bounded_run")

    def after(path):
        return lambda: 10 ** 6 if path.exists() else 0.0                # a deterministic seam

    ready, mark = tmp_path / "ready", tmp_path / "graceful"
    polite = "trap 'echo term > %s; exit 0' TERM; : > %s; sleep 60 & wait" % (mark, ready)
    result = br.run_group(polite, tmp_path, 60, shell=True, term_grace=5, clock=after(ready))
    assert (result.outcome, result.escalated) == (br.TIMEOUT, False), tuple(result)
    assert mark.read_text().strip() == "term", "the command never saw SIGTERM first"
    pidfile = tmp_path / "stubborn.pid"
    grand = None
    try:
        stubborn = "trap '' TERM; sleep 60 & echo $! > %s; wait" % pidfile
        result = br.run_group(stubborn, tmp_path, 60, shell=True, term_grace=1, clock=after(pidfile))
        grand = support.pid_in(pidfile)
        assert (result.outcome, result.escalated) == (br.TIMEOUT, True), tuple(result)
        assert result.seconds >= 0.9, "the grace was not given"
        assert support.gone(grand), "a tree that ignores SIGTERM must be killed"
    finally:
        support.reap(grand)
    _main, scratch = support.verify_scratch(tmp_path, monkeypatch)
    vpid, vgrand = tmp_path / "verify-stubborn.pid", None
    try:
        verified = br.run_verify("trap '' TERM; sleep 60 & echo $! > %s; wait" % vpid, scratch, 20, term_grace=1,
                                 clock=after(vpid))
        vgrand = support.pid_in(vpid)
        assert (verified.outcome, verified.escalated) == (br.TIMEOUT, True), tuple(verified)
        assert 0.9 <= verified.seconds < 6, "term_grace and clock must reach the runner (the default grace is ten seconds)"
        assert support.gone(vgrand)
    finally:
        support.reap(vgrand)
    kill_first = support.variant("bounded_run", ("_signal_group(pgid, signal.SIGTERM)\n    deadline",
                                                 "_signal_group(pgid, signal.SIGKILL)\n    deadline"))
    mark.unlink()
    ready.unlink()
    kill_first.run_group(polite, tmp_path, 60, shell=True, term_grace=5, clock=after(ready))
    assert not mark.exists(), "a kill-first runner must not give the command its graceful exit"


def test_stop_file_ends_the_run(tmp_path, monkeypatch):
    """A stop file ends the run and the tree without being deleted; one present at the start spawns nothing."""
    br = support.load("bounded_run")
    _main, scratch = support.verify_scratch(tmp_path, monkeypatch)
    vstop, vpid, vgrand = tmp_path / "verify-stop", tmp_path / "verify.pid", None
    try:                                                    # the documented gesture: stop_path must reach the runner
        stopped = br.run_verify("sleep 60 & echo $! > %s; touch %s; wait" % (vpid, vstop), scratch, 8,
                                stop_path=vstop, term_grace=3)
        vgrand = support.pid_in(vpid)
        assert stopped.outcome == br.STOPPED and stopped.seconds < 6, tuple(stopped)
        assert support.gone(vgrand), "the verify tree must be gone"
    finally:
        support.reap(vgrand)
    stop, pidfile = tmp_path / "stop", tmp_path / "grandchild.pid"
    command = "sleep 60 & echo $! > %s; touch %s; wait" % (pidfile, stop)
    grand = None
    try:
        result = br.run_group(command, tmp_path, 60, shell=True, stop_path=stop, term_grace=3)
        grand = support.pid_in(pidfile)
        assert result.outcome == br.STOPPED and result.seconds < 10, tuple(result)
        assert support.gone(grand) and stop.exists(), "the tree must be gone and the stop file left in place"
        marker = tmp_path / "ran"
        seen = support.spy_popen(monkeypatch)
        early = br.run_group("touch %s" % marker, tmp_path, 60, shell=True, stop_path=stop)
        assert (early.outcome, marker.exists(), seen) == (br.STOPPED, False, []), tuple(early)
        absent = br.run_group("exit 0", tmp_path, 60, shell=True, stop_path=tmp_path / "never")
        assert absent.outcome == br.OK
    finally:
        support.reap(grand)
    ignoring = support.variant("bounded_run", ("        if stop_path is not None and os.path.exists(stop_path):\n"
                                               "            return STOPPED\n", "        if False:\n"
                                                                              "            return STOPPED\n"))
    stop2, pidfile2 = tmp_path / "stop2", tmp_path / "grandchild2.pid"
    stop.unlink()
    grand2 = None
    try:
        again = ignoring.run_group("sleep 60 & echo $! > %s; touch %s; wait" % (pidfile2, stop2), tmp_path, 4,
                                   shell=True, stop_path=stop2, term_grace=2)
        grand2 = support.pid_in(pidfile2)
        assert again.outcome == br.TIMEOUT, "without the poll the run must only end at its budget"
    finally:
        support.reap(grand2)


def test_a_killed_caller_leaves_no_tree(tmp_path):
    """When the caller is SIGKILLed the command tree, in a session of its own, is still stopped; not without the sentinel."""
    support.load("bounded_run")
    results = []
    for label, module_file in (("real", support.SCRIPTS / "bounded_run.py"),
                               ("no lifeline", support.variant_file(
                                   tmp_path, "bounded_run",
                                   ('if sys.stdin.buffer.read().startswith(b"done"):\n    sys.exit(0)\n',
                                    'sys.exit(0)\n')))):
        pidfile = tmp_path / ("tree-%s.pid" % label.replace(" ", "-"))
        command = "sleep 60 & echo $! > %s; wait" % pidfile
        caller = subprocess.Popen([sys.executable, "-c", CALLER, str(module_file), command, str(tmp_path), "1"],
                                  start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        grand = None
        try:
            grand = support.pid_in(pidfile)
            os.killpg(caller.pid, signal.SIGKILL)       # the caller's own wall-clock cap, which nothing inside sees
            caller.wait(timeout=10)
            results.append((label, support.gone(grand, 8.0)))
        finally:
            support.reap(grand)
            if caller.poll() is None:
                caller.kill()
    assert results == [("real", True), ("no lifeline", False)], results


def test_output_is_bounded_and_escapees_let_go(tmp_path):
    """Output is a bounded tail; a descendant in another session holding the pipe cannot stall the run."""
    br = support.load("bounded_run")
    big = br.run_group("yes abcdefgh | head -c 8000000; echo END", tmp_path, 60, shell=True, tail_bytes=4096)
    assert big.outcome == br.OK and len(big.out) <= 4096 and big.out.endswith("END\n") and big.truncated
    head = br.run_group("yes abcdefgh | head -c 100000; echo oops 1>&2", tmp_path, 60, shell=True, merge=False,
                        max_out_bytes=1000)
    assert (len(head.out), head.truncated, head.err) == (1000, True, "oops\n"), (len(head.out), head.truncated)
    small = br.run_group("echo little", tmp_path, 60, shell=True, tail_bytes=4096)
    assert (small.out, small.truncated) == ("little\n", False)
    pidfile = tmp_path / "escapee.pid"
    code = ("import os, time\nif os.fork() == 0:\n    os.setsid()\n    open(%r, 'w').write(str(os.getpid()))\n"
            "    time.sleep(12)\n    os._exit(0)\n" % str(pidfile))
    command = "%s; echo done" % support.python_command(code)
    escapee = None
    try:
        result = br.run_group(command, tmp_path, 60, shell=True)
        escapee = support.pid_in(pidfile)
        assert result.outcome == br.OK and "done" in result.out and result.seconds < 3.0, tuple(result)
        patient = support.variant("bounded_run", ("and _read_ready(selector, 0):",
                                                  "and (_read_ready(selector, 1) or True):"))
        assert patient.run_group(command, tmp_path, 60, shell=True).seconds >= 4.0, "a patient drain must stall"
    finally:
        support.reap(escapee)


def test_environment_pins_and_location_variables(tmp_path, monkeypatch):
    """The command sees no repository-location variable and the prompt pins; the caller's mapping is not changed."""
    br = support.load("bounded_run")
    support.hermetic(tmp_path, monkeypatch)
    main = support.make_repo(tmp_path)
    support.trust(main)
    scratch = support.add_worktree(main, tmp_path / "scratch")
    hostile = {name: "/nonexistent-920" for name in sorted(br.LOCATION_VARS)}
    hostile.update({"GIT_EDITOR": "vim", "GIT_ASKPASS": "/bin/evil", "SSH_ASKPASS": "/bin/evil",
                    "GIT_TERMINAL_PROMPT": "1", "KEEP_920": "yes"})
    base = dict(os.environ, **hostile)

    def seen_by_the_command(module):
        result = module.run_verify("env", scratch, 30, env=base)
        assert result.outcome == module.OK, (result.outcome, result.detail)
        return dict(line.split("=", 1) for line in result.out.splitlines() if "=" in line)

    env = seen_by_the_command(br)
    assert [name for name in br.LOCATION_VARS if name in env] == [], "repository-location variables leaked"
    assert [env.get(n) for n in ("GIT_TERMINAL_PROMPT", "GIT_EDITOR", "GIT_ASKPASS", "SSH_ASKPASS")] == ["0", "true", "", ""]
    assert env.get("KEEP_920") == "yes", "the operator's other variables are inherited"
    assert all(base[name] == value for name, value in hostile.items()), "the caller's mapping was changed"
    assert br.unattended_env({"A": "1"}) == dict({"A": "1"}, **dict(br.PROMPT_PINS))
    no_pin = support.variant("bounded_run", ('("GIT_ASKPASS", ""), ', ""))
    assert seen_by_the_command(no_pin).get("GIT_ASKPASS") == "/bin/evil", "the askpass pin is what blanks it"
    no_strip = support.variant("bounded_run", ("if key not in LOCATION_VARS}", "if True}"))
    assert seen_by_the_command(no_strip).get("GIT_DIR") == "/nonexistent-920", "the strip is what removes it"
