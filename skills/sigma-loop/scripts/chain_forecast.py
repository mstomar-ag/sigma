"""How far a chain run will get, from the rules `run` itself follows (#1907). Library only; no main.

ONE RULE, TWO CALLERS. Whether a member can be claimed, which of them one cycle may claim at once, and why
a cycle that claims nothing is stuck are decided here and nowhere else. `chain.run` calls `ready_members`
(its width plan) and `stuck_reason` (its stuck line); `forecast` calls the same two on a simulated run. A
second copy of either would let the plan promise what the run does not do, which is the failure this module
exists to end (a planner's own rules once said 26 goals where a reading of the issues said 4 to 6).

THE TABLE. `VERDICTS` maps a member's verdict to its marker, whether a run may claim it, and a short reason;
`OVERLAYS` does the same for flags a verdict does not carry. A new kind of member is one row. A verdict the
table does not know is NOT workable: the forecast never promises work it cannot name.

FORECAST. From the plan record alone (no `gh` call), as the person's answers leave it (`chain_asks.view`: a released
member is armed, a kept-parked one stops the run, a taken-over one is the runner's, a dismissed wait is no edge). The assignee filter is re-applied for the runner asked
about (`login`, default the one who planned), through `label_drain.assignee_gate`, the clause `_Judge.verdict`
uses. It then plays the run: each cycle claims what `ready_members` offers at the configured width, every
claimed goal is assumed to merge, until nothing is offered. What is left is the stop: its reason is
`stuck_reason` over the members left, and its roots are the left members that wait on no other left member.

Not modelled, and said so in the output: the cap (`chain.max_goals`, `max_usd`, `max_minutes`), a goal that
fails or is parked by its own session. A plan read from the map applies the assignee filter from the map's node
assignees; a member without them is read as not the runner's and counted in a note. Ceiling: O(cycles x members) per call, members bounded by `chain.max_members` (default 100).
"""
import importlib.util
import pathlib
import re

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


label_drain = _load("label_drain")
chain_parallel = _load("chain_parallel")
chain_checkpoint = _load("chain_checkpoint")
chain_asks = _load("chain_asks")

READY, AFTER = "ready now", "after"
MARKERS = (READY, AFTER, "needs decision", "someone else's", "checkpoint", "not workable")
#: verdict -> (marker, a run may claim it, why it does not). The one place a verdict is given a meaning.
VERDICTS = {
    "armed": (READY, True, ""),
    "parked": ("needs decision", False, "parked"),
    "unarmed": ("needs decision", False, "not armed"),
    "upper-tier": ("needs decision", False, "an upper-tier issue, plan its slices"),
    "assigned-elsewhere": ("someone else's", False, "assigned to someone else"),
    "unassigned": ("not workable", False, "assigned to nobody"),
    "held-by-gate": ("not workable", False, "held by a gate"),
    "checkpoint": ("checkpoint", False, "a checkpoint, not work"),                      # #1904
    "not-a-goal": ("needs decision", False, "blocks others but is not a goal"),         # #1903
    "awaiting-owner": ("needs decision", False, "waiting for its owner to confirm it"), # #1905
    # Not verdicts the label drain gives: the person's answers in the chain record turn a member into these.
    "kept-waiting": ("needs decision", False, "kept waiting on a closed prerequisite's open follow-up or question"),
    "declined": ("not workable", False, "a paid step you answered not to run"),
    chain_asks.KEPT_PARKED: ("needs decision", False, "parked; you answered to keep it parked"),
    "unknown": ("not workable", False, "could not be read"),
}
#: flag on a row -> (marker, why). Checked after the verdict, in this order, as `label_drain._reason` does. The
#: `blocked` flag is an overlay only while NOTHING in the chain explains it: a row that waits on members of the chain
#: carries the label because of those waits (the blocker sweep adds it and clears it when they close), so it is
#: classified by its waits, and a run that closes them frees it.
OVERLAYS = (
    ("in_progress", "someone else's", "already in progress"),
    ("blocked", "not workable", "carries the blocked label"),
)


