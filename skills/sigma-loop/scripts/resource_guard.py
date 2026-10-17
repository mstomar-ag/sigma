"""Machine resource readers: one typed `Sample` of memory, swap, pressure and CPU, and a per-process footprint.

WHAT IT IS. A small library with a command line (`resource_guard.py sample [--json]`). It reads what the operating
system says about this machine so a later caller can DERIVE a limit from it instead of copying a constant from a
developer's laptop. Nothing in the product calls it yet: this release ships the readers and the proof they agree
with an independent reader.

THE CONTRACT.
  * `sample(root="/") -> Sample`. Fields: total, available, swap_used, swap_total (bytes), swapouts (a counter) and
    swapouts_at (wall-clock seconds when it was read), pressure, cpu_count, load1.
  * macOS reads through ctypes: `sysctlbyname` (hw.memsize, vm.swapusage, kern.memorystatus_vm_pressure_level) and
    `host_statistics64` (available = free + inactive pages). `pressure` is the OS level 1, 2 or 4.
  * Linux reads /proc and cgroup v2 under `root`: MemAvailable, reduced to the smallest `memory.max - memory.current`
    of every visible cgroup ancestor; `pswpout` from /proc/vmstat; `pressure` is {"some": avg10, "full": avg10} from
    /proc/pressure/memory, or None when the file is absent; cpu_count from the affinity mask, reduced by `cpu.max`.
  * `footprint(pid) -> (bytes, approx) | None`. macOS: `ri_phys_footprint` from `proc_pid_rusage` (resident size is
    NOT the measure: summed RSS undercounted summed footprint by about 2.5x on a measured 24 GiB machine). Linux:
    `Pss` from smaps_rollup, else `statm` resident pages with approx=True. An unreadable or missing pid is None.
  * Any other platform, or an unreadable total or available, raises `Unsupported(reason)`. Never a guess, never a
    shell-command fallback. The command line then prints one line to stderr, nothing to stdout, and exits 3.

DERIVED LIMITS (#1598). `derive(sample, config, leases, observed) -> Limits` turns a Sample into a reserve, a share cap
and a CPU reserve as ratios of THIS machine (the fractions are policy defaults, uncalibrated against a real
exhaustion, and `Limits.uncalibrated` says so). The `resource` config block is all-null = derived; a bad value reads as
derived plus a note, never as zero. `decide(history, limits, unit_cost, prior)` holds on the lowest `available` of the
last three samples and resumes on the higher of the lowest two, after `resume_samples` samples at least `sample_gap`
seconds apart. `hold.json` (written by atomic rename; a corrupt file reads as open plus a note) lives outside every
repository.

NOT HERE, ON PURPOSE. No child process, no signal, no gate that stops anything: this module reports facts and
decisions; a caller acts on them.
"""
import contextlib
import ctypes
import json
import math
import mmap
import os
import sys
import threading
import time

EXIT_UNSUPPORTED = 3


class Unsupported(Exception):
    """The platform or a required source cannot be read; `reason` says which."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Sample:
    FIELDS = ("total", "available", "swap_used", "swap_total", "swapouts", "swapouts_at", "pressure", "cpu_count",
              "load1")
    __slots__ = FIELDS

    def __init__(self, **values):
        for name in self.FIELDS:
            setattr(self, name, values[name])

    def as_dict(self):
        return {name: getattr(self, name) for name in self.FIELDS}

    def __repr__(self):
        return "Sample(%s)" % ", ".join("%s=%r" % kv for kv in self.as_dict().items())


# ---------------------------------------------------------------------------------------------------- Linux
def _read_text(path):
    with open(path, "r") as handle:
        return handle.read()


def _try(path):
    try:
        return _read_text(path)
    except (OSError, UnicodeDecodeError):
        return None


def _kv_bytes(text):
    out = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[name.strip()] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1)
    return out


def _cgroup_chain(root):
    """Directories from the process's cgroup up to the v2 mount, or [] when there is no readable v2 mapping."""
    cg, mi = _try(os.path.join(root, "proc/self/cgroup")), _try(os.path.join(root, "proc/self/mountinfo"))
    if cg is None or mi is None:
        return []
    rel = next((line[3:] for line in cg.splitlines() if line.startswith("0::")), None)
    mount = None
    for line in mi.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if sep and right.split()[:1] == ["cgroup2"] and len(fields) >= 5:
            mount = fields[4]
            break
    if rel is None or mount is None:
        return []
    top = os.path.normpath(os.path.join(root, mount.lstrip("/")))
    path = os.path.normpath(os.path.join(top, rel.strip().lstrip("/")))
    chain = []
    while True:
        chain.append(path)
        if path == top or len(path) <= len(top):
            return chain
        path = os.path.dirname(path)


