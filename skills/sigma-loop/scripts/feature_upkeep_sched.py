"""The upkeep scheduler: the machine-level decision step the ledger watcher runs once per tick, and the rows the doctor shows.

WHERE IT RUNS. `watch_daemon.tick()` loads this module lazily (once, cached) AFTER its eight calls and calls `scheduler_tick`
in its own guard. The decorator is the first action: gate closed means nothing is read, written or spawned, the tick log gains
no line and the heartbeat is not touched. The machine door needs the project opt-in, the machine variable AND the shared
ledger switch (see `feature_upkeep`). Nothing here reads the config block itself; settings come from the gate's reader.

WHAT ONE OPEN TICK DOES. (1) reaps a finished job and writes its outcome as an UNADDRESSED ledger note, in THIS process; (2) if
a job is still alive, stops there; (3) otherwise lists the registry's units (the include and exclude lists decide which),
drops those in their cooldown with a state read alone (no git), measures the rest within a per-tick time budget, and picks AT
MOST ONE due unit, the most behind first; (4) records the attempt BEFORE the work and starts the detached job; (5) writes the
status file. A unit it had no time to measure is not skipped for good: the status file keeps a cursor, and the
next tick resumes after it.

CEILING (stated, not discovered). One unit per tick is `86400 / interval` units a day: 96 at the 900 s default. A first
enable on N units with no state drains in N ticks, N / 96 days.

WHY THE WATCHER WRITES THE NOTES. A new writer process per run adds one ledger file and one inbox-cursor key per run, whatever
actor it is pinned to (measured: 1000 runs = 1000 files, 1000 keys). The watcher is one long-lived process: one file, one key,
ever. It writes as the configured actor else the shell user (never an actor lookup over the network), and never addressed.

PLATFORM. Windows is refused: the status file records the refusal once and no job is ever started there.
"""
import contextlib
import fnmatch
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent
_LOADED = {}


