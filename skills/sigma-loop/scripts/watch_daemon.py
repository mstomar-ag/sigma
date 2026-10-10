#!/usr/bin/env python3
"""Ledger watcher — keeps the shared ledger fresh and tells YOUR loop when a teammate needs you.

It runs against the ledger WORKTREE only (.sdlc/ledger, checked out to the ops branch), so
fetching every few minutes never touches your code checkout: no surprise rebase, no lost work.
Each tick: pull the ops branch -> classify what is addressed to you and not yet surfaced ->
write .sdlc/state/inbox.md -> check every claimed goal's registered agent marker for a
genuinely dead pid and notify (agent_watch.py, off by default — agent_watch.enabled) ->
check every claimed issue for new comments and notify the claimant (comment_watch.py, off by
default — comment_watch.enabled) -> apply reconcile.py's AUTOMATIC-tier board/label corrections
(reconcile_tick.py, #2294, off by default — discovery.reconcile.mode, same TTL-throttled sweep
next_batch already runs, now with a heartbeat independent of active picking) -> push a wake-up
event to the CLI/Channels adapter's local webhook for a new unactioned mention/assignment/blocker
(channel_notify.py, #1322, off by default — needs ledger.autowatch.enabled AND
channel_webhook_url set) -> post a passive feature-branch drift summary to Slack, ledger-deduped
across the whole team (drift_tick.py, #2311, off by default — needs ledger.enabled AND
drift_watch.enabled, own ttl_minutes cadence coarser than this loop's own tick) -> publish
anything of your own that is still local -> sleep.

The loop reads that inbox between goals (loop.py next prints it on stderr), which is the honest
delivery mechanism: nothing can inject a message into a running session, so the hand-off waits at
a boundary the loop already stops at. Worst-case latency is one goal.

#2499: watch.log is capped -- at the start of a tick the owner rolls it to ONE predecessor
(state/watch.log.1) once it reaches ledger.watch.log_max_bytes (default 1 MiB, clamped to 1% of
free disk, floor 64 KiB). Follow it with `tail -F`, not `tail -f`.

Stop it any time: touch <sdlc>/state/watch.stop
  watch_daemon.py [sdlc_dir]  (default .sdlc)
Env: SIGMA_WATCH_INTERVAL — seconds between ticks (default: config ledger.watch.interval_seconds, else 900);
     SIGMA_WATCH_MAX_TICKS — stop after N ticks (default 0 = forever; tests set a small number);
     SIGMA_WATCH_SLEEP_SCALE — multiply the wait (tests set 0; default 1);
     SIGMA_WATCH_CALL_TIMEOUT — per-sub-call wall-clock bound in seconds (default 120).
     Every one of them DEGRADES to its documented default with one tee'd warning if it does not
     parse (#2488 D-2), where the bash original aborted on two of them and silently ignored a
     third. A value that DOES parse is used verbatim, including 0 and negatives.

#1227: the "already running" guard below is PID-existence-plus-freshness, not PID-existence
alone. A bare `kill -0 $(cat watch.pid)` only proves SOME process currently holds that PID
number — the kernel has no "is this the same process that wrote the file" concept, so once a
watcher dies, the FIRST unrelated process the kernel later hands that PID number to makes the
guard read "already running" forever (confirmed: this is exactly how a 13-day-dead watcher on
the sdlc-ledger branch went undetected). The fix pairs the liveness probe with a per-tick
heartbeat file (<state>/watch.heartbeat, touched at startup and before every sub-call in the tick
loop) and a staleness bound (STALE_AFTER, derived from INTERVAL — see below): "already running"
now requires a live PID AND a heartbeat touched within STALE_AFTER seconds. A dead watcher's
leftover pidfile has no heartbeat at all (or a stale one), so it now reads dead even though the
liveness probe on its stale/reused PID still succeeds. This mirrors the pid_alive()-plus-mtime-TTL
idiom `loop.py`'s `_session_pid_live()`/`agent_alive()` already use for the identical class of
problem on their own markers (`ledger.py`'s `pid_alive()`/`DEFAULT_LEASE_TTL_HOURS`).

Residual risk, closed by #2416/#2443: all 8 tick-loop calls (sync.py pull, watch.py,
agent_watch.py, comment_watch.py, reconcile_tick.py, channel_notify.py, drift_tick.py, sync.py
publish) are wrapped via run_with_timeout.py with a real, bounded, configurable timeout
(SIGMA_WATCH_CALL_TIMEOUT, default 120s — see above), which kills the WHOLE process group on
overrun so a hung call -- or anything it spawns -- can no longer make a genuinely-alive watcher
look dead to a concurrent second invocation. Refreshing the heartbeat before each individual
sub-call (rather than once per tick) still matters with the timeout in place: it bounds a single
hung call's exposure to CALL_TIMEOUT itself, not the whole 8-call tick's duration, so a
concurrent second invocation never has to wait longer than one call's own bound before deciding
this watcher is genuinely alive.

#2488: this file is the Python port of the original `watch.sh`, with the same on-disk contract,
the same messages on the same streams, the same exit codes and the same 8-call order. It is
importable on purpose (D-3): every decision predicate here is a pure function of its inputs, so
the mutex, the staleness rule and the liveness conjunct are proved in-process in milliseconds
rather than only through ten-second subprocess races. bash's `set -uo pipefail` WITHOUT `-e` is
reproduced as: a failing command never aborts the script, and the only non-zero exit this file can
PRODUCE is main()'s one remaining deliberate refusal (an unwritable state dir).

#2498: this file used to refuse to start on win32 (D-6) because `ledger.pid_alive`'s
`os.kill(pid, 0)` calls `TerminateProcess` for any signal but CTRL_C_EVENT/CTRL_BREAK_EVENT there
-- a liveness probe that would have killed the very watcher it was checking. That refusal is now
REMOVED: `pid_alive()` below routes win32 through a dedicated ctypes OpenProcess/GetExitCodeProcess
probe instead (`_win32_pid_alive`), which has no code path that can terminate anything it checks.
#2494 validated this against a real windows-latest GitHub Actions runner (workflow run
34949212549): `_win32_pid_alive` correctly reads BOTH a genuinely-alive real PID (`os.getpid()`)
and a genuinely-exited real PID as alive/dead respectively, with zero mocking of `_win32_kernel32`
-- see `tests/test_windows_real.py::test_win32_pid_alive_against_real_processes`. The stop-file
mechanism was also proven real (`test_watch_daemon_stop_file_halts_on_windows`). `ledger.pid_alive` itself is deliberately UNCHANGED (see `pid_alive()`
below) -- its remaining win32 exposure for OTHER callers (`loop.py` claim-reclaim, and the CLI
channel adapter's own single-instance guard, #1322) is tracked separately as #2508. (Deliberately
not naming that adapter's module here at all: a #2396 scope-creep guard elsewhere in this repo's
test suite asserts a specific literal string never appears in THIS file, precisely so a stray
mention here can never be mistaken for real wiring between the two.)
"""
import atexit
import importlib.util
import os
import pathlib
import signal
import subprocess
import sys
import time
import traceback
from typing import NamedTuple