def blockers_of(row, open_now):
    """-> the open in-chain members `row` waits on, then "outside" if it has an unresolved prerequisite."""
    return [w for w in row.get("waits_on") or [] if w in open_now] + (["outside"] if row.get("unresolved") else [])


def classify(row, open_now):
    """-> (marker, why, after) for one open member given the members still open. THE claim rule: a member is
    READY only when its verdict is workable, no overlay applies and it waits on nothing open."""
    marker, workable, why = VERDICTS.get(row.get("verdict"), ("not workable", False, "an unrecognised state"))
    if workable:
        for key, o_marker, o_why in OVERLAYS:
            if row.get(key):
                if key == "blocked" and row.get("waits_on"):
                    continue    # Sigma's own sweep sets it for a wait on an open goal and clears it when that closes
                marker, workable, why = o_marker, False, o_why
                break
    if not workable:
        return marker, why, []
    waits = blockers_of(row, open_now)
    chained = [w for w in waits if w != "outside"]
    if chained:
        return AFTER, "waits on %s" % ", ".join("#%d" % w for w in chained), chained
    if waits:
        return "not workable", "waits on an open prerequisite outside the chain", []
    return READY, "", []


def pickable(row, open_now):
    return classify(row, open_now)[0] == READY


def ready_members(rows, open_now, in_flight, width):
    """-> the numbers one cycle may claim, in the order given: pickable, not in flight, at most
    `width - len(in_flight)`. `rows` carry `verdict`, `waits_on`, `unresolved`, `in_progress`."""
    return chain_parallel.ready_set([r["number"] for r in rows if pickable(r, open_now)], {}, in_flight, width)


def checkpoint_detail(row):
    """The stop line for one checkpoint row: its number, its unit and the one step that releases it."""
    ck = row.get("checkpoint") or {}
    unit = ck.get("unit")
    if ck.get("state") == chain_checkpoint.MET:
        return ("#%d is a checkpoint whose unit %s has landed, but its issue is still open: close it to release "
                "what waits on it" % (row["number"], unit))
    if unit:
        return ("#%d is a checkpoint waiting for unit %s to land on the integration branch (%s), or for a "
                "person to close it" % (row["number"], unit, ck.get("state") or "unknown"))
    return "#%d is a checkpoint waiting for a person to close it" % row["number"]


def roots_of(rows, outside=()):
    """-> the rows no other open row of `rows` stands behind in the sense that they wait on nothing open: what
    HOLDS the chain, as opposed to what is held. A parked goal that itself waits on an open goal is held, and
    naming it sends a person to answer something that could not move anyway. `outside` members that were left out of
    the chain (not a goal, or waiting for its owner: `label_drain.GATED_KIND`) count as open: what waits only on one
    of them is held by it, and is not named as a root itself (`other_roots` names the left-out member). A
    checkpoint outside the chain does not: a member behind it that needs a person is still the thing to act on. Every
    row when each waits on another (a cycle), so a stop is never left without a name."""
    left_out = {r["number"] for r in outside if (r.get("own_verdict") or r.get("verdict")) in label_drain.GATED_KIND}
    open_now = {r["number"] for r in rows} | left_out
    free = [r for r in rows if not [w for w in r.get("waits_on") or [] if w in open_now and w != r["number"]]]
    if not free and any(w in left_out for r in rows for w in r.get("waits_on") or []):
        return []                       # everything left waits on a member nobody promoted: that one holds it
    return free or rows