def _sibling(name):
    if name not in _LOADED:
        spec = importlib.util.spec_from_file_location(name, _HERE.joinpath(name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LOADED[name] = module
    return _LOADED[name]


feature_upkeep = _sibling("feature_upkeep")

STATUS_SCHEMA = "upkeep-sched/1"
RECORD_SCHEMA = "upkeep-job/1"
STATUS_REL = "state/upkeep/scheduler.json"      # the status file: rewritten every open tick, so its AGE is tick liveness
RECORD_REL = "state/upkeep/job.json"            # the job's own record: running, then done (written by the job, noted by the watcher)
LOCK_REL = "state/upkeep/job.lock"              # held (flock) for the job's whole life; the kernel frees it on any death
BEAT_REL = "state/upkeep/job.heartbeat"         # touched from the job's poll loop
STOP_REL = "state/upkeep.stop"                  # the operator's lever; the job never deletes it
WATCH_STOP_REL = "state/watch.stop"             # the watcher's own stop file also stops a running job
BEAT_EVERY_SECONDS = 5                          # heartbeat cadence; the poll itself is bounded_run's 0.25 s
BEAT_STALE_SECONDS = BEAT_EVERY_SECONDS * 12    # a held lock with a heartbeat older than this is a HUNG job
TICK_BUDGET_DIVISOR = 4                         # a tick's decision work may use a quarter of the per-call timeout
CAP_MARGIN_SECONDS = 900                        # wall-clock cap = the verify timeout + this (one pass also fetches and pushes)
NOTE_ATTEMPTS = 3
WINDOWS = "windows"
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "XDG_CONFIG_HOME",
             "GIT_SSH_COMMAND", "GIT_SSH")
ENV_ALLOW_PREFIXES = ("GH_", "GITHUB_")
CALL_TIMEOUT_ENV = "SIGMA_WATCH_CALL_TIMEOUT"
_JOBS = []                                      # Popen handles of jobs this process started; polled so none stays a zombie


# ------------------------------------------------------------------------------------------ small files

def path_of(sdlc_dir, rel):
    return pathlib.Path(sdlc_dir).joinpath(*rel.split("/"))


def write_json(sdlc_dir, rel, doc):
    """Atomic, symlink-refusing publish of a small JSON document. -> bool; never raises."""
    state = _sibling("state")
    try:
        state.refuse_symlinks(sdlc_dir, rel, True)
        state.atomic_write_text(path_of(sdlc_dir, rel), json.dumps(doc, sort_keys=True))
        return True
    except Exception:                                       # noqa: BLE001 - a status that cannot be written is reported, not raised
        return False


def read_json(sdlc_dir, rel, cap=65536):
    try:
        text = path_of(sdlc_dir, rel).read_text(encoding="utf-8")
        doc = json.loads(text) if len(text) <= cap else None
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def file_age(path, now):
    try:
        return max(0, int(now - os.stat(path).st_mtime))
    except OSError:
        return None


# ------------------------------------------------------------------------------------------ the job's liveness

def job_alive(sdlc_dir, now):
    """-> "running" (the job's lock is held), "idle" or "hung" (held, but the heartbeat is older than BEAT_STALE_SECONDS).
    Where flock is unavailable the heartbeat age alone answers."""
    lock = path_of(sdlc_dir, LOCK_REL)
    if not lock.exists():
        return "idle"
    held = None
    try:
        import fcntl
        with open(lock, "a") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = False
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                held = True
    except (ImportError, OSError):
        held = None
    age = file_age(path_of(sdlc_dir, BEAT_REL), now)
    if held is None:
        held = age is not None and age <= BEAT_STALE_SECONDS
    if not held:
        return "idle"
    return "hung" if age is None or age > BEAT_STALE_SECONDS else "running"


def _reap():
    _JOBS[:] = [proc for proc in _JOBS if proc.poll() is None]


# ------------------------------------------------------------------------------------------ units

def selected(names, include, exclude):
    """The units the include and exclude lists admit: case-folded glob match, and an exclude always wins."""
    def hit(name, patterns):
        return any(fnmatch.fnmatchcase(name.lower(), str(p).lower()) for p in patterns)
    return sorted(n for n in names if hit(n, include or ["*"]) and not hit(n, exclude or []))


def list_units(sdlc_dir):
    registry = _sibling("feature_registry")
    with contextlib.redirect_stderr(io.StringIO()):          # one stderr note per unreadable shard, every call
        return sorted(registry.read(registry.registry_dir(sdlc_dir)))


def refs_of(config, unit):
    """-> (base_ref, unit_ref) as the remote's names, or (None, None) when no base is configured."""
    rebase = _sibling("feature_rebase")
    remote = rebase._remote(config)
    base = (rebase._settings(config).get("base") or "").strip() if isinstance(rebase._settings(config), dict) else ""
    if not base:
        return None, None
    return "%s/%s" % (remote, base), "%s/%s%s" % (remote, _sibling("features").BRANCH_PREFIX, unit)


class _Opaque:
    """A drift nobody measured: only the cooldown can hold on it, which is exactly the state-only filter."""
    behind = drift = threshold = dormant = None


def _rank(item):
    drift = item[2]
    known = type(drift.behind) is int                     # an unmeasurable count cannot be ordered: it sorts after every known one
    return (0 if known else 1, -(drift.behind if known else 0), item[1])


class OutOfTime(Exception):
    """Raised by a measure that found the tick's budget spent before its next git read: the unit is left unexamined."""


def make_measure(config, settings, sdlc_dir, root, now, expired=None):
    """-> measure(unit, doc) -> Drift, built on the bounded group-kill git runner. The two reads that do not depend on the
    unit (the shallow check and the base walk) are made once per tick. `expired()` is asked before each read, so the budget
    is enforced inside a unit's measurement, not only between units; it raises OutOfTime (each read is itself bounded)."""
    drift_mod, pass_mod = _sibling("feature_upkeep_drift"), _sibling("feature_upkeep_pass")
    tune, shared = drift_mod.tuning(settings), {}

    def check():
        if expired is not None and expired():
            raise OutOfTime()

    def measure(unit, doc):
        check()
        base_ref, unit_ref = refs_of(config, unit)
        if base_ref is None:
            return drift_mod.Drift(drift_mod.UNKNOWN, drift_mod.UNKNOWN, drift_mod.UNKNOWN, drift_mod.UNKNOWN, ("no-base",))
        if "shallow" not in shared:
            shared["shallow"] = drift_mod.is_shallow(pass_mod._git, root)
        check()
        if "base" not in shared:
            shared["base"] = drift_mod.walk(pass_mod._git, root, base_ref, None, drift_mod.WALK_CAP)
        check()
        return drift_mod.measure_unit(pass_mod._git, root, base_ref, unit_ref, tune, now,
                                      anchor=(doc or {}).get("unit_tip"), base_walk=shared["base"], shallow=shared["shallow"])
    return measure


# ------------------------------------------------------------------------------------------ the job

def job_env(config, settings, environ=None):
    """The detached job's environment: an allowlist (not the watcher's whole environment), the machine variable, an explicit
    call timeout for the push guard instead of the inherited one, and the prompt pins. No run id is set: the job is not a
    supervised run."""
    source = os.environ if environ is None else environ
    env = {k: v for k, v in source.items()
           if k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES) or k.startswith("LC_")}
    env[feature_upkeep.ENV_MACHINE] = "1"
    env[CALL_TIMEOUT_ENV] = str(cap_seconds(settings))
    return _sibling("bounded_run").unattended_env(env)


