"""Bounded, unattended process runs: the shared verify runner and the process mechanism under it.

WHAT IT IS. A library (no command line) that runs ONE command in a process group of its own, under a wall-clock
budget, and returns a `Result` instead of raising (a bad budget is the one ValueError). Nothing in the product calls it yet: this release ships the
runner and the proof that it works; the upkeep job (a later slice) is its first caller, behind its own gate.

THE CONTRACT. `run_verify(command, worktree, timeout, ...)` is the verify entry. In order it: refuses a bad budget
(ValueError, the only way it raises); returns NO_COMMAND for an absent or blank command; REFUSES where process
groups do not exist (Windows); REFUSES, spawning nothing, when a repository-location variable is exported in the
process environment (git would then read the trust setting from the repository that variable names, not the
worktree's); returns UNTRUSTED, spawning nothing, unless the operator made the git-local shell-command decision for
that checkout (the refusal text is `shell_policy`'s own, verbatim); returns REFUSED, again spawning nothing, for the
project's main checkout, and with a different diagnostic when git cannot say what the directory is (verify runs in a
scratch worktree only); then runs the command.
`run_group(...)` is the mechanism underneath and is also what the unattended git runner is built on.

HOW A RUN ENDS, and what is left behind (the answers were measured, see the plan record of this change):
  * the command exits: OK for exit 0, FAILED otherwise. Anything it left running in its group is swept (TERM, a
    grace, then KILL), so a run never leaves a process behind.
  * the budget runs out: TIMEOUT. The WHOLE group is stopped, not just the shell: SIGTERM first, a grace, then
    SIGKILL for what is left. TERM comes first because a git process killed outright while it holds a reference lock
    leaves the lock behind and every later fetch fails until a person removes it.
  * the stop file appears (polled every POLL_SECONDS): STOPPED, the same way. The runner never deletes the file; it
    belongs to whoever created it.
  * the caller itself dies (even by SIGKILL, which no handler sees): a tiny lifeline process in a session of its own
    watches a pipe whose write end only the caller holds, and on end-of-file stops the group the same way. A tree in
    its own session would otherwise outlive a killed caller.
  * the platform cannot give the group guarantee: REFUSED, loudly, rather than a kill that cannot reach the tree.

OUTPUT IS BOUNDED. Pipes are read without blocking into a ring that keeps the LAST `tail_bytes` (merged stdout and
stderr), or, in split mode, the first `max_out_bytes` of stdout and the last `tail_bytes` of stderr. A command that
prints 200 MB costs a bounded amount of memory, not a multiple of its output: MEASURED, merged mode keeps about the
tail plus one chunk, and split mode peaks near three times `max_out_bytes` (the kept bytes, a copy made to decode
them, the decoded text), so the default 16 MiB cap peaked near 74 MiB under a 300 MB flood (the earlier 64 MiB cap
peaked near 227 MiB, hence the lower figure). A descendant that escaped the group
(its own session) and still holds a pipe cannot stall the run: after the group is stopped the pipes are drained for
at most REAP_SECONDS and then closed.

THE ENVIRONMENT is the caller's, with the repository-location variables git reads removed and the prompt pins added
(`unattended_env`): no prompt, no editor, no askpass helper. The command inherits the operator's other variables,
credentials included; a caller that needs an allowlist passes `env`.

NOT HERE, ON PURPOSE. No configuration is read (the budget is a parameter; the caller derives it), no gate is
consulted, no file is written, no claim or ledger entry is armed, nothing from the loop script is loaded. The only
sibling loaded is `shell_policy`.
"""
import collections
import importlib.util
import math
import os
import pathlib
import selectors
import signal
import subprocess
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shell_policy = _load("shell_policy")

