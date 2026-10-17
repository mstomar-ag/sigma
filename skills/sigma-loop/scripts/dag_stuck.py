"""Root-cause lines for stuck chains (#1614): which person-decision holds how much work.

Reads the stored map through `dag_chain.stuck` (a local file, no network, nothing written) and renders
one line per decision root, most freeing first (the open goals that wait on it, descending, ties by
number), in the shapes the pick, `status` and the doctor share:

  #X, #Y, #Z wait on #R's decision; /sigma-unpark #R      (parked)
  #X, #Y wait on #R's decision; /sigma-promote #R         (unarmed or unmanaged)
  #X, #Y wait on #R's decision; PR #R is open             (a pull-request root)
  #a, #b wait on each other                               (a cycle)

More than `SHOWN` numbers print the first five then `and N more`. A chain is stuck only when a leaf of
its open-blocker subgraph needs a person; a chain whose leaves are all ordinary open goals is not (the
loop picks the leaf). With `discovery.dag.mode` off, or a map that is missing, stale or unreadable,
every function answers "nothing" (`[]` / `None`) and the caller prints exactly what it printed before.

Scale: one map read plus linear work in nodes and edges per call.
"""
import importlib.util
import pathlib
import re
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


dag_chain = _load("dag_chain")

SHOWN = 5


def roots(sdlc, now=None):
    """The `dag_chain.stuck` entries, or `[]` when mode is off or the map is unusable. Never raises."""
    try:
        return dag_chain.stuck(sdlc, now=now)
    except Exception:                                   # noqa: BLE001 - no answer is not an error here
        return []


def _numbers(nums):
    shown = ", ".join("#%d" % n for n in nums[:SHOWN])
    return shown + (" and %d more" % (len(nums) - SHOWN) if len(nums) > SHOWN else "")


def _action(entry):
    r = entry["root"]
    if entry.get("kind") == "pr":
        return "PR #%d is open" % r
    if entry["why"] == "parked":
        return "/sigma-unpark #%d" % r
    if entry["why"] in ("unarmed", "unmanaged"):
        return "/sigma-promote #%d" % r
    return entry["hint"]


def line(entry):
    """The one line for one `stuck` entry."""
    if entry["why"] == "cycle":
        return "%s wait on each other" % _numbers(entry.get("cycle") or [entry["root"]])
    return "%s wait on #%d's decision; %s" % (_numbers(entry["waiting"]), entry["root"], _action(entry))


def lines(sdlc, now=None):
    """Every root-cause line, most freeing first; `[]` when there is nothing to say."""
    return [line(e) for e in roots(sdlc, now)]


def pick_lines(sdlc, held, now=None):
    """The root-cause lines for a pick that found nothing pickable, or `None`. `held` is the held
    subjects (`"#12"`). `None` unless EVERY held goal is a number the map shows waiting on a decision
    root or cycle: one plain open blocker, a local goal or a goal the map does not know keeps today's text."""
    try:
        nums = [int(m.group(1)) for m in (re.fullmatch(r"#(\d+)", str(h)) for h in held)]
    except (AttributeError, ValueError):
        return None
    if not nums:
        return None
    found = roots(sdlc, now)
    if not found:
        return None
    waiting = set()
    for e in found:
        waiting |= set(e["waiting"])
        if e["why"] == "cycle":
            waiting |= set(e.get("cycle") or [])
    if not set(nums) <= waiting:
        return None
    return [line(e) for e in found if set(e["waiting"]) & set(nums)
            or e["why"] == "cycle" and set(e.get("cycle") or []) & set(nums)]
