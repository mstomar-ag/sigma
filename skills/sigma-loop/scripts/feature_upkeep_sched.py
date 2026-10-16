"""The upkeep scheduler: the machine-level decision step the ledger watcher runs once per tick, and the rows the doctor shows.

WHERE IT RUNS. `watch_daemon.tick()` loads this module lazily (once, cached) AFTER its eight calls and calls `scheduler_tick`
in its own guard. The decorator is the first action: gate closed means nothing is read, written or spawned, the tick log gains
no line and the heartbeat is not touched. The machine door needs the project opt-in, the machine variable AND the shared
ledger switch (see `feature_upkeep`). Nothing here reads the config block itself; settings come from the gate's reader.

WHAT ONE OPEN TICK DOES (#1387: a window, not a per-unit poll). (1) reaps a finished job and writes its outcome as an
UNADDRESSED ledger note, in THIS process; (2) if a job is still alive, stops there; (3) reads the window stamp
(`feature_upkeep_window`, one small file): window closed means decision `window-closed` and NOTHING else, no unit listing, no
git, no gh; (4) window open: stamps the attempt BEFORE the work and starts ONE detached sweep job over every unit; (5) writes
the status file. The per-unit drift, backstop and cooldown rule no longer decides when upkeep runs.

CEILING (stated, not discovered). One sweep per window: `24 / window_hours` sweeps a day per repository at most (2 at the 12 h
default), each bounded by one total cap; units the cap leaves unreached wait for the next window and are reported.

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
LAST_START_REL = "state/upkeep/last_start.json"   # who started the last job: trigger (a merge), tick (the watcher) or pick
STARTED_BY = ("trigger", "tick", "pick")
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
CREDENTIAL_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST")
CALL_TIMEOUT_ENV = "SIGMA_WATCH_CALL_TIMEOUT"
SWEEP_RUN_ENV = "SIGMA_UPKEEP_SWEEP_RUN"         # the sweep job sets it in each child; the Level 2 sweep counter reads it (#1536)
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


def list_units_notes(sdlc_dir):
    """-> (sorted unit names, the registry reader's stderr notes as a list of lines). #1493: the notes are kept, not dropped."""
    registry = _sibling("feature_registry")
    sink = io.StringIO()
    with contextlib.redirect_stderr(sink):                   # one stderr note per unreadable shard, every call
        names = sorted(registry.read(registry.registry_dir(sdlc_dir)))
    return names, [line.strip() for line in sink.getvalue().splitlines() if line.strip()]


def list_units(sdlc_dir):
    return list_units_notes(sdlc_dir)[0]


def unregistered_heads(sdlc_dir, remote, cwd=None, registered=None):
    """#1493: the remote-tracking `feature/*` branches (one local git read, no network) whose unit is not in the registry:
    the sweep cannot see them, so nothing maintains them. Sorted unit names; [] on any error, never an exception."""
    try:
        if not (isinstance(remote, str) and remote and not remote.startswith("-")):
            return []
        known = {n.lower() for n in (list_units(sdlc_dir) if registered is None else registered)}
        prefix = "refs/remotes/%s/%s" % (remote, _sibling("features").BRANCH_PREFIX)
        out = _drift_runner(cwd or pathlib.Path(sdlc_dir).parent, ["for-each-ref", "--format=%(refname)", prefix])
        names = {line.strip()[len(prefix):] for line in str(out).splitlines() if line.strip().startswith(prefix)}
        return sorted(n for n in names if n and n.lower() not in known)
    except Exception:                                        # noqa: BLE001 - an unanswered read is no finding
        return []


def refs_of(config, unit):
    """-> (base_ref, unit_ref) as the remote's names, or (None, None) when no base is configured."""
    rebase = _sibling("feature_rebase")
    remote = rebase._remote(config)
    base = rebase.base_of(config).strip()                                     # #1425: `origin/main` means `main`
    if not base or rebase.is_unit_base(base, remote, config):             # #1311: a unit branch is no base; nothing is offered
        return None, None
    return "%s/%s" % (remote, base), "%s/%s%s" % (remote, _sibling("features").BRANCH_PREFIX, unit)


def unit_base_configured(config):
    """#1327: True when `work.base` is set but names a unit branch (so the scheduler's note can say so, not just `no-base`)."""
    rebase = _sibling("feature_rebase")
    settings = rebase._settings(config)
    base = (settings.get("base") or "").strip() if isinstance(settings, dict) else ""
    return bool(base) and rebase.is_unit_base(base, rebase._remote(config), config)


def _drift_runner(cwd, argv):
    """The drift module's `run(cwd, argv)` convention has argv start with `git`; the pass default runner prepends it itself."""
    argv = list(argv)
    return _sibling("feature_upkeep_pass")._git(cwd, argv[1:] if argv[:1] == ["git"] else argv)


class OutOfTime(Exception):
    """Raised by a measure that found the tick's budget spent before its next git read: the unit is left unexamined."""


def make_measure(config, settings, sdlc_dir, root, now, expired=None):
    """-> measure(unit, doc) -> Drift, built on the bounded group-kill git runner. The two reads that do not depend on the
    unit (the shallow check and the base walk) are made once per tick. `expired()` is asked before each read, so the budget
    is enforced inside a unit's measurement, not only between units; it raises OutOfTime (each read is itself bounded)."""
    drift_mod = _sibling("feature_upkeep_drift")
    tune, shared = drift_mod.tuning(settings), {}

    def check():
        if expired is not None and expired():
            raise OutOfTime()

    def measure(unit, doc):
        check()
        base_ref, unit_ref = refs_of(config, unit)
        if base_ref is None:
            why = _sibling("feature_rebase").UNIT_BASE_NOTE if unit_base_configured(config) else "no-base"   # #1327
            return drift_mod.Drift(drift_mod.UNKNOWN, drift_mod.UNKNOWN, drift_mod.UNKNOWN, drift_mod.UNKNOWN, (why,))
        if "shallow" not in shared:
            shared["shallow"] = drift_mod.is_shallow(_drift_runner, root)
        check()
        if "base" not in shared:
            shared["base"] = drift_mod.walk(_drift_runner, root, base_ref, None, drift_mod.WALK_CAP)
        check()
        return drift_mod.measure_unit(_drift_runner, root, base_ref, unit_ref, tune, now,
                                      anchor=(doc or {}).get("unit_tip"), base_walk=shared["base"], shallow=shared["shallow"])
    return measure


# ------------------------------------------------------------------------------------------ the job

def allowed_env(source, extra=()):
    """-> the variables of `source` the job and its engine child may see: the named allowlist, the locale family, the named
    credential variables and `extra`. Nothing else, whatever its name looks like."""
    return {k: v for k, v in source.items()
            if k in ENV_ALLOW or k in CREDENTIAL_ENV or k in extra or k.startswith("LC_")}


def job_env(config, settings, environ=None):
    """The detached job's environment: an allowlist (not the watcher's whole environment), the machine variable, an explicit
    call timeout for the push guard instead of the inherited one, and the prompt pins. No run id is set: the job is not a
    supervised run."""
    env = allowed_env(os.environ if environ is None else environ)
    env[feature_upkeep.ENV_MACHINE] = "1"
    env[CALL_TIMEOUT_ENV] = str(cap_seconds(settings))
    return _sibling("bounded_run").unattended_env(env)


def cap_seconds(settings):
    """The job's wall-clock cap: the verify timeout the project chose plus a stated margin, never a laptop constant."""
    return settings["verify.timeout_minutes"] * 60 + CAP_MARGIN_SECONDS


def start_job(sdlc_dir, root, unit, run_id, config, settings, environ=None, sweep=False):
    """Start the detached job: its own session, all three stdio on the null device (an inherited stdout would hold the tick's
    call for the child's whole life), an absolute state path and the project root as its working directory.
    -> the Popen handle (kept so it is polled, never left a zombie). Raises OSError when it cannot start."""
    argv = [sys.executable, "-I", str(_HERE.joinpath("feature_upkeep_job.py")), "--sdlc", str(pathlib.Path(sdlc_dir).resolve()),
            "--root", str(pathlib.Path(root).resolve()), "--unit", unit, "--run-id", run_id,
            "--cap", str(cap_seconds(settings))] + (["--sweep"] if sweep else [])
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
    if record.get("unit") and not record.get("sweep_job"):
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
def scheduler_tick(config, sdlc_dir, *, environ=None, now=None, budget=30, clock=time.monotonic, deps=None,
                   started_by="tick"):
    """One open tick (see the module docstring). -> a dict with `decision`: refused, job-running, idle (nothing due),
    window-closed, started, state-unwritable, spawn-failed. `started_by` "tick" is the watcher; any other value (the merge trigger) is a
    quiet pass: it neither rewrites the tick status file (its age is tick liveness) nor collects outcome notes (the watcher
    is their one writer). Both write the last-start record when they start a job. `deps` injects spawn and append (tests only)."""
    deps = deps or {}
    if started_by not in STARTED_BY:
        raise ValueError("started_by must be one of %s" % ", ".join(STARTED_BY))
    status = _status if started_by == "tick" else (lambda _sdlc, _now, **fields: fields)
    now = int(time.time() if now is None else now)
    gate_reading = feature_upkeep.read(config)
    settings = gate_reading.settings
    bounded = _sibling("bounded_run")
    if bounded.group_refusal() is not None or sys.platform == "win32":
        prior = read_json(sdlc_dir, STATUS_REL) or {}
        if prior.get("refusal") != WINDOWS:                  # recorded ONCE: later ticks leave the file alone
            status(sdlc_dir, now, decision="refused", refusal=WINDOWS)
        return {"decision": "refused", "refusal": WINDOWS}
    collected = collect(config, sdlc_dir, now, deps.get("append")) if started_by == "tick" else None
    alive = job_alive(sdlc_dir, now)
    if alive != "idle" or collected == "running":
        return status(sdlc_dir, now, decision="job-running", job=alive, noted=collected)
    window = _sibling("feature_upkeep_window")
    if not window.is_open(settings, sdlc_dir, now):          # a stamp read and arithmetic: no unit listing, no git, no gh
        return status(sdlc_dir, now, decision="window-closed", noted=collected)
    root = str(pathlib.Path(sdlc_dir).resolve().parent)
    if not window.stamp_attempt(sdlc_dir, now, "attempted", (window.read(sdlc_dir) or {}).get("unreached") or []):
        return status(sdlc_dir, now, decision="state-unwritable", unit="*", detail="window stamp")
    run_id = "%d-%d" % (now, os.getpid())
    try:
        (deps.get("spawn") or start_job)(sdlc_dir, root, "*", run_id, config, settings, environ, True)
    except OSError:
        return status(sdlc_dir, now, decision="spawn-failed", unit="*")
    write_json(sdlc_dir, LAST_START_REL, {"schema": "upkeep-start/1", "by": started_by, "unit": "*", "run_id": run_id, "at": now})
    return status(sdlc_dir, now, decision="started", unit="*", run_id=run_id, noted=collected)


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
                     "this shell is missing: " + ", ".join(verdict["missing"])
                     + " (a check of this shell only; the watcher's own environment is not read, so this does not show the watcher lacks it)"))
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
    window = _sibling("feature_upkeep_window")
    settings = feature_upkeep.read(config).settings
    stamp = window.read(sdlc_dir)
    old, age = window.stamp_age(settings, sdlc_dir, now) if stamp else (False, None)
    left = ", unreached: " + ", ".join(stamp.get("unreached") or []) if stamp and stamp.get("unreached") else ""
    rows.append(("upkeep window", not old, "%d h window (triggers.* no longer schedule); %s%s%s" % (
        window.hours_of(settings), window.describe(settings, sdlc_dir, now), left,
        " -- older than %d windows: no sweep is running" % window.STALE_WINDOWS if old else "")))
    last = read_json(sdlc_dir, LAST_START_REL)
    if isinstance(last, dict) and last.get("by") in STARTED_BY:
        rows.append(("upkeep last start", True, "started by %s, unit %s, %ss ago" % (
            last["by"], last.get("unit"), max(0, now - int(last.get("at") or now)))))
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


# ------------------------------------------------------------------------------------------ the merge trigger's command

def main(argv=None):
    """`feature_upkeep_sched.py decision <sdlc_dir>`: one quiet due-check (#1225), run by the merge path through the timed
    runner. Prints nothing and always exits 0; a closed gate does nothing at all."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] in (["-h"], ["--help"]) or len(argv) != 2 or argv[0] != "decision":
        print("usage: feature_upkeep_sched.py decision <sdlc_dir>   (one quiet upkeep due-check; prints nothing)")
        return 0
    sdlc_dir = argv[1]
    try:
        config = json.loads(pathlib.Path(sdlc_dir, "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    try:
        budget = int(float(os.environ.get(CALL_TIMEOUT_ENV) or 120)) // TICK_BUDGET_DIVISOR or 1
    except ValueError:
        budget = 30
    scheduler_tick(config, sdlc_dir, budget=budget, started_by="trigger")
    return 0


if __name__ == "__main__":
    sys.exit(main())
