"""Cost estimate and chain-level spend cap for one chain (#1586). Library only; no main.

Reads the `spend` and `phase` events of the local journal (`<sdlc_dir>/events/*.jsonl`). Attribution is by
goal number from those events, never a delta of a shared counter. Optional keys `chain.max_usd`,
`chain.max_goals` (default members plus `chain.max_added`, default 5), `chain.max_minutes` (unset by
default). With no cap set nothing here reads the disk. A ledger that cannot be read while a cap is set
FAILS CLOSED, and so does an unpriced phase when no p90 exists to charge it at.
"""
import calendar
import json
import math
import os
import pathlib
import re
import time

DEFAULT_MAX_ADDED = 5
MIN_BASIS = 3
OVERSHOOT_SENTENCE = ("The cap is checked before each claim, so it can be overshot by the cost of the "
                      "goals in flight.")


def _epoch(ts):
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    try:
        return float(calendar.timegm(time.strptime(re.sub(r"\.\d+Z$", "Z", str(ts)), "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def _cents(v):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _events(sdlc_dir):
    """-> (records, ok). A missing directory is an empty history; an unreadable file is not ok."""
    base = pathlib.Path(sdlc_dir) / "events"
    out = []
    try:
        paths = sorted(p for p in (base / n for n in os.listdir(base)) if p.suffix == ".jsonl")
    except FileNotFoundError:
        return [], True
    except OSError:
        return [], False
    for p in paths:
        try:
            raw = p.read_bytes().decode("utf-8", "replace")
        except OSError:
            return [], False
        for line in raw.splitlines():
            try:
                r = json.loads(line)
            except (ValueError, RecursionError):
                continue   # torn or garbage line
            if isinstance(r, dict) and r.get("kind") in ("spend", "phase"):
                out.append(r)
    return out, True


def _rank(sorted_vals, q):
    return sorted_vals[max(0, math.ceil(q * len(sorted_vals)) - 1)]


def _p90_phase(records):
    costs = sorted(c for r in records if r["kind"] == "spend" and (c := _cents(r.get("cost_cents"))) is not None)
    return _rank(costs, 0.9) if costs else None


def estimate(sdlc_dir, members):
    """p50 and p90 cost per goal (cents) over earlier goals that are not members. Fewer than 3 measured
    goals gives no number: `text` carries the sentence and p50/p90 are None."""
    records, ok = _events(sdlc_dir)
    mem = {str(m) for m in members}
    per = {}
    for r in records:
        c = _cents(r.get("cost_cents")) if r["kind"] == "spend" else None
        if c is not None and str(r.get("goal")) not in mem:
            per[str(r.get("goal"))] = per.get(str(r.get("goal")), 0) + c
    vals = sorted(per.values())
    if len(vals) < MIN_BASIS:
        return {"p50": None, "p90": None, "basis": len(vals), "ok": ok,
                "text": "no estimate: only %d measured goals" % len(vals)}
    return {"p50": _rank(vals, 0.5), "p90": _rank(vals, 0.9), "basis": len(vals), "ok": ok, "text": ""}


def spent(sdlc_dir, members, since):
    """-> (cents, ok): the sum over `spend` events of member goals at or after `since`."""
    records, ok = _events(sdlc_dir)
    return _spent_of(records, members, since), ok


def _spent_of(records, members, since):
    mem = {str(m) for m in members}
    t0 = _epoch(since)
    total = 0
    for r in records:
        if r["kind"] != "spend" or str(r.get("goal")) not in mem:
            continue
        t = _epoch(r.get("ts"))
        if t0 is not None and (t is None or t < t0):
            continue
        c = _cents(r.get("cost_cents"))
        if c is not None:
            total += c
    return total


_BAD = object()


def _num(block, key):
    """-> None when the key is absent (unset), the number, or _BAD when present but not a positive number."""
    if key not in block or block[key] is None:
        return None
    v = block[key]
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
    return float(v) if ok else _BAD


def admit(sdlc_dir, config, chain, now=None):
    """-> (True, "") or (False, "capped: <reason>"). `chain` carries `members`, `approved_at` (the approval
    stamp), and optionally `started_at`."""
    try:
        block = config["chain"]
    except Exception:
        block = {}
    if not isinstance(block, dict):
        return False, "capped: chain config is not a mapping"
    max_usd, max_min, max_goals = _num(block, "max_usd"), _num(block, "max_minutes"), _num(block, "max_goals")
    for key, v in (("max_usd", max_usd), ("max_minutes", max_min), ("max_goals", max_goals)):
        if v is _BAD:
            return False, "capped: invalid chain.%s (not a positive number)" % key
    members = list(chain.get("members") or [])
    added = block.get("max_added", DEFAULT_MAX_ADDED)
    added = added if isinstance(added, int) and not isinstance(added, bool) and added >= 0 else DEFAULT_MAX_ADDED
    if max_goals is None:
        max_goals = len(members) + added
    if len(members) > max_goals:
        return False, "capped: %d goals over the %d goal cap" % (len(members), max_goals)
    since = chain.get("approved_at")
    if max_min is not None:
        t0 = _epoch(since)
        if t0 is None:
            return False, "capped: chain.max_minutes is set and there is no approval stamp to measure from"
        if ((now if now is not None else time.time()) - t0) / 60 >= max_min:
            return False, "capped: ran %g minutes, the cap is %g" % (max_min, max_min)
    if max_usd is None:
        return True, ""
    records, ok = _events(sdlc_dir)
    if not ok:
        return False, "capped: the spend ledger is unreadable and a cap is set"
    cents = _spent_of(records, members, since)
    mem = {str(m) for m in members}
    t0 = _epoch(since)

    def _after(r):
        t = _epoch(r.get("ts"))
        return t0 is None or (t is not None and t >= t0)

    priced = {(str(r.get("goal")), r.get("phase")) for r in records if r["kind"] == "spend" and _after(r)}
    unpriced = {(str(r.get("goal")), r.get("phase")) for r in records
                if r["kind"] == "phase" and str(r.get("goal")) in mem and _after(r)} - priced
    extra = 0
    if unpriced:
        p90 = _p90_phase(records)
        if p90 is None:
            return False, "capped: %d unpriced phases and no p90 to charge them at" % len(unpriced)
        extra = p90 * len(unpriced)
    if cents + extra >= max_usd * 100:
        return False, "capped: spent %.2f USD (%.2f of it unpriced phases at p90) reaches the %.2f cap" % (
            (cents + extra) / 100, extra / 100, max_usd)
    return True, ""
