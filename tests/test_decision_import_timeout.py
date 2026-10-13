"""#1310: a classifier timeout is skipped (retried), counted, configurable, and kills the process group."""
import importlib.util
import json
import os
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_t1310", SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DI, FJ, CFG = _mod("decision_import"), _mod("feature_judge"), _mod("rulebook_cfg")


def _cfg(**extra):
    return {"rulebook": {"records": {"enabled": True, "repo_visibility": "private"},
                         "autonomy": {"enabled": False}, "import": dict(extra, classifier="model")}}


def _sd(tmp_path):
    p = tmp_path / ".sdlc"
    p.mkdir()
    (p / "categories.json").write_text(json.dumps(
        {"schema": "sigma/categories@1", "categories": [{"id": "money", "areas": ["billing"]}]}))
    return str(p)


def _c(i):
    return {"source": "transcript", "when": "2026-10-01T10:%02d:00Z" % i, "question": "merge the docs change %d" % i,
            "options": ["yes", "no"], "choice": "A", "reason": "B", "issue": 7, "id": "c%03d" % i,
            "superseded": False}


def test_timeout_row_is_not_stored_and_is_retried(tmp_path, monkeypatch):
    sd, cfg = _sd(tmp_path), _cfg()
    monkeypatch.setattr(DI, "_load", lambda n, _l=DI._load: FJ if n == "feature_judge" else _l(n))
    monkeypatch.setattr(FJ, "ask_claude", lambda *a, **k: (None, 0.0))   # a timeout
    out = DI.apply(sd, cfg, [_c(1), _c(2)])
    assert out["records"] == 0 and out["timed_out"] == 2
    calls = []

    def ok(prompt, model, budget):
        calls.append(1)
        return {"kind": None, "area": None, "cost_usd": 0.01}
    out = DI.apply(sd, cfg, [_c(1), _c(2)], call=ok)
    assert len(calls) == 2 and out["timed_out"] == 0


def test_timeout_setting_default_and_override(tmp_path, monkeypatch):
    assert CFG.import_limits({})["timeout_s"] == 60
    assert CFG.import_limits(_cfg(timeout_s=300))["timeout_s"] == 300
    assert CFG.import_limits(_cfg(timeout_s="x"))["timeout_s"] == 60
    seen = []
    monkeypatch.setattr(DI, "_load", lambda n, _l=DI._load: FJ if n == "feature_judge" else _l(n))
    monkeypatch.setattr(FJ, "ask_claude", lambda *a, **k: seen.append(k.get("timeout_s")) or (None, 0.0))
    DI.apply(_sd(tmp_path), _cfg(timeout_s=300), [_c(1)])
    assert seen == [300]


def test_timeout_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "gc.pid"
    code = ("import subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            "open(%r,'w').write(str(p.pid));time.sleep(60)" % str(pidfile))
    try:
        FJ._run_group([sys.executable, "-c", code], timeout=1.5)
        raise AssertionError("expected a timeout")
    except FJ.subprocess.TimeoutExpired:
        pass
    pid = _wait_pid(pidfile)
    assert _gone(pid, 5), "grandchild survived the timeout"


def _wait_pid(pidfile, limit=30):
    """Poll for the pidfile (#1353): a slow machine may not have written it when the timeout fires."""
    end = time.time() + limit
    while time.time() < end:
        if pidfile.exists() and pidfile.read_text().strip():
            return int(pidfile.read_text())
        time.sleep(0.1)
    raise AssertionError("pidfile never appeared")


def _gone(pid, limit):
    end = time.time() + limit
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    return False


def test_read_after_group_kill_is_bounded(tmp_path, monkeypatch):
    """A descendant that left the group (own session) holds the pipe open; the post-kill read must give up."""
    monkeypatch.setattr(FJ, "_POST_KILL_READ_S", 1.0)
    pidfile = tmp_path / "esc.pid"
    code = ("import subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(25)'],start_new_session=True);"
            "open(%r,'w').write(str(p.pid));time.sleep(60)" % str(pidfile))
    t0 = time.time()
    try:
        FJ._run_group([sys.executable, "-c", code], timeout=3)
        raise AssertionError("expected a timeout")
    except FJ.subprocess.TimeoutExpired:
        pass
    took = time.time() - t0
    esc = _wait_pid(pidfile)
    _gone(esc, 0)                               # clean up the escaped sleeper
    assert took < 15, "post-kill read blocked for %.1fs" % took


def test_interrupt_kills_the_group_and_reraises(tmp_path, monkeypatch):
    pidfile = tmp_path / "int.pid"
    code = ("import subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            "open(%r,'w').write(str(p.pid));time.sleep(60)" % str(pidfile))
    real = FJ.subprocess.Popen

    class P(real):
        def communicate(self, *a, **k):
            _wait_pid(pidfile)                  # the grandchild exists before the "Ctrl-C" lands
            raise KeyboardInterrupt

    monkeypatch.setattr(FJ.subprocess, "Popen", P)
    try:
        FJ._run_group([sys.executable, "-c", code], timeout=60)
        raise AssertionError("expected KeyboardInterrupt")
    except KeyboardInterrupt:
        pass
    assert _gone(int(pidfile.read_text()), 5), "grandchild survived the interrupt"


def test_closed_gate_is_byte_identical(tmp_path):
    cfg = {"rulebook": {"records": {"enabled": False}, "import": {"classifier": "model"}}}

    def boom(*a):
        raise AssertionError("called")
    out = DI.apply(_sd(tmp_path), cfg, [_c(1)], call=boom)
    assert json.dumps(out, sort_keys=True) == json.dumps({"gate": False, "records": 0, "watch": 0, "note": ""},
                                                          sort_keys=True)