def cap_seconds(settings):
    """The job's wall-clock cap: the verify timeout the project chose plus a stated margin, never a laptop constant."""
    return settings["verify.timeout_minutes"] * 60 + CAP_MARGIN_SECONDS


def start_job(sdlc_dir, root, unit, run_id, config, settings, environ=None):
    """Start the detached job: its own session, all three stdio on the null device (an inherited stdout would hold the tick's
    call for the child's whole life), an absolute state path and the project root as its working directory.
    -> the Popen handle (kept so it is polled, never left a zombie). Raises OSError when it cannot start."""
    argv = [sys.executable, "-I", str(_HERE.joinpath("feature_upkeep_job.py")), "--sdlc", str(pathlib.Path(sdlc_dir).resolve()),
            "--root", str(pathlib.Path(root).resolve()), "--unit", unit, "--run-id", run_id,
            "--cap", str(cap_seconds(settings))]
    proc = subprocess.Popen(argv, cwd=str(root), env=job_env(config, settings, environ), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    _JOBS.append(proc)
    return proc


# ------------------------------------------------------------------------------------------ the notes

def with_actor(config):
    """A copy of the config whose ledger actor is the CONFIGURED one else the shell user: the ledger's own resolution would ask
    the network for the account name, with no timeout. The same offline rule the knowledge commit subject uses."""
    cfg = dict(config) if isinstance(config, dict) else {}
    block = dict(cfg.get("ledger") or {})
    block["actor"] = _sibling("sync").knowledge_actor(config)
    cfg["ledger"] = block
    return cfg


def write_outcome_note(config, sdlc_dir, record, append=None):
    """The job's outcome as one UNADDRESSED note, written by THIS process. A filing the job made is not echoed as a note to
    anyone: the job files with no goal, so the ledger row it leaves carries no addressee either. -> bool."""
    tip = str(record.get("unit_tip") or "")[:12] or "none"
    done = _sibling("feature_upkeep_pass").ledger_note(with_actor(config), sdlc_dir, str(record.get("unit") or ""),
                                                       str(record.get("outcome") or "unknown"), tip, append=append)
    return bool(done.get("ok"))


def _close_killed(sdlc_dir, record, now):
    """A job whose process was killed outright (not just its child) never wrote its final record: the lock is free and the
    record still says running. Close it as a `failed` outcome so the same path notes it, and count it against the unit."""
    closed = dict(record, state="done", outcome="failed", unit_tip=None, finished_at=now, killed=True)
    write_json(sdlc_dir, RECORD_REL, closed)
    if record.get("unit"):
        _sibling("feature_upkeep_state").record_outcome(sdlc_dir, str(record["unit"]), now, "failed")
    return closed


def collect(config, sdlc_dir, now, append=None):
    """Note a finished job's outcome, once. -> "noted", "failed", "none" or "running". The record gains `noted`; a note that
    could not be written is tried again next tick, at most NOTE_ATTEMPTS times in all."""
    _reap()
    record = read_json(sdlc_dir, RECORD_REL)
    if not record or record.get("schema") != RECORD_SCHEMA or record.get("noted"):
        return "none"
    if record.get("state") == "running":
        if job_alive(sdlc_dir, now) != "idle":
            return "running"
        record = _close_killed(sdlc_dir, record, now)       # lock free, record still running: the job process itself was killed
    elif record.get("state") != "done":
        return "none"
    if job_alive(sdlc_dir, now) != "idle":
        return "running"
    attempts = record.get("note_attempts") if type(record.get("note_attempts")) is int else 0
    if attempts >= NOTE_ATTEMPTS:
        return "none"
    ok = write_outcome_note(config, sdlc_dir, record, append)
    record = dict(record, noted=ok, note_attempts=attempts + 1)
    write_json(sdlc_dir, RECORD_REL, record)
    return "noted" if ok else "failed"


# ------------------------------------------------------------------------------------------ the decision step

def _status(sdlc_dir, now, **fields):
    doc = {"schema": STATUS_SCHEMA, "last_tick": now}
    doc.update(fields)
    write_json(sdlc_dir, STATUS_REL, doc)
    return fields


@feature_upkeep.gated("machine")
def scheduler_tick(config, sdlc_dir, *, environ=None, now=None, budget=30, clock=time.monotonic, deps=None):
    """One open tick (see the module docstring). -> a dict with `decision`: refused, job-running, idle (nothing due),
    started, state-unwritable, spawn-failed. `deps` injects list_units, measure, spawn, append (tests only)."""
    deps = deps or {}
    now = int(time.time() if now is None else now)
    gate_reading = feature_upkeep.read(config)
    settings = gate_reading.settings
    bounded = _sibling("bounded_run")
    if bounded.group_refusal() is not None or sys.platform == "win32":
        prior = read_json(sdlc_dir, STATUS_REL) or {}
        if prior.get("refusal") != WINDOWS:                  # recorded ONCE: later ticks leave the file alone
            _status(sdlc_dir, now, decision="refused", refusal=WINDOWS)
        return {"decision": "refused", "refusal": WINDOWS}
    collected = collect(config, sdlc_dir, now, deps.get("append"))
    alive = job_alive(sdlc_dir, now)
    if alive != "idle" or collected == "running":
        return _status(sdlc_dir, now, decision="job-running", job=alive, noted=collected)
    state, drift_mod = _sibling("feature_upkeep_state"), _sibling("feature_upkeep_drift")
    sched = state.schedule(settings)
    root = str(pathlib.Path(sdlc_dir).resolve().parent)
    names = selected((deps.get("list_units") or list_units)(sdlc_dir), settings["units.include"], settings["units.exclude"])
    reads = {n: state.read_state(sdlc_dir, n, now) for n in names}
    waiting = [n for n in names if state.decide(sched, reads[n], _Opaque, now).reason != "cooldown"]
    cursor = (read_json(sdlc_dir, STATUS_REL) or {}).get("cursor")                       # where the last out-of-time tick stopped
    if isinstance(cursor, str):
        waiting = [n for n in waiting if n > cursor] + [n for n in waiting if n <= cursor]
    deadline, due, examined = clock() + budget, [], 0
    measure = deps.get("measure") or make_measure(config, settings, sdlc_dir, root, now, expired=lambda: clock() >= deadline)
    for name in waiting:
        if clock() >= deadline:
            break
        try:
            drift = measure(name, reads[name].doc)
        except OutOfTime:                                  # the budget ran out inside this unit: it is retried first next tick
            break
        examined += 1
        verdict = state.decide(sched, reads[name], drift, now)
        if verdict.due:
            due.append((0, name, drift, verdict))
    out_of_time = examined < len(waiting)
    resume = waiting[examined - 1] if out_of_time and examined else None               # the next tick starts after this unit
    if not due:
        return _status(sdlc_dir, now, decision="idle", examined=examined, waiting=len(waiting), out_of_time=out_of_time,
                       cursor=resume, noted=collected)
    due.sort(key=_rank)
    _, name, drift, verdict = due[0]
    claim = state.claim(sdlc_dir, name, sched, drift, now)
    if not claim.started:
        return _status(sdlc_dir, now, decision="state-unwritable", unit=name, detail=claim.detail, examined=examined)
    run_id = "%d-%d" % (now, os.getpid())
    try:
        (deps.get("spawn") or start_job)(sdlc_dir, root, name, run_id, config, settings, environ)
    except OSError:
        state.record_outcome(sdlc_dir, name, now, "failed")        # a failed start costs the cooldown, and is counted
        return _status(sdlc_dir, now, decision="spawn-failed", unit=name, examined=examined)
    return _status(sdlc_dir, now, decision="started", unit=name, reason=claim.reason, run_id=run_id, examined=examined,
                   waiting=len(waiting), out_of_time=out_of_time, cursor=resume,
                   noted=collected)


# ------------------------------------------------------------------------------------------ the doctor's rows

def project_open(config):
    """True when the doctor should show scheduler rows at all: the block is on, or it is on but invalid."""
    reading = feature_upkeep.read(config)
    return bool(reading.enabled or reading.problems)


def health(config, sdlc_dir, now=None):
    """-> [(label, ok, detail)]. Liveness rows (the scheduler's tick age, the job's heartbeat, the last outcome) and the
    readiness row (which opt-ins are missing on THIS shell). Read-only; no process is started. Empty when the block is off."""
    if not project_open(config):
        return []
    now = int(time.time() if now is None else now)
    rows = []
    verdict = feature_upkeep.evaluate(config, "machine")
    if verdict["problems"]:
        rows.append(("upkeep settings", False, "invalid: " + "; ".join(verdict["problems"])))
    elif verdict["missing"]:
        rows.append(("upkeep scheduler readiness", False,
                     "not ready on this machine, missing: " + ", ".join(verdict["missing"])))
    else:
        rows.append(("upkeep scheduler readiness", True, "all opt-ins present in this shell"))
    wk = config.get("work") if isinstance(config, dict) else None
    if not (wk.get("base") or "").strip() if isinstance(wk, dict) else True:
        rows.append(("upkeep integration branch", False,
                     "work.base is empty, so every upkeep pass ends no-base and does nothing; "
                     "set work.base in .sdlc/config.json to your default branch"))
    sync = _sibling("sync")
    stale = sync.stale_after_seconds(sync.watch_interval_seconds(config))
    status = read_json(sdlc_dir, STATUS_REL)
    if status is None:
        rows.append(("upkeep scheduler liveness", False, "no tick recorded yet (the ledger watcher has not run a tick with the opt-in open)"))
    elif status.get("refusal") == WINDOWS:
        rows.append(("upkeep scheduler liveness", False, "refused: background upkeep is not supported on Windows"))
    else:
        age = file_age(path_of(sdlc_dir, STATUS_REL), now)
        ok = age is not None and age <= stale
        rows.append(("upkeep scheduler liveness", ok, "last tick %ss ago, decision %s%s" % (
            age, status.get("decision"), "" if ok else " (older than %ss: the watcher may be down)" % stale)))
    alive = job_alive(sdlc_dir, now)
    record = read_json(sdlc_dir, RECORD_REL) or {}
    if alive == "hung":
        rows.append(("upkeep job", False, "HUNG: the lock is held but the heartbeat is older than %ss" % BEAT_STALE_SECONDS))
    elif alive == "running":
        rows.append(("upkeep job", True, "running, heartbeat %ss ago" % file_age(path_of(sdlc_dir, BEAT_REL), now)))
    elif record.get("state") == "done":
        rows.append(("upkeep job", True, "last outcome %s" % record.get("outcome")))
    else:
        rows.append(("upkeep job", True, "none has run yet"))
    return rows
