"""#1551: a reviewer run that is still ALIVE must never read as failed, and a bounded `wait` verb lets a
yielding host (Codex `exec_command`) find out instead of parking.

While `reviewer.py run` is alive its receipt says `running` (runner pid, started_at, timeout_seconds,
run_id). ONE classifier inside `_read_receipt` turns a running receipt whose pid is dead, whose derived
deadline (`started_at + timeout_seconds + _RUN_GRACE`) has passed, or that is malformed, into
`failed/not_completed`, so every existing fail-closed path still applies. The plan's control table
(C1-C32, C5a-g) names which test goes red under each single-clause break."""
import io
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import types

import pytest

from test_reviewer_failure_parks import (  # noqa: F401  (`_env` is the autouse fixture, re-exported)
    APPROVE, GOAL, S, SHA, _config_host, _env, _project, _rv, brief_text, record_review, reviewer_script,
    run_reviewer)

_DROP = object()
COMMAND = {"mechanism": "command", "host": "other"}        # the route `_project`'s review.command resolves
CODEX = {"mechanism": "process", "host": "codex"}
APPROVE_OUT = "looks fine\nVERDICT: approve\n"


def _sd(p):
    return str(p / ".sdlc")


def _path(p, phase="review"):
    return p / ".sdlc" / "state" / "reviewer-runs" / ("0001-x.%s.json" % phase)


def _raw(p, phase="review"):
    """The receipt exactly as written, bypassing `_read_receipt` (and so the classifier)."""
    try:
        return json.loads(_path(p, phase).read_text())
    except FileNotFoundError:
        return {}


def _rcpt(rv, p, phase="review", **over):
    """A running receipt written through the module's own writer; `_DROP` removes a field."""
    fields = {"outcome": "running", "pid": os.getpid(), "started_at": int(time.time()),
              "timeout_seconds": 900, "run_id": "r1", "route": rv._route_tag(rv.resolve(_sd(p)))}
    fields.update(over)
    rv._write_receipt(_sd(p), GOAL, phase, **{k: v for k, v in fields.items() if v is not _DROP})


def _dead_pid():
    q = subprocess.Popen([sys.executable, "-c", "pass"])
    q.wait()                                    # reaped: not a zombie, so kill(pid, 0) says no such process
    return q.pid


def _run_io(rv, p, phase="review", stdout=None):
    """In-process `run_review` with the module's stdout/stderr swapped for buffers -> (code, out, err).
    Keeping its prints out of pytest's captured sections keeps red attributable (red_green.py refuses
    traceback attribution from a run whose report carries captured output)."""
    out, err = stdout if stdout is not None else io.StringIO(), io.StringIO()
    saved = rv.sys
    rv.sys = types.SimpleNamespace(**{**vars(sys), "stdout": out, "stderr": err})
    try:
        code = rv.run_review(_sd(p), str(p / "brief.md"), [str(p / "scratch")], goal=GOAL, phase=phase)
    finally:
        rv.sys = saved
    return code, (out.getvalue() if isinstance(out, io.StringIO) else ""), err.getvalue()


def _run(rv, p, phase="review", stdout=None):
    return _run_io(rv, p, phase, stdout)[0]


def _fake_reviewer(m, rv, communicate=None, start=None):
    """Replace ONLY the reviewer launch (a `review.command` string, shell=True); git and every other
    argv-list Popen (subprocess.run included) stays real."""
    real = subprocess.Popen

    class Fake:
        def __init__(self):
            if start:
                start()
            self.pid, self.returncode = 0, None
            self.stdin = self.stdout = self.stderr = None

        def communicate(self, input=None, timeout=None):
            out, code = communicate() if communicate else (APPROVE_OUT, 0)
            self.returncode = code
            return out, ""

        def kill(self):
            pass

        def wait(self, *a, **k):
            return self.returncode

    m.setattr(rv.subprocess, "Popen", lambda cmd, *a, **k: Fake() if isinstance(cmd, str) else real(cmd, *a, **k))
    m.setattr(rv, "_stop_review_process", lambda proc: None)