OK = "ok"                    # exit 0
FAILED = "failed"            # any other exit
TIMEOUT = "timeout"          # the budget ran out; the group was stopped
STOPPED = "stopped"          # the stop file appeared; the group was stopped
NO_COMMAND = "no-command"    # nothing configured; nothing run
UNTRUSTED = "untrusted"      # no git-local trust for this checkout; nothing spawned
REFUSED = "refused"          # this platform or this directory cannot be run safely; nothing spawned
ERROR = "error"              # the command could not be started
OUTCOMES = (OK, FAILED, TIMEOUT, STOPPED, NO_COMMAND, UNTRUSTED, REFUSED, ERROR)

#: outcome; code = exit status (negative: killed by that signal; None: never started); out = merged tail, or
#: stdout in split mode; err = stderr tail in split mode, else empty; seconds = wall clock; escalated = SIGKILL was
#: needed after SIGTERM; truncated = output was dropped; detail = one line for a person.
Result = collections.namedtuple("Result", "outcome code out err seconds escalated truncated detail",
                                defaults=(None, "", "", 0.0, False, False, ""))

#: How often the wait wakes to look at the stop file and the budget: the worst-case delay between either and the
#: start of the stop. A mechanism constant, not a resource limit; measured stop latency at this value was 0.30 s.
POLL_SECONDS = 0.25
#: How long a stopped group may take to leave after SIGTERM before SIGKILL. Mirrors the bounded drive that already
#: ships (ten seconds), and is long enough for git to drop a reference lock.
TERM_GRACE_SECONDS = 10.0
#: The bound on draining pipes after the group is stopped, so a pipe held by an escapee cannot stall the caller.
REAP_SECONDS = 5.0
#: The kept tail of merged output, and of stderr in split mode.
TAIL_BYTES = 65536
#: The cap on stdout in split mode. Far above any answer the engine reads (a name list for a repository one hundred
#: times this one is a few megabytes) and far below a runaway; output beyond it is dropped and flagged. Measured
#: peak memory is about three times the cap (a 64 MiB cap peaked near 227 MiB, this one near 74 MiB).
MAX_OUT_BYTES = 16 * 1024 * 1024
#: The longest budget accepted: the upper bound of the gate's own verify.timeout_minutes (1440 minutes).
MAX_SECONDS = 86400
_CHUNK = 65536

