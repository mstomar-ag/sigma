"""Pick trace (#1607): where a pick's runner calls and seconds go, measured in-process.

OFF by default. Enabled by `SIGMA_PICK_TRACE=1` or `loop.py next --trace`. When off, `loop._next`
gets the `NULL` recorder (every method a no-op), the source's counter hook stays `None`, nothing is
written, and no runner call is added: the trace only ever observes calls the pick already makes.

Counting is at ONE seam: `sources.GitHubSource._raw` is the only place that hands arguments to the
runner, and `_run` (hence every `gh_api` helper, which is given `self._run`) goes through it, so a
call is counted exactly once, retries included (each retry is a runner call). The hook is an
attribute on the source rather than module state because `loop.py` and `sources.py` each load their
own copy of sibling modules by path; an explicit hook is the only thing both copies share.

On: each pick appends ONE JSON line to `.sdlc/state/pick-trace.jsonl` holding, per named stage, the
call counts by class and the seconds. No issue text, titles, bodies or arguments are recorded, only
class names and numbers. The file rotates at 1 MB to `pick-trace.jsonl.1` (one generation, replaced
each time), so the ceiling is about 2 MB. A write failure is swallowed: the trace must never break a pick.

`report(sdlc_dir)` prints per-stage median and p90 of calls and seconds across the recorded picks.
Stages a pick did not enter count as zero for that pick.
"""
import json
import math
import os
import statistics
import time

ENV_VAR = "SIGMA_PICK_TRACE"
TRACE_FILE = "pick-trace.jsonl"
ROTATE_BYTES = 1024 * 1024
CLASSES = ("list", "read", "write", "graphql", "project")
STAGES = ("reconcile", "auto_unpark", "reclaim", "release_unstarted", "pending_fetch",
          "dependency_hold", "gates", "claim_writes", "other")

_clock = time.monotonic          # a test swaps this for a fake clock


def enabled(env, forced=False):
    """True for `--trace` (`forced`) or `SIGMA_PICK_TRACE=1`; anything else is off."""
    return bool(forced) or str((env or {}).get(ENV_VAR, "")).strip() == "1"


def classify(args):
    """Class of one `gh` argument vector: list, read, write, graphql or project."""
    a = [str(x) for x in (args or [])]
    if not a:
        return "read"
    if a[0] == "project":
        return "project"
    if a[0] == "api":
        if len(a) > 1 and a[1] == "graphql":
            return "graphql"
        method = "GET"
        for i, tok in enumerate(a):
            if tok in ("--method", "-X") and i + 1 < len(a):
                method = a[i + 1].upper()
            elif tok.startswith("--method="):
                method = tok.split("=", 1)[1].upper()
        if method != "GET":
            return "write"
        path = a[1] if len(a) > 1 else ""
        base = path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if "--paginate" in a or "per_page" in path or base in (
                "issues", "pulls", "comments", "labels", "events", "reviews", "assignees"):
            return "list"
        return "read"
    verb = a[1] if len(a) > 1 else ""
    if verb == "list":
        return "list"
    if verb in ("view", "status", "diff", "checks", "get"):
        return "read"
    return "write"


class _Null:
    """The off recorder: every call a no-op, so the hot path pays one attribute lookup."""
    active = False

    def mark(self, name): pass
    def count_args(self, args): pass
    def end(self): pass


NULL = _Null()


class Recorder:
    active = True

    def __init__(self, sdlc_dir):
        self.sdlc_dir = sdlc_dir
        self.stages = {}
        self.current = "other"
        self.t0 = self.t_mark = _clock()

    def _bucket(self, name):
        return self.stages.setdefault(name, {"seconds": 0.0, **{c: 0 for c in CLASSES}})

    def mark(self, name):
        """Charge the time since the last mark to the stage that was current, then switch."""
        now = _clock()
        self._bucket(self.current)["seconds"] += now - self.t_mark
        self.t_mark = now
        self.current = name

    def count_args(self, args):
        self._bucket(self.current)[classify(args)] += 1

    def end(self):
        self.mark(self.current)
        wall = self.t_mark - self.t0
        stages = {}
        for name, b in self.stages.items():
            calls = {c: b[c] for c in CLASSES if b[c]}
            if calls or b["seconds"] > 0:
                stages[name] = {"calls": calls, "seconds": round(b["seconds"], 6)}
        line = json.dumps({"v": 1, "ts": int(time.time()), "wall": round(wall, 6), "stages": stages},
                          sort_keys=True)
        _append(self.sdlc_dir, line)


def begin(sdlc_dir, env=None, forced=False):
    return Recorder(sdlc_dir) if enabled(os.environ if env is None else env, forced) else NULL


def _append(sdlc_dir, line):
    try:
        d = os.path.join(str(sdlc_dir), "state")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, TRACE_FILE)
        try:
            if os.path.getsize(path) >= ROTATE_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _p90(values):
    s = sorted(values)
    return s[max(0, math.ceil(0.9 * len(s)) - 1)]


def report(sdlc_dir):
    """Text report: per-stage median and p90 of calls and seconds over the recorded picks."""
    path = os.path.join(str(sdlc_dir), "state", TRACE_FILE)
    picks = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("stages"), dict):
                    picks.append(row)
    except OSError:
        pass
    if not picks:
        return "pick trace: no picks recorded (set SIGMA_PICK_TRACE=1 or pass --trace to `next`)\n"
    seen = {s for p in picks for s in p["stages"]}
    names = [s for s in STAGES if s in seen] + sorted(seen - set(STAGES))
    out = ["pick trace: %d pick(s)" % len(picks),
           "%-18s %10s %10s %10s %10s" % ("stage", "calls_p50", "calls_p90", "secs_p50", "secs_p90")]
    for name in names:
        calls, secs = [], []
        for p in picks:
            st = p["stages"].get(name) or {}
            calls.append(sum(int(v) for v in (st.get("calls") or {}).values()))
            secs.append(float(st.get("seconds") or 0.0))
        out.append("%-18s %10.1f %10.1f %10.3f %10.3f" % (
            name, statistics.median(calls), _p90(calls), statistics.median(secs), _p90(secs)))
    walls = [float(p.get("wall") or 0.0) for p in picks]
    out.append("%-18s %10s %10s %10.3f %10.3f" % ("wall", "-", "-", statistics.median(walls), _p90(walls)))
    return "\n".join(out) + "\n"