def _launch(p):
    """A real `reviewer.py run` whose reviewer records its own pid (under .sdlc/state, outside the
    content key) and then sleeps; returns once the reviewer has started."""
    pidfile = p / ".sdlc" / "state" / "child.pid"
    reviewer_script(p, "echo $$ > %s\nsleep 30\necho 'VERDICT: approve'\n" % pidfile)
    (p / "brief.md").write_text(brief_text("code-review"))
    runner = subprocess.Popen([sys.executable, str(S / "reviewer.py"), "run", ".sdlc", "brief.md", "--scratch",
                               "scratch", "--goal", GOAL, "--phase", "review"], cwd=p,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = time.time() + 10
    while time.time() < end and not (pidfile.exists() and pidfile.read_text().strip()):
        time.sleep(0.05)
    return runner, pidfile


def _reap(runner, pidfile):
    try:
        runner.kill()
    except OSError:
        pass
    runner.wait()
    try:
        os.killpg(os.getpgid(int(pidfile.read_text())), signal.SIGKILL)      # the setsid reviewer group
    except (OSError, ValueError):
        pass


def _opt(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def _gate(p):
    """An approving review recorded and its phase bracketed (as failure_parks' phase-record test), then a
    reader of the phase_gate `review` row that re-reads config each call."""
    import importlib.util
    reviewer_script(p, APPROVE)
    assert run_reviewer(p).returncode == 0
    assert record_review(p).returncode == 0
    for verb, extra in (("start", ["--model", "sonnet"]), ("end", [])):
        subprocess.run([sys.executable, str(S / "phase_report.py"), verb, ".sdlc", GOAL, "review", *extra],
                       cwd=p, capture_output=True, text=True, check=True)
    spec = importlib.util.spec_from_file_location("pg_1551", S / "phase_gate.py")
    pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)

    def row():
        cfg = json.loads((p / ".sdlc/config.json").read_text())
        return next(r for r in pg.status(_sd(p), cfg, GOAL)["rows"] if r[0] == "review")
    return row


class _Clock:
    """A fake monotonic clock whose sleep advances it; `limit` turns a broken bound into a FAILURE, not a hang."""

    def __init__(self, limit, on_sleep=None):
        self.t, self.calls, self.limit, self.on_sleep = 0.0, 0, limit, on_sleep

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.calls += 1
        assert self.calls <= self.limit, "wait ignored its bound (%d sleeps)" % self.calls
        self.t += seconds
        if self.on_sleep:
            self.on_sleep(self.calls)


# ---- Step 1: classifier, pid probe, route filter ------------------------------------------------------

def test_live_runner_before_deadline_classifies_running(tmp_path):
    p = _project(tmp_path)
    rv = _rv()
    _rcpt(rv, p)
    state, got = rv._read_receipt(_sd(p), GOAL, "review")
    assert state == "ok" and got["outcome"] == "running", got
    assert rv.failed_receipt(_sd(p), GOAL, "review") is None
    assert rv.running_receipt(_sd(p), GOAL, "review")


@pytest.mark.parametrize("case", ["dead_pid", "past_deadline"])
def test_dead_or_expired_runner_classifies_not_completed(tmp_path, case):
    p = _project(tmp_path)
    rv = _rv()
    if case == "dead_pid":
        _rcpt(rv, p, pid=_dead_pid())
    else:
        _rcpt(rv, p, started_at=int(time.time()) - 900 - rv._RUN_GRACE - 1)
    got = rv._read_receipt(_sd(p), GOAL, "review")[1]
    assert got["outcome"] == "failed" and got.get("failure") == "not_completed", got
    failed = rv.failed_receipt(_sd(p), GOAL, "review")
    assert failed is not None and failed[0] == "not_completed", failed


_MALFORMED = {
    "pid_missing": lambda now: {"pid": _DROP},
    "pid_bool": lambda now: {"pid": True},
    "pid_zero": lambda now: {"pid": 0},
    "pid_huge": lambda now: {"pid": 2 ** 31},                 # os.kill raises OverflowError, not OSError (N2)
    "started_missing": lambda now: {"started_at": _DROP},
    "timeout_missing": lambda now: {"timeout_seconds": _DROP},
    "timeout_over_cap": lambda now: {"timeout_seconds": 10 ** 9, "started_at": now - 3600},
    "started_in_future": lambda now: {"started_at": now + 10 ** 6},
}


@pytest.mark.parametrize("case", sorted(_MALFORMED))
def test_malformed_running_receipt_fails_closed(tmp_path, case):
    p = _project(tmp_path)
    rv = _rv()
    _rcpt(rv, p, **_MALFORMED[case](int(time.time())))
    got = rv._read_receipt(_sd(p), GOAL, "review")[1]           # never raises (N2), whatever the pid
    assert got["outcome"] == "failed" and got.get("failure") == "not_completed", (case, got)


def test_non_posix_reader_never_probes_the_pid(tmp_path, monkeypatch):
    p = _project(tmp_path)
    rv = _rv()
    dead = _dead_pid()

    def kill(*a, **k):
        raise AssertionError("os.kill probed on a non-POSIX reader")
    monkeypatch.setattr(rv, "os", types.SimpleNamespace(**{**vars(os), "name": "nt", "kill": kill}))
    _rcpt(rv, p, pid=dead)                                       # before its deadline: only the deadline bounds it
    assert rv._read_receipt(_sd(p), GOAL, "review")[1]["outcome"] == "running"
    _rcpt(rv, p, pid=dead, started_at=int(time.time()) - 900 - rv._RUN_GRACE - 1)
    got = rv._read_receipt(_sd(p), GOAL, "review")[1]
    assert got["outcome"] == "failed" and got.get("failure") == "not_completed", got


# ---- Step 2: run_review writes running, finalises, persists findings, hints ---------------------------

def test_receipt_is_running_with_pid_nonce_while_reviewer_runs(tmp_path):
    p = _project(tmp_path)
    runner, pidfile = _launch(p)
    try:
        assert pidfile.exists(), "the reviewer never started"
        raw = _raw(p)
        assert raw.get("outcome") == "running", raw
        assert raw.get("pid") == runner.pid, raw
        assert isinstance(raw.get("started_at"), int) and not isinstance(raw.get("started_at"), bool), raw
        assert raw.get("timeout_seconds") == 900, raw
        assert isinstance(raw.get("run_id"), str) and raw["run_id"], raw
        assert raw.get("route") == COMMAND, raw
    finally:
        _reap(runner, pidfile)


def test_launch_hint_only_on_the_cli(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE)
    r = run_reviewer(p)                                          # the documented gesture
    assert r.returncode == 0, r.stderr
    hints = [line for line in r.stderr.splitlines() if "reviewer.py wait" in line]
    assert len(hints) == 1, r.stderr
    raw = _raw(p)
    m = re.search(r"(reviewer\.py wait .*?--run-id \S+)", hints[0])
    assert m, hints[0]
    argv = shlex.split(m.group(1))
    assert _opt(argv, "--goal") == GOAL and _opt(argv, "--phase") == "review", argv
    assert _opt(argv, "--max-seconds") == "20", argv
    assert _opt(argv, "--run-id") == raw.get("run_id"), (argv, raw)
    # THAT gesture, run: only its first word (a bare `reviewer.py`) becomes interpreter + script (N6).
    got = subprocess.run([sys.executable, str(S / "reviewer.py"), *argv[1:]], cwd=p, capture_output=True,
                         text=True, timeout=60)
    assert got.returncode == 0 and "FINISHED verdict=approve" in got.stdout, (got.returncode, got.stdout, got.stderr)
    # In process (work.py's caller): no hint in the captured error text.
    code, out, err = _run_io(_rv(), p)
    assert code == 0 and "VERDICT: approve" in out, (code, out, err)
    assert "reviewer.py wait" not in err, err


def test_completed_run_overwrites_running_with_ok_and_findings(tmp_path, monkeypatch):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE)
    rv = _rv()
    writes, real = [], rv._write_receipt

    def spy(*a, **k):
        writes.append(dict(k))
        return real(*a, **k)
    monkeypatch.setattr(rv, "_write_receipt", spy)
    assert _run(rv, p) == 0
    raw = _raw(p)
    assert writes and writes[0].get("outcome") == "running", writes
    assert raw.get("outcome") == "ok" and raw.get("verdict") == "approve", raw
    assert raw.get("findings") == APPROVE_OUT, raw
    assert "pid" not in raw and raw.get("run_id") == writes[0].get("run_id"), (raw, writes[0])
    # A huge non-ASCII finding is capped on ENCODED bytes, keeping the head.
    reviewer_script(p, "PYTHONIOENCODING=utf-8 %s -c \"print(chr(233) * 60000)\"\necho 'VERDICT: approve'\n"
                    % shlex.quote(sys.executable))
    assert run_reviewer(p).returncode == 0
    raw = _raw(p)
    assert raw.get("verdict") == "approve", raw.get("verdict")
    assert _path(p).stat().st_size <= rv._FINDINGS_CAP + 2048, _path(p).stat().st_size
    findings = raw.get("findings") or ""
    assert findings.startswith(chr(233) * 1000) and "[truncated" in findings, findings[-80:]
    rv.clear_receipts(_sd(p), GOAL)
    assert not _path(p).exists()


