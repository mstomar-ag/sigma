"""The detached, bounded upkeep job: one unit, one pass, started by the scheduler (`feature_upkeep_sched`).

Usage: feature_upkeep_job.py --sdlc DIR --root DIR --unit NAME --run-id ID --cap SECONDS
       feature_upkeep_job.py --sweep --sdlc DIR --root DIR --run-id ID --cap SECONDS   (#1387: every unit, one total cap)
       feature_upkeep_job.py --engine --sdlc DIR --root DIR --unit NAME      (the pass itself; the job starts this)

Not meant to be run by hand: the scheduler starts it with every stdio stream on the null device, in its own session.

BOUNDS. The pass runs as a CHILD in its own process group under `bounded_run.run_group`: a wall-clock cap (the verify timeout
plus a margin, derived by the scheduler), a stop file (`state/upkeep.stop` or the watcher's own `state/watch.stop`; the job
never deletes either), a lifeline (the child group dies if this process is killed) and a heartbeat file touched from the
poll loop (no thread). One job at a time: the job holds an exclusive lock on `state/upkeep/job.lock` for its whole life, which
the kernel frees on any death.

RECORD. The job has no output channel, so every exit path writes a final record (`state/upkeep/job.json`): done, with the
outcome. A crash is recorded as a failed outcome, never silence. The WATCHER turns the record into a ledger note.

FILING. The pass is handed NO goal, so a conflict it files is a finding about the unit with no addressee: no note is written
to anyone, and no driven run can start from it on another machine.

GATE. Both entry points are behind the machine door (project opt-in, the machine variable, the ledger switch); the scheduler
passes the variable in the job's allowlisted environment, and a closed job writes nothing.
"""
import argparse
import importlib.util
import json
import os
import pathlib
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


sched = _sibling("feature_upkeep_sched")
feature_upkeep = sched.feature_upkeep


class EitherStop(os.PathLike):
    """A stop path that exists when ANY of the given files does: `bounded_run` takes one stop path and tests it with
    `os.path.exists`, which accepts this."""

    def __init__(self, *paths):
        self.paths = [str(p) for p in paths]

    def __fspath__(self):
        for path in self.paths:
            if os.path.exists(path):
                return path
        return self.paths[0]


class BeatingClock:
    """The supervisor's clock, which also refreshes the heartbeat every `every` seconds: `bounded_run` reads it on every
    poll, so the heartbeat comes from the poll loop and no thread is needed."""

    def __init__(self, touch, every=sched.BEAT_EVERY_SECONDS, base=time.monotonic):
        self.touch, self.every, self.base, self.last = touch, every, base, None

    def __call__(self):
        now = self.base()
        if self.last is None or now - self.last >= self.every:
            self.last = now
            self.touch()
        return now


def _touch(path):
    try:
        pathlib.Path(path).touch()
    except OSError:
        pass


def _record(sdlc_dir, doc):
    return sched.write_json(sdlc_dir, sched.RECORD_REL, doc)


PRUNE_REL = "state/upkeep/prune.json"     # #1557: the last automatic backup prune (read by the doctor)


def _prune_pass(config, sdlc_dir, root, at, prune=None, prune_run=None, beat_path=None):
    """#1557: once per sweep, prune backup refs past keep_days (newest keep_last per unit kept; at most MAX_DELETES). Never
    raises: a failure is recorded as outcome `failed` and the next sweep replans the same refs. -> the bounded record."""
    try:
        if beat_path:                       # #1632: the prune touches the sweep beat before every git call, so a slow remote never reads as a dead sweep
            inner = prune_run or _sibling("feature_backup").sync._run

            def prune_run(*a, **k):
                _touch(beat_path)
                return inner(*a, **k)
        res = (prune or _sibling("feature_backup").prune_backups)(
            config, sdlc_dir, remote=_sibling("feature_rebase")._remote(config), cwd=root, **({"run": prune_run} if prune_run else {}))
        word = str(res.get("outcome") or "failed")[:20]
        doc = {"outcome": word, "deleted": res.get("deleted") if isinstance(res.get("deleted"), int) else 0,
               "listed": res.get("listed") if isinstance(res.get("listed"), int) else 0, "at": at}
    except Exception:                       # noqa: BLE001 - a prune that cannot run never fails the sweep
        doc = {"outcome": "failed", "deleted": 0, "listed": 0, "at": at}
    sched.write_json(sdlc_dir, PRUNE_REL, doc)
    return doc


