"""#1601 - verify under admission: derived workers, a process group, a bounded tail, a measured cost, exit 5.

No model call, no network. Controls (break one thing, see red) are named in each docstring. The state folder is
always a temp one (SIGMA_RESOURCE_DIR); machine readings are faked through the lease module's own seams.
"""
import importlib.util
import json
import os
import pathlib
import resource
import signal
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
MIB = 1024 ** 2
GIB = 1024 ** 3
unix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")


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
def env(tmp_path, monkeypatch):
    """The verify module with a temp state folder and a roomy fake machine (16 GiB, 8 cpus)."""
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(tmp_path / "state"))
    for name in ("resource_guard", "resource_lease", "bounded_run", "resource_verify"):
        sys.modules.pop(name, None)
    verify = _fresh("resource_verify")
    lease = verify._sibling("resource_lease")
    box = {"available": 12 * GIB}

    def fake_sample():
        return lease.guard.Sample(total=16 * GIB, available=box["available"], swap_used=0, swap_total=0, swapouts=0,
                                  swapouts_at=0.0, pressure=1, cpu_count=8, load1=0.0)
    monkeypatch.setattr(lease, "take_sample", fake_sample)
    verify.box, verify.lease, verify.state = box, lease, tmp_path / "state"
    return verify


def seed(env, values):
    path = env.state / "observed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"verify": values}))


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ------------------------------------------------------------------------------------------ {workers} token
def test_workers_token_is_an_integer_and_other_braces_are_untouched(env):
    got = env.substitute_workers("pytest -n {workers} --x={other} {workers}", 3)
    assert got == "pytest -n 3 --x={other} 3"


@pytest.mark.parametrize("bad", ["2; echo x", "2", 2.0, True, 0, -1, None])
def test_workers_value_must_be_a_positive_int(env, bad):
    """Injection control: a non-int never reaches the shell text."""
    with pytest.raises(ValueError):
        env.substitute_workers("pytest -n {workers}", bad)


def test_injection_text_in_the_command_adds_nothing_the_author_did_not_write(env, tmp_path):
    marker = tmp_path / "x.txt"
    out = env.run("verify", "echo {workers}", str(tmp_path), 30, "g1")
    assert out.returncode == 0 and out.outputs[0].strip() == "1"   # cold ring: one worker, an integer only
    assert not marker.exists()


def test_literal_worker_detection(env):
    assert env.literal_workers("pytest -n 6 -q") == 6
    assert env.literal_workers("pytest --numprocesses=4") == 4
    assert env.literal_workers("pytest -n auto") is None
    assert env.literal_workers("pytest -nauto -k n6 --dist=loadfile") is None


# ------------------------------------------------------------------------------------------ env levers
def test_env_levers_reach_the_child_and_the_grant_is_derived(env, tmp_path):
    seed(env, [1 * GIB])
    env.box["available"] = 12 * GIB
    out = env.run("verify", 'echo "$SIGMA_VERIFY_WORKERS $PYTEST_XDIST_AUTO_NUM_WORKERS"', str(tmp_path), 30, "g1")
    n = out.workers
    assert 1 <= n <= 7 and out.outputs[0].split() == [str(n), str(n)]


def test_xdist_honours_the_auto_num_workers_variable(env, tmp_path):
    """The lever is kept only because this passes against the installed xdist; if it ever fails, delete the lever."""
    xdist = pytest.importorskip("xdist", reason="pytest-xdist not installed here")
    (tmp_path / "test_w.py").write_text(
        "import os\n\n\ndef test_a(worker_id):\n    pass\n\n\ndef test_b(worker_id):\n    pass\n\n\n"
        "def test_c(worker_id):\n    pass\n\n\ndef test_d(worker_id):\n    pass\n")
    cmd = (sys.executable + " -m pytest -p no:cacheprovider -n auto -v test_w.py 2>&1 | grep -o 'created: [0-9]*/[0-9]* workers'")
    done = subprocess.run(cmd, shell=True, cwd=tmp_path, capture_output=True, text=True,
                          env=dict({k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "XDIST"))},
                                    PYTEST_XDIST_AUTO_NUM_WORKERS="2", PYTHONDONTWRITEBYTECODE="1"))
    # the header names the worker count itself; counting the worker ids that ran tests depended on load (a replaced
    # worker or an uneven spread showed a third id in a full suite run)
    assert xdist and done.stdout.strip() == "created: 2/2 workers", done.stdout + done.stderr