@pytest.mark.parametrize("case", ["ok_write", "failed_write"])
def test_stale_runner_cannot_clobber_a_newer_nonce(tmp_path, monkeypatch, case):
    p = _project(tmp_path)
    rv = _rv()

    def communicate():
        raw = _raw(p)
        raw["run_id"] = "other"                                  # a newer run took the receipt over
        _path(p).write_text(json.dumps(raw))
        return APPROVE_OUT, (0 if case == "ok_write" else 3)
    _fake_reviewer(monkeypatch, rv, communicate)
    code = _run(rv, p)
    assert _raw(p).get("run_id") == "other", _raw(p)
    if case == "ok_write":
        assert code == 1


def test_final_write_does_not_resurrect_a_cleared_receipt(tmp_path, monkeypatch):
    p = _project(tmp_path)
    rv = _rv()

    def communicate():
        rv.clear_receipts(_sd(p), GOAL)                           # `work.py finish` ran meanwhile
        return APPROVE_OUT, 0
    _fake_reviewer(monkeypatch, rv, communicate)
    code = _run(rv, p)
    assert not _path(p).exists(), _raw(p)
    assert code == 1


def test_content_unbound_still_overwrites_an_earlier_ok(tmp_path, monkeypatch):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE)
    assert run_reviewer(p).returncode == 0
    assert _raw(p).get("outcome") == "ok"
    for i in range(4):
        (p / ("u%d" % i)).write_text("x")
    rv = _rv()
    monkeypatch.setattr(rv, "_MAX_UNTRACKED", 3)
    assert _run(rv, p) == 1
    raw = _raw(p)
    assert raw.get("outcome") == "failed" and raw.get("failure") == "content_unbound", raw
    # N1: the running receipt is written before the key step, so this is an ordinary post-launch failure,
    # finalised under this run's own nonce.
    assert isinstance(raw.get("run_id"), str) and raw["run_id"], raw
    r = record_review(p)
    assert r.returncode == 2 and "content_unbound" in r.stderr, r.stderr