def _stop_of_first_root(p, unconfirmed=(), held=(), declined=(), ctx=None):
    """-> (reason, detail), ONE reason from `chain.STUCK_REASONS` for an empty pick with members still open,
    given plan rows `p["members"]` (each may carry `waits_on`: the members it waits on) and `p["outside"]`. Only
    the ROOTS are named (`roots_of`). Numbers only in the detail. The order is the order a person would act in:
    what needs a person, then what only waits. A failed read is the caller's, ahead of
    this. `ctx` (`chain_asks.stop_context`) only changes the words, never the reason: the parked members the person
    answered to keep parked, whose a member held elsewhere is (the person chose to wait for them), and the unit
    boundaries the person answered `stop here` about."""
    ctx = ctx or {}
    kept, waiting, stopped = ctx.get("kept") or (), ctx.get("waiting") or {}, ctx.get("stopped") or {}
    rows, outside = p["members"], p.get("outside") or []
    if any(r.get("kind") in ("not-read", "unreadable") for r in rows + outside):
        return "degraded-read", "a parked member could not be read"
    rows = roots_of(rows, outside)              # name what holds the chain, not what waits behind it
    parked = sorted(r["number"] for r in rows if r["verdict"] in ("parked", chain_asks.KEPT_PARKED))
    if parked:
        if parked[0] in kept or any(r["number"] == parked[0] and r["verdict"] == chain_asks.KEPT_PARKED for r in rows):
            return "decision", ("#%d is parked and you answered to keep it parked; release it (answer `release it`) "
                                "to include it" % parked[0])
        return "decision", "#%d is parked and needs an answer" % parked[0]
    gated = sorted(r["number"] for r in rows if r["verdict"] == "held-by-gate")
    if gated:
        return "held-gate", "#%d is held by a gate" % gated[0]
    no_spend = sorted(r["number"] for r in rows if r["number"] in declined)
    if no_spend:        # #1908: the person answered not to run a paid step; the run skips it and says so
        return "held-gate", "#%d is a paid step you answered not to run; answer go ahead, or change the plan" % no_spend[0]
    claimed = sorted(r["number"] for r in rows if r.get("in_progress") or r["verdict"] == "assigned-elsewhere")
    if claimed:
        who = waiting.get(claimed[0])
        return "claimed", "#%d is in progress or assigned to someone else%s" % (
            claimed[0], " (%s; you chose to wait for them)" % who if who else "")
    cps = sorted((r for r in rows + outside if (r.get("own_verdict") or r["verdict"]) == "checkpoint"),
                 key=lambda r: r["number"])
    left_out = sorted(r["number"] for r in outside if r.get("own_verdict") in label_drain.GATED_KIND)
    if left_out:        # #1903/#1905: nobody promoted it, so it was never labelled; work waits on it
        return "decision", ("#%d was left out of the chain (not a goal, or waiting for its owner) and work waits "
                            "on it; promote it, or finish what it stands for" % left_out[0])
    ext = sorted({r["number"] for r in outside if (r.get("own_verdict") or r["verdict"]) != "checkpoint"}
                 | {r["number"] for r in rows if r.get("unresolved")})
    if ext:
        return "external", "#%d waits on an open prerequisite outside the chain" % ext[0]
    needs = sorted(r["number"] for r in rows if r["verdict"] in ("unarmed", "upper-tier") + tuple(label_drain.GATED_KIND))
    if needs:
        return "decision", "#%d is not armed and needs a person" % needs[0]
    boundary = sorted(r["number"] for r in rows if r["number"] in stopped)
    if boundary:        # the person answered `stop here` about a unit boundary: the run stops before the dependent
        return "decision", ("#%d waits on #%d in another unit and you answered to stop here; answer keep going to "
                            "include it" % (boundary[0], stopped[boundary[0]]))
    kept_waiting = sorted(r["number"] for r in rows if r["number"] in held)
    if kept_waiting:        # #1906: the person said to keep waiting; the run skips it, so say so rather than "none claimable"
        return "decision", ("#%d is kept waiting on a closed prerequisite's open follow-up or question (your "
                            "answer); finish what it waits on, or answer proceed" % kept_waiting[0])
    if cps:             # #1904: only after everything a person can act on, so it is named when it is the only thing left
        return "checkpoint", checkpoint_detail(cps[0])
    if unconfirmed:
        return "degraded-read", "#%d could not be confirmed closed" % sorted(unconfirmed)[0]
    return "claimed", "open members remain but none could be claimed this cycle (skipped or leased elsewhere)"