def _reason_of(result, bounded):
    """#1327: the child's `reason` text, kept next to the outcome in the job record; None when it said none. Never raises."""
    try:
        doc = json.loads((result.out or "").strip().splitlines()[-1]) if result.outcome == bounded.OK else None
    except (ValueError, IndexError):
        return None
    reason = doc.get("reason") if isinstance(doc, dict) else None
    return reason[:200] if isinstance(reason, str) and reason else None


def _outcome_of(result, bounded):
    """-> (outcome word, unit_tip or None) from the child's Result. Words in the state vocabulary pass through; anything else
    the engine did not say is `failed`; a stop or timeout keeps its own word for the note."""
    if result.outcome == bounded.TIMEOUT:
        return "timeout", None
    if result.outcome == bounded.STOPPED:
        return "stopped", None
    if result.outcome != bounded.OK:
        return "failed", None
    try:
        doc = json.loads((result.out or "").strip().splitlines()[-1])
        word = doc.get("outcome") if isinstance(doc, dict) else None
        tip = doc.get("unit_tip") if isinstance(doc, dict) else None
    except (ValueError, IndexError):
        return "failed", None
    return (word if isinstance(word, str) and word else "failed"), (tip if isinstance(tip, str) else None)


@feature_upkeep.gated("machine")
def run_job(config, sdlc_dir, root, unit, run_id, cap, *, environ=None, command=None, clock=time.monotonic, beat=None):
    """Run the pass for `unit` as a bounded child and record how it ended. -> {"outcome", "unit_tip"} or {"outcome": "busy"}
    when another job holds the lock, or {"outcome": "refused"} on a host that cannot run a process group."""
    bounded, state = _sibling("bounded_run"), _sibling("feature_upkeep_state")
    if bounded.group_refusal() is not None:
        return {"outcome": "refused"}
    try:
        import fcntl
    except ImportError:
        return {"outcome": "refused"}
    lock_path = sched.path_of(sdlc_dir, sched.LOCK_REL)
    _sibling("state").refuse_symlinks(sdlc_dir, sched.LOCK_REL, True)
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"outcome": "busy"}
        head = {"schema": sched.RECORD_SCHEMA, "unit": unit, "run_id": run_id, "noted": False}
        _record(sdlc_dir, dict(head, state="running", started_at=int(time.time())))
        beat_path = sched.path_of(sdlc_dir, sched.BEAT_REL)
        _touch(beat_path)
        outcome, tip, reason = "failed", None, None       # what a crash inside this block leaves behind
        try:
            argv = command or [sys.executable, "-I", str(pathlib.Path(__file__).resolve()), "--engine", "--sdlc", str(sdlc_dir),
                               "--root", str(root), "--unit", unit,
                               "--run-id", str(run_id), "--cap", str(cap)]
            stop = EitherStop(sched.path_of(sdlc_dir, sched.STOP_REL), sched.path_of(sdlc_dir, sched.WATCH_STOP_REL))
            result = bounded.run_group(argv, root, cap, env=bounded.unattended_env(sched.allowed_env(
                                           os.environ if environ is None else environ, (sched.CALL_TIMEOUT_ENV, feature_upkeep.ENV_MACHINE))),
                                       stop_path=stop, merge=False,
                                       clock=beat or BeatingClock(lambda: _touch(beat_path), base=clock))
            outcome, tip = _outcome_of(result, bounded)
            reason = _reason_of(result, bounded)
        finally:
            _record(sdlc_dir, dict(head, state="done", outcome=outcome, unit_tip=tip, finished_at=int(time.time()),
                                   **({"reason": reason} if reason else {})))
            if outcome == "timeout":              # the engine records its own outcomes (#1492); a killed child never reached it
                state.record_outcome(sdlc_dir, unit, int(time.time()), "failed")
    return {"outcome": outcome, "unit_tip": tip}