try:                    # portable output: force UTF-8 so this file's own non-ASCII (the em-dash in
    import sys as _sys  # five operator-facing messages below) doesn't garble to '?' or crash on a
    _sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # non-UTF-8 console (the
    _sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")   # Windows cp1252 default);
except Exception:                          # a stream without reconfigure is left as-is. Copied from
    pass                                   # loop.py:17-22, which makes the same guarantee.

# resolve(), never os.path.dirname: a plugin installed behind a symlink would otherwise find the
# wrong siblings. The idiom every script in this directory already uses (loop.py:24, sync.py:38).
_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


legacy = _load("legacy")            # #239: operator env vars under the previous prefix
ledger = _load("ledger")            # pid_alive() for the POSIX liveness probe, _config() for the
                                     # read. #2498: win32 no longer delegates here -- see pid_alive()
                                     # below, which routes to _win32_pid_alive() instead.
sync = _load("sync")                # watch_interval_seconds(), stale_after_seconds(), heartbeat_path()

#: B-16. Strictly greater than this many seconds makes the decision mutex reclaimable.
RECLAIM_AFTER_SECONDS = 30
#: B-11. A STRING, not an int: it is passed verbatim to run_with_timeout.py, whose own `:25` is
#: `float(argv[1])` and whose `:69` formats `{seconds:g}` precisely so a fractional value prints as
#: `0.5s`. Re-formatting it here would change a working configuration (plan-review B3).
DEFAULT_CALL_TIMEOUT = "120"


class Paths(NamedTuple):
    """B-5. The FILENAMES are a cross-component contract -- `doctor.py::_ledger_watcher_state` and
    `hooks/session_start.sh` read `watch.heartbeat`/`watch.pid` by name -- and are frozen. The field
    names are internal."""
    state: pathlib.Path       # <sdlc_dir>/state
    log: pathlib.Path         # state/watch.log
    stop: pathlib.Path        # state/watch.stop
    pid: pathlib.Path         # state/watch.pid
    heartbeat: pathlib.Path   # state/watch.heartbeat
    mutex: pathlib.Path       # state/watch.decide.lock            (a DIRECTORY)
    reclaim: pathlib.Path     # state/watch.decide.lock.reclaim    (a DIRECTORY)


def paths(sdlc_dir):
    """B-3/B-4/B-5. NEVER `.resolve()` the dir: a relative `sdlc_dir` stays relative and every state
    path is derived from it verbatim, exactly as bash did. `sync.worktree()` resolves to absolute
    separately (tests/test_watch.py::test_worktree_path_is_absolute_even_for_a_relative_sdlc_dir);
    the watcher itself does not and must not start."""
    state = pathlib.Path(sdlc_dir) / "state"
    return Paths(
        state=state,
        log=state / "watch.log",
        stop=state / "watch.stop",
        pid=state / "watch.pid",
        # sync.heartbeat_path() rather than a second literal -- one fewer independent copy of a
        # filename doctor.py and session_start.sh both read (#2488 closes the duplication it can;
        # #2490 closes the rest -- doctor.py calls sync.heartbeat_path() too now, and the staleness
        # rule lives in sync.stale_after_seconds).
        heartbeat=sync.heartbeat_path(sdlc_dir),
        mutex=state / "watch.decide.lock",
        reclaim=state / "watch.decide.lock.reclaim",
    )


def read_config(sdlc_dir):
    """B-9. `ledger._config` is a bare `json.loads(...read_text())` and raises on a missing or
    invalid config.json, where bash degraded to 900 (`2>/dev/null || echo 900`). Fail open to `{}`:
    `sync.watch_interval_seconds({})` is 900, so the fallback composes correctly."""
    try:
        return ledger._config(sdlc_dir) or {}
    except Exception:            # noqa: BLE001 - fail-open by design; bash degraded here too
        return {}


#: #2498. The least-privilege access right that can still query exit status -- deliberately NOT
#: PROCESS_TERMINATE (0x0001) and NOT PROCESS_ALL_ACCESS. No handle `_win32_pid_alive` opens can be
#: used to terminate anything, even by mistake: the Win32 API itself refuses TerminateProcess() on
#: a handle opened without PROCESS_TERMINATE rights.
_WIN32_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WIN32_STILL_ACTIVE = 259           # GetExitCodeProcess's sentinel for "still running"
_WIN32_ERROR_ACCESS_DENIED = 5


