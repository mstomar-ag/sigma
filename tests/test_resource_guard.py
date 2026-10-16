"""#1597 — resource_guard: platform readers and the typed Sample.

Real-host tests run on macOS only; Linux readers are driven by fixture trees (a root directory holding
proc/ and sys/fs/cgroup/). Controls are described next to the tests that carry them.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "sigma-loop" / "scripts" / "resource_guard.py"
PARITY = ROOT / "tools" / "parity" / "resource_sources.py"
DARWIN = sys.platform == "darwin"
GIB = 1024 ** 3


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guard():
    return _load(SCRIPT, "resource_guard_under_test")


def _write(root, rel, text):
    path = pathlib.Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fixture(root, *, total_kb=16 * 1024 * 1024, avail_kb=8 * 1024 * 1024, psi=True, cgroup=None):
    """A plain Linux tree. `cgroup` = (max, current) for a leaf cgroup `app` under the mount, or None."""
    _write(root, "proc/meminfo", f"MemTotal: {total_kb} kB\nMemAvailable: {avail_kb} kB\n"
                                 "SwapTotal: 2048 kB\nSwapFree: 1024 kB\n")
    _write(root, "proc/vmstat", "pgfault 1\npswpout 42\n")
    _write(root, "proc/loadavg", "0.50 0.40 0.30 1/100 999\n")
    if psi:
        _write(root, "proc/pressure/memory",
               "some avg10=1.50 avg60=0.00 avg300=0.00 total=1\nfull avg10=0.25 avg60=0.00 avg300=0.00 total=1\n")
    if cgroup is not None:
        _write(root, "proc/self/cgroup", "0::/app\n")
        _write(root, "proc/self/mountinfo",
               "30 25 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n")
        _write(root, "sys/fs/cgroup/cgroup.controllers", "memory cpu\n")
        _write(root, "sys/fs/cgroup/memory.max", "max\n")
        _write(root, "sys/fs/cgroup/memory.current", "0\n")
        _write(root, "sys/fs/cgroup/app/memory.max", f"{cgroup[0]}\n")
        _write(root, "sys/fs/cgroup/app/memory.current", f"{cgroup[1]}\n")
    return pathlib.Path(root)


@pytest.mark.skipif(not DARWIN, reason="macOS real-host reader")
def test_macos_real_host_sample():
    s = guard().sample()
    hw = int(subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout)
    assert s.total == hw
    assert 0 <= s.available <= s.total
    assert s.cpu_count >= 1 and s.load1 >= 0
    assert s.pressure in (1, 2, 4, None)
    assert 0 <= s.swap_used <= max(s.swap_total, s.swap_used)


def test_linux_plain_fixture(tmp_path):
    s = guard().linux_sample(fixture(tmp_path), cpus=4)
    assert s.total == 16 * GIB and s.available == 8 * GIB
    assert (s.swap_total, s.swap_used, s.swapouts) == (2048 * 1024, 1024 * 1024, 42)
    assert s.pressure == {"some": 1.5, "full": 0.25}
    assert s.cpu_count == 4 and s.load1 == 0.5 and s.swapouts_at > 0


def test_linux_cgroup_limit_below_memtotal(tmp_path):
    root = fixture(tmp_path, cgroup=(2 * GIB, 512 * 1024 * 1024))
    s = guard().linux_sample(root, cpus=2)
    assert s.available == int(1.5 * GIB)  # min(MemAvailable 8 GiB, 2 GiB - 0.5 GiB)


def test_linux_cgroup_limit_above_meminfo_is_ignored(tmp_path):
    root = fixture(tmp_path, cgroup=(64 * GIB, 0))
    assert guard().linux_sample(root, cpus=2).available == 8 * GIB


def test_linux_absent_psi_is_null(tmp_path):
    s = guard().linux_sample(fixture(tmp_path, psi=False), cpus=2)
    assert s.pressure is None


def test_linux_cpu_is_affinity_and_cpu_max(tmp_path):
    g = guard()
    root = fixture(tmp_path, cgroup=(4 * GIB, 0))
    assert g.linux_sample(root, cpus=3).cpu_count == 3
    _write(root, "sys/fs/cgroup/app/cpu.max", "150000 100000\n")
    assert g.linux_sample(root, cpus=8).cpu_count == 1.5
    _write(root, "sys/fs/cgroup/app/cpu.max", "max 100000\n")
    assert g.linux_sample(root, cpus=8).cpu_count == 8


def test_unreadable_meminfo_is_unsupported(tmp_path):
    g = guard()
    root = fixture(tmp_path)
    (root / "proc/meminfo").unlink()
    with pytest.raises(g.Unsupported):
        g.linux_sample(root, cpus=2)
    _write(root, "proc/meminfo", "MemTotal: 100 kB\n")  # no MemAvailable
    with pytest.raises(g.Unsupported):
        g.linux_sample(root, cpus=2)


def test_malformed_vmstat_is_unsupported(tmp_path):
    g = guard()
    root = fixture(tmp_path)
    _write(root, "proc/vmstat", "pswpout abc\n")
    with pytest.raises(g.Unsupported):
        g.linux_sample(root, cpus=2)
    _write(root, "proc/vmstat", "pswpout  \n")
    with pytest.raises(g.Unsupported):
        g.linux_sample(root, cpus=2)


def test_malformed_pressure_is_unsupported(tmp_path):
    g = guard()
    root = fixture(tmp_path)
    _write(root, "proc/pressure/memory", "some avg10=oops avg60=0.00\n")
    with pytest.raises(g.Unsupported):
        g.linux_sample(root, cpus=2)


def test_malformed_vmstat_cli_exits_3(tmp_path):
    root = fixture(tmp_path)
    _write(root, "proc/vmstat", "pswpout abc\n")
    code = textwrap.dedent("""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("rg", sys.argv[1])
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        m.sys.platform = "linux"
        orig = m.linux_sample
        m.linux_sample = lambda root="/": orig(sys.argv[2], cpus=2)
        sys.exit(m.main(["sample", "--json"]))
    """)
    done = subprocess.run([sys.executable, "-c", code, str(SCRIPT), str(root)], capture_output=True, text=True)
    assert done.returncode == 3 and done.stdout == ""


def test_footprint_rejects_pid_outside_c_int():
    g = guard()
    assert g.footprint(2 ** 31) is None
    assert g.footprint(2 ** 32 + os.getpid()) is None  # would wrap to this very process as a C int
    assert g.footprint(0) is None and g.footprint(-1) is None


def _host_send_refs():
    """User references on this task's send right to the host port (mach_host_self adds one per call)."""
    c = __import__("ctypes")
    lib = c.CDLL("libSystem.B.dylib")
    lib.mach_host_self.restype = c.c_uint32
    lib.mach_port_get_refs.argtypes = [c.c_uint32, c.c_uint32, c.c_int, c.POINTER(c.c_uint32)]
    task = c.c_uint32.in_dll(lib, "mach_task_self_").value
    host = lib.mach_host_self()
    refs = c.c_uint32()
    assert lib.mach_port_get_refs(task, host, 0, c.byref(refs)) == 0  # 0 = MACH_PORT_RIGHT_SEND
    lib.mach_port_deallocate(task, host)
    return refs.value


