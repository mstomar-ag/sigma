"""#920: the doctor's bounded local-probe helper (`_bounded_run` in skills/sigma-doctor/scripts/doctor.py).

PLANNED TESTS of the pull-request red-to-green gate. Each starts by asking for the helper through `getattr` and
asserting that it exists, so before it is written each fails by AssertionError. The file is bound by its whole-file hash
and is not edited after the red verify.

`_real_run` keeps local probes uncapped on purpose (pinned in tests/test_doctor.py); this helper is the new, bounded
way in for a caller that must not hang on a wedged binary. The first test also shows, on a real hung binary, why it is
needed: `_real_run` is still waiting when the helper has already answered."""
import importlib.util
import os
import threading
import time

import unattended_support as support

DOCTOR = support.ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py"


def doctor():
    spec = importlib.util.spec_from_file_location("doctor_bounded_probe", DOCTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    helper = getattr(module, "_bounded_run", None)
    assert helper is not None, "doctor.py has no _bounded_run helper yet"
    return module, helper


def test_a_hung_local_binary_is_cut_off(tmp_path, monkeypatch):
    """The helper answers a hung binary within its limit and stops the tree; `_real_run` is still waiting."""
    d, bounded = doctor()
    pidfile = tmp_path / "hang.pid"
    support.script(tmp_path / "hang-probe-920", "sleep 60 &\necho $! > %s\nwait\n" % pidfile)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    grand, waiter = None, None
    try:
        started = time.monotonic()
        result = bounded(["hang-probe-920"], timeout=2)
        grand = support.pid_in(pidfile)
        assert not result and isinstance(result, d._RawFailure) and "timed out" in result.raw, repr(result)
        assert time.monotonic() - started < 10 and support.gone(grand), "the probe must end and its tree must be gone"
        pidfile.unlink()
        monkeypatch.setenv("SIGMA_WATCH_CALL_TIMEOUT", "2")
        assert "timed out" in bounded(["hang-probe-920"]).raw, "the default limit is the fleet's per-call bound"
        pidfile.unlink()
        answered = []
        waiter = threading.Thread(target=lambda: answered.append(d._real_run(["hang-probe-920"])), daemon=True)
        waiter.start()
        waiter.join(1.5)
        grand = support.pid_in(pidfile)
        assert waiter.is_alive() and answered == [], "_real_run is expected to wait on a hung local binary"
    finally:
        support.reap(grand)
        if waiter is not None:
            waiter.join(10)


def test_the_helper_keeps_the_runner_contract(tmp_path, monkeypatch):
    """Output text on success, a falsy failure carrying the text otherwise, never an exception."""
    d, bounded = doctor()
    support.script(tmp_path / "say-bad-920", "echo bad\nexit 3\n")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    ok = bounded(["echo", "hi"], timeout=30)
    assert ok == "hi\n" == d._real_run(["echo", "hi"]) and isinstance(ok, str)
    assert bounded(["echo", 7], timeout=30) == "7\n"
    failed = bounded(["say-bad-920"], timeout=30)
    assert not failed and isinstance(failed, d._RawFailure) and failed.raw.strip() == "bad", repr(failed)
    missing = bounded(["no-such-binary-920"], timeout=30)
    assert not missing and "not found" in missing.raw, repr(missing)
    empty = bounded([], timeout=30)
    assert not empty and isinstance(empty, d._RawFailure), repr(empty)
