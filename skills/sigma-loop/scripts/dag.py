#!/usr/bin/env python3
"""dag.py -- the dependency map's command line (#1610, #1611).

  dag.py show [--sdlc DIR]      mode, age and counts of the stored map
  dag.py rebuild [--sdlc DIR]   read GitHub once, publish the map, print the call counts by class

With `discovery.dag.mode` off, both verbs print `dag: off` and touch nothing. `rebuild` runs
`dag_collect.refresh` (under the refresh lock; a failed or empty read leaves the old file as it was)
and exits 1 when nothing was published. `show` only reads: it never creates the state directory, never takes the refresh lock and never calls `gh`.
"""
import argparse
import importlib.util
import os
import pathlib
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _age(seconds):
    s = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def show(sdlc):
    ds = _load("dag_store")
    try:
        config = ds.state.load_config(sdlc)
    except Exception:
        config = {}
    md = ds.mode(config)
    if md == "off":
        return "dag: off"
    m, why = ds.load(sdlc, config)
    if m is None:
        return f"dag: mode {md}, {why}"
    t = ds.stamp(m)
    age = _age(time.time() - t) if t is not None else "unknown"
    ok, reason = ds.usable(m, time.time(), ds.ttl_minutes(config))
    return (f"dag: mode {md}, age {age}, nodes {len(m['nodes'])}, edges {len(m['edges'])}, "
            f"{'usable' if ok else 'not usable (' + reason + ')'}")


def rebuild(sdlc):
    """`(line, exit_code)`: the outcome and the call counts by class (list, read, pull)."""
    dc = _load("dag_collect")
    rep = dc.refresh(sdlc)
    if rep["status"] == "off":
        return "dag: off", 0
    c = rep["calls"]
    line = (f"dag rebuild: {rep['status']}, calls list {c['list']} read {c['read']} pull {c['pull']}, "
            f"unread {len(rep['unread'])}")
    if rep["partial"]:
        line += ", partial"
    if rep["last_error"]:
        line += f", last_error {rep['last_error']}"
    return line, (0 if rep["status"] in ("saved", "busy") else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dag.py", description="The dependency map's command line.")
    sub = ap.add_subparsers(dest="verb", required=True)
    sp = sub.add_parser("show", help="print mode, age and counts of the stored map")
    sp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    rp = sub.add_parser("rebuild", help="read GitHub and publish the map; print call counts by class")
    rp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    args = ap.parse_args(argv)
    sdlc = args.sdlc or os.path.join(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(), ".sdlc")
    if args.verb == "rebuild":
        line, code = rebuild(sdlc)
        print(line)
        return code
    print(show(sdlc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
