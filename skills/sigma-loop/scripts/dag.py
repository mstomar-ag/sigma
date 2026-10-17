#!/usr/bin/env python3
"""dag.py -- the dependency map's command line (#1610). One verb so far.

  dag.py show [--sdlc DIR]    mode, age and counts of the stored map

With `discovery.dag.mode` off, `show` prints `dag: off` and touches nothing. `show` only reads: it
never creates the state directory, never takes the refresh lock and never calls `gh`.
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dag.py", description="The dependency map's command line.")
    sub = ap.add_subparsers(dest="verb", required=True)
    sp = sub.add_parser("show", help="print mode, age and counts of the stored map")
    sp.add_argument("--sdlc", default=None, help="the .sdlc directory (default: ./.sdlc)")
    args = ap.parse_args(argv)
    sdlc = args.sdlc or os.path.join(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(), ".sdlc")
    print(show(sdlc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