@pytest.mark.skipif(not DARWIN, reason="macOS host port")
def test_macos_sample_does_not_leak_host_port():
    g = guard()
    g.sample()
    before = _host_send_refs()
    for _ in range(1000):
        g.sample()
    assert _host_send_refs() - before <= 1


def test_win32_is_unsupported_and_cli_exits_3(monkeypatch):
    g = guard()
    monkeypatch.setattr(g.sys, "platform", "win32")
    with pytest.raises(g.Unsupported):
        g.sample()
    code = textwrap.dedent("""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("rg", sys.argv[1])
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        m.sys.platform = "win32"
        sys.exit(m.main(["sample", "--json"]))
    """)
    done = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], capture_output=True, text=True)
    assert done.returncode == 3 and done.stdout == ""
    assert len(done.stderr.strip().splitlines()) == 1


@pytest.mark.skipif(not DARWIN, reason="macOS real-host CLI")
def test_cli_json_on_macos():
    done = subprocess.run([sys.executable, str(SCRIPT), "sample", "--json"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    for key in ("total", "available", "swap_used", "swap_total", "swapouts", "pressure", "cpu_count", "load1"):
        assert key in data


def test_footprint_own_pid_and_missing_pid():
    g = guard()
    if not DARWIN and not sys.platform.startswith("linux"):
        pytest.skip("no reader")
    got = g.footprint(os.getpid())
    assert got is not None and got[0] > 0 and isinstance(got[1], bool)
    assert g.footprint(2 ** 22 + 12345) is None
    assert g.footprint(-5) is None


def test_footprint_sees_child_allocation():
    g = guard()
    if not DARWIN and not sys.platform.startswith("linux"):
        pytest.skip("no reader")
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time\nb=bytearray(200*1024*1024)\nfor i in range(0,len(b),4096): b[i]=1\n"
         "print('r',flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "r"
        got = g.footprint(child.pid)
        assert got is not None and got[0] >= 150 * 1024 * 1024
    finally:
        child.stdin.close()
        child.wait(timeout=30)


# --- cross-check against the parity reader ---------------------------------------------------------
#: Free + inactive moves between two reads; 2 GiB or 15 % of total is far wider than that drift and far
#: narrower than a 2x skew of a 24 GiB host's availability.
def _within(a, b, total):
    return abs(a - b) <= max(2 * GIB, 0.15 * total)


def _parity_available():
    return _load(PARITY, "parity_under_test").darwin_memory()["available_bytes"]


@pytest.mark.skipif(not DARWIN, reason="parity reader is the macOS one here")
def test_available_agrees_with_parity_reader():
    s = guard().sample()
    assert _within(s.available, _parity_available(), s.total)


@pytest.mark.skipif(not DARWIN, reason="parity reader is the macOS one here")
def test_skewed_reader_is_caught():
    """Control: the tolerance check goes red when one reader is skewed by 2x (kept as a live assertion)."""
    s = guard().sample()
    assert not _within(s.available * 2, _parity_available(), s.total)


# --- no processes, no writes ------------------------------------------------------------------------
def _forbid(monkeypatch):
    import builtins
    import io

    def no_spawn(*a, **k):
        raise AssertionError("sample spawned a process")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+"):
            raise AssertionError(f"sample opened {file} for write")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)


def test_sample_spawns_nothing_and_writes_nothing(monkeypatch, tmp_path):
    g = guard()
    root = fixture(tmp_path / "fx")
    _forbid(monkeypatch)
    g.linux_sample(root, cpus=2)
    if DARWIN:
        g.sample()
        g.footprint(os.getpid())


def test_spawn_control_is_red(monkeypatch, tmp_path):
    """Control: a subprocess.run inside a reader trips the Popen guard."""
    g = guard()
    root = fixture(tmp_path / "fx")
    _forbid(monkeypatch)
    real = g._read_text
    monkeypatch.setattr(g, "_read_text", lambda p: (subprocess.run(["true"]), real(p))[1])
    with pytest.raises(AssertionError, match="spawned"):
        g.linux_sample(root, cpus=2)


def test_module_has_no_stop_signals():
    text = SCRIPT.read_text()
    assert "SIGSTOP" not in text and "SIGCONT" not in text and "subprocess" not in text
