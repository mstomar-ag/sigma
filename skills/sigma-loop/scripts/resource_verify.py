"""Verify under admission (#1601): a worker count Sigma derives, a process group, a bounded tail, a measured cost.

A library, loaded by path; `resource_guard.run(...)` is its entry point. It joins three landed pieces and invents no
field of its own: `resource_lease.admit/release` (the lease table and the learned per-unit cost ring),
`bounded_run.run_group` (own process group, lifeline for a killed caller, wall-clock budget, bounded tail) and
`resource_guard.footprint` (the per-process measure).

THE CONTRACT.
  * `run("verify", commands, root, timeout, goal, ...) -> Outcome`. `commands` is one shell string or a list run in
    order, stopping at the first non-zero exit, under ONE lease. The lease is released in a `finally`.
  * Hold: `Outcome.hold` is the lease table's `Hold` (reason, retry_after); nothing was run.
  * Workers, in order of reliability: a `{workers}` token in the command, replaced by the granted integer before the
    shell sees it (only that exact token, only an int); env `SIGMA_VERIFY_WORKERS`; env
    `PYTEST_XDIST_AUTO_NUM_WORKERS` for `-n auto`. A literal `-n N` cannot be lowered: it needs N workers, and a
    warm cost ring that cannot fit N holds rather than running fewer.
  * Cost: while the group runs, the footprint of this process's descendants is summed every `sample_gap` seconds
    and its peak is written into the lease's `peak`; `release` then records `peak // workers` into the ring.
  * Nothing is killed except this run's own process group, on its own timeout, or (#1602) when `resource.shed` is
    "verify" (the default) and the machine stays `critical` for `shed_after` consecutive samples: a stop file for this
    run appears, `bounded_run` stops the group (TERM, a grace, then KILL) and the result is a Hold ending "(shed)":
    no evidence, the goal is not failed, and the caller reruns once admission reopens. The file is removed in a
    `finally`. A pid Sigma did not start is never signalled, and nothing here pauses or resumes a process.
"""
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

_HERE = pathlib.Path(__file__).resolve().parent
TOKEN = "{workers}"
SAMPLE_GAP = 2.0
SHED_AFTER = 3          # #1602: consecutive critical samples before the run's own group is stopped
DEFAULT_TIMEOUT = 3600
TAIL_BYTES = 65536
ENV_WORKERS = "SIGMA_VERIFY_WORKERS"
ENV_XDIST = "PYTEST_XDIST_AUTO_NUM_WORKERS"
_LITERAL = re.compile(r"(?<![\w-])(?:-n|--numprocesses)(?:\s+|=)(\d+)(?![\w.-])")


def _sibling(name):
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def substitute_workers(command, n):
    """`command` with every exact `{workers}` replaced by the integer `n`; other braces are untouched."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("workers must be a positive integer, got %r" % (n,))
    return command.replace(TOKEN, str(n))


def literal_workers(command):
    """N from a literal `-n N` / `--numprocesses N` (the largest, if several), else None. `auto` is not literal."""
    found = [int(m) for m in _LITERAL.findall(command)]
    return max(found) if found else None


def proc_table(proc="/proc"):
    """[(pid, ppid)] read from /proc/<pid>/stat: the process table where `ps` is not installed (minimal containers)."""
    table = []
    try:
        names = os.listdir(proc)
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "stat"), "r") as handle:
                after = handle.read().rpartition(")")[2].split()
            table.append((int(name), int(after[1])))
        except (OSError, ValueError, IndexError):
            continue
    return table


def tree_footprint(root_pid, footprint, table=None):
    """Summed footprint of every descendant of `root_pid` (not itself). `table` is [(pid, ppid)] (default: ps)."""
    if table is None:
        try:
            done = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=10)
        except OSError:                                          # no `ps` here (a minimal container): read /proc
            table = proc_table()
            done = None
        except subprocess.SubprocessError:
            return 0
        else:
            table = []
        for line in (done.stdout.splitlines() if done else ()):
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                table.append((int(parts[0]), int(parts[1])))
    children = {}
    for pid, ppid in table:
        children.setdefault(ppid, []).append(pid)
    total, stack, seen = 0, list(children.get(root_pid, [])), set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
        got = footprint(pid)
        total += got[0] if got else 0
    return total


class Outcome:
    """hold: a resource_lease.Hold or None. result: the bounded_run.Result of the last command run (None on hold).
    outputs: the merged tail of each command run. returncode: 0 only when every command exited 0."""

    def __init__(self, hold=None, result=None, outputs=(), returncode=None, workers=None, peak=0, shed=False):
        self.hold, self.result, self.outputs, self.shed = hold, result, list(outputs), shed
        self.returncode, self.workers, self.peak = returncode, workers, peak


class _Meter(threading.Thread):
    """Samples this process's descendants and keeps the peak, mirrored into the lease's `peak` as it grows."""

    def __init__(self, lease, lease_id, gap, shed=None):
        super().__init__(daemon=True)
        self.lease, self.id, self.gap = lease, lease_id, gap
        self.peak, self.stop_event = 0, threading.Event()
        self.shed = shed            # None, or (stop_path, config, shed_after) while shedding is on
        self.critical_run = 0

    def sample_once(self):
        got = tree_footprint(os.getpid(), self.lease.take_footprint)
        if got > self.peak:
            self.peak = got
            _amend(self.lease, self.id, peak=got)
        if self.shed:
            self.check_shed()

    def check_shed(self):
        """#1602: `shed_after` critical samples in a row create this run's stop file (never anything else)."""
        stop_path, config, after = self.shed
        limits = self.lease.guard.derive(self.lease.take_sample(), config)
        self.critical_run = self.critical_run + 1 if limits.critical else 0
        if self.critical_run >= after and not os.path.exists(stop_path):
            os.makedirs(os.path.dirname(stop_path), exist_ok=True)
            with open(stop_path, "w", encoding="utf-8") as fh:
                fh.write("critical for %d consecutive samples\n" % self.critical_run)

    def run(self):
        while not self.stop_event.wait(self.gap):
            try:
                self.sample_once()
            except Exception:                                    # noqa: BLE001 - a meter must never break the run
                pass


