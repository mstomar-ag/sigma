"""Session-start door of upkeep (#1224, #1387). An ACCELERATOR: the watcher is the mechanism, this covers a session that starts first.

    upkeep_session.py lines <sdlc_dir>    # prints ONE line (last sweep age, next window), only while upkeep.enabled; reads one file
    upkeep_session.py start <sdlc_dir>    # window open + machine opt-in: starts ONE detached sweep and returns at once; prints nothing

`lines` reads the window stamp and writes nothing. `start` goes through `feature_upkeep_sched.scheduler_tick` (so the same gates:
`upkeep.enabled`, SIGMA_UPKEEP_JOB and the ledger switch); the sweep runs detached with its stdio on the null device, so the
session never waits for it. Any failure prints nothing and exits 0, so it can never block a session. Callable directly on a
host without hooks; the Claude SessionStart hook only runs this script.
"""
import importlib.util
import json
import os
import pathlib
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent
USAGE = "usage: upkeep_session.py lines|start <sdlc_dir>"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(sdlc_dir):
    config = json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text())
    return config if isinstance(config, dict) else None


def session_lines(sdlc_dir, environ=None, now=None, deps=None):
    """-> [one line] when upkeep is enabled, else [] (also on anything at all failing)."""
    try:
        config = _config(sdlc_dir)
        feature_upkeep = _load("feature_upkeep")
        reading = feature_upkeep.read(config)
        if not reading.enabled:
            return []
        window = _load("feature_upkeep_window")
        return [window.describe(reading.settings, sdlc_dir, int(time.time() if now is None else now))]
    except Exception:            # noqa: BLE001 - a report must never break its caller
        return []


def start(sdlc_dir, environ=None, now=None, deps=None):
    """Start the sweep when the window is open and the machine door is open. -> the tick's decision dict, or None. Never raises."""
    try:
        config = _config(sdlc_dir)
        sched = _load("feature_upkeep_sched")
        got = sched.scheduler_tick(config, sdlc_dir, environ=environ, now=now, deps=deps, started_by="trigger")
        return got
    except Exception:            # noqa: BLE001
        return None


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE + "\n\n" + __doc__.strip())
        return 0
    if len(argv) != 3 or argv[1] not in ("lines", "start"):
        print(USAGE, file=sys.stderr)
        return 2
    if argv[1] == "start":
        start(argv[2])
        return 0
    lines = session_lines(argv[2])
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