SWEEP_NOTES_MAX, SWEEP_NOTE_CHARS, SWEEP_UNREGISTERED_MAX = 5, 200, 50      # PROVISIONAL bounds on the record's size (#1493)
SWEEP_UNIT = "window"          # the record's unit key for a sweep (it is no unit's outcome)


def _engine_argv(sdlc_dir, root, unit, run_id, cap):
    return [sys.executable, "-I", str(pathlib.Path(__file__).resolve()), "--engine", "--sdlc", str(sdlc_dir),
            "--root", str(root), "--unit", unit, "--run-id", str(run_id), "--cap", str(cap)]


@feature_upkeep.gated("machine")
def run_sweep(config, sdlc_dir, root, run_id, cap, *, environ=None, command=None, clock=time.monotonic, beat=None,
              list_units=None, now=None, prune=None, prune_run=None):
    """#1387: bring EVERY registry unit (include and exclude lists applied) up to the base in one bounded run. Takes the same
    exclusive lock as `run_job`: when it is held the answer is {"outcome": "busy"}, recorded in the window stamp as a refusal,
    never as a failure. Each unit goes, in turn, through the existing per-unit engine child (lease, backup ref, direct-commit
    refusal, conflicts), which reports `current` for a unit already at the base, so a unit another machine's sweep brought
    current costs one bounded read and nothing else. A refused, parked or failed unit does not stop the others. `cap` is the
    TOTAL wall-clock for the sweep; a unit the cap or a stop file prevented is listed as unreached, in the job record and in
    the window stamp. -> {"outcome": "swept"|"busy"|"refused", "sweep": {...}}."""
    bounded, state, window = _sibling("bounded_run"), _sibling("feature_upkeep_state"), _sibling("feature_upkeep_window")
    if bounded.group_refusal() is not None:
        return {"outcome": "refused"}
    try:
        import fcntl
    except ImportError:
        return {"outcome": "refused"}
    stamp_now = lambda: int(time.time() if now is None else now)           # noqa: E731
    lock_path = sched.path_of(sdlc_dir, sched.LOCK_REL)
    _sibling("state").refuse_symlinks(sdlc_dir, sched.LOCK_REL, True)
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            prior = window.read(sdlc_dir) or {}
            window.stamp_attempt(sdlc_dir, stamp_now(), "busy", prior.get("unreached") or [])
            return {"outcome": "busy"}
        head = {"schema": sched.RECORD_SCHEMA, "unit": SWEEP_UNIT, "run_id": run_id, "noted": False, "sweep_job": True}
        _record(sdlc_dir, dict(head, state="running", started_at=stamp_now()))
        beat_path = sched.path_of(sdlc_dir, sched.BEAT_REL)
        _touch(beat_path)
        counts, unreached, outcome = {}, [], "failed"
        counter_failed = False
        seen = {"units_listed": 0, "unregistered": [], "notes": []}        # #1493: what the sweep could not see
        try:
            reading = feature_upkeep.read(config)
            agent = feature_upkeep.conflict_level(config) == "agent"
            if agent:                                             # #1516: count the units that reach the Level 2 resolver in this sweep
                try:
                    _sibling("feature_upkeep_level2").sweep_begin(sdlc_dir, run_id)
                except (OSError, ValueError):                     # no counter, no agent level: an absent counter would read as "no sweep"
                    counter_failed = True
            if list_units:
                listed = list(list_units(sdlc_dir))
            else:
                listed, seen["notes"] = sched.list_units_notes(sdlc_dir)
                seen["notes"] = [n[:SWEEP_NOTE_CHARS] for n in seen["notes"][:SWEEP_NOTES_MAX]]
            seen["units_listed"] = len(listed)
            seen["unregistered"] = sched.unregistered_heads(sdlc_dir, _sibling("feature_rebase")._remote(config), root,
                                                            registered=listed)[:SWEEP_UNREGISTERED_MAX]
            names = sched.selected(listed, reading.settings["units.include"], reading.settings["units.exclude"])
            clk = beat or BeatingClock(lambda: _touch(beat_path), base=clock)
            started = clk()
            stops = (sched.path_of(sdlc_dir, sched.STOP_REL), sched.path_of(sdlc_dir, sched.WATCH_STOP_REL))
            env = bounded.unattended_env(sched.allowed_env(os.environ if environ is None else environ,
                                                           (sched.CALL_TIMEOUT_ENV, feature_upkeep.ENV_MACHINE)))
            if agent:                                         # #1536: the children know their sweep, so a lost counter fails closed
                env[sched.SWEEP_RUN_ENV] = str(run_id)[:80]
            if counter_failed:
                unreached = list(names)
            for index, name in enumerate([] if counter_failed else names):
                remaining = cap - (clk() - started)
                if remaining <= 0 or any(os.path.exists(p) for p in stops):
                    unreached = names[index:]
                    break
                argv = command(name) if command else _engine_argv(sdlc_dir, root, name, run_id, cap)
                result = bounded.run_group(argv, root, remaining, env=env, stop_path=EitherStop(*stops), merge=False, clock=clk)
                word, tip = _outcome_of(result, bounded)
                counts[word] = counts.get(word, 0) + 1
                if word == "timeout":             # the engine records its own outcomes (#1492); a killed child never reached it
                    state.record_outcome(sdlc_dir, name, int(time.time()), "failed")
                if word in ("timeout", "stopped"):                 # the cap or a stop file cut this child short: it and everything after it are left for the next trigger
                    unreached = names[index:]
                    break
            outcome = "failed" if counter_failed else "swept"
            if not counter_failed and not any(os.path.exists(p) for p in stops):
                if cap - (clk() - started) <= 0:      # #1632: the cap is spent, so the prune (which has no cap of its own) waits for the next sweep
                    seen["prune"] = {"outcome": "skipped", "deleted": 0, "listed": 0, "at": stamp_now()}
                else:
                    seen["prune"] = _prune_pass(config, sdlc_dir, root, stamp_now(), prune, prune_run, beat_path)      # #1557
        finally:
            if feature_upkeep.conflict_level(config) == "agent":
                try:
                    _sibling("feature_upkeep_level2").sweep_end(sdlc_dir)
                except Exception:                                 # noqa: BLE001 - a counter that cannot close is bounded by the next begin
                    pass
            summary = dict(seen, counts=counts, unreached=unreached)
            _record(sdlc_dir, dict(head, state="done", outcome=outcome, unit_tip=None, finished_at=stamp_now(), sweep=summary))
            done_units = sum(n for w, n in counts.items() if w not in ("timeout", "stopped"))
            every_failed = bool(counts) and set(counts) == {"failed"}       # nothing worked: an attempt, retried after retry_minutes
            if outcome == "swept" and (done_units or not unreached) and not every_failed:      # a sweep cut off before any unit finished is an attempt
                window.stamp(sdlc_dir, stamp_now(), outcome, unreached)
            else:
                window.stamp_attempt(sdlc_dir, stamp_now(), outcome, unreached)
    return {"outcome": outcome, "sweep": summary}