def test_post_run_key_step_stays_inside_the_deadline(tmp_path, monkeypatch):
    p = _project(tmp_path)
    rv = _rv()
    _fake_reviewer(monkeypatch, rv)
    seen, real = [], rv._content_key

    def key(sdlc_dir, goal):
        seen.append(_raw(p))                                     # what a reader sees during each key step
        return real(sdlc_dir, goal)
    monkeypatch.setattr(rv, "_content_key", key)
    assert _run(rv, p) == 0
    assert len(seen) >= 2, seen
    raw = seen[1]                                                # the post-run key step
    assert raw.get("pid") == os.getpid(), raw                    # in-process: the runner is this process
    assert raw.get("outcome") == "running", raw
    now = raw["started_at"] + 900 + 4 * rv._KEY_SECONDS          # four git calls at their own timeout each
    assert rv._classify(raw, now=now)["outcome"] == "running"


@pytest.mark.parametrize("case", ["popen_valueerror", "post_run_key", "verdict_parse", "after_ok_write"])
def test_any_exception_after_launch_finalises_not_completed(tmp_path, monkeypatch, case):
    p = _project(tmp_path)
    rv = _rv()
    if case == "popen_valueerror":
        def start():
            raise ValueError("bad Popen argument")
        _fake_reviewer(monkeypatch, rv, start=start)
        exc = ValueError
    else:
        _fake_reviewer(monkeypatch, rv)
    if case == "post_run_key":
        calls, real = [], rv._content_key

        def key(sdlc_dir, goal):
            calls.append(1)
            if len(calls) >= 2:
                raise RuntimeError("key step crashed")
            return real(sdlc_dir, goal)
        monkeypatch.setattr(rv, "_content_key", key)
        exc = RuntimeError
    elif case == "verdict_parse":
        def lone(output):
            raise RuntimeError("verdict parse crashed")
        monkeypatch.setattr(rv, "lone_verdict", lone)
        exc = RuntimeError
    elif case == "after_ok_write":
        class Broken:
            def write(self, s):
                raise BrokenPipeError("stdout closed")

            def flush(self):
                pass
        exc = BrokenPipeError
    with pytest.raises(exc):
        _run(rv, p, stdout=Broken() if case == "after_ok_write" else None)
    raw = _raw(p)
    if case == "after_ok_write":
        assert raw.get("outcome") == "ok" and raw.get("verdict") == "approve", raw   # the verdict stands
    else:
        assert raw.get("outcome") == "failed" and raw.get("failure") == "not_completed", raw
    assert isinstance(raw.get("run_id"), str) and raw["run_id"], raw