#: The variables that point git at a repository other than the one the working directory names. A hook, or a stray
#: export, would otherwise send a replay or a verify to a different index. The same set the work script strips.
LOCATION_VARS = frozenset((
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX"))
#: No prompt, no editor, no askpass. An exported editor outranks `-c core.editor`; the askpass variables, set to the
#: empty text, outrank the askpass configuration and the ssh helper, which `GIT_TERMINAL_PROMPT=0` alone does not.
PROMPT_PINS = (("GIT_TERMINAL_PROMPT", "0"), ("GIT_EDITOR", "true"), ("GIT_ASKPASS", ""), ("SSH_ASKPASS", ""))

NO_GROUP_REFUSAL = ("bounded_run: REFUSED [no-process-group]: this host cannot run a command in a process group of "
                    "its own and stop the whole group on timeout (POSIX setsid/killpg required; Windows is not "
                    "supported) -- not running, rather than risk an orphaned process tree")
MAIN_CHECKOUT_REFUSAL = ("bounded_run: REFUSED [main-checkout]: the verify command runs in a scratch worktree of the "
                         "project, never in the project's own checkout, and this directory is not one")
UNKNOWN_CHECKOUT_REFUSAL = ("bounded_run: REFUSED [unknown-checkout]: git could not say whether this directory is a "
                            "linked worktree (git older than 2.31, an unreadable directory or a safe-directory "
                            "refusal), so nothing was run")
LOCATION_REFUSAL = ("bounded_run: REFUSED [location-variable]: %s set in the process environment would send git to a "
                    "repository other than the worktree's, so the trust setting could be read from the wrong one; "
                    "unset it (names only are shown)")

#: The lifeline sentinel: a session of its own, holding only the READ end of a pipe whose write end exists only in the
#: process running `run_group`. It reads the group id, then blocks. `done` means the run ended normally: exit quietly.
#: End-of-file without it means the caller is gone, by SIGKILL included, so it does what `_stop_group` does.
_SENTINEL = r"""
import os, signal, sys, time
for name in ("SIGINT", "SIGHUP"):
    signal.signal(getattr(signal, name), signal.SIG_IGN)
grace = float(sys.argv[1])
try:
    pgid = int(sys.stdin.buffer.readline())
except ValueError:
    sys.exit(0)
if sys.stdin.buffer.read().startswith(b"done"):
    sys.exit(0)
def alive():
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
try:
    os.killpg(pgid, signal.SIGTERM)
except OSError:
    pass
deadline = time.monotonic() + grace
while alive() and time.monotonic() < deadline:
    time.sleep(0.05)
if alive():
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
"""


def group_refusal():
    """The refusal text where this host cannot run and stop a process group, else None."""
    if sys.platform == "win32" or not hasattr(os, "killpg"):
        return NO_GROUP_REFUSAL
    return None


def seconds(value, name="timeout"):
    """A budget in seconds: a finite number above 0 and at most MAX_SECONDS. A boolean is not a number. -> float.
    Raises ValueError (naming the parameter and the type, never echoing the value)."""
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not 0 < value <= MAX_SECONDS):
        raise ValueError("%s must be a number of seconds above 0 and at most %d (got %s)"
                         % (name, MAX_SECONDS, type(value).__name__))
    return float(value)


def unattended_env(base=None):
    """-> a new environment: `base` (default: the process environment) without the repository-location variables,
    with the prompt pins set. Never mutates its input."""
    source = os.environ if base is None else base
    env = {key: value for key, value in source.items() if key not in LOCATION_VARS}
    env.update(PROMPT_PINS)
    return env


# ------------------------------------------------------------------------------------------ the group

def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:          # on darwin: the only member left is an unreaped zombie leader
        return True
    return True


def _signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _stop_group(proc, grace, drain):
    """Stop everything in the group `proc` leads -> True when SIGKILL was needed. A group that is already gone costs
    nothing. Else SIGTERM, wait up to `grace` for the group to leave (reaping the leader, so a zombie cannot keep it
    "alive", and reading the pipes so a dying process cannot block on a full one), then SIGKILL for what is left."""
    pgid = proc.pid
    proc.poll()
    if not _group_alive(pgid):
        return False
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while _group_alive(pgid) and time.monotonic() < deadline:
        drain()
        proc.poll()
        time.sleep(0.05)
    if not _group_alive(pgid):
        return False
    _signal_group(pgid, signal.SIGKILL)
    end = time.monotonic() + REAP_SECONDS
    while _group_alive(pgid) and time.monotonic() < end:
        drain()
        proc.poll()
        time.sleep(0.05)
    return True


def _start_lifeline(grace):
    """-> (sentinel process, write fd). The write end is non-inheritable and every Popen here closes fds, so no child
    holds it: only the death of this process, or an explicit close, ends the pipe."""
    read_fd, write_fd = os.pipe()
    try:
        sentinel = subprocess.Popen([sys.executable, "-c", _SENTINEL, str(grace)], stdin=read_fd,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
    return sentinel, write_fd


def _group_confirmed_gone(proc):
    """True only when the group `proc` led is gone, checked after reaping the leader. A run that never started has
    no group and nothing for the lifeline to guard."""
    if proc is None:
        return True
    proc.poll()
    end = time.monotonic() + REAP_SECONDS
    while _group_alive(proc.pid):
        if time.monotonic() >= end:
            return False
        proc.poll()
        time.sleep(0.05)
    return True


def _end_lifeline(sentinel, write_fd, group_gone=True):
    """Release the sentinel and reap it. `done` is told only when `group_gone` (the group is confirmed gone); else
    the pipe is just closed, and the sentinel treats end-of-file as a dead caller and stops the group itself, so a
    second interrupt during the stop cannot disarm it."""
    actions = [lambda: os.write(write_fd, b"done")] if group_gone else []
    for action in actions + [lambda: os.close(write_fd)]:
        try:
            action()
        except OSError:
            pass
    try:
        sentinel.wait(timeout=REAP_SECONDS)
    except Exception:                                       # noqa: BLE001 - bounded and best-effort
        pass


class _Sink:
    """Bytes kept from one pipe: the last `cap` (keep="tail") or the first `cap` (keep="head")."""

    def __init__(self, cap, keep):
        self.buffer, self.cap, self.keep, self.dropped = bytearray(), cap, keep, False

    def add(self, chunk):
        if self.keep == "tail":
            self.buffer += chunk
            if len(self.buffer) > self.cap:
                del self.buffer[:len(self.buffer) - self.cap]
                self.dropped = True
        else:
            room = self.cap - len(self.buffer)
            if len(chunk) > room:
                self.dropped = True
            if room > 0:
                self.buffer += chunk[:room]

    def text(self):
        return bytes(self.buffer).decode("utf-8", errors="replace")


def _read_ready(selector, timeout):
    """Read one chunk from every ready pipe; unregister a pipe at end-of-file. -> number of events."""
    events = selector.select(timeout)
    for key, _mask in events:
        try:
            chunk = os.read(key.fd, _CHUNK)
        except BlockingIOError:
            continue
        except OSError:
            chunk = b""
        if chunk:
            key.data.add(chunk)
        else:
            selector.unregister(key.fd)
    return len(events)


def _drain(selector):
    """Read whatever is immediately available, for at most REAP_SECONDS (a producer that never stops cannot hold us)."""
    end = time.monotonic() + REAP_SECONDS
    while selector.get_map() and time.monotonic() < end and _read_ready(selector, 0):
        pass


def _supervise(proc, selector, deadline, stop_path, clock):
    """Wait for the command. -> "exit", TIMEOUT or STOPPED. The exit is checked first, so a command that finished is
    never reported as overrun."""
    while True:
        if proc.poll() is not None:
            return "exit"
        if stop_path is not None and os.path.exists(stop_path):
            return STOPPED
        remaining = deadline - clock()
        if remaining <= 0:
            return TIMEOUT
        _read_ready(selector, min(remaining, POLL_SECONDS))


def run_group(command, cwd, timeout, *, shell=False, env=None, stop_path=None, merge=True, tail_bytes=TAIL_BYTES,
              max_out_bytes=MAX_OUT_BYTES, term_grace=TERM_GRACE_SECONDS, clock=time.monotonic, stdin_path=None):
    """Run `command` (an argv list, or a string with shell=True) in `cwd` under a budget of `timeout` seconds, in a
    process group of its own, with stdin closed. -> Result; raises ValueError for a bad budget or grace.

    `merge=True` (the verify shape): stderr joins stdout and `out` is the merged tail. `merge=False` (the git shape):
    `out` is stdout alone, up to `max_out_bytes`, and `err` the stderr tail. `env` is used as given (callers pass
    `unattended_env(...)`). `clock` drives only the budget (a test can jump it forward); the stop grace is real time.
    """
    timeout, grace = seconds(timeout), seconds(term_grace, "term_grace")
    refusal = group_refusal()
    if refusal:
        return Result(REFUSED, None, detail=refusal)
    if stop_path is not None and os.path.exists(stop_path):
        return Result(STOPPED, None, detail="stop file present before the command started")
    started = time.monotonic()
    try:
        sentinel, write_fd = _start_lifeline(grace)
    except Exception as exc:                                # noqa: BLE001 - never raise
        return Result(ERROR, None, detail="could not start the lifeline process: %s" % exc)
    proc = None
    try:
        stdin_file = None
        try:
            stdin_file = subprocess.DEVNULL if stdin_path is None else open(stdin_path, "rb")
            proc = subprocess.Popen(command, shell=shell, cwd=None if cwd is None else str(cwd), env=env,
                                    stdin=stdin_file, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT if merge else subprocess.PIPE, start_new_session=True)
        except (OSError, ValueError) as exc:
            return Result(ERROR, None, seconds=time.monotonic() - started, detail="could not start: %s" % exc)
        finally:
            if stdin_path is not None and stdin_file is not None:
                stdin_file.close()
        selector = selectors.DefaultSelector()
        sinks = [_Sink(tail_bytes, "tail")] if merge else [_Sink(max_out_bytes, "head"), _Sink(tail_bytes, "tail")]
        try:
            for stream, sink in zip((proc.stdout, proc.stderr), sinks):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream.fileno(), selectors.EVENT_READ, sink)
            try:
                os.write(write_fd, ("%d\n" % proc.pid).encode())
            except OSError as exc:                          # the lifeline is gone: do not run unprotected
                _stop_group(proc, grace, lambda: _drain(selector))
                return Result(ERROR, proc.poll(), seconds=time.monotonic() - started,
                              detail="the lifeline process is gone, the command was stopped: %s" % exc)
            verdict = _supervise(proc, selector, clock() + timeout, stop_path, clock)
            escalated = _stop_group(proc, grace, lambda: _drain(selector))
            _drain(selector)
        except BaseException:
            _stop_group(proc, grace, lambda: _drain(selector))
            raise
        finally:
            selector.close()
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        try:
            proc.wait(timeout=REAP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        code = proc.returncode
        outcome = verdict if verdict != "exit" else (OK if code == 0 else FAILED)
        return Result(outcome, code, sinks[0].text(), sinks[1].text() if not merge else "",
                      time.monotonic() - started, escalated, sinks[0].dropped,
                      {TIMEOUT: "timed out after %gs; the process group was stopped" % timeout,
                       STOPPED: "stop file appeared; the process group was stopped"}.get(outcome, ""))
    finally:
        try:
            _end_lifeline(sentinel, write_fd, _group_confirmed_gone(proc))
        except Exception:                                   # noqa: BLE001 - unconfirmed: close the pipe, sentinel stops the group
            _end_lifeline(sentinel, write_fd, False)


# ------------------------------------------------------------------------------------------ the verify entry

def checkout_kind(path):
    """"linked" for a linked worktree, "main" for a repository's own checkout (or a bare or submodule git dir), None
    when git cannot say. One read: the git dir and the common dir are the same place exactly in the main checkout."""
    try:
        done = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-dir",
                               "--git-common-dir"], capture_output=True, text=True, timeout=5, check=False,
                              stdin=subprocess.DEVNULL, env=unattended_env())
    except (OSError, subprocess.SubprocessError):
        return None
    lines = done.stdout.splitlines()
    if done.returncode != 0 or len(lines) != 2:
        return None
    return "main" if os.path.realpath(lines[0]) == os.path.realpath(lines[1]) else "linked"