def _cgroup_room(chain):
    room = None
    for directory in chain:
        limit, used = _try(os.path.join(directory, "memory.max")), _try(os.path.join(directory, "memory.current"))
        if limit is None or used is None or limit.strip() == "max":
            continue
        try:
            left = max(int(limit) - int(used), 0)
        except ValueError:
            continue
        room = left if room is None else min(room, left)
    return room


def _cgroup_cpus(chain):
    cap = None
    for directory in chain:
        text = _try(os.path.join(directory, "cpu.max"))
        parts = text.split() if text else []
        if len(parts) == 2 and parts[0] != "max":
            try:
                value = int(parts[0]) / int(parts[1])
            except (ValueError, ZeroDivisionError):
                continue
            cap = value if cap is None else min(cap, value)
    return cap


def _pressure(text):
    found = {}
    for line in (text or "").splitlines():
        fields = line.split()
        if fields and fields[0] in ("some", "full"):
            for item in fields[1:]:
                if item.startswith("avg10="):
                    try:
                        found[fields[0]] = float(item[6:])
                    except ValueError:
                        raise Unsupported("pressure text is malformed") from None
    return found or None


def linux_sample(root="/", cpus=None):
    """Sample from a /proc and cgroup-v2 tree under `root`. `cpus` overrides the affinity count (fixtures)."""
    meminfo = _try(os.path.join(root, "proc/meminfo"))
    if meminfo is None:
        raise Unsupported("/proc/meminfo is unreadable")
    mem = _kv_bytes(meminfo)
    if mem.get("MemTotal", 0) <= 0 or "MemAvailable" not in mem:
        raise Unsupported("/proc/meminfo lacks MemTotal or MemAvailable")
    total, available = mem["MemTotal"], min(mem["MemAvailable"], mem["MemTotal"])
    chain = _cgroup_chain(root)
    room = _cgroup_room(chain)
    if room is not None:
        available = min(available, room)
    swap_total = mem.get("SwapTotal", 0)
    swapouts = 0
    for line in (_try(os.path.join(root, "proc/vmstat")) or "").splitlines():
        if line.startswith("pswpout "):
            try:
                swapouts = int(line.split()[1])
            except (ValueError, IndexError):
                raise Unsupported("/proc/vmstat is malformed") from None
    swapouts_at = time.time()
    if cpus is None:
        cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    cap = _cgroup_cpus(chain)
    load = _try(os.path.join(root, "proc/loadavg"))
    return Sample(total=total, available=available, swap_used=max(swap_total - mem.get("SwapFree", 0), 0),
                  swap_total=swap_total, swapouts=swapouts, swapouts_at=swapouts_at,
                  pressure=_pressure(_try(os.path.join(root, "proc/pressure/memory"))),
                  cpu_count=cpus if cap is None else min(cpus, cap),
                  load1=float(load.split()[0]) if load else os.getloadavg()[0])


def _linux_footprint(pid):
    base = "/proc/%d/" % pid
    text = _try(base + "smaps_rollup")
    if text:
        for line in text.splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) * 1024, False
    text = _try(base + "statm")
    if text and len(text.split()) >= 2:
        return int(text.split()[1]) * os.sysconf("SC_PAGE_SIZE"), True
    return None