def test_sigkilled_runner_reads_not_completed(tmp_path):
    p = _project(tmp_path)
    rv = _rv()
    runner, pidfile = _launch(p)
    try:
        assert pidfile.exists(), "the reviewer never started"
        assert _raw(p).get("outcome") == "running", _raw(p)
        os.kill(runner.pid, signal.SIGKILL)
        runner.wait()                                            # reaped: dead, not a zombie
        got = rv._read_receipt(_sd(p), GOAL, "review")[1]
        assert got["outcome"] == "failed" and got.get("failure") == "not_completed", got
        r = record_review(p)
        assert r.returncode == 2 and "not_completed" in r.stderr, r.stderr
    finally:
        _reap(runner, pidfile)


# ---- Step 3: gates treat a live run as "wait", never approve, never park ------------------------------

def test_independent_run_refusal_waits_while_running(tmp_path):
    p = _project(tmp_path)
    rv = _rv()
    _rcpt(rv, p)
    msg = rv.independent_run_refusal(_sd(p), GOAL, "review")
    assert isinstance(msg, str), msg
    assert "reviewer.py wait" in msg and "--run-id r1" in msg and "still running" in msg, msg
    for banned in ("Park", "parked", "FAILED", "no run of it is on record"):
        assert banned not in msg, (banned, msg)


def test_independent_run_refusal_after_crash_still_parks(tmp_path):
    p = _project(tmp_path)
    rv = _rv()
    _rcpt(rv, p, pid=_dead_pid())
    msg = rv.independent_run_refusal(_sd(p), GOAL, "review") or ""
    assert "not_completed" in msg and "Park the goal" in msg, msg


def test_subagent_transcript_refusal_waits_while_running(tmp_path):
    p = _project(tmp_path)
    rv = _rv()
    _rcpt(rv, p)
    msg = rv.subagent_transcript_refusal(_sd(p), GOAL, "review", "unverified") or ""
    assert "reviewer.py wait" in msg and "(unknown)" not in msg, msg
    assert rv.subagent_transcript_refusal(_sd(p), GOAL, "review", "transcript") is None