def run_verify(command, worktree, timeout, *, stop_path=None, env=None, term_grace=TERM_GRACE_SECONDS,
               clock=time.monotonic):
    """The shared verify runner -> Result. `command` is the repository's verify command (a shell string),
    `worktree` a scratch worktree of the project, `timeout` seconds. See the module docstring for the order of the
    refusals; `detail` carries the shipped refusal text for UNTRUSTED."""
    timeout = seconds(timeout)
    if not isinstance(command, str) or not command.strip():
        return Result(NO_COMMAND, None, detail="no verify command is configured")
    refusal = group_refusal()
    if refusal:
        return Result(REFUSED, None, detail=refusal)
    located = sorted(name for name in LOCATION_VARS if name in os.environ)
    if located:
        return Result(REFUSED, None, detail=LOCATION_REFUSAL % ", ".join(located))
    if not shell_policy.repository_shell_commands_allowed(worktree):
        return Result(UNTRUSTED, None, detail=shell_policy.refusal_message())
    kind = checkout_kind(worktree)
    if kind != "linked":
        return Result(REFUSED, None, detail=MAIN_CHECKOUT_REFUSAL if kind == "main" else UNKNOWN_CHECKOUT_REFUSAL)
    return run_group(command, worktree, timeout, shell=True, env=unattended_env(env), stop_path=stop_path,
                     merge=True, term_grace=term_grace, clock=clock)
