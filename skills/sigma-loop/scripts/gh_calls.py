"""GitHub call counts by component (#2019): which part of Sigma spends the GitHub calls.

Counting is at the runners that actually spawn `gh` (`sources._run_gh`, `gh_api._default_run`, `gh_api.run_gh`
and `gh_api.bounded_runner`), never at `GitHubSource._raw`: that is the injected-runner seam, so a call made with
`run=None` (the auto-unpark sweep) would be invisible there. A call is counted when it is made, failures included.

What is stored is a COUNT per (hour, component, bucket) and nothing else: no arguments, titles, bodies or
numbers. Buckets are `rest`, `graphql` and `search`. The component is the innermost `with component("name")`,
else `SIGMA_GH_COMPONENT`, else the running script's own name.

Counts are held in memory and appended, one JSON line per (hour, component, bucket), to
`<sdlc>/state/gh-calls.jsonl` when the hour changes and at exit. The sdlc dir is `SIGMA_SDLC_DIR`, else `./.sdlc`
if it exists, else nothing is written. The file rotates at 1 MB to `gh-calls.jsonl.1` (one generation). Every
failure here is swallowed: counting must never break a call. This module never makes a GitHub call.
Each by-path-loaded copy of this module holds its own counts and writes its own lines; the report sums lines.
"""
import atexit
import contextlib
import json
import os
import re
import sys
import time

ENV_COMPONENT = "SIGMA_GH_COMPONENT"
ENV_DIR = "SIGMA_SDLC_DIR"
FILE = "gh-calls.jsonl"
ROTATE_BYTES = 1024 * 1024
BUCKETS = ("rest", "graphql", "search")

_clock = time.time            # a test swaps this for a fake clock
_counts = {}                  # (hour_start, component, bucket) -> n
# innermost-last component names. Shared through `sys` because loop.py, sources.py and gh_api.py each load their
# own copy of this module by path: a `with component(...)` opened in one must be seen by a call counted in another.
_stack = sys.__dict__.setdefault("_sigma_gh_component_stack", [])
_registered = False


def classify(args):
    """Bucket of one `gh` argument vector: `search`, `graphql` or `rest`."""
    a = [str(x) for x in (args or [])]
    if not a:
        return "rest"
    if a[0] == "search" or (a[0] == "api" and len(a) > 1 and a[1].lstrip("/").startswith("search")):
        return "search"
    if a[0] == "api":
        return "graphql" if len(a) > 1 and a[1] == "graphql" else "rest"
    return "graphql" if a[0] in ("project", "issue", "pr") else "rest"


def _clean(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:40] or "unknown"


@contextlib.contextmanager
def component(name):
    """Attribute every call made inside the block to `name` (innermost wins)."""
    _stack.append(_clean(name))
    try:
        yield
    finally:
        _stack.pop()


def current_component():
    if _stack:
        return _stack[-1]
    env = os.environ.get(ENV_COMPONENT)
    if env:
        return _clean(env)
    base = os.path.basename(sys.argv[0] or "") if sys.argv else ""
    return _clean(base[:-3] if base.endswith(".py") else base)


def _sdlc_dir():
    d = os.environ.get(ENV_DIR)
    if d:
        return d
    return ".sdlc" if os.path.isdir(".sdlc") else None


def _hour(now):
    return int(now // 3600) * 3600


def count(args):
    """Count one `gh` call. Never raises, never calls GitHub."""
    global _registered
    try:
        hour = _hour(_clock())
        if any(k[0] != hour for k in _counts):
            flush(only_before=hour)
        key = (hour, current_component(), classify(args))
        _counts[key] = _counts.get(key, 0) + 1
        if not _registered:
            atexit.register(flush)
            _registered = True
    except Exception:           # noqa: BLE001 - counting must never break a call
        pass


def flush(only_before=None, sdlc_dir=None):
    """Append the held counts (those of hours before `only_before`, else all) and drop them. Never raises."""
    try:
        keys = [k for k in _counts if only_before is None or k[0] < only_before]
        if not keys:
            return
        lines = []
        for k in sorted(keys):
            lines.append(json.dumps({"hour": k[0], "component": k[1], "bucket": k[2], "n": _counts.pop(k)},
                                    sort_keys=True))
        base = sdlc_dir or _sdlc_dir()
        if not base:
            return
        d = os.path.join(str(base), "state")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, FILE)
        try:
            if os.path.getsize(path) >= ROTATE_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:           # noqa: BLE001
        pass


def report(sdlc_dir, hours=None, now=None):
    """Text: component x bucket totals from the recorded lines (and the one rotated generation), read-only.
    `hours` keeps only lines from the last N hours. Held in-memory counts of THIS process are not included."""
    now = _clock() if now is None else now
    totals = {}
    path = os.path.join(str(sdlc_dir), "state", FILE)
    for p in (path + ".1", path):
        try:
            with open(p, encoding="utf-8") as f:
                for raw in f:
                    try:
                        row = json.loads(raw)
                        hr, comp, bucket, n = int(row["hour"]), str(row["component"]), str(row["bucket"]), int(row["n"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    if hours is not None and hr < now - hours * 3600:
                        continue
                    t = totals.setdefault(comp, dict.fromkeys(BUCKETS, 0))
                    t[bucket] = t.get(bucket, 0) + n
        except OSError:
            continue
    if not totals:
        return "gh calls: not measured (no recorded calls)\n"
    out = ["%-24s %8s %8s %8s %8s" % ("component", "rest", "graphql", "search", "total")]
    for comp in sorted(totals, key=lambda c: -sum(totals[c].values())):
        t = totals[comp]
        out.append("%-24s %8d %8d %8d %8d" % (comp, t["rest"], t["graphql"], t["search"], sum(t.values())))
    return "\n".join(out) + "\n"


USAGE = "usage: gh_calls.py report <sdlc_dir> [--hours N]"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "report":
        hours = None
        if "--hours" in argv:
            try:
                hours = float(argv[argv.index("--hours") + 1])
            except (IndexError, ValueError):
                print(USAGE, file=sys.stderr)
                return 2
        sys.stdout.write(report(argv[2], hours))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
