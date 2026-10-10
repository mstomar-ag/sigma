"""Bounded counter and depth stamp for AI-filed issues (decision rubric, slice 15, #1003). Library only.

`create_tracked_issue` asks `over_cap` before it files, and `bump`s after it has. The file lives under the
state directory (`ai_filed.counter_path`), holds `{"entries": [{"run", "goal", "t"}]}`, and is bounded two
ways: entries older than `ai_filed.counter_retention_days` are dropped on every bump, and never more than
HARD_MAX are kept (newest win). A missing file is an empty counter; an unreadable one is `None`, which
`over_cap` reads as OVER (fails closed: the filing is parked, visibly). `bump` never overwrites an unreadable
file, so the lever is to delete or fix it. Writes are an atomic replace, not a lock: two writers racing can
lose one entry, so the cap may be exceeded by the number of concurrent writers, never by more.

Depth is carried in the filed body as a last line `sigma-depth: N`; a goal without one is depth 0.
Config (key `ai_filed`, every value PROVISIONAL and overridable; a wrong type reads as the default):
max_per_goal 5, max_per_run 20, max_depth 3, on_cap "park", counter_path "state/ai_filed_counter.json",
counter_retention_days 14. Stdlib only.
"""
import json
import os
import pathlib
import re

HARD_MAX = 2000
DEFAULTS = {"max_per_goal": 5, "max_per_run": 20, "max_depth": 3, "on_cap": "park",
            "counter_path": "state/ai_filed_counter.json", "counter_retention_days": 14}
DEPTH_PREFIX = "sigma-depth: "
_DEPTH = re.compile(r"^sigma-depth: (\d{1,3})\s*$", re.MULTILINE)


def limits(config):
    """-> the effective settings; each wrong-typed or out-of-range value reads as its default."""
    out = dict(DEFAULTS)
    try:
        blk = config.get("ai_filed") if isinstance(config, dict) else None
        blk = blk if isinstance(blk, dict) else {}
        for k in ("max_per_goal", "max_per_run", "max_depth", "counter_retention_days"):
            v = blk.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v >= (0 if k == "max_depth" else 1):
                out[k] = v
        if blk.get("on_cap") == "park":
            out["on_cap"] = "park"
        p = blk.get("counter_path")
        if isinstance(p, str) and p.strip() and not os.path.isabs(p) and ".." not in pathlib.PurePath(p).parts:
            out["counter_path"] = p
    except Exception:
        return dict(DEFAULTS)
    return out


def load(path):
    """-> {"entries": [...]}; an absent file is empty; an unreadable or mis-shaped one is None."""
    p = pathlib.Path(str(path))
    if not p.exists():
        return {"entries": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ents = data["entries"]
        if not isinstance(data, dict) or not isinstance(ents, list) or not all(
                isinstance(e, dict) and isinstance(e.get("t"), (int, float)) for e in ents):
            return None
        return {"entries": ents}
    except Exception:
        return None


def over_cap(counter, goal, run, config):
    """True when one more filing would pass the per-goal or per-run cap, or the counter is unreadable."""
    if counter is None:
        return True
    lim = limits(config)
    ents = counter.get("entries", [])
    if sum(1 for e in ents if e.get("goal") == str(goal)) >= lim["max_per_goal"]:
        return True
    return run is not None and sum(1 for e in ents if e.get("run") == run) >= lim["max_per_run"]


def bump(path, run, goal, now, config):
    """Record one filing and prune. -> True when written, False when the file was unreadable or unwritable."""
    try:
        cur = load(path)
        if cur is None:
            return False
        lim = limits(config)
        floor = now - lim["counter_retention_days"] * 86400
        ents = [e for e in cur["entries"] if e["t"] >= floor]
        ents.append({"run": run, "goal": str(goal), "t": now})
        ents = ents[-HARD_MAX:]
        p = pathlib.Path(str(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".%d.tmp" % os.getpid())
        tmp.write_text(json.dumps({"entries": ents}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def ancestry_depth(goal, source=None):
    """The depth stamped on `goal`'s own body (0 when none, or when it cannot be read). Never raises."""
    try:
        body = source.fetch_title_body(goal)[1]
        m = _DEPTH.findall(body or "")
        return int(m[-1]) if m else 0
    except Exception:
        return 0


def stamp(body, depth):
    return (body or "").rstrip("\n") + "\n\n" + DEPTH_PREFIX + str(int(depth))