def test_inline_review_refusal_waits_while_running(tmp_path):
    p = _project(tmp_path)
    rv = _rv()
    _rcpt(rv, p)
    msg = rv.inline_review_refusal(_sd(p), GOAL, rv.resolve(_sd(p))) or ""
    assert "reviewer.py wait" in msg and "FAILED" not in msg, msg
    _rcpt(rv, p, pid=_dead_pid())
    msg = rv.inline_review_refusal(_sd(p), GOAL, rv.resolve(_sd(p))) or ""
    assert "FAILED" in msg and "not_completed" in msg, msg


def test_accept_inline_refuses_a_live_run_and_accepts_a_crashed_one(tmp_path):
    p = _project(tmp_path, review={"host": "inline"})
    rv = _rv()
    # A command-route plan-review run, then the owner set the route inline.
    _rcpt(rv, p, phase="plan_review", route=COMMAND, plan_sha256=SHA)
    try:
        rv.accept_inline(_sd(p), GOAL, "plan_review", "owner chose inline review")
        raised = None
    except ValueError as exc:
        raised = str(exc)
    assert raised and "still running" in raised, raised
    _rcpt(rv, p, phase="plan_review", route=COMMAND, plan_sha256=SHA, pid=_dead_pid())
    got = rv.accept_inline(_sd(p), GOAL, "plan_review", "owner chose inline review")
    assert got.get("outcome") == "waived_inline", got
    assert got.get("failure") == "not_completed" and got.get("plan_sha256") == SHA, got   # N3: fields kept


def test_phase_gate_reports_running_not_failed(tmp_path):
    p = _project(tmp_path)
    rv = _rv()
    row = _gate(p)
    _rcpt(rv, p)
    name, ok, detail, fix = row()
    assert ok is False and "still running" in detail, detail
    assert "reviewer.py wait" in fix and "park" not in fix.lower(), fix
    _rcpt(rv, p, pid=_dead_pid())
    name, ok, detail, fix = row()
    assert ok is False and "independent reviewer failed after the approving verdict" in detail, detail


def test_phase_gate_ignores_a_running_receipt_on_the_subagent_route(tmp_path, monkeypatch):
    p = _project(tmp_path)
    rv = _rv()
    row = _gate(p)
    # Control: on the route it was made under, the live run IS reported (so None below is the filter).
    _rcpt(rv, p, route=COMMAND)
    assert rv.running_receipt(_sd(p), GOAL, "review")
    assert "still running" in row()[2], row()
    # Another host's live run does not hold this route.
    _rcpt(rv, p, route=CODEX)
    assert rv.running_receipt(_sd(p), GOAL, "review") is None
    assert "still running" not in row()[2], row()
    # N3: a crashed run keeps its route through the classifier, so it does not bar this route either.
    _rcpt(rv, p, route=CODEX, pid=_dead_pid())
    assert rv.failed_receipt(_sd(p), GOAL, "review") is None
    # The subagent route has its own proof: a live process run is not its business.
    monkeypatch.setenv("CLAUDECODE", "1")
    _config_host(p, "claude")
    _rcpt(rv, p, route=COMMAND)
    assert rv.running_receipt(_sd(p), GOAL, "review") is None
    assert "still running" not in row()[2], row()


# ---- Step 4: the wait verb ----------------------------------------------------------------------------

def test_wait_bounds_and_default_below_measured_yield(tmp_path):
    p = _project(tmp_path)
    rv = _rv()
    _rcpt(rv, p)
    c = _Clock(limit=5 + 1)
    code, text = rv.wait_review(_sd(p), GOAL, "review", 5, run_id="r1", sleep=c.sleep, clock=c.clock)
    assert code == 3 and text.startswith("RUNNING pid="), (code, text)
    assert rv.WAIT_DEFAULT_SECONDS == 20 < 30                    # below the one measured 30.2 s yield
    started = time.monotonic()
    r = subprocess.run([sys.executable, str(S / "reviewer.py"), "wait", ".sdlc", "--goal", GOAL, "--phase",
                        "review", "--max-seconds", "1"], cwd=p, capture_output=True, text=True, timeout=60)
    assert r.returncode == 3 and time.monotonic() - started < 5, (r.returncode, r.stdout, r.stderr)