# ------------------------------------------------------------------------------------------ hold
def test_hold_runs_nothing_and_returns_the_lease_hold(env, tmp_path):
    seed(env, [4 * GIB])
    env.box["available"] = 3 * GIB          # below the reserve plus one unit
    marker = tmp_path / "ran"
    out = env.run("verify", "touch %s" % marker, str(tmp_path), 30, "g1", config={"mode": "enforce"})
    assert out.hold is not None and out.hold.retry_after > 0 and "memory" in out.hold.reason
    assert out.result is None and not marker.exists()
    assert not list((env.state / "leases").glob("*.json"))


def test_literal_n_that_a_warm_ring_cannot_fit_holds_instead_of_running_fewer(env, tmp_path):
    seed(env, [4 * GIB])
    out = env.run("verify", "true -n 6", str(tmp_path), 30, "g1", config={"mode": "enforce"})
    assert out.hold is not None and "literal -n 6" in out.hold.reason
    assert not list((env.state / "leases").glob("*.json"))


# ------------------------------------------------------------------------------------------ process group
@unix_only
def test_timeout_stops_the_whole_group_including_a_sleeping_grandchild(env, tmp_path):
    """Control: with a bare subprocess.run (no group) the timeout cannot reach the grandchild, which stays alive."""
    pidfile = tmp_path / "pid"
    cmd = "sh -c 'sleep 300 & echo $! > %s; wait'" % pidfile
    out = env.run("verify", cmd, str(tmp_path), 1, "g1", term_grace=1)
    assert out.result.outcome == "timeout" and out.returncode != 0
    gone = False
    pid = int(pidfile.read_text())
    for _ in range(40):
        if not alive(pid):
            gone = True
            break
        time.sleep(0.25)
    if not gone:
        os.kill(pid, signal.SIGKILL)
    assert gone


@unix_only
def test_control_a_bare_subprocess_run_leaves_the_grandchild_alive(env, tmp_path):
    """The control for the test above: the same command through a bare subprocess.run runner is NOT stopped."""
    pidfile = tmp_path / "pid"

    def bare(command, cwd, timeout, **kwargs):
        try:
            subprocess.run(command, shell=True, cwd=cwd, timeout=timeout, capture_output=True)
        except subprocess.TimeoutExpired:
            pass
        return env._sibling("bounded_run").Result("timeout", None, "", "", 0, False, 0, "")

    env.run("verify", "sh -c 'sleep 300 & echo $! > %s; wait'" % pidfile, str(tmp_path), 1, "g1", run_group=bare)
    pid = int(pidfile.read_text())
    try:
        assert alive(pid)
    finally:
        os.kill(pid, signal.SIGKILL)


@unix_only
def test_a_killed_caller_leaves_no_group_behind(tmp_path):
    """SIGKILL the calling process; the lifeline must take the group (incl. grandchild) down within the grace."""
    pidfile = tmp_path / "pid"
    code = (
        "import sys, os\n"
        "sys.path.insert(0, %r)\n"
        "import resource_verify as v\n"
        "l = v._sibling('resource_lease')\n"
        "l.take_sample = lambda: l.guard.Sample(total=16 * 1024**3, available=12 * 1024**3, swap_used=0, swap_total=0,"
        " swapouts=0, swapouts_at=0.0, pressure=1, cpu_count=8, load1=0.0)\n"
        "v.run('verify', \"sh -c 'sleep 300 & echo $! > %s; wait'\", %r, 600, 'g1', term_grace=1)\n"
    ) % (str(SCRIPTS), str(pidfile), str(tmp_path))
    state = tmp_path / "state"
    child = subprocess.Popen([sys.executable, "-c", code], env=dict(os.environ, SIGMA_RESOURCE_DIR=str(state)))
    for _ in range(80):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.25)
    pid = int(pidfile.read_text())
    assert alive(pid)
    child.kill()
    child.wait()
    gone = False
    for _ in range(60):
        if not alive(pid):
            gone = True
            break
        time.sleep(0.25)
    if not gone:
        os.kill(pid, signal.SIGKILL)
    assert gone


