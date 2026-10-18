#!/usr/bin/env python3
"""chain_status.py -- what a chain is doing and how long ago it last moved (#1594). Pure; reads only a record.

`status_of(rec, config, now)` -> one dict per chain record; `render_line` / `render_status` print it;
`slot_facts` hands render.py (the only thing that builds a Block A line) a slot; `doctor_summary` is the
one local read behind the doctor row. Nothing here touches GitHub or writes a file: `chain.py` owns the
verb, the rebuild and the leftovers read.

STATES (closed): planned, approved, running, stuck(<reason>), paused(decision), capped, done, stopped.
No run block reads planned (or approved once the record says so); a run in `running` or `approved-running`
reads running; a stuck run reads stuck(<reason>) except the reason `decision`, which reads paused(decision).

STALE. Last progress is the run's heartbeat (a running chain with none uses `armed_at`). A `running`
chain whose last progress is older than `chain.stale_minutes` (default 30; anything that is not a positive
number reads 30) prints `running STALE`. Only a running chain can be stale: a done, stopped, capped, stuck,
planned or approved chain is never STALE, however old its heartbeat. The age is clamped at 0 (clock skew).
Measured: nothing; the threshold is a default, not a measured number.

ROOT CAUSE. A stuck or stopped chain prints ONE line from the record: `#2, #3 wait on #4's decision;
/sigma-unpark #4` (numbers only), or for another reason the reason word and the member number.
"""
import calendar
import re
import time

DEFAULT_STALE_MINUTES = 30
PHASE = "P5 IMPLEMENT"
_MARKER = {"running": "🔵", "stuck": "🔴", "paused": "🔴", "capped": "⏸️", "stopped": "⏸️", "done": "✅",
           "planned": "⚪", "approved": "⚪"}


def stale_minutes(config):
    block = (config or {}).get("chain")
    v = block.get("stale_minutes") if isinstance(block, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return DEFAULT_STALE_MINUTES
    return v


def _epoch(text):
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def age_text(seconds):
    s = max(0, int(seconds or 0))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return "%d%s" % (s // size, unit)
    return "%ds" % s


def _state(rec):
    blk = rec.get("run")
    if not isinstance(blk, dict) or not blk.get("state"):
        return ("approved", None) if rec.get("state") == "approved" else ("planned", None)
    st = blk["state"]
    if st in ("running", "approved-running"):
        return "running", None
    if st == "stuck":
        reason = blk.get("stuck") or "unknown"
        return ("paused", "decision") if reason == "decision" else ("stuck", reason)
    if st in ("capped", "stopped", "done"):
        return st, None
    return "planned", None


def _decision_issue(rec, blk):
    m = re.match(r"#(\d+)\b", str(blk.get("reason") or ""))
    if m:
        return int(m.group(1))
    parked = sorted(r["number"] for r in rec["members"] if r.get("verdict") == "parked")
    return parked[0] if parked else None


def _waiters(rec, n):
    """Members that wait on `n`, directly or through another member. Numbers only."""
    edges = {r["number"]: set(r.get("waits_on") or ()) for r in rec["members"]}
    out, grew = set(), True
    while grew:
        grew = False
        for m, ws in edges.items():
            if m != n and m not in out and (n in ws or ws & out):
                out.add(m)
                grew = True
    return sorted(out)


def _ids(nums):
    return ", ".join("#%d" % n for n in nums)


def root_cause(rec, state, reason):
    blk = rec.get("run") or {}
    if state == "stopped":
        return "stopped by a person; chain.py resume <sdlc_dir> %d" % rec["goal"]
    if state not in ("stuck", "paused"):
        return None
    m = re.match(r"#(\d+)\b", str(blk.get("reason") or ""))
    if state == "paused":
        n = _decision_issue(rec, blk)
        if n is None:
            return "waiting on a decision; /sigma-unpark"
        w = _waiters(rec, n)
        who = _ids(w) if w else "the chain"
        return "%s %s on #%d's decision; /sigma-unpark #%d" % (who, "wait" if len(w) != 1 else "waits", n, n)
    return "%s%s" % (reason, " at #%s" % m.group(1) if m else "")


def status_of(rec, config=None, now=None):
    now = time.time() if now is None else now
    state, reason = _state(rec)
    blk = rec.get("run") if isinstance(rec.get("run"), dict) else {}
    last = _epoch(blk.get("heartbeat"))
    if last is None and state == "running":
        last = _epoch(rec.get("armed_at"))
    age = None if last is None else max(0, int(now - last))
    stale = bool(state == "running" and age is not None and age > stale_minutes(config) * 60)
    return {"goal": rec["goal"], "state": state, "reason": reason, "stale": stale, "age_s": age,
            "last_progress_at": blk.get("heartbeat"), "members": len(rec["members"]),
            "root_cause": root_cause(rec, state, reason)}


def state_word(st):
    word = "%s(%s)" % (st["state"], st["reason"]) if st["reason"] else st["state"]
    return word + (" STALE" if st["stale"] else "")


def render_line(st):
    ago = "last progress %s ago" % age_text(st["age_s"]) if st["age_s"] is not None else "no progress recorded"
    return "chain #%d: %s (%s, %d members)" % (st["goal"], state_word(st), ago, st["members"])


def render_status(st):
    out = [render_line(st)]
    if st["root_cause"]:
        out.append(st["root_cause"])
    return "\n".join(out) + "\n"


def slot_facts(st):
    """One render.py Block A slot for a chain; the renderer builds the line, no new block shape."""
    ago = "last progress %s ago" % age_text(st["age_s"]) if st["age_s"] is not None else "no progress yet"
    nxt = {"running": "next cycle", "stuck": "chain.py status", "paused": "/sigma-unpark",
           "capped": "chain.py resume", "stopped": "chain.py resume"}.get(st["state"])
    desc = "%s, %s" % (state_word(st), ago) + (" -> %s" % nxt if nxt else "")
    return {"marker": _marker_name(st["state"]), "ref": "#%d" % st["goal"], "url": None, "phase": PHASE,
            "title": "Chain for end goal %d" % st["goal"], "description": desc.replace("->", "→"),
            "model_tier": None}


_NAME = {"running": "running", "stuck": "blocked", "paused": "blocked", "capped": "parked",
         "stopped": "parked", "done": "done", "planned": "queued", "approved": "queued"}


def _marker_name(state):
    return _NAME[state]


def doctor_summary(sdlc_dir, config, now=None):
    """-> (text, ok) for the doctor row, or None when no readable chain record exists. Local read only."""
    import json
    import pathlib
    d = pathlib.Path(sdlc_dir) / "state" / "chains"
    rows = []
    try:
        paths = sorted(d.glob("c*.json"))
    except OSError:
        return None
    for p in paths:
        try:
            rec = json.loads(p.read_text())
            if isinstance(rec, dict) and isinstance(rec.get("goal"), int) and isinstance(rec.get("members"), list):
                rows.append(status_of(rec, config, now))
        except (OSError, ValueError):
            continue
    if not rows:
        return None
    parts, ok = [], True
    for st in rows:
        ago = " (%s)" % age_text(st["age_s"]) if st["age_s"] is not None else ""
        parts.append("#%d %s%s" % (st["goal"], state_word(st), ago))
        ok = ok and not (st["stale"] or st["state"] in ("stuck", "paused"))
    return "chain: " + "; ".join(parts), ok