SKIPPED_AFTER = "rebased"     # the older pass's outcome word for "I moved the unit": the new unit pass then stays out of the run


def _unit_pass(config, sdlc_dir, root, unit, unit_pass=None):
    """The pass entry point (`run_unit_pass`) for the unit, against the remote-tracking refs of its remote. -> {"unit_pass":
    the pass's result word}, or {} when no base is configured. Never raises: a failure reads `failed`, the line is still printed."""
    try:
        rebase = _sibling("feature_rebase")
        remote = rebase._remote(config)
        base = rebase.base_of(config)
        branch = rebase.features.BRANCH_PREFIX + unit
        if not base:
            return {}
        call = unit_pass or _sibling("feature_upkeep_pass").run_unit_pass
        got = call(config, str(sdlc_dir), unit, "refs/remotes/%s/%s" % (remote, branch), "refs/remotes/%s/%s" % (remote, base),
                   base, int(time.time()), remote=remote, branch=branch, cwd=str(root))
        got = got if isinstance(got, dict) else {}
        return dict({"unit_pass": got.get("result") or "failed"}, **({"lease": True} if got.get("reason") == "lease-refused" else {}))
    except Exception:                                     # noqa: BLE001 - maintenance never costs the line
        return {"unit_pass": "failed"}


# #1330: the only outcomes after which the unit pass may run; every other word (a failure, or an engine that refused or did not look:
# disabled, busy, remote-unreadable, no-base, no-unit, not-adopted) leaves the unit alone, since the pass force-pushes with no backup.
UNIT_PASS_OUTCOMES = frozenset({"current", "no-branch"})