# ------------------------------------------------------------------------------------------ bounded output
@unix_only
def test_200_mb_of_output_stays_bounded(env, tmp_path):
    """A command printing 200 MB: the caller's resident growth is measured (ru_maxrss delta, this process) and the
    kept text is the 64 KiB tail. Bound asserted: under 64 MiB growth and a tail under 128 KiB."""
    cmd = sys.executable + " -c \"import sys\nfor _ in range(200):\n    sys.stdout.write('x' * (1024 * 1024))\n\""
    scale = 1 if sys.platform == "darwin" else 1024
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
    out = env.run("verify", cmd, str(tmp_path), 120, "g1")
    grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale - before
    assert out.returncode == 0
    assert len(out.outputs[0]) < 128 * 1024
    assert grown < 64 * MIB, grown


# ------------------------------------------------------------------------------------------ learned cost
_FRESH_RUN = """
import importlib.util, json, sys
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("resource_verify", sys.argv[1] + "/resource_verify.py")
verify = importlib.util.module_from_spec(spec); sys.modules["resource_verify"] = verify; spec.loader.exec_module(verify)
lease = verify._sibling("resource_lease")
G = 1024 ** 3
lease.take_sample = lambda: lease.guard.Sample(total=16 * G, available=12 * G, swap_used=0, swap_total=0, swapouts=0,
                                                swapouts_at=0.0, pressure=1, cpu_count=8, load1=0.0)
out = verify.run("verify", sys.argv[2], sys.argv[3], 60, "g1", sample_gap=0.2)
print(json.dumps({"workers": out.workers}))
"""


@unix_only
def test_learned_cost_is_peak_tree_footprint_over_workers_within_20_percent(tmp_path):
    """The child tree allocates and touches 300 MiB; observed.json gets about 300 MiB per worker (one worker on a
    cold ring). The run happens in a fresh interpreter so the meter's tree holds only this test's child (inside a
    long-lived test worker other tests' leftovers were counted: a 2 GiB reading, seen in a full run).
    Control: a meter that sums nothing records the lease-holder's own tiny footprint, far outside 20%."""
    alloc = 300 * MIB
    cmd = sys.executable + " -c \"import time\nb=bytearray(%d)\nfor i in range(0,len(b),4096): b[i]=1\ntime.sleep(3)\"" % alloc
    state = tmp_path / "state"
    envv = {k: v for k, v in os.environ.items() if not k.startswith("SIGMA_")}
    envv["SIGMA_RESOURCE_DIR"] = str(state)
    for _ in range(3):          # a loaded host can compress pages mid-run and under-read one sample
        (state / "observed.json").unlink(missing_ok=True)
        done = subprocess.run([sys.executable, "-c", _FRESH_RUN, str(SCRIPTS), cmd, str(tmp_path)],
                              env=envv, capture_output=True, text=True, timeout=120)
        assert json.loads(done.stdout.strip().splitlines()[-1])["workers"] == 1, done.stdout + done.stderr
        ring = json.loads((state / "observed.json").read_text())["verify"]
        assert len(ring) == 1
        if abs(ring[-1] - alloc) <= 0.2 * alloc:
            break
    assert abs(ring[-1] - alloc) <= 0.2 * alloc, (ring, alloc)
    assert not list((state / "leases").glob("*.json"))      # released in finally


# ------------------------------------------------------------------------------------------ loop.py end to end
def _repo(tmp_path, cfg, command="echo hi"):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "--local", "sigma.allowRepositoryShellCommands", "true"],
                   check=True)
    sdlc = tmp_path / ".sdlc"
    (sdlc / "goals").mkdir(parents=True)
    (sdlc / "state").mkdir()
    cfg = dict(cfg, verify=dict(cfg.get("verify", {}), command=command), work={"enabled": False})
    (sdlc / "config.json").write_text(json.dumps(cfg))
    (sdlc / "goals" / "0001.md").write_text("---\nid: 0001\nstatus: pending\n---\nx\n")
    return str(sdlc), "0001"


