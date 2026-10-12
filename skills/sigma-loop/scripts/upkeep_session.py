"""Session-start report of units upkeep will rebase (#1224). An ACCELERATOR that only reports.

    upkeep_session.py lines <sdlc_dir>    # prints at most 3 lines, then a count, only while upkeep.enabled; else nothing

The mechanism stays the scheduler and the loop pick (`feature_upkeep_sched`). This reads git state through
`feature_upkeep_sched.due_units` and writes nothing. Any failure prints nothing and exits 0, so it can never block a session.
Callable directly on a host without hooks; the Claude SessionStart hook only runs this script.
"""
import importlib.util
import json
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
MAX_LINES = 3
DEFAULT_CALL_TIMEOUT = 120      # SIGMA_WATCH_CALL_TIMEOUT's own default; the budget is a quarter of it, as the tick's


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _budget(environ):
    try:
        raw = _load("legacy").getenv("SIGMA_WATCH_CALL_TIMEOUT", environ=environ)
        return int(raw or DEFAULT_CALL_TIMEOUT) // 4 or 1
    except ValueError:
        return DEFAULT_CALL_TIMEOUT // 4


def session_lines(sdlc_dir, environ=None, now=None, deps=None):
    """-> list of lines (empty when the gate is closed, nothing is due, or anything at all fails)."""
    try:
        environ = os.environ if environ is None else environ
        config = json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text())
        if not isinstance(config, dict):
            return []
        sched = _load("feature_upkeep_sched")
        got = sched.due_units(config, sdlc_dir, now=now, budget=_budget(environ), deps=deps)
        due = got["due"]
        lines = ["unit %s is %d merges behind %s; upkeep will rebase it" % (d["unit"], d["behind"], d["base"] or "its base")
                 for d in due[:MAX_LINES]]
        if lines:
            lines.append("%d unit%s due for upkeep." % (len(due), "" if len(due) == 1 else "s"))
        return lines
    except Exception:            # noqa: BLE001 - a report must never break its caller
        return []


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print("usage: upkeep_session.py lines <sdlc_dir>\n\n" + __doc__.strip())
        return 0
    if len(argv) != 3 or argv[1] != "lines":
        print("usage: upkeep_session.py lines <sdlc_dir>", file=sys.stderr)
        return 2
    lines = session_lines(argv[2])
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