MAX_OTHER_ROOTS = 6


def stuck_reason(p, unconfirmed=(), held=(), declined=(), ctx=None):
    """-> (reason, detail): `_stop_of_first_root`, the one thing a person acts on first. `other_roots` names the rest."""
    return _stop_of_first_root(p, unconfirmed, held, declined, ctx)


def other_roots(p, detail):
    """-> the numbers of the members that ALSO hold the chain: the roots (`roots_of`) the stop `detail` does not name,
    at most MAX_OTHER_ROOTS, then a count in `more`. -> ([numbers], more). A chain can be held by several things at
    once; the stop line names the first one to act on, and this names the others (the run prints them on a line of
    their own and records them for `chain.py status`; `detail` itself is unchanged, so the forecast's and the run's
    stop line stay equal)."""
    named = {int(x) for x in re.findall(r"#(\d+)", detail or "")}
    outside = p.get("outside") or []
    held_out = [r for r in outside if (r.get("own_verdict") or r["verdict"]) in label_drain.GATED_KIND]
    others = sorted(({r["number"] for r in roots_of(p["members"], outside)} | {r["number"] for r in held_out}) - named)
    return others[:MAX_OTHER_ROOTS], max(0, len(others) - MAX_OTHER_ROOTS)


def holding_text(numbers, more=0):
    return "also holding the chain: %s%s" % (", ".join("#%d" % n for n in numbers), " and %d more" % more if more else "")


# --- the forecast ------------------------------------------------------------------------------------------

def verdict_for(row, login, filtered):
    """The member's verdict for a run by `login`. The record holds the verdict as judged for whoever planned;
    `base_verdict` is the same judgement without the assignee clause, which is re-applied here by the clause
    `_Judge.verdict` uses. No assignee data (a plan from the map), no filter or no known login: as recorded."""
    base = row.get("base_verdict") or row.get("verdict")
    logins = row.get("assignees")
    if not filtered or base not in ("armed", "held-by-gate"):
        return base
    if row.get("assignees_unknown"):      # #1993: a map node without the key is never read as a match
        return "assigned-elsewhere" if login else base
    if logins is None:
        return base
    gate = label_drain.assignee_gate(logins, login)
    return gate[0] if gate else base


def _answered(r, login, filtered, held, declined):
    """The verdict the person's answers leave a member with: a paid step they declined, or a member they said to
    keep waiting; a checkpoint or a parked member keeps its own."""
    base = verdict_for(r, login, filtered)
    if base in ("checkpoint", "parked") or base in label_drain.GATED_KIND:
        return base
    if r["number"] in declined:
        return "declined"
    return "kept-waiting" if r["number"] in held else base


def _normalise(rec, login, filtered, held=(), declined=()):
    needed = {}
    for e in rec.get("externals") or []:
        for n in e.get("needed_by") or []:
            needed.setdefault(n, []).append(e.get("ref"))
    return [{"number": r["number"], "verdict": _answered(r, login, filtered, held, declined), "kind": r.get("kind"),
             "waits_on": list(r.get("waits_on") or []), "unresolved": needed.get(r["number"], []),
             "in_progress": bool(r.get("in_progress")), "blocked": bool(r.get("blocked")),
             "checkpoint": r.get("checkpoint")}
            for r in rec.get("members") or []]