def _loop(monkeypatch, tmp_path, available):
    for name in ("resource_guard", "resource_lease", "bounded_run", "resource_verify"):
        sys.modules.pop(name, None)
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(tmp_path / "rstate"))
    monkeypatch.setenv("SIGMA_RUN_ID", "")
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("loop_under_test_1601", SCRIPTS / "loop.py")
        loop = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loop)
    finally:
        sys.path.remove(str(SCRIPTS))
    lease = _fresh("resource_lease")
    monkeypatch.setattr(lease, "take_sample", lambda: lease.guard.Sample(
        total=16 * GIB, available=available, swap_used=0, swap_total=0, swapouts=0, swapouts_at=0.0, pressure=1,
        cpu_count=8, load1=0.0))
    return loop


def test_end_to_end_hold_gives_exit_5_no_evidence_events_and_done_still_refuses(tmp_path, monkeypatch, capsys):
    sdlc, goal = _repo(tmp_path, {"resource": {"mode": "enforce"}, "journal": {"enabled": True}, "action_log": {"enabled": True}})
    rstate = tmp_path / "rstate"
    rstate.mkdir()
    (rstate / "observed.json").write_text(json.dumps({"verify": [4 * GIB]}))
    loop = _loop(monkeypatch, tmp_path, available=3 * GIB)
    assert loop.verify_goal(sdlc, goal) == 5
    err = capsys.readouterr().err
    assert "RESOURCE-HOLD" in err and "retry_after" in err
    assert not (pathlib.Path(sdlc) / "state" / "verify" / "0001.json").exists()
    events = (pathlib.Path(sdlc) / "state" / "verify")
    assert not events.exists() or not list(events.glob("*.json"))
    files = [p for p in pathlib.Path(sdlc).rglob("*") if p.is_file() and p.suffix in (".jsonl", ".json", ".log")]
    texts = [p.read_text() for p in files]
    assert any('"kind": "verify_run"' in t and '"exit": "5"' in t for t in texts)      # action log
    assert any('"kind": "verify"' in t and ('"exit": 5' in t or '"exit":5' in t) for t in texts), \
        [str(p) for p in files]                                                          # ledger
    assert loop.state.done_refusal(sdlc, goal)             # no evidence: record done refuses


def test_end_to_end_admitted_run_writes_the_same_evidence_shape_as_off(tmp_path, monkeypatch):
    """mode off vs enforce: the evidence file has the same keys, and for the off run the exact historical shape."""
    shapes = {}
    for mode in ("off", "enforce"):
        sub = tmp_path / mode
        sub.mkdir()
        sdlc, goal = _repo(sub, {"resource": {"mode": mode}}, command="echo hi")
        loop = _loop(monkeypatch, sub, available=12 * GIB)
        assert loop.verify_goal(sdlc, goal) == 0
        ev = json.loads((pathlib.Path(sdlc) / "state" / "verify" / "0001.json").read_text())
        shapes[mode] = ev
    assert list(shapes["off"]) == list(shapes["enforce"])
    for key in ("command", "exit", "verify_state", "tail"):
        assert shapes["off"][key] == shapes["enforce"][key], key
    assert shapes["off"]["tail"] == ["hi"]
    assert not (tmp_path / "off" / "rstate").exists()           # off: the lease table is never touched


def test_the_guard_engages_only_for_an_explicit_non_off_mode(tmp_path, monkeypatch):
    loop = _loop(monkeypatch, tmp_path, available=12 * GIB)
    assert not loop._resource_verify_on({"resource": {"mode": "off"}})
    assert not loop._resource_verify_on({})
    assert loop._resource_verify_on({"resource": {"mode": "observe"}})
    assert loop._verify_timeout_seconds({"verify": {"timeout_minutes": 5}}) == 300
    assert loop._verify_timeout_seconds({"verify": {"timeout_minutes": True}}) == 3600
