"""One shared rolling weekly and monthly spend cap (#1502). Library only; no main.

Optional keys `rulebook.spend.weekly_usd` and `rulebook.spend.monthly_usd` (positive numbers; anything
else reads as unset). Every paid model call (import classifier, drift judge) checks `check` before the
call and `record`s its charged cost after. With no cap set nothing here touches the disk.

The ledger is `<sdlc_dir>/state/spend-cap.jsonl`, one JSON line per call. Appends are one `write` on an
O_APPEND descriptor (newline first, so a torn tail cannot merge with it), so concurrent callers do not interleave lines; a torn last line (crash mid-write)
or a garbage line is skipped on read. When the file passes PRUNE_BYTES an append rewrites it without
records older than the longest window (write-temp-then-rename); a concurrent append landing inside that
rewrite can be lost, which under-counts by one call at most. A ledger that cannot be read while a cap
is set FAILS CLOSED: the call is refused rather than made blind.
"""
import json
import math
import os
import pathlib
import time

LEDGER = "spend-cap.jsonl"
DAY = 24 * 3600
WINDOWS = (("weekly", "weekly_usd", 7 * DAY), ("monthly", "monthly_usd", 30 * DAY))
PRUNE_BYTES = 64 * 1024   # about 1000 records; the longest window then holds far fewer calls than this


def _num(v):
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    return float(v) if ok else None


def caps(config):
    """-> {window: cap_usd} for the windows that carry a positive number; {} when none."""
    try:
        block = config["rulebook"]["spend"]
        if not isinstance(block, dict):
            return {}
        out = {}
        for name, key, _ in WINDOWS:
            v = _num(block.get(key))
            if v is not None and v > 0:
                out[name] = v
        return out
    except Exception:
        return {}


def _path(state_dir):
    return pathlib.Path(state_dir) / "state" / LEDGER


def read(state_dir, now=None):
    """-> (records, ok). Missing file is an empty history; an unreadable one is (None-free) ([], False)."""
    path = _path(state_dir)
    if not path.exists():
        return [], True
    try:
        raw = path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return [], False
    out = []
    for line in raw.splitlines():
        try:
            r = json.loads(line)
            ts, usd = _num(r.get("ts")), _num(r.get("usd"))
        except Exception:
            continue   # torn or garbage line
        if ts is not None and usd is not None and usd >= 0:
            out.append({"ts": ts, "usd": usd, "source": str(r.get("source", ""))[:40]})
    return out, True


def append(state_dir, source, usd, now=None):
    """Append one record. Never raises: a spend record that cannot be written must not kill a run that
    already paid; the next `check` still sees every record that did land."""
    try:
        now = time.time() if now is None else now
        amount = _num(usd)
        if amount is None or amount < 0:
            return False
        path = _path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        # the leading newline keeps a torn last line (no newline) from swallowing this record
        line = ("\n" + json.dumps({"ts": now, "usd": amount, "source": str(source)[:40]})).encode("utf-8")
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if size > PRUNE_BYTES:
            _prune(path, now)
        return True
    except Exception:
        return False


def _prune(path, now):
    horizon = now - max(w for _, _, w in WINDOWS)
    recs, ok = read(path.parent.parent)
    if not ok:
        return
    kept = [r for r in recs if r["ts"] > horizon]
    tmp = path.with_name(LEDGER + ".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")
    os.replace(str(tmp), str(path))


def record(state_dir, config, source, usd, now=None):
    """`append`, but only when a cap is set, so an uncapped project never gets a ledger file."""
    if not caps(config):
        return False
    return append(state_dir, source, usd, now)


def _reset_at(window_records, spent, cap, window_s):
    """When the oldest records have aged out far enough that the spend is back under the cap."""
    left = spent
    for r in sorted(window_records, key=lambda r: r["ts"]):
        left -= r["usd"]
        if left < cap:
            return r["ts"] + window_s
    return None


def check(state_dir, config, now=None):
    """-> {"ok", "window", "cap", "spent", "reset_at", "reason"}. ok is False once a window's spend has
    reached its cap, or when a cap is set and the ledger cannot be read (reason "unreadable")."""
    c = caps(config)
    res = {"ok": True, "window": None, "cap": None, "spent": 0.0, "reset_at": None, "reason": None}
    if not c:
        return res
    now = time.time() if now is None else now
    recs, ok = read(state_dir)
    if not ok:
        return dict(res, ok=False, reason="unreadable")
    for name, _, win in WINDOWS:
        if name not in c:
            continue
        live = [r for r in recs if r["ts"] > now - win]
        spent = sum(r["usd"] for r in live)
        if spent >= c[name]:
            return dict(res, ok=False, window=name, cap=c[name], spent=spent, reason="cap_reached",
                        reset_at=_reset_at(live, spent, c[name], win))
    return res


def status(state_dir, config, now=None):
    """-> one row per capped window {window, cap, spent, reset_at} for the doctor; [] when unset or unreadable."""
    c = caps(config)
    now = time.time() if now is None else now
    recs, ok = read(state_dir)
    if not c or not ok:
        return []
    rows = []
    for name, _, win in WINDOWS:
        if name in c:
            live = [r for r in recs if r["ts"] > now - win]
            spent = sum(r["usd"] for r in live)
            rows.append({"window": name, "cap": c[name], "spent": spent,
                         "reset_at": _reset_at(live, spent, c[name], win) if live else None})
    return rows


def refusal_line(res):
    """The one printed line for a refused call."""
    if res.get("reason") == "unreadable":
        return "spend cap set but the spend ledger cannot be read: no model call made"
    return "spend cap reached (%s $%.2f of $%.2f, resets %s): no model call made" % (
        res["window"], res["spent"], res["cap"],
        time.strftime("%a %H:%M", time.localtime(res["reset_at"])) if res["reset_at"] else "unknown")