def forecast(rec, config=None, login=None, held=(), declined=(), assumed=(), cap=None):
    """-> the forecast for the plan record `rec` as run by `login` (default: who planned it). Keys: `runner`,
    `width`, `members` ([{number, marker, why, after}], the record's order), `finish` (claimed in order),
    `cycles`, `of`, `reason` and `detail` (None when everything finishes), `stops` ([{number, marker, why}],
    the roots), `notes`, `line`."""
    rec = chain_asks.view(rec)          # the record as the answers leave it (idempotent): the forecast reads the answers
    filtered = rec.get("assignee_filter") is True
    me = login if login is not None else rec.get("runner")
    me = str(me).strip().lstrip("@").casefold() if me else None
    notes = []
    if rec.get("verdict_basis") == "map-life" and rec.get("assignee_filter") is None:
        if any("assignees" in r for r in rec.get("members") or []):
            notes.append("the assignee filter is not applied: the plan was read from the map for no runner, so nothing is filtered by assignee")
        else:
            notes.append("the assignee filter is not applied: the plan was read from the map, and no member's node carries assignees")
    elif filtered and me is None:
        notes.append("the runner's login is unknown, so the assignee filter is not applied; pass --as <login>")
    elif login is not None and rec.get("assignee_filter") is False:
        notes.append("no assignee filter is configured, so --as changes nothing")
    unknown = sum(1 for r in rec.get("members") or [] if r.get("assignees_unknown"))
    if filtered and unknown and me:
        notes.append("%d member%s no assignees on the map (an outside node or a map older than assignees): read as not "
                     "%s" % (unknown, " has" if unknown == 1 else "s have", "the runner's"))
    if assumed:
        notes.append("%s wait%s on a closed prerequisite's open follow-up or question nobody has answered yet: read as "
                     "the recommended answer, keep waiting" % (", ".join("#%d" % n for n in sorted(assumed)),
                                                               "s" if len(assumed) == 1 else ""))
    rows = _normalise(rec, me, filtered, held, declined)
    width = chain_parallel.width(config or {})
    open_now = {r["number"] for r in rows}
    out_members = []
    for r in rows:
        marker, why, after = classify(r, open_now)
        out_members.append({"number": r["number"], "marker": marker, "why": why, "after": after})
    finish, cycles = [], 0
    over = cap is not None and cap[1] > cap[0]      # `chain_spend.admit` refuses before any claim: same words
    while not over:
        batch = ready_members([r for r in rows if r["number"] in open_now], open_now, set(), width)
        if not batch:
            break
        cycles += 1
        finish += batch
        open_now -= set(batch)
    left = [r for r in rows if r["number"] in open_now]
    reason = detail = None
    stops = []
    if over:
        reason, detail = "capped", "capped: %d goals over the %d goal cap" % (cap[1], cap[0])
    elif left:
        reason, detail = stuck_reason({"members": left, "outside": []}, (), held, declined,
                                      chain_asks.stop_context(rec))
        for r in left:
            if not [w for w in r["waits_on"] if w in open_now]:
                marker, why, _after = classify(r, open_now)
                stops.append({"number": r["number"], "marker": marker, "why": why})
    who = "a run from %s" % me if me else "a run"
    if not left:
        line = "%s can finish all %d of %d" % (who, len(finish), len(rows))
    else:
        line = "%s can finish %d of %d before it stops on %s: %s" % (
            who, len(finish), len(rows), reason,
            ", ".join("#%d %s (%s)" % (s["number"], s["marker"], s["why"]) for s in stops) or detail)
    return {"runner": me, "width": width, "members": out_members, "finish": finish, "cycles": cycles,
            "of": len(rows), "reason": reason, "detail": detail, "stops": stops, "notes": notes, "line": line}


def member_lines(fc):
    """One line per member: its marker, or `after #N` while it waits on members still open."""
    out = []
    for m in fc["members"]:
        shown = "after " + ", ".join("#%d" % a for a in m["after"]) if m["marker"] == AFTER else m["marker"]
        out.append("  #%d %s%s" % (m["number"], shown, " (%s)" % m["why"] if m["why"] and m["marker"] != AFTER else ""))
    return out


def answer_lines(fc):
    """The one-line answer, then what it rests on. The sheet prints these first (#1908)."""
    return (["Forecast: %s" % fc["line"]] + ["  note: " + n for n in fc["notes"]]
            + ["  assumes every goal it claims merges; the cap is not modelled"])


def render_forecast(fc):
    """The forecast as lines of text: one per member, then the one-line answer and what it assumes."""
    head = "Forecast, a run%s (width %d):" % (" from " + fc["runner"] if fc["runner"] else "", fc["width"])
    return [head] + member_lines(fc) + answer_lines(fc)