def test_wait_returns_final_verdict_with_findings(tmp_path):
    p = _project(tmp_path)
    rv = _rv()
    for verdict in ("approve", "block"):
        rv._write_receipt(_sd(p), GOAL, "review", outcome="ok", verdict=verdict, findings="looks fine\n",
                          run_id="r1", route=COMMAND)
        c = _Clock(limit=5)
        code, text = rv.wait_review(_sd(p), GOAL, "review", 5, sleep=c.sleep, clock=c.clock)
        assert code == 0 and "FINISHED verdict=%s" % verdict in text and "looks fine" in text, (code, text)
        assert "finished is not approved" in text, text
    _rcpt(rv, p)

    def finish(calls):
        if calls == 2:
            rv._write_receipt(_sd(p), GOAL, "review", outcome="ok", verdict="approve", findings="late\n",
                              run_id="r1", route=COMMAND)
    c = _Clock(limit=10 + 1, on_sleep=finish)
    code, text = rv.wait_review(_sd(p), GOAL, "review", 10, sleep=c.sleep, clock=c.clock)
    assert code == 0 and "verdict=approve" in text and "late" in text, (code, text)


def test_wait_reports_failed_without_park_prose(tmp_path):
    p = _project(tmp_path)
    rv = _rv()
    texts = []
    rv._write_receipt(_sd(p), GOAL, "review", outcome="failed", failure="nonzero_exit", run_id="r1", route=COMMAND)
    c = _Clock(limit=5)
    code, text = rv.wait_review(_sd(p), GOAL, "review", 5, sleep=c.sleep, clock=c.clock)
    assert code == 1 and "FAILED" in text, (code, text)
    texts.append(text)
    _rcpt(rv, p, pid=_dead_pid())
    c = _Clock(limit=5)
    code, text = rv.wait_review(_sd(p), GOAL, "review", 5, run_id="r1", sleep=c.sleep, clock=c.clock)
    assert code == 1 and "FAILED" in text, (code, text)                  # N3: the crash keeps run_id r1
    texts.append(text)
    _rcpt(rv, p)
    dead = _dead_pid()

    def crash(calls):
        if calls == 1:
            _rcpt(rv, p, pid=dead)
    c = _Clock(limit=10 + 1, on_sleep=crash)
    code, text = rv.wait_review(_sd(p), GOAL, "review", 10, run_id="r1", sleep=c.sleep, clock=c.clock)
    assert code == 1, (code, text)
    texts.append(text)
    assert all("Park" not in t for t in texts), texts


def test_wait_usage_run_id_and_missing_retry(tmp_path, capsys):
    p = _project(tmp_path)
    rv = _rv()
    _rcpt(rv, p)
    capsys.readouterr()
    assert rv.main(["wait", _sd(p), "--goal", GOAL, "--phase", "review", "--max-seconds", "1",
                    "--run-id", "other"]) == 2
    out = capsys.readouterr()
    assert "different run" in out.out + out.err, out
    assert rv.main(["wait", _sd(p), "--goal", GOAL, "--phase", "bogus"]) == 2
    assert rv.main(["wait", _sd(p), "--goal", GOAL, "--phase", "review", "--max-seconds", "abc"]) == 2
    # A wait issued at launch, before the running receipt exists, retries briefly instead of exiting 2.
    rv.clear_receipts(_sd(p), GOAL)

    def appear(calls):
        if calls == 1:
            _rcpt(rv, p)
    c = _Clock(limit=20 + 1, on_sleep=appear)
    code, text = rv.wait_review(_sd(p), GOAL, "review", 20, sleep=c.sleep, clock=c.clock)
    assert code == 3, (code, text)
    rv.clear_receipts(_sd(p), GOAL)
    c = _Clock(limit=20 + 1)
    code, text = rv.wait_review(_sd(p), GOAL, "review", 20, sleep=c.sleep, clock=c.clock)
    assert code == 2 and c.calls <= 5, (code, c.calls, text)
