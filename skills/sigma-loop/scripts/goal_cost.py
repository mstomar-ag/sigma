"""Cost per goal and per lane (#2019), read-only, from phase events the loop already records.

`phase_report.py end` appends one `phase` / `end` row to the goal's action log
(`<sdlc>/state/log/<goal>.jsonl`) whose `cost` field is the figure its banner printed (`$0.31`) or
`unavailable`. This module only READS those rows. It makes no model call, no GitHub call and writes nothing.

A goal with no priced phase row is `not measured`, never `$0.00`: an unavailable cost is an absence, not a zero.
A lane is the `lane:` frontmatter of `<sdlc>/goals/<goal>.md` when that file exists; otherwise `not recorded`.
Totals count only priced phases, and the phase counts say how many were priced of how many ended.
"""
import json
import os
import re
import sys

NOT_MEASURED = "not measured"
NO_LANE = "not recorded"
_COST = re.compile(r"^\$(\d+(?:\.\d+)?)$")
_LANE = re.compile(r"^lane:\s*(\w+)\s*$", re.M)


def _lane(sdlc_dir, goal):
    try:
        with open(os.path.join(str(sdlc_dir), "goals", goal + ".md"), encoding="utf-8") as f:
            m = _LANE.search(f.read(4096))
    except OSError:
        return NO_LANE
    return m.group(1).lower() if m else NO_LANE


def goal_costs(sdlc_dir):
    """-> {goal: {"lane", "priced", "ended", "usd"}}; `usd` is None when no phase was priced."""
    base = os.path.join(str(sdlc_dir), "state", "log")
    out = {}
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        goal = name[:-6]
        priced = ended = 0
        total = 0.0
        try:
            with open(os.path.join(base, name), encoding="utf-8", errors="replace") as f:
                for raw in f:
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(row, dict) or row.get("kind") != "phase" or row.get("state") != "end":
                        continue
                    ended += 1
                    m = _COST.match(str(row.get("cost", "")).strip())
                    if m:
                        priced += 1
                        total += float(m.group(1))
        except OSError:
            continue
        if ended:
            out[goal] = {"lane": _lane(sdlc_dir, goal), "priced": priced, "ended": ended,
                         "usd": total if priced else None}
    return out


def lane_costs(goals):
    """-> {lane: {"goals", "measured", "usd"}} from `goal_costs` output; `usd` None when no goal there is measured."""
    out = {}
    for info in goals.values():
        t = out.setdefault(info["lane"], {"goals": 0, "measured": 0, "usd": None})
        t["goals"] += 1
        if info["usd"] is not None:
            t["measured"] += 1
            t["usd"] = (t["usd"] or 0.0) + info["usd"]
    return out


def _usd(v):
    return NOT_MEASURED if v is None else "$%.2f" % v


def report(sdlc_dir):
    goals = goal_costs(sdlc_dir)
    if not goals:
        return "cost: %s (no phase end rows recorded)\n" % NOT_MEASURED
    out = ["%-28s %-12s %-14s %s" % ("goal", "lane", "cost", "phases priced")]
    for g in sorted(goals):
        i = goals[g]
        out.append("%-28s %-12s %-14s %d of %d" % (g, i["lane"], _usd(i["usd"]), i["priced"], i["ended"]))
    out.append("")
    out.append("%-12s %-14s %s" % ("lane", "cost", "goals measured"))
    for lane, t in sorted(lane_costs(goals).items()):
        out.append("%-12s %-14s %d of %d" % (lane, _usd(t["usd"]), t["measured"], t["goals"]))
    return "\n".join(out) + "\n"


USAGE = "usage: goal_cost.py report <sdlc_dir>"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "report":
        sys.stdout.write(report(argv[2]))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