def _win32_kernel32():
    """Indirection seam ONLY -- so tests can monkeypatch a fake kernel32-shaped object without
    `ctypes.WinDLL` existing on this (non-Windows) test host (confirmed:
    `hasattr(ctypes, "WinDLL")` is False on darwin/linux). Never called except from
    `_win32_pid_alive`, itself only reached when `sys.platform == "win32"`.

    Sets `restype`/`argtypes` explicitly on all three functions used -- required correctness, not
    style: ctypes' default `restype` for an unconfigured foreign function is `c_int` (32-bit
    signed). `OpenProcess` returns a `HANDLE` (pointer-sized, 64-bit on x64 Windows); without an
    explicit `restype` ctypes would truncate/reinterpret that 64-bit return as a 32-bit int -- an
    ABI bug no mocked test can catch (the fakes are plain Python objects, not real ctypes foreign
    functions), so it is only checkable on paper here and would otherwise only surface on a real
    Windows host (#2494). Declared HERE, not in `_win32_pid_alive`, so the mocked-object unit
    tests (which never construct a real `WinDLL`) are unaffected by this block."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.GetExitCodeProcess.restype = wintypes.BOOL
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def _win32_get_last_error():
    """Indirection seam for `ctypes.get_last_error` -- mirrors `_win32_kernel32()`'s reasoning
    exactly. Confirmed: `hasattr(ctypes, "get_last_error")` is False outside Windows (the attribute
    is conditionally defined by platform in the ctypes source), so a test that reaches for the
    obvious `monkeypatch.setattr(ctypes, "get_last_error", fake)` gets an immediate
    `AttributeError` (pytest's `monkeypatch.setattr` defaults `raising=True`) rather than a working
    mock. Routing through this seam instead -- monkeypatched as `_win32_get_last_error` on this
    module -- means a test never touches the real, absent attribute."""
    import ctypes
    return ctypes.get_last_error()


def _win32_pid_alive(pid):
    """D-6/#2498. OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess. NO code
    path here can terminate the process it is checking -- the requested access right cannot open a
    handle capable of TerminateProcess, unlike `ledger.pid_alive`'s `os.kill(pid, 0)`, which on
    Windows calls TerminateProcess for any signal value but CTRL_C_EVENT/CTRL_BREAK_EVENT.

    Mirrors `ledger.pid_alive`'s fail-toward-alive posture for anything ambiguous:
    ERROR_ACCESS_DENIED (5) means a live process this account cannot fully query -> True (exists,
    matching `ledger.pid_alive`'s own `PermissionError -> True` branch). A failed OpenProcess for
    any OTHER reason (no such pid, or anything else) -> False. A successful open whose
    GetExitCodeProcess call itself fails -> True (can't tell, fail toward alive, the same posture
    as `ledger.pid_alive`'s bare `except Exception -> True`). Otherwise: exit code == STILL_ACTIVE
    (259) -> True, any other exit code -> False.

    Proven against a real Windows host (#2494, workflow run 34949212549) -- see the module
    docstring."""
    import ctypes
    from ctypes import wintypes
    kernel32 = _win32_kernel32()
    handle = kernel32.OpenProcess(_WIN32_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return _win32_get_last_error() == _WIN32_ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD(0)
        ok = kernel32.GetExitCodeProcess(handle, ctypes.pointer(exit_code))
        if not ok:
            return True
        return exit_code.value == _WIN32_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def resolve_int_env(env, name, default):
    """-> `(value, warning|None)` for the three INTEGER variables (D-2): `SIGMA_WATCH_INTERVAL`,
    `_MAX_TICKS`, `_SLEEP_SCALE`. bash only ever put those three through `$(( ))` arithmetic and
    `[ -gt ]`, and `SLEEP_SCALE=0.5` is a measured abort -- so `int()` is the right parser for them
    and `float()` is the right one for `_CALL_TIMEOUT` (see `resolve_call_timeout`).

    Unset OR empty falls through to the default: bash used `${VAR:-default}`, so an explicitly-empty
    value behaves exactly like an unset one. A whitespace-only value is treated as empty too, a
    harmless widening of bash's abort."""
    raw = (legacy.getenv(name, environ=env) or "").strip()
    if not raw:
        return default, None
    try:
        return int(raw), None
    except ValueError:
        return default, f"watch: {name} ('{raw}') is not a valid integer -- using {default}"


def resolve_call_timeout(env):
    """-> `(raw_string, warning|None)` for `SIGMA_WATCH_CALL_TIMEOUT` (B-11, plan-review B3).

    Validated with **`float()`**, and the operator's RAW STRING is returned unchanged when it parses
    -- `"0.5"` stays `"0.5"`, `"120"` stays `"120"`. `run_with_timeout.py:25` does its own
    `float(argv[1])` and `:69` formats `{seconds:g}`, so a fractional timeout is a supported input
    by construction and works end to end today. Validating this one with `int()` would silently turn
    a working `0.5` into `120` -- a 240x change to a live configuration, announced by a warning that
    would claim a perfectly valid timeout "is not a valid integer". A value that parses is used
    verbatim including `0` and negatives, exactly as bash did."""
    raw = (legacy.getenv("SIGMA_WATCH_CALL_TIMEOUT", environ=env) or "").strip()
    if not raw:
        return DEFAULT_CALL_TIMEOUT, None
    try:
        float(raw)               # validate only -- the RAW string is what run_with_timeout.py gets
    except ValueError:
        return DEFAULT_CALL_TIMEOUT, (
            f"watch: SIGMA_WATCH_CALL_TIMEOUT ('{raw}') is not a valid number "
            f"-- using {DEFAULT_CALL_TIMEOUT}")
    return raw, None


def resolve_interval(config, env):
    """-> `(interval, warning|None)`. B-8: the env var WINS over config, and unset-or-empty falls
    through to `sync.watch_interval_seconds(config)` -- the config walk and the 900 default, reused
    rather than re-derived.

    D-2's one non-parse degradation lives here: a NON-POSITIVE env interval degrades to the config
    value with a warning. bash passed a negative straight through, which floors STALE_AFTER at 180,
    makes `sleep_for` negative, and so never sleeps at all -- a hot loop firing 8 subprocesses back
    to back forever. `sync.watch_interval_seconds` already refuses a non-positive CONFIG value for
    exactly that reason; the env path must not be weaker. (`SIGMA_WATCH_SLEEP_SCALE=0` stays
    valid and still means "no sleep" -- that is the documented test knob, not a malformation.)"""
    from_config = sync.watch_interval_seconds(config)
    raw = (legacy.getenv("SIGMA_WATCH_INTERVAL", environ=env) or "").strip()
    if not raw:
        return from_config, None
    try:
        value = int(raw)
    except ValueError:
        return from_config, (f"watch: SIGMA_WATCH_INTERVAL ('{raw}') is not a valid integer "
                             f"-- using {from_config}")
    if value <= 0:
        return from_config, (f"watch: SIGMA_WATCH_INTERVAL ('{raw}') must be positive "
                             f"-- using {from_config}")
    return value, None


def stale_after_seconds(interval):
    """B-10: `max(interval * 3, 180)`, derived from the EFFECTIVE (env-first) interval.

    THE ARITHMETIC MOVED, THE PRECEDENCE DID NOT (#2490). The rule itself now has exactly one home,
    `sync.stale_after_seconds(interval)`, and this function delegates to it rather than holding a
    third copy of `max(x * 3, 180)`. What stays HERE is the thing that was never arithmetic: WHICH
    interval the bound is derived from. `sync.watcher_stale_after_seconds(config)` reads the
    interval from CONFIG ONLY, so it is still NOT a drop-in for this function -- using it would
    ignore `SIGMA_WATCH_INTERVAL` and turn an expected 180 into 2700 (dossier §3.3 -- "the
    single biggest trap in the slice"). The control has been run: swapping this out for
    `sync.watcher_stale_after_seconds(config)` at the `main()` call site reds three tests --
    `test_stale_after_seconds_floors_at_the_shared_minimum` (this function gone entirely),
    `test_watch_daemon_stale_after_boundary` (a 210s-old heartbeat reads FRESH, so the run says
    "already running" instead of taking over) and
    `test_watch_daemon_warns_when_call_timeout_is_not_safely_under_stale_after` (the bound rises
    past the 200s timeout, so the warning is not printed at all and neither "200" nor "180" reaches
    `state/watch.log`). The env-first precedence is the watcher's own and belongs at the watcher;
    that is exactly why the shared rule takes an INTERVAL and not a config."""
    return sync.stale_after_seconds(interval)


def mtime_or_none(path):
    """The single epoch-mtime read this file has (B-15/B-17/B-21). `None` means CAN'T TELL.

    **Never `0`, and never "infinitely stale".** `watch.sh:159-165` records a `|| echo 0` fallback
    here as a REAL, empirically proven bug: `now - 0` is always huge, so on a fast race every loser
    reads an already-legitimately-freed mutex as ancient and they ALL "reclaim" it at once --
    reintroducing the exact class of bug the mutex exists to close, one level up. Every caller must
    treat `None` as "not acquired" / "not fresh".

    The whole `stat -f` vs `stat -c` portability essay `watch.sh:140-158` carried is obsolete here:
    `os.stat().st_mtime` is one call and is identical on every platform Python runs on, so the
    GNU-stat-stdout-into-arithmetic crash that essay documents cannot happen at all."""
    try:
        return int(os.stat(path).st_mtime)
    except OSError:
        return None


# --------------------------------------------------------------------- the two-layer decision mutex
#
# Idempotent AND race-safe: one watcher per ledger, even under a genuine concurrent-start race
# (F21/#339) -- two near-simultaneous triggers (more likely now that a single session can dispatch
# several loop.py calls close together, F10.5-3/#375) must not both pass this guard, run the loop
# concurrently, and contend on the ledger worktree's git index lock.
#
# An EARLIER version of this fix tried to make "evict a stale pidfile, then create fresh" itself
# race-safe via `mv`-based eviction plus a content-verify-and-restore step (mirroring loop.py's
# flock-based claim lock, F10.5-2/#387, which needed exactly that shape in Python). It reproduced a
# real double-win in roughly 1 of every 4 six-way concurrent-start test runs: racer B's `mv` can grab
# racer A's ALREADY-fresh pidfile (mv has no "is this still the same generation" signal, same as a
# plain unlink), and by the time B's verify step catches the mismatch and restores it, a THIRD racer
# C's own create can already have landed in the now-briefly-empty gap. Multi-step
# check-then-evict-then-verify-then-maybe-restore sequences have TOCTOU windows for EVERY step
# boundary, no matter how carefully each individual step is made atomic -- proven empirically here,
# not assumed.
#
# The fix: don't make the sequence itself race-safe -- make it NON-CONCURRENT. `mkdir` is a single,
# genuinely atomic POSIX operation (no partial states, no "verify identity" complexity a directory's
# mere existence can't already answer) used here purely as a short-lived MUTEX around the whole
# check+evict+create decision, held for a handful of near-instant local filesystem calls (no network,
# no sleep) and released immediately after -- not for the watcher's lifetime. `os.mkdir` raising
# FileExistsError is the same atomic signal, and is already used for exactly this purpose at
# the Slack-commands listener's own single-instance lock -- but NOT for that lock's
# lifetime-held shape, which §1.4 forbids here.


def _decision_pause(stage):
    """Test seam — a no-op here, and it must stay one. Called at the three instants where this
    file's concurrency is decided: "before-mtime" (the mutex vanished between our failed mkdir and
    our stat — B-17's "can't tell"), "after-mtime" (several racers holding the SAME stale
    observation — the unguarded-reclaim bug the .reclaim gate closes), and "before-takeover"
    (two processes both past the liveness check with neither having written a pidfile — F21/#339).
    The corresponding races are ~30,000x narrower in Python than they were in bash (plan #2488
    D-7, measured), so they are proved by widening them here by fiat rather than by racing 15
    processes and hoping. Monkeypatched by tests/test_watch.py; NEVER replaced in production."""
    return None


def acquire_decision_mutex(p, now):
    """B-13/B-14/B-16/B-17/B-18/B-19 -> True iff WE now hold `watch.decide.lock`.

    Layer 1 is `os.mkdir(MUTEX)`. Layer 2 -- reached only when layer 1 found it held -- is the
    RECLAIM GATE. Independent review of an earlier version of this fix found and empirically
    reproduced (60-67% double-win at a 10-40 racer burst against a deliberately orphaned mutex, up
    to 7 processes alive at once) a second-order version of the exact bug this file exists to close:
    a plain, unguarded stat + age-check + rmdir + mkdir reclaim sequence has its OWN TOCTOU window,
    because nothing stops several racers who all read the SAME stale mtime from racing that
    four-step sequence against each other -- the identical shape as the original pidfile bug, one
    level down. Patching that sequence again would be the same mistake repeated; the fix is the SAME
    discipline applied one level deeper: gate the RECLAIM decision itself behind its own atomic
    mkdir, so at most one racer can ever be inside the stat-evict-create sequence at a time. A racer
    that loses THIS gate falls through with "not acquired" and backs off exactly like a racer that
    lost the outer one -- correct, since a sibling really is deciding, just one door in.

    `MUTEX.reclaim` gets no staleness recovery of its own -- the regress has to stop somewhere, and
    here is the right place: its critical section is even shorter than MUTEX's own (one stat, one
    comparison, at most one rmdir+mkdir pair), so a crash landing inside IT is rarer still, and
    unlike the bug being fixed, an orphaned reclaim gate fails SAFE -- every future racer just prints
    "a sibling is already deciding" and exits 0, an inert watcher (the ledger stops updating) rather
    than silent double-execution. Both backoff messages are tee'd to the log, not just echoed -- a
    real invocation (loop.py's _ensure_watcher) redirects stdout/stderr to /dev/null, so without that
    the "self-announcing" half of this trade-off would be theoretical only, visible in an interactive
    shell but silent in the one context this actually runs in (independent review caught this gap).
    A human clearing a stuck `.reclaim` directory by hand is an acceptable residual once it is
    actually discoverable via the log; two watchers quietly contending on the ledger's git index lock
    is not.

    EVERY filesystem call here catches `OSError`, not just `FileExistsError`. `watch.sh:111, 113,
    175, 176, 178, 219` every one carries `2>/dev/null`, which means bash read EVERY mkdir/rmdir
    failure -- ENOSPC, EACCES on a shared .sdlc, an NFS hiccup, EROFS -- as "not acquired" and exited
    0. A port that caught only FileExistsError would let those escape as a traceback and rc 1, at
    which point tests/test_watch.py's `_race` loser assertion goes red with `[rc=1]` in the
    diagnostic -- the right test failing for entirely the wrong reason."""
    try:
        os.mkdir(p.mutex)
        return True                          # B-13: layer 1, the atomic create
    except FileExistsError:
        pass                                 # held (live or stale) -> try the reclaim gate
    except OSError:
        return False                         # any other filesystem failure reads as "not acquired"
    try:
        os.mkdir(p.reclaim)                  # B-14: layer 2, the reclaim gate
    except OSError:
        return False
    try:
        _decision_pause("before-mtime")      # TEST SEAM (no-op): the mutex can vanish right here
        age = mtime_or_none(p.mutex)         # B-15/B-17: None means CAN'T TELL, never age 0
        _decision_pause("after-mtime")       # TEST SEAM (no-op): the unguarded-reclaim window
        if age is None or int(now) - age <= RECLAIM_AFTER_SECONDS:
            return False                     # B-16: strictly > 30s is reclaimable, nothing else is
        try:
            os.rmdir(p.mutex)                # B-18: best-effort -- a failure just means someone
        except OSError:                      # else already cleared it
            pass
        try:
            os.mkdir(p.mutex)
            return True
        except OSError:
            return False
    finally:
        try:
            os.rmdir(p.reclaim)              # B-19: released whether or not the reclaim succeeded
        except OSError:
            pass


def release_decision_mutex(p):
    """B-26. Best-effort, OSError-safe, and called the instant the decision is made -- never held
    for the process lifetime (see `decide`)."""
    try:
        os.rmdir(p.mutex)
    except OSError:
        pass


# ------------------------------------------------------- the liveness guard, takeover and cleanup
#
# #1227: a bare liveness probe alone only proves a process exists at that PID number, not that it is
# OUR watcher -- a dead watcher's PID silently handed to any unrelated live process would satisfy it
# forever. Require a heartbeat touched within STALE_AFTER seconds too: a genuinely stuck/dead
# watcher's heartbeat stops advancing even if its old PID number happens to still resolve to
# something. Missing heartbeat entirely reads the SAME as stale, on purpose -- it is exactly the
# shape a pre-#1227 crashed watcher's leftover pidfile has (this fix's own heartbeat concept did not
# exist yet when it wrote that file), so a dead watcher from before the fix self-heals the first
# time a new watcher checks it, no special-casing needed.


def pid_from_file(pidf):
    """B-22 -> the pid, or None. An empty or garbage pidfile made bash's `kill -0` fail, which read
    as dead; `None` reproduces that. `0` and negatives are passed through unchanged -- `os.kill(0,0)`
    and `os.kill(-N,0)` signal a process GROUP exactly as `kill -0` does, so the behaviour matches
    without special-casing."""
    try:
        return int(pidf.read_text().strip())
    except (ValueError, OSError):
        return None


def pid_alive(pid):
    """This file's OWN liveness probe. On POSIX it is `ledger.pid_alive` unchanged; on win32
    (#2498) it routes to `_win32_pid_alive`, a dedicated OpenProcess/GetExitCodeProcess probe with
    no path that can terminate anything -- the win32 REFUSAL this used to raise (D-6 / plan-review
    B2) is gone; see the module docstring for why it's now safe to remove.

    `os.kill(pid, 0)` on Windows is not a probe: CPython treats only CTRL_C_EVENT and
    CTRL_BREAK_EVENT as signals there and calls `TerminateProcess(handle, sig)` for every other
    value, `0` included. That is exactly why `_win32_pid_alive` does not use it and instead opens a
    handle with only PROCESS_QUERY_LIMITED_INFORMATION -- an access right the Win32 API itself
    will not let TerminateProcess use.

    `ledger.pid_alive` itself is deliberately NOT modified: it is shared with ledger.py's own claim
    logic (also used by loop.py's claim-reclaim and by the CLI channel adapter's single-instance
    guard, #1322 -- their remaining win32 exposure is tracked separately as #2508), and changing a
    shared liveness probe to suit one caller's platform is outside this slice's blast radius. On
    POSIX this function's behaviour is inherited, unchanged -- ProcessLookupError -> False,
    PermissionError -> True, any other exception -> True."""
    if sys.platform == "win32":
        return _win32_pid_alive(pid)
    return ledger.pid_alive(pid)


def heartbeat_fresh(hb, stale_after, now):
    """B-21: a regular file whose mtime is STRICTLY less than `stale_after` seconds old. Not a
    regular file -> False. An unreadable mtime -> False (B-17's "can't tell", never "age 0").

    This folds `watch.sh`'s `_hb_fresh` and the mutex block's duplicated `python3 -c` mtime snippet
    into the single `mtime_or_none` helper. The duplication there was deliberate only to avoid
    touching an already-subtle, uncovered bash branch (watch.sh:193-195) -- a reason that does not
    survive the port."""
    if not hb.is_file():
        return False
    mtime = mtime_or_none(hb)
    if mtime is None:
        return False
    return int(now) - mtime < stale_after


def already_running(p, stale_after, now):
    """B-22: ALL THREE must hold -- the pidfile is a regular file, its contents name a live process,
    and the heartbeat is fresh. Dropping the third conjunct is the literal pre-#1227 bug (control
    S9)."""
    if not p.pid.is_file():
        return False
    pid = pid_from_file(p.pid)
    if pid is None:
        return False
    return pid_alive(pid) and heartbeat_fresh(p.heartbeat, stale_after, now)


def take_over(p):
    """B-24. Both writes happen INSIDE the mutex hold, in this order. A heartbeat written only after
    the mutex is released would let a second, closely-following invocation observe "live PID, no
    heartbeat yet" during a brand-new watcher's own ordinary startup and misread it as the #1227 bug,
    rather than a sibling still mid-launch (watch.sh:211-213). The unlinks are safe unconditionally:
    the mutex guarantees we are the ONLY process that can be touching either file right now, live or
    stale, no exceptions."""
    p.pid.unlink(missing_ok=True)
    p.heartbeat.unlink(missing_ok=True)
    try:                                     # #240: `watch.pid`'s format is frozen, so WHOSE
        _coexist().write_watch_owner(p.state, os.getpid())   # watcher this is lives beside it --
    except Exception:                        # noqa: BLE001 - written BEFORE the pid, so no reader
        pass                                 # sees our live pid unmarked; best-effort (a NOTE only)
    p.pid.write_text(f"{os.getpid()}\n", encoding="utf-8")   # B-25: `<pid>\n`
    p.heartbeat.touch()


def decide(p, stale_after, now):
    """-> `"sibling"` | `"already-running"` | `"acquired"`. The whole decision section as ONE
    callable -- which is what makes the F21/#339 race provable by two threads in-process (control
    S13) instead of by fifteen processes and a prayer.

    **The mutex is held for THIS function and not one statement longer** (B-26, watch.sh:219). It is
    released BEFORE either early exit and before any cleanup is registered, in a `finally` so it
    holds on every path including an exception. The Slack-commands listener's own
    `acquire_single_instance` holds its lock for the whole process LIFE; that shape is WRONG here,
    and it is pinned against by S14 (in-process, all three outcomes) and by `_race`'s own in-window
    assertion (S26). The damage a lifetime-held mutex does is invisible to a naive check: its mtime
    never advances, so after 30s any racer reclaims it from a LIVE watcher and both run -- F21's
    double-win through a new door.

    Keeping the messages in `main()` rather than here keeps `decide()` free of stdio, which is why
    the in-process controls can call it eight times in a millisecond."""
    if not acquire_decision_mutex(p, now):
        return "sibling"                     # B-20: never release a mutex we did not acquire
    try:
        running = already_running(p, stale_after, now)
        _decision_pause("before-takeover")   # TEST SEAM (no-op): F21's own window
        if not running:
            take_over(p)
    finally:
        release_decision_mutex(p)            # B-26: before either early exit, on every path
    return "already-running" if running else "acquired"


_COEXIST = []


def _coexist():
    """`coexist.py` (#240), loaded once, lazily: it loads this module itself for the live-watcher
    probe, so a module-scope load here would recurse."""
    if not _COEXIST:
        _COEXIST.append(_load("coexist"))
    return _COEXIST[0]


def _coexist_notice(sdlc_dir):
    """#240/#314: the notice line (once per run) when the old plugin is also active here, else "".
    Never a refusal -- the shared lock below admits one watcher whoever started it. A detector
    that cannot run says nothing."""
    import io
    buf = io.StringIO()
    try:
        _coexist().gate(sdlc_dir, "watcher start", stream=buf, once=True)
    except Exception:                        # noqa: BLE001 - the lock below still admits one watcher
        return ""
    return buf.getvalue().rstrip("\n")


_SCHEDULER = []


def _scheduler():
    """The upkeep scheduler module, loaded once and only when a tick first needs it: it loads the opt-in gate, and a
    module-scope load here would exit before `main` on a load failure (BR-23)."""
    if not _SCHEDULER:
        _SCHEDULER.append(_load("feature_upkeep_sched"))
    return _SCHEDULER[0]


def scheduler_step(p, sdlc_dir, call_timeout):
    """The machine-level upkeep decision, AFTER the eight calls and in its own guard, so a failure here can never skip them.
    The config is read fresh each tick (the machine variable is still the one the watcher started with). The module's gate is
    the first action: closed, this logs nothing, touches no heartbeat and writes nothing. -> the decision dict or None."""
    try:
        config = read_config(sdlc_dir)
        budget = int(call_timeout) // 4 or 1
        return _scheduler().scheduler_tick(config, sdlc_dir, budget=budget)
    except Exception as exc:                         # noqa: BLE001 - never abort the tick, never skip the rest of it
        log_line(p, f"watch: scheduler step failed (non-fatal): {type(exc).__name__}")
        return None


def applier_step(p, sdlc_dir, call_timeout, config=None):
    """Decision rubric slice 19: the act-and-tell applier, one more timed call AFTER the eight and the scheduler step, in its own
    guard. The gate is read here from the config and nothing is spawned or logged unless it is the JSON boolean true; the call
    goes through `run_call`, so through `run_with_timeout.py` like every other tick call. -> the summary line or ""."""
    try:
        config = read_config(sdlc_dir) if config is None else config
        gate = config["decision_rubric"]["autonomy"]["applier"]["enabled"]
        if gate is not True:
            return ""
    except Exception:                                # noqa: BLE001 - any shape but the open one is closed
        return ""
    try:
        touch_heartbeat(p)
        summary = run_call(p, sdlc_dir, call_timeout, "applier.py", (), "summary")
        if summary:
            tee(p, f"watch: {summary}")
        return summary
    except Exception as exc:                         # noqa: BLE001 - never abort the tick
        log_line(p, f"watch: applier step failed (non-fatal): {type(exc).__name__}")
        return ""


def cleanup(p, my_pid):
    """B-28/B-29. Registered ONLY on the takeover path, after the mutex is released -- a process
    that exits as a sibling or as "already running" removes nothing.

    Ownership-checked: the pidfile must still name US. Defence in depth against exactly the symptom
    F21 named ("the first to exit's trap removes the pid file, orphaning the survivor"): the mutex
    already prevents two watchers from ever coexisting, but this guard means even an unanticipated
    edge case can never make one process's exit rip files out from under someone else's. The
    heartbeat carries no identity of its own (just an mtime), so it rides the SAME pidfile-ownership
    check rather than an unconditional unlink -- otherwise a delayed cleanup firing after a successor
    has already taken over would delete THAT successor's live heartbeat, reintroducing the identical
    orphaning bug F21 fixed, one file over.

    Idempotent (ownership check + `missing_ok`), because it can run from both a signal handler and
    `atexit` in the same exit."""
    if pid_from_file(p.pid) != my_pid:
        return
    try:
        p.pid.unlink(missing_ok=True)
        p.heartbeat.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _coexist().clear_watch_owner(p.state, my_pid)    # #240: ownership-checked, like the pidfile
    except Exception:                        # noqa: BLE001 - cleanup must never raise
        pass


# ------------------------------------------------------------------- the tick: 8 calls, in order
#
# Ops branch only. Both sync.py calls are non-fatal: a network blip must not kill the watcher.
# Heartbeat refreshed before EACH sub-call, not just once per tick. Every one of the 8 calls in this
# tick is wrapped via run_with_timeout.py (SIGMA_WATCH_CALL_TIMEOUT, default 120s -- #2416/#2443),
# so a single call can no longer hang past CALL_TIMEOUT itself: even at STALE_AFTER's 180s floor (the
# smallest it can ever be), CALL_TIMEOUT=120 still leaves a 60s (1.5x) worst-case margin, not just the
# 22.5x margin the 900s-default production case gets. Refreshing per-call (rather than per-tick) on
# top of that still matters -- it bounds a single hung call's own exposure to CALL_TIMEOUT, not the
# whole 8-call tick's duration.
#
# THE ORDER IS LOAD-BEARING AND MUST NOT BE "TIDIED" (B-31):
#   * reconcile_tick.py (#2294) sits AFTER comment_watch.py (label/board correction is not
#     time-sensitive the way a dead agent or a new comment is) and BEFORE channel_notify.py (that
#     step's own candidate detection reads the ledger as it stands after every OTHER correction this
#     tick has already made). Ordering has no functional dependency either way -- reconcile_tick.py
#     touches GitHub issue labels only, never the ledger channel_notify.py reads -- this position
#     just keeps the "corrections before notifications" reading order intact.
#   * drift_tick.py (#2311) is a NOTIFICATION (it writes nothing to GitHub), not a correction, so it
#     belongs after every corrective step this tick has already run: right after channel_notify.py,
#     the last step before the ledger is published.
TICK_CALLS = (
    ("sync.py",           ("pull",),    "log"),      # 1
    ("watch.py",          (),           "summary"),  # 2
    ("agent_watch.py",    (),           "summary"),  # 3
    ("comment_watch.py",  (),           "summary"),  # 4
    ("reconcile_tick.py", (),           "summary"),  # 5
    ("channel_notify.py", (),           "summary"),  # 6
    ("drift_tick.py",     (),           "summary"),  # 7
    ("sync.py",           ("publish",), "log"),      # 8
)


def call_argv(sdlc_dir, call_timeout, script, subcmd):
    """B-32: `python3 run_with_timeout.py <timeout> <script> [subcommand] <sdlc_dir>` -- calls 1 and
    8 carry `pull`/`publish` BETWEEN the script path and the dir. `call_timeout` is the operator's
    raw STRING, passed verbatim exactly as bash did; `run_with_timeout.py:25` does its own
    `float(argv[1])`. `sys.executable` rather than a PATH lookup for `python3` is a deliberate
    improvement: a sub-call cannot pick a different interpreter than the one running the daemon."""
    return [sys.executable, str(_HERE / "run_with_timeout.py"), call_timeout,
            str(_HERE / script), *subcmd, sdlc_dir]


def log_line(p, line):
    """One line appended to watch.log. Opened FRESH, in append mode, for every use -- mirroring
    bash's per-redirection open, and sidestepping every shared-offset/buffering hazard a single
    long-lived handle would create between this process's buffered writes and its children's direct
    fd writes. Best-effort: a log we cannot write must never stop a tick (B-1/B-36)."""
    try:
        with open(p.log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def tee(p, line):
    """bash's `echo ... | tee -a "$LOG"`: appended to the log AND printed. `flush=True` because
    `echo` is unbuffered and a `_race` loser must not withhold its line."""
    log_line(p, line)
    print(line, flush=True)


def touch_heartbeat(p):
    """B-33's single refresh point, called at all eight positions -- one immediately BEFORE each
    call, and none after call 8, so the heartbeat's age during the sleep is measured from just
    before `publish`."""
    p.heartbeat.touch()


def run_call(p, sdlc_dir, call_timeout, script, subcmd, mode):
    """One wrapped sub-call -> its summary (`""` for the two `log`-mode calls).

    stdio is THE CONTRACT, not an implementation detail (§6.4). `run_with_timeout.py:45-48`
    deliberately sets no `stdout=`/`stderr=` so the grandchild inherits this process's already
    redirected streams, and its own comment rejects capture-and-reprint as risking stdout/stderr
    reordering -- so real file descriptors are passed here, never a Python-level wrapper.

    THE `|| echo ''` SUBTLETY a naive port gets backwards: bash's
    `summary="$(cmd 2>>"$LOG" || echo '')"` captures the WHOLE COMPOUND's stdout, so a call that
    prints "partial" and then exits 1 still yields "partial". **Capture stdout regardless of rc and
    ignore the rc entirely**; `summary = out if rc == 0 else ""` is a behavioural regression (S17).

    Never pass `env=` (§6.5): every sub-call inherits the watcher's environment verbatim, including
    SIGMA_SLACK_BOT_TOKEN (read by drift_tick.py/drift_watch.py) and SIGMA_RUN_ID, which a
    hand-built dict would silently drop. Never pass `start_new_session=` either: bash's sub-calls run
    in the watcher's own process group, and `run_with_timeout.py:49` sets it for ITS child, which is
    what makes the timeout kill reach grandchildren -- adding it a level up changes that topology."""
    argv = call_argv(sdlc_dir, call_timeout, script, subcmd)
    if mode == "log":
        with open(p.log, "ab", buffering=0) as log_fh:          # `>> "$LOG" 2>&1`, rc ignored
            subprocess.run(argv, stdout=log_fh, stderr=log_fh)
        return ""
    with open(p.log, "ab", buffering=0) as log_fh:              # `$(... 2>>"$LOG" || echo '')`
        out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=log_fh).stdout
    return (out or b"").decode("utf-8", "replace").rstrip("\n")


def rotate_log(p, config):
    """#2499. Roll `state/watch.log` to its ONE predecessor `watch.log.1` when it is at or over its
    cap; True iff it rolled. THE FIRST ACT OF A TICK, called before the tick line and before any
    `run_call` opens the log, so every fd this process hands its children is opened AFTER the
    rename and the current tick's line always lands in the fresh file, never in `.1`.

    ONE `os.replace`, same directory => atomic on POSIX, and it replaces an existing `.1` in the same
    step -- there is never a `.2`, so disk is bounded at ~2 x cap + whatever one tick writes after
    the check (observed at most ~7 KB; not bounded). Racing writers (a `_race` loser's tee'd line,
    an old-plugin watcher) open by path per write: one that opened before the rename lands in `.1`,
    one after creates the new file. No line is lost. Only the mutex owner reaches `tick()`, so there
    is one rotator per state dir.

    THE ONE GUARD, and it is broad on purpose (D-6): the cap path runs sync.py code against an
    operator's config, so a non-OSError (a TypeError from a malformed shape, a sync bug) must not
    escape any more than win32's PermissionError on a file another process holds open (D-7). A
    failed roll never skips or aborts the tick; it is retried next tick, and a log that keeps not
    rolling surfaces in doctor's `ledger watcher` row at 2 x cap. The call site in `tick()` is
    bare -- no second guard -- so this `except` is the load-bearing control (C-4)."""
    try:
        cap = sync.watch_log_cap_bytes(config, p.state)
        if p.log.stat().st_size < cap:
            return False
        os.replace(p.log, p.log.with_name("watch.log.1"))
        return True
    except Exception:            # noqa: BLE001 - D-6: a log we cannot roll must never stop a tick
        return False


def tick(p, sdlc_dir, call_timeout, n, config=None):
    """B-30 step 4 through B-34. The tick line is LOG-ONLY, never stdout (§1.8, control S16).

    #2499: `rotate_log` is the FIRST statement, outside the `try` and unguarded here -- its own
    `except` is the single guard (D-6), and it must run before `log_line` opens the log so this
    tick's line lands in the fresh file. `config=None` reads as `{}`, i.e. the DEFAULT cap: the
    two pre-#2499 callers in tests/test_watch.py pass no config and still get rotation, never a
    skip (F-4/C-7). `_run` passes the config it already read.

    The whole body is absorbed: bash ran under `set -uo pipefail` WITHOUT `-e`, so a failing command
    never aborted the script, and a port that lets an exception escape here is a regression. The
    message is the exception's CLASS NAME and never its text (plan-review finding 8): an OSError
    raised anywhere on this path routinely carries a sub-script path in its message, and
    tests/test_watch.py asserts `"drift" not in log` against a FIVE-character substring -- a `{exc}`
    catch-all would red that contract test the first time anything on the tick path failed, which is
    precisely when the log line matters most. The full traceback goes to stderr, where bash's own
    command errors went. That compensation is narrower than it reads: under `_ensure_watcher`'s
    `stdout=DEVNULL, stderr=DEVNULL` spawn (loop.py:61) the traceback is discarded, so the durable
    record of a repeating tick failure in the one context this actually runs in is a bare class name
    in watch.log. Still the right trade against reddening the absence assertions -- but a trade."""
    rotate_log(p, config or {})
    try:
        log_line(p, f"watch: tick #{n} — {time.strftime('%a %b %d %H:%M:%S %Z %Y')}")
        for script, subcmd, mode in TICK_CALLS:
            touch_heartbeat(p)                       # B-33: BEFORE the call, all eight of them
            summary = run_call(p, sdlc_dir, call_timeout, script, subcmd, mode)
            if mode == "summary" and summary:        # B-34: an empty summary produces NO line
                tee(p, f"watch: {summary}")
        scheduler_step(p, sdlc_dir, call_timeout)
        applier_step(p, sdlc_dir, call_timeout)
    except Exception as exc:                         # noqa: BLE001 - B-1/B-36: absorb, never abort
        log_line(p, f"watch: tick failed (non-fatal): {type(exc).__name__}")
        print(traceback.format_exc(), file=sys.stderr, flush=True)


def _install_cleanup(p, my_pid):
    """B-28/B-29: registered only on the takeover path, after the mutex is released.

    **This is the one place a straight transliteration silently regresses** (dossier §6.2, measured
    on this host). bash's `trap ... EXIT` fires on SIGINT (rc -2), SIGTERM (rc -15) and SIGHUP
    (rc -1), and not on SIGKILL. Python's `atexit`/`try...finally` fire on SystemExit and
    KeyboardInterrupt but **not** on SIGTERM or SIGHUP -- the default disposition terminates the
    interpreter without unwinding, leaving watch.pid + watch.heartbeat behind on every kill/pkill/
    logout: precisely the stale-marker shape #1227 exists to prevent. The Slack-commands listener
    installs the same SIGTERM/SIGHUP handlers around its own cleanup (#424).

    `getattr(signal, name, None)` is load-bearing: a bare `signal.SIGHUP` raises AttributeError on
    Windows, which is the platform this whole migration is for. Re-raising after restoring the
    default disposition reproduces bash's exit codes exactly AND keeps SIGINT from printing a
    KeyboardInterrupt traceback, which would violate B-36's "exit 0 / no stray output"."""

    def _on_signal(signum, _frame):
        cleanup(p, my_pid)
        signal.signal(signum, signal.SIG_DFL)
        try:
            os.kill(os.getpid(), signum)     # die BY the signal -> the same rc bash produced
        except Exception:                    # noqa: BLE001 - last resort, same rc by hand
            os._exit(128 + signum)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)    # SIGHUP does not exist on Windows
        if sig is not None:
            signal.signal(sig, _on_signal)
    atexit.register(cleanup, p, my_pid)      # normal returns and SystemExit


def _run(p, sdlc_dir):
    # #2498: the win32 refusal that used to be the first act here is REMOVED -- pid_alive() (via
    # _win32_pid_alive) now probes liveness safely on win32 too, so there is nothing left for a
    # platform check to guard against at this point. See the module docstring.
    try:
        os.makedirs(p.state, exist_ok=True)          # B-4
    except OSError as exc:
        # bash instead continued, both mkdir calls then failed, and it exited 0 printing the
        # misleading "a sibling is already deciding". AGENTS.md SAFETY: refuse loudly rather than
        # proceed weakly. HONEST SCOPE: under loop.py:61's DEVNULL spawn both this line and the rc
        # are discarded, and the log cannot receive it because the unwritable state dir IS the
        # condition -- it buys a true reason for a hand-run operator and a distinguishable rc for a
        # future supervisor, and nothing more.
        try:
            print(f"watch: cannot create {p.state} ({type(exc).__name__}) — refusing to start",
                  file=sys.stderr, flush=True)
        except OSError:
            pass                              # a broken stderr must not make this refusal rc 0
        return 1

    config = read_config(sdlc_dir)
    interval, interval_warning = resolve_interval(config, os.environ)
    max_ticks, max_ticks_warning = resolve_int_env(os.environ, "SIGMA_WATCH_MAX_TICKS", 0)
    scale, scale_warning = resolve_int_env(os.environ, "SIGMA_WATCH_SLEEP_SCALE", 1)
    call_timeout, call_timeout_warning = resolve_call_timeout(os.environ)
    stale_after = stale_after_seconds(interval)

    # Every warning is tee'd BEFORE the mutex is taken, so a process that then backs off as a loser
    # still prints it -- the same placement watch.sh:82-86 gives the B-12 warning below.
    for warning in (interval_warning, max_ticks_warning, scale_warning, call_timeout_warning):
        if warning:
            tee(p, warning)

    # B-12. Nothing enforces CALL_TIMEOUT < STALE_AFTER at the type level -- a misconfigured
    # SIGMA_WATCH_CALL_TIMEOUT set at or above the effective STALE_AFTER would silently
    # reintroduce the exact false-dead-window bug this file exists to close (#2416). Non-fatal
    # warning only, matching this file's fail-open posture -- never block a tick over a config smell.
    # `float()` here fires correctly for a FRACTIONAL timeout, where bash's `[ -ge ]` errored out and
    # silently suppressed the warning altogether (§5 delta 13).
    if float(call_timeout) >= stale_after:
        tee(p, f"watch: SIGMA_WATCH_CALL_TIMEOUT ({call_timeout}s) >= STALE_AFTER "
               f"({stale_after}s) -- a single hung call could still misread as a dead watcher")

    # #240/#314: the plugin under the previous name takes the SAME lock files, so two watchers are
    # impossible whoever starts first; its presence is a NOTICE (once per run, into the log), and
    # this watcher goes on to the lock. A foreign holder is named below, never signalled.
    notice = _coexist_notice(sdlc_dir)
    if notice:
        tee(p, "watch: " + notice)

    outcome = decide(p, stale_after, time.time())
    if outcome == "sibling":
        tee(p, "watch: a sibling is already deciding — nothing to do")
        return 0
    if outcome == "already-running":
        try:                                  # re-read at print time, as `cat 2>/dev/null` did
            pid_text = p.pid.read_text(encoding="utf-8").strip()
        except OSError:
            pid_text = ""
        tee(p, f"watch: already running (pid {pid_text}) — nothing to do")
        if pid_text.isdigit() and _coexist().watch_owner_pid(p.state) != int(pid_text):
            tee(p, f"watch: pid {pid_text} was not started by Sigma (no matching state/watch.owner:"
                   f" the old plugin's, or a Sigma watcher from before that marker) — Sigma starts "
                   f"no second watcher beside it and never signals it; to hand over, "
                   f"{_coexist().stop_lever(sdlc_dir)}")
        return 0

    _install_cleanup(p, os.getpid())

    ticks = 0
    while True:
        if p.stop.is_file():                                          # B-30 step 1
            tee(p, "watch: stop-file present — exiting")
            return 0
        if max_ticks > 0 and ticks >= max_ticks:                      # B-30 step 2
            tee(p, f"watch: max ticks ({max_ticks}) reached — exiting")   # prints MAX_TICKS, not ticks
            return 0
        ticks += 1
        tick(p, sdlc_dir, call_timeout, ticks, config)
        sleep_for = interval * scale                                  # B-35: SCALE=0 means NO sleep
        if sleep_for > 0:
            time.sleep(sleep_for)


USAGE = "usage: watch_daemon.py [sdlc_dir]"


def main(argv):
    """B-3/B-36. `exit 0` everywhere: the only non-zero exits this function can PRODUCE are `_run`'s
    deliberate refusal (an unwritable state dir -> 1; #240's exit 2 for the plugin under the previous
    name being active here is gone since #314 -- a notice now, see coexist.py) -- the win32 refusal that used to be
    a second one here is gone (#2498; `pid_alive()` now probes win32 safely instead of refusing).
    Everything else -- every tick failure, every malformed env value, every mutex OSError, every
    loser and every early exit -- returns 0.

    One honest qualification: a failure in the module-scope `_load("ledger")`/`_load("sync")` above
    happens BEFORE this function exists, so it exits 1 with a traceback and this guarantee cannot
    cover it. bash degraded there (its config read was `2>/dev/null || echo 900`). Accepted rather
    than wrapped, per dossier §6.10: no pidfile is written, so `doctor.py` reports "ON, but NEVER
    RUN" -- honest, and more truthful than a live-but-useless watcher.
    An escaping exception would otherwise become rc 1 plus a traceback, and `_race` puts `[rc=...]`
    in its loser diagnostic while asserting a specific substring, so it would red a race test for
    entirely the wrong reason (dossier §6.10)."""
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    sdlc_dir = argv[1] if len(argv) > 1 else ".sdlc"
    p = paths(sdlc_dir)
    try:
        return _run(p, sdlc_dir)
    except Exception as exc:                 # noqa: BLE001 - see the docstring; class name only
        log_line(p, f"watch: aborted (non-fatal): {type(exc).__name__}")
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        return 0


def _symlink_guard(argv):
    """#708: refuse a committed symlink under .sdlc/state or .sdlc/journey before any write."""
    import importlib.util as _u
    import pathlib as _p
    spec = _u.spec_from_file_location("_guard_state", _p.Path(__file__).resolve().parent / "state.py")
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.guard_argv(argv, _p.Path(__file__).name)


if __name__ == "__main__":
    sys.exit(_symlink_guard(sys.argv) or main(sys.argv))