@feature_upkeep.gated("machine")
def run_engine(config, sdlc_dir, root, unit, *, environ=None, upkeep=None, unit_pass=None):
    """The pass itself, inside the child: the engine's own entry point, called in-process, handed NO goal. Prints one JSON
    line (`outcome`, `unit_tip`) as its last line of output. -> the report."""
    rebase = _sibling("feature_rebase")
    if rebase.is_unit_base(rebase.base_of(config), rebase._remote(config), config):
        line = {"outcome": rebase.NO_BASE, "unit_tip": None, "unit_pass": "skipped", "reason": rebase.UNIT_BASE_WHY}   # #1311
        print(json.dumps(line, sort_keys=True), flush=True)
        return line
    upkeep = upkeep or rebase.upkeep
    report = upkeep(sdlc_dir, config, None, unit, cwd=str(root))
    report = report if isinstance(report, dict) else {}
    tip = report.get("tip") or report.get("after") or report.get("unit_tip")
    line = {"outcome": report.get("outcome") or "failed", "unit_tip": tip if isinstance(tip, str) else None}
    if line["outcome"] == SKIPPED_AFTER:                  # the older pass already rewrote this unit in this run: ONE path owns it
        line["unit_pass"] = "skipped"
    elif line["outcome"] not in UNIT_PASS_OUTCOMES:       # #1330: only an engine that looked and found nothing to do hands over to the unit pass
        line["unit_pass"] = "skipped"
    else:
        line.update(_unit_pass(config, sdlc_dir, root, unit, unit_pass))
        if line.pop("lease", False):                      # #1344: a correct refusal is recorded as one, not as a failure
            line["outcome"] = "lease-refused"
    print(json.dumps(line, sort_keys=True), flush=True)
    return line


def build_parser():
    parser = argparse.ArgumentParser(prog="feature_upkeep_job.py", description="The detached, bounded upkeep job (started by "
                                     "the scheduler; not meant to be run by hand).")
    parser.add_argument("--sdlc", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--unit", default="")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--cap", type=float, required=True)
    parser.add_argument("--engine", action="store_true")
    return parser


def _windows():
    return sys.platform == "win32"


def main(argv=None):
    args = build_parser().parse_args(argv)
    if _windows():                                        # #1344: the scheduler and the doctor refuse; so does a direct run
        print("feature_upkeep_job.py: refused: background upkeep is not supported on Windows", file=sys.stderr)
        return 2
    try:
        config = json.loads(pathlib.Path(args.sdlc).joinpath("config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0                                          # no readable config: the gate would be closed
    if args.sweep:
        run_sweep(config, args.sdlc, args.root, args.run_id, args.cap)
    elif not args.unit:
        build_parser().error("--unit is required")
    elif args.engine:
        run_engine(config, args.sdlc, args.root, args.unit)
    else:
        run_job(config, args.sdlc, args.root, args.unit, args.run_id, args.cap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
