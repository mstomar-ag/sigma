"""The detached, bounded upkeep job: one unit, one pass, started by the scheduler (`feature_upkeep_sched`).

Usage: feature_upkeep_job.py --sdlc DIR --root DIR --unit NAME --run-id ID --cap SECONDS
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
        outcome, tip = "failed", None                     # what a crash inside this block leaves behind
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
        finally:
            _record(sdlc_dir, dict(head, state="done", outcome=outcome, unit_tip=tip, finished_at=int(time.time())))
            if outcome in state.OUTCOMES:
                state.record_outcome(sdlc_dir, unit, int(time.time()), outcome, tip if tip and len(tip) in (40, 64) else None)
            elif outcome == "timeout":
                state.record_outcome(sdlc_dir, unit, int(time.time()), "failed")
    return {"outcome": outcome, "unit_tip": tip}


@feature_upkeep.gated("machine")
def run_engine(config, sdlc_dir, root, unit, *, environ=None, upkeep=None):
    """The pass itself, inside the child: the engine's own entry point, called in-process, handed NO goal. Prints one JSON
    line (`outcome`, `unit_tip`) as its last line of output. -> the report."""
    upkeep = upkeep or _sibling("feature_rebase").upkeep
    report = upkeep(sdlc_dir, config, None, unit, cwd=str(root))
    report = report if isinstance(report, dict) else {}
    tip = report.get("tip") or report.get("after") or report.get("unit_tip")
    line = {"outcome": report.get("outcome") or "failed", "unit_tip": tip if isinstance(tip, str) else None}
    print(json.dumps(line, sort_keys=True), flush=True)
    return line


def build_parser():
    parser = argparse.ArgumentParser(prog="feature_upkeep_job.py", description="The detached, bounded upkeep job (started by "
                                     "the scheduler; not meant to be run by hand).")
    parser.add_argument("--sdlc", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--cap", type=float, required=True)
    parser.add_argument("--engine", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = json.loads(pathlib.Path(args.sdlc).joinpath("config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0                                          # no readable config: the gate would be closed
    if args.engine:
        run_engine(config, args.sdlc, args.root, args.unit)
    else:
        run_job(config, args.sdlc, args.root, args.unit, args.run_id, args.cap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