# ---------------------------------------------------------------------------------------------------- macOS
class _VMStats(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in ("free_count", "active_count", "inactive_count", "wire_count")]
    _fields_ += [(n, ctypes.c_uint64) for n in ("zero_fill_count", "reactivations", "pageins", "pageouts", "faults",
                                                "cow_faults", "lookups", "hits", "purges")]
    _fields_ += [(n, ctypes.c_uint32) for n in ("purgeable_count", "speculative_count")]
    _fields_ += [(n, ctypes.c_uint64) for n in ("decompressions", "compressions", "swapins", "swapouts")]
    _fields_ += [(n, ctypes.c_uint32) for n in ("compressor_page_count", "throttled_count", "external_page_count",
                                                "internal_page_count")]
    _fields_ += [("total_uncompressed_pages_in_compressor", ctypes.c_uint64)]


class _SwapUsage(ctypes.Structure):
    _fields_ = [("total", ctypes.c_uint64), ("avail", ctypes.c_uint64), ("used", ctypes.c_uint64),
                ("pagesize", ctypes.c_int32), ("encrypted", ctypes.c_int32)]


class _RusageV2(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)]
    _fields_ += [(n, ctypes.c_uint64) for n in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size", "resident_size",
        "phys_footprint", "proc_start_abstime", "proc_exit_abstime", "child_user_time", "child_system_time",
        "child_pkg_idle_wkups", "child_interrupt_wkups", "child_pageins", "child_elapsed_abstime",
        "diskio_bytesread", "diskio_byteswritten")]


_RUSAGE_INFO_V2 = 2
_HOST_VM_INFO64 = 4


def _darwin_lib():
    c = ctypes
    try:
        lib = c.CDLL("libSystem.B.dylib", use_errno=True)
        lib.mach_host_self.argtypes, lib.mach_host_self.restype = [], c.c_uint32
        lib.mach_port_deallocate.argtypes, lib.mach_port_deallocate.restype = [c.c_uint32, c.c_uint32], c.c_int
        lib.task_self = c.c_uint32.in_dll(lib, "mach_task_self_")
        lib.host_statistics64.argtypes = [c.c_uint32, c.c_int, c.c_void_p, c.POINTER(c.c_uint32)]
        lib.host_statistics64.restype = c.c_int
        lib.sysctlbyname.argtypes = [c.c_char_p, c.c_void_p, c.POINTER(c.c_size_t), c.c_void_p, c.c_size_t]
        lib.sysctlbyname.restype = c.c_int
        lib.proc_pid_rusage.argtypes, lib.proc_pid_rusage.restype = [c.c_int, c.c_int, c.c_void_p], c.c_int
        page = c.c_ulong.in_dll(lib, "vm_kernel_page_size").value
    except (OSError, AttributeError, ValueError):
        raise Unsupported("libSystem bindings are unavailable") from None
    if page <= 0:
        raise Unsupported("vm_kernel_page_size is unreadable")
    return lib, page


def _sysctl(lib, name, ctype):
    value, size = ctype(), ctypes.c_size_t(ctypes.sizeof(ctype))
    failed = lib.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) != 0
    return None if failed or size.value != ctypes.sizeof(ctype) else value