def _amend(lease, lease_id, **fields):
    """Rewrite fields of our own lease record (keeping a larger `peak`); False when the lease is gone."""
    path = os.path.join(lease.state_dir(), "leases", os.path.basename(str(lease_id)) + ".json")
    record = lease._read_json(path, None)
    if not isinstance(record, dict):
        return False
    if "peak" in fields:
        fields["peak"] = max(fields["peak"], record.get("peak", 0))
    lease._atomic_write(path, dict(record, **fields))
    return True


def _want(guard, lease, config, literal):
    if literal:
        return literal
    cfg = guard.resolve_config(config)[0]
    cpus = lease.take_sample().cpu_count
    want = max(1, cpus - max(1, -(-cpus // 8)))
    cap = cfg.get("max_verify_workers")
    return min(want, cap) if guard._number(cap) and cap >= 1 else want


def run(kind, commands, root, timeout, goal, *, config=None, repo=None, env=None, sample_gap=SAMPLE_GAP,
        tail_bytes=TAIL_BYTES, term_grace=None, run_group=None, shed_after=None):
    """Admit, run each command in its own process group under `timeout` seconds, measure, release. -> Outcome."""
    guard, lease, bounded = _sibling("resource_guard"), _sibling("resource_lease"), _sibling("bounded_run")
    commands = [commands] if isinstance(commands, str) else list(commands)
    literal = max([literal_workers(c) or 0 for c in commands]) or None
    want = _want(guard, lease, config, literal)
    admitted = lease.admit(kind, want, repo or root, goal, ttl=timeout + 60, config=config)
    if isinstance(admitted, lease.Hold):
        return Outcome(hold=admitted)
    n = admitted.n
    try:
        record = lease._read_json(os.path.join(lease.state_dir(), "leases", admitted.id + ".json"), {})
        if literal and n < literal:
            if record.get("est_mem", 0) > 0:                     # a measured cost says N workers do not fit
                lease.note_waiting(goal, kind)                   # #1768: a held verify blocks new goal starts
                return Outcome(hold=lease.Hold("memory: literal -n %d needs %d workers, %d fit" % (literal, literal, n),
                                               lease._retry()))
            n = literal                                          # cold ring: nothing to compare, learn from this run
            _amend(lease, admitted.id, workers=n)
        child_env = bounded.unattended_env(env)
        child_env[ENV_WORKERS] = str(n)
        child_env[ENV_XDIST] = str(n)
        resolved = guard.resolve_config(config)[0]
        if shed_after is None:                                   # #1769: an explicit argument wins, then the key
            shed_after = resolved.get("shed_after") or SHED_AFTER
        # observe only logs would-be actions (#1600): the shed acts in enforce mode alone
        shedding = resolved.get("shed") != "off" and guard.resolve_mode(config) == "enforce"   # #1794: env, config, default
        stop_path = os.path.join(lease.state_dir(), "shed", admitted.id + ".stop") if shedding else None
        meter = _Meter(lease, admitted.id, sample_gap, (stop_path, config, shed_after) if shedding else None)
        meter.start()
        runner = run_group or bounded.run_group
        kwargs = {} if term_grace is None else {"term_grace": term_grace}
        if shedding:
            kwargs["stop_path"] = stop_path
        outputs, last, code, shed = [], None, 0, False
        deadline = time.monotonic() + timeout                    # #1768: ONE budget across every command
        try:
            for command in commands:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    last = bounded.Result(bounded.TIMEOUT, None, detail="timed out after %gs; no time left for: %s"
                                          % (timeout, command))
                    outputs.append(last.detail)
                    code = 1
                    break
                last = runner(substitute_workers(command, n), root, remaining, shell=True, env=child_env, merge=True,
                              tail_bytes=tail_bytes, **kwargs)
                outputs.append(last.out + (("\n" + last.detail) if last.detail else ""))
                code = last.code if last.code not in (None, 0) else (0 if last.outcome == bounded.OK else 1)
                if last.outcome == bounded.STOPPED:
                    shed = True
                if code != 0:
                    break
        finally:
            meter.stop_event.set()
            meter.join(timeout=sample_gap + 5)
            meter.shed = None                                    # the closing sample only measures
            try:
                meter.sample_once()
            except Exception:                                    # noqa: BLE001
                pass
            if stop_path:
                try:
                    os.unlink(stop_path)
                except OSError:
                    pass
        if shed:
            reason = "memory: critical pressure, the verify process group was stopped (shed)"
            return Outcome(lease.Hold(reason, lease._retry()), last, outputs, code, n, meter.peak, shed=True)
        return Outcome(None, last, outputs, code, n, meter.peak)
    finally:
        lease.release(admitted.id)