def darwin_sample():
    lib, page = _darwin_lib()
    total = _sysctl(lib, b"hw.memsize", ctypes.c_uint64)
    stats = _VMStats()
    count = ctypes.c_uint32(ctypes.sizeof(stats) // 4)
    host = lib.mach_host_self()  # a new send right on every call: release it or a poller leaks one per sample
    try:
        failed = total is None or total.value <= 0 or not host or lib.host_statistics64(
            host, _HOST_VM_INFO64, ctypes.byref(stats), ctypes.byref(count)) != 0
    finally:
        if host:
            lib.mach_port_deallocate(lib.task_self.value, host)
    if failed:
        raise Unsupported("hw.memsize or host_statistics64 is unreadable")
    available = min((stats.free_count + stats.inactive_count) * page, total.value)
    swap = _sysctl(lib, b"vm.swapusage", _SwapUsage)
    level = _sysctl(lib, b"kern.memorystatus_vm_pressure_level", ctypes.c_int)
    ncpu = _sysctl(lib, b"hw.logicalcpu", ctypes.c_int)
    return Sample(total=total.value, available=available, swap_used=swap.used if swap else 0,
                  swap_total=swap.total if swap else 0, swapouts=stats.swapouts, swapouts_at=time.time(),
                  pressure=level.value if level else None, cpu_count=ncpu.value if ncpu else (os.cpu_count() or 1),
                  load1=os.getloadavg()[0])


def _darwin_footprint(pid):
    try:
        lib, _ = _darwin_lib()
    except Unsupported:
        return None
    info = _RusageV2()
    if lib.proc_pid_rusage(pid, _RUSAGE_INFO_V2, ctypes.byref(info)) != 0:
        return None
    return int(info.phys_footprint), False


# ---------------------------------------------------------------------------------------------------- public
def sample(root="/"):
    """One reading of this machine. Raises Unsupported rather than guessing."""
    if sys.platform == "darwin":
        return darwin_sample()
    if sys.platform.startswith("linux"):
        return linux_sample(root)
    raise Unsupported("no reader for platform %s" % sys.platform)


def footprint(pid):
    """(bytes, approx) for one process, or None if it cannot be read. Never raises."""
    try:
        pid = int(pid)
        if pid <= 0 or pid > 2 ** 31 - 1:  # proc_pid_rusage takes a C int: a larger pid would wrap to another process
            return None
        if sys.platform == "darwin":
            return _darwin_footprint(pid)
        if sys.platform.startswith("linux"):
            return _linux_footprint(pid)
    except (OSError, ValueError, OverflowError):
        pass
    return None


# ------------------------------------------------------------------------------------------ derived limits
PAGE_SIZE = mmap.PAGESIZE
DEFAULTS = {"mode": None, "reserve_fraction": None, "max_share_fraction": None, "cpu_reserve_cores": None,
            "load_hold_ratio": None, "swapout_hold_mb_per_s": None, "shed": None, "shed_after": None, "max_goals": None,
            "max_verify_workers": None, "hold_alert_minutes": None}
# Policy defaults, ratios of the machine. Uncalibrated: not measured against a real exhaustion.
RESERVE_FRACTION = 0.20
SHARE_FRACTION = 0.60
SESSION_FRACTION = 1 / 32
LOAD_HOLD_RATIO = 1.0
PSI_HOLD = 10.0
RESUME_SAMPLES = 2
SHED_AFTER = 3          # consecutive critical samples before a verify run is shed (#1602, key #1769)
SAMPLE_GAP = 2.0
_MODES = ("enforce", "observe", "off")
_SHEDS = ("verify", "off")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid(key, value):
    if key == "mode":
        return value in _MODES
    if key == "shed":
        return value in _SHEDS
    if key in ("reserve_fraction", "max_share_fraction"):
        return _number(value) and 0 < value < 1
    if key in ("load_hold_ratio", "swapout_hold_mb_per_s"):
        return _number(value) and value > 0
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def resolve_config(cfg):
    """(resolved, notes): every DEFAULTS key, None where absent, null or bad; a bad value adds one note."""
    resolved, notes = dict(DEFAULTS), []
    if cfg is None:
        return resolved, notes
    if not isinstance(cfg, dict):
        return resolved, ["resource: config block is not an object; limits derived"]
    for key, value in cfg.items():
        if key not in DEFAULTS:
            notes.append("resource.%s: unknown key ignored" % key)
        elif value is None:
            continue
        elif _valid(key, value):
            resolved[key] = value
        else:
            notes.append("resource.%s: bad value %r read as derived" % (key, value))
    return resolved, notes


class Limits:
    def __init__(self, **values):
        self.__dict__.update(values)

    def headroom_for(self, available):
        return min(available - self.reserve - self.unmaterialised, self.share_cap - self.leased)

    def admits(self, kind, running=0):
        if self.critical:
            return False
        if kind == "goal":
            return not self.goal_block
        return not self.goal_block or running < 1


def derive(sample, config=None, leases=(), observed=None):
    """Limits for this machine. `leases` are (cost_bytes, materialised) pairs; `observed` may hold `session_cost`."""
    cfg, notes = resolve_config(config)
    reserve = int(sample.total * (cfg["reserve_fraction"] or RESERVE_FRACTION))
    share_cap = int(sample.total * (cfg["max_share_fraction"] or SHARE_FRACTION))
    cpu_reserve = cfg["cpu_reserve_cores"] or max(1, -(-sample.cpu_count // 8))
    if cfg["cpu_reserve_cores"] and cfg["cpu_reserve_cores"] >= sample.cpu_count:
        notes.append("resource.cpu_reserve_cores: %r is not below the %d cores here; read as derived"
                     % (cfg["cpu_reserve_cores"], sample.cpu_count))
        cpu_reserve = max(1, -(-sample.cpu_count // 8))
    load_hold = (cfg["load_hold_ratio"] or LOAD_HOLD_RATIO) * sample.cpu_count
    session = (observed or {}).get("session_cost")
    session_cost = int(session) if _number(session) and session > 0 else int(sample.total * SESSION_FRACTION)
    pressure = sample.pressure
    if isinstance(pressure, dict):
        some = pressure.get("some")
        level = 0
        psi_high = _number(some) and some > PSI_HOLD
    else:
        level = pressure if isinstance(pressure, int) and not isinstance(pressure, bool) else 0
        psi_high = False
    critical = level >= 4 or sample.available < reserve / 2
    limits = Limits(
        mode=cfg["mode"] or "enforce", shed=cfg["shed"] or "verify", shed_after=cfg["shed_after"] or SHED_AFTER, reserve=reserve, share_cap=share_cap,
        cpu_reserve=cpu_reserve, load_hold=load_hold, session_cost=session_cost,
        swap_hold=cfg["swapout_hold_mb_per_s"], max_goals=cfg["max_goals"],
        max_verify_workers=cfg["max_verify_workers"], hold_alert_minutes=cfg["hold_alert_minutes"],
        unmaterialised=sum(c for c, done in leases if not done), leased=sum(c for c, _ in leases),
        goal_block=level >= 2 or psi_high or critical, critical=critical,
        uncalibrated=True, notes=notes)
    limits.headroom = limits.headroom_for(sample.available)
    return limits


# ------------------------------------------------------------------------------------------- hysteresis
class Decision:
    def __init__(self, state, reason, resource, since, retry_after, swap_rate=None):
        self.state, self.reason, self.resource = state, reason, resource
        self.since, self.retry_after, self.swap_rate = since, retry_after, swap_rate

    def as_dict(self):
        return {"state": self.state, "since": self.since, "reason": self.reason, "resource": self.resource,
                "retry_after": self.retry_after}


def _hold_level(values):
    """Hold decisions use the lowest of the last three readings."""
    return min(values[-3:])


def _resume_level(values):
    """Resume uses the higher of the lowest two of the last three readings."""
    return sorted(values[-3:])[min(1, len(values[-3:]) - 1)]


def _spaced_run(times, need, gap):
    """True when the last `need` timestamps exist and each is at least `gap` seconds after the one before."""
    run = times[-need:]
    return len(run) >= need and all(b - a >= gap for a, b in zip(run, run[1:]))


def swap_rate(history):
    """MB/s of swap-out over the last three samples; None when it cannot be told."""
    pair = history[-3:]
    if len(pair) < 2 or pair[-1].swapouts_at <= pair[0].swapouts_at:
        return None
    pages = pair[-1].swapouts - pair[0].swapouts
    return max(pages, 0) * PAGE_SIZE / 1e6 / (pair[-1].swapouts_at - pair[0].swapouts_at)


def decide(history, limits, unit_cost, prior="open", resume_samples=RESUME_SAMPLES, sample_gap=SAMPLE_GAP,
           now=None, prior_since=None):
    """Hold or open for one requested unit costing `unit_cost` bytes. `history` is oldest first.

    `prior_since` is the start of the hold already in force; a repeat hold keeps it. No history reads as open."""
    now = time.time() if now is None else now
    if not history:
        return Decision("open", "no samples", None, now, None, None)
    rate = swap_rate(history)
    avail = [s.available for s in history]
    held = prior == "hold"
    since = prior_since if held and prior_since is not None else now
    resume_line = unit_cost + limits.session_cost
    reason = resource = None
    if limits.critical:
        reason, resource = "critical", "memory"
    elif limits.swap_hold and rate is not None and rate > (limits.swap_hold / 4 if held else limits.swap_hold):
        reason, resource = "swap", "swap"
    elif held:
        ok = (limits.headroom_for(_resume_level(avail)) >= resume_line
              and all(limits.headroom_for(a) >= resume_line for a in avail[-resume_samples:])
              and _spaced_run([s.swapouts_at for s in history], resume_samples, sample_gap))
        if not ok:
            reason, resource = "memory", "memory"
    elif limits.headroom_for(_hold_level(avail)) < unit_cost:
        reason, resource = "memory", "memory"
    if reason and limits.mode != "enforce":
        return Decision("open", "%s: would hold (%s)" % (limits.mode, reason), None, now, None, rate)
    if reason:
        return Decision("hold", reason, resource, since, now + sample_gap * resume_samples, rate)
    return Decision("open", "ok", None, now, None, rate)


# ------------------------------------------------------------------------------------------ session gate (#1603)
_LEASE = {}


def _lease():
    """The lease module, loaded on first use (resource_lease imports this module, so not at import time)."""
    if "m" not in _LEASE:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location("resource_lease", os.path.join(here, "resource_lease.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LEASE["m"] = module
    return _LEASE["m"]


def session_mode(config=None, env=None):
    """enforce | observe | off for a model-session spawn: the config block, else SIGMA_RESOURCE_MODE, else off.

    Off by default on purpose: no config route reaches every spawn site yet, so the gate is the operator's opt-in
    (SAFETY: nothing consumes or records anything unasked). off means no lease, no state, no sample.
    """
    env = os.environ if env is None else env
    mode = config.get("mode") if isinstance(config, dict) else None
    if mode not in _MODES:
        mode = env.get("SIGMA_RESOURCE_MODE")
    return mode if mode in _MODES else "off"


@contextlib.contextmanager
def session_lease(kind, goal, repo=None, ttl=None, env=None, config=None, renew_every=None, clock=time.time):
    """Hold one "session" lease around a model-session spawn. Yields None (mode off), a Grant, or a Hold.

    The caller treats a Hold as its own existing "could not start now" outcome. The lease is released in `finally`,
    so an exception or a timeout in the child never leaves one behind. Never raises for pressure, a busy lock or a
    table error. On a platform that cannot take a lease (or a table error): observe proceeds unguarded, enforce yields a
    Hold that says so (refuse loudly rather than a silent half-guarantee).

    While a Grant is held a daemon thread renews the lease every `renew_every` seconds (default ttl/3), so a session
    longer than the ttl keeps it; the thread is stopped and joined before release. Off, a Hold, an error: no thread.
    """
    mode = session_mode(config, env)
    if mode == "off":
        yield None
        return
    lease = _lease()
    cfg = dict(config) if isinstance(config, dict) else {}
    cfg["mode"] = mode
    try:
        got = lease.admit("session", 1, repo if repo is not None else os.getcwd(), "%s:%s" % (kind, goal),
                          ttl=ttl, config=cfg, env=env)
    except Exception as exc:  # noqa: BLE001 - never a crash; resource_lease holds its own Unsupported class
        why = ("unsupported: %s" % getattr(exc, "reason", exc)) if type(exc).__name__ == "Unsupported" \
            else "lease table error (%s)" % type(exc).__name__
        yield None if mode == "observe" else _Held(why)
        return
    stop, thread = threading.Event(), None
    if hasattr(got, "id"):
        every = renew_every
        if not _number(every) or every <= 0:
            every = (ttl if _number(ttl) and ttl > 0 else lease.DEFAULT_TTL) / 3.0

        def _renew():
            while not stop.wait(every):
                try:
                    lease.renew(got.id, now=clock(), env=env)
                except Exception:  # noqa: BLE001 - a missed renewal costs only the ttl margin
                    pass
        thread = threading.Thread(target=_renew, name="lease-renewer", daemon=True)
        thread.start()
    try:
        yield got
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=5)
        if hasattr(got, "id"):
            try:
                lease.release(got.id, env=env)
            except Exception:  # noqa: BLE001 - release is best effort; the ttl and the pid check reclaim it
                pass


class _Held:
    def __init__(self, reason, retry_after=60.0):
        self.reason, self.retry_after = reason, retry_after


# --------------------------------------------------------------------------------------------- hold.json
def hold_path(env=None):
    env = os.environ if env is None else env
    if env.get("SIGMA_RESOURCE_DIR"):
        return os.path.join(env["SIGMA_RESOURCE_DIR"], "hold.json")
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "sigma", "resource", "hold.json")


def write_hold(path, decision, _replace=os.replace):
    """Write the decision as hold.json by atomic rename: a reader sees the old file or the new, never half."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + "." + str(os.getpid()) + ".tmp"
    try:
        with open(tmp, "w") as handle:
            handle.write(json.dumps(decision.as_dict(), sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_hold(path):
    """(record, notes). Absent reads as open; truncated or corrupt reads as open plus one note."""
    open_record = {"state": "open", "since": None, "reason": None, "resource": None, "retry_after": None}
    try:
        with open(path, "r") as handle:
            text = handle.read()
    except FileNotFoundError:
        return open_record, []
    except (OSError, UnicodeDecodeError):
        return open_record, ["hold.json unreadable; read as open"]
    try:
        record = json.loads(text)
        if (isinstance(record, dict) and record.get("state") in ("open", "hold")
                and set(record) == set(open_record)):
            return record, []
    except ValueError:
        pass
    return open_record, ["hold.json corrupt; read as open"]


def run(kind, command, root, timeout, goal, **kwargs):
    """Verify under admission (#1601): admit, run `command` in its own process group, measure, release.
    Implemented in resource_verify.py (kept apart: this module starts no process); -> its Outcome."""
    import importlib.util as _util
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    spec = _util.spec_from_file_location("resource_verify", os.path.join(here, "resource_verify.py"))
    module = _util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run(kind, command, root, timeout, goal, **kwargs)


USAGE = ("usage: resource_guard.py sample [--json]\n"
         "       resource_guard.py sample --record <sdlc_dir>\n"
         "  sample    print one machine reading (exit 3 and one stderr line when the platform is unsupported)\n"
         "  --json    print it as one JSON object\n"
         "  --record  the watcher's call (#1604): refresh last.json and hold.json and reclaim stale leases; prints\n"
         "            one line while held, nothing when open or when resource.mode is off\n")


def _record(sdlc_dir):
    """`sample --record <sdlc_dir>` (#1604): resource_liveness.refresh under the config in <sdlc_dir>/config.json."""
    import importlib.util as _util
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    spec = _util.spec_from_file_location("resource_liveness", os.path.join(here, "resource_liveness.py"))
    module = _util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        with open(os.path.join(sdlc_dir, "config.json"), "r") as handle:
            config = json.loads(handle.read())
    except (OSError, ValueError):
        config = {}
    try:
        got = module.refresh(sdlc_dir, config)
    except Unsupported as refusal:
        sys.stderr.write("resource_guard: unsupported: %s\n" % refusal.reason)
        return EXIT_UNSUPPORTED
    if got is not None and got["state"] == "hold":      # silent when open: the tick prints nothing for an empty line
        sys.stdout.write("resource: admission HELD (%s)\n" % got["reason"])
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0
    if args[:2] == ["sample", "--record"] and len(args) == 3:
        return _record(args[2])
    if args[0] != "sample" or set(args[1:]) - {"--json"}:
        sys.stderr.write(USAGE)
        return 2
    try:
        got = sample()
    except Unsupported as refusal:
        sys.stderr.write("resource_guard: unsupported: %s\n" % refusal.reason)
        return EXIT_UNSUPPORTED
    if "--json" in args:
        sys.stdout.write(json.dumps(got.as_dict(), sort_keys=True) + "\n")
    else:
        sys.stdout.write(" ".join("%s=%s" % kv for kv in got.as_dict().items()) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
