#!/usr/bin/env python3
"""Lease table over resource_guard: what has been admitted, and what units actually cost (#1599).

A measure-only gate lets two simultaneous admitters both see "enough", because memory is not consumed until a
suite warms up. This module records every admitted unit as a lease, counts the unmaterialised remainder of each
lease against the next admit, and learns a per-kind cost from what released units actually used.

State lives in the resource state folder (SIGMA_RESOURCE_DIR, else XDG_STATE_HOME/sigma/resource, else
~/.local/state/sigma/resource - the same folder as hold.json), outside every repository:
  admit.lock       fcntl.flock, bounded wait; every read-decide-write below happens while it is held
  leases/<id>.json one live lease
  last.json        the last decision and the sample it was made on (hold age, swap-out rate)
  observed.json    per-kind ring of the last 32 measured per-unit footprints
  waiting.json     verifies that were held, so a new goal start does not take their memory
  events.jsonl     bounded log (rotated once, one generation kept)

Nothing here spawns a process or pauses one: admission is paused (a Hold is returned), never a process.
POSIX only (flock); on a platform without it the module refuses with resource_guard.Unsupported.

Commands: admit, release, status, wait. Exit 0 ok, 10 held / at the cap, 2 usage, 3 unsupported.
"""
import argparse
import contextlib
import hashlib
import json
import math
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resource_guard as guard  # noqa: E402

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

EXIT_HELD = 10
RING = 32
OUTLIER_FACTOR = 4
PERCENTILE = 0.9
LOCK_WAIT = 5.0  # seconds a caller waits for admit.lock before it is told to hold
DEFAULT_TTL = 6 * 3600.0  # seconds; callers pass the verify timeout or session hold
WAIT_TTL = 600.0  # seconds a held verify keeps blocking new goal starts
EVENTS_MAX = 262144  # events.jsonl is rotated once past this size
POLL = 0.5


class Grant:
    def __init__(self, n, lease_id):
        self.n, self.id = n, lease_id

    def as_dict(self):
        return {"n": self.n, "id": self.id}


class Hold:
    def __init__(self, reason, retry_after):
        self.reason, self.retry_after = reason, retry_after

    def as_dict(self):
        return {"hold": True, "reason": self.reason, "retry_after": self.retry_after}


# ------------------------------------------------------------------------------------------ seams
def take_sample():
    return guard.sample()


def take_footprint(pid):
    return guard.footprint(pid)


def pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it exists, it is someone else's
        return True
    return True


def expired(record, now):
    return now > record["start"] + record["ttl"]


def remainder(est, seen):
    """What a lease has promised but not yet used."""
    return max(0, est - seen)


# ------------------------------------------------------------------------------------------ files
def state_dir(env=None):
    return os.path.dirname(guard.hold_path(env))


def _read_json(path, default):
    try:
        with open(path, "r") as handle:
            return json.loads(handle.read())
    except (OSError, ValueError):
        return default


def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(json.dumps(obj, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def log_event(directory, record):
    """Append one event; rotate to events.jsonl.1 past EVENTS_MAX. Best effort: a log failure never fails a decision."""
    path = os.path.join(directory, "events.jsonl")
    try:
        os.makedirs(directory, exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) >= EVENTS_MAX:
            os.replace(path, path + ".1")
        with open(path, "a") as handle:
            handle.write(json.dumps(dict(record, at=record.get("at", time.time())), sort_keys=True) + "\n")
    except OSError:
        pass


@contextlib.contextmanager
def _locked(directory):
    """Yield True when admit.lock is held, False when it stayed busy for LOCK_WAIT seconds."""
    if fcntl is None:
        raise guard.Unsupported("no flock on platform %s" % sys.platform)
    os.makedirs(directory, exist_ok=True)
    handle = open(os.path.join(directory, "admit.lock"), "a+")
    got = False
    deadline = time.monotonic() + LOCK_WAIT
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        yield got
    finally:
        handle.close()  # closing drops the flock


# ------------------------------------------------------------------------------------------ learned cost
def _median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def learned_cost(ring):
    """p90 (nearest rank) of the ring after dropping values above 4x its median; None with no observations."""
    values = [v for v in ring if guard._number(v) and v > 0]
    if not values:
        return None
    median = _median(values)
    kept = [v for v in values if v <= OUTLIER_FACTOR * median] or values
    kept.sort()
    return kept[max(0, math.ceil(PERCENTILE * len(kept)) - 1)]


def record_observation(directory, kind, values):
    """Append per-unit footprints to the kind's ring (bounded at 32); an outlier is logged, and excluded later."""
    path = os.path.join(directory, "observed.json")
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    ring = [v for v in data.get(kind, []) if guard._number(v)]
    for value in values:
        if ring and value > OUTLIER_FACTOR * _median(ring):
            log_event(directory, {"event": "outlier", "kind": kind, "value": value, "median": _median(ring)})
        ring.append(value)
    data[kind] = ring[-RING:]
    _atomic_write(path, data)


# ------------------------------------------------------------------------------------------ the table
def _lease_files(directory):
    root = os.path.join(directory, "leases")
    try:
        return [os.path.join(root, n) for n in sorted(os.listdir(root)) if n.endswith(".json")]
    except OSError:
        return []


def _load_leases(directory):
    """[(path, record or None)]; None marks an unreadable or malformed file."""
    found = []
    for path in _lease_files(directory):
        record = _read_json(path, None)
        ok = (isinstance(record, dict) and isinstance(record.get("pid"), int) and guard._number(record.get("start"))
              and guard._number(record.get("ttl")) and guard._number(record.get("est_mem")))
        found.append((path, record if ok else None))
    return found


def _stale(record, now):
    return record is None or not pid_alive(record["pid"]) or expired(record, now)


def _seen(record):
    foot = take_footprint(record["pid"])
    return max(record.get("peak", 0), foot[0] if foot else 0)


def _finish(directory, path, record, why):
    """Record the unit's measured peak into its ring and remove the lease file."""
    if record is not None:
        peak = _seen(record)
        workers = max(1, record.get("workers", 1))
        if peak > 0:
            record_observation(directory, record["kind"], [peak // workers or 1])
        log_event(directory, {"event": why, "id": record["id"], "kind": record["kind"], "peak": peak})
    try:
        os.unlink(path)
    except OSError:
        pass


def _view(directory, now, config=None, reclaim=False):
    """(sample, limits, live leases, ring data). With reclaim, stale leases are finished and removed."""
    live = []
    for path, record in _load_leases(directory):
        if _stale(record, now):
            if reclaim:
                _finish(directory, path, record, "reclaimed")
            continue
        live.append((path, record))
    sample = take_sample()
    limits = guard.derive(sample, config)
    limits.unmaterialised = sum(remainder(r["est_mem"], _seen(r)) for _, r in live)
    limits.leased = sum(r["est_mem"] for _, r in live)
    limits.headroom = limits.headroom_for(sample.available)
    observed = _read_json(os.path.join(directory, "observed.json"), {})
    return sample, limits, live, observed if isinstance(observed, dict) else {}


def _room(kind, limits):
    """Headroom a new unit of `kind` may use; a goal start also leaves room for one model session (priority)."""
    return limits.headroom - (limits.session_cost if kind == "goal" else 0)


def _retry():
    return guard.SAMPLE_GAP * guard.RESUME_SAMPLES


def _waiting(directory, now):
    data = _read_json(os.path.join(directory, "waiting.json"), {})
    return {g: w for g, w in data.items() if isinstance(w, dict) and w.get("until", 0) > now} \
        if isinstance(data, dict) else {}


def _choose(kind, want, goal, limits, live, ring, waiting, now):
    """(n, est_mem) or a Hold. Pure over its inputs; the caller holds the lock."""
    started = {r["goal"] for _, r in live}
    if limits.critical:
        return Hold("critical: memory below half the reserve", _retry())
    if kind in ("goal", "session") and not limits.admits(kind, running=len(live)):
        return Hold("pressure: new work is held", _retry())
    if kind == "goal":
        if any(g != goal and g in started for g in waiting):
            return Hold("verify of a started goal is waiting", _retry())
        if limits.max_goals and sum(1 for _, r in live if r["kind"] == "goal") >= limits.max_goals:
            return Hold("max_goals reached", _retry())
    room = _room(kind, limits)
    cost = learned_cost(ring)
    if cost is None:
        if any(r["kind"] == kind for _, r in live):
            return Hold("no observation of %s yet and one unit is already running" % kind, _retry())
        if room <= 0:
            return Hold("memory: no room above the reserve", _retry())
        return 1, 0
    n = min(want, int(room // max(cost, 1)))
    if kind == "verify" and limits.max_verify_workers:
        n = min(n, limits.max_verify_workers)
    if n < 1:
        return Hold("memory: headroom %d below one %s unit (%d)" % (max(room, 0), kind, cost), _retry())
    return n, n * cost


def admit(kind, want, repo, goal, pid=None, ttl=None, config=None, now=None, env=None):
    """Grant(n, id) for the largest n <= want that fits, or Hold(reason, retry_after). Atomic under admit.lock."""
    directory = state_dir(env)
    now = time.time() if now is None else now
    pid = os.getpid() if pid is None else pid
    want = max(1, int(want))
    with _locked(directory) as got:
        if not got:
            return Hold("admit lock busy", 1.0)
        sample, limits, live, observed = _view(directory, now, config, reclaim=True)
        for path, record in live:  # keep the peak: a process that exits before release still taught us its size
            seen = _seen(record)
            if seen > record.get("peak", 0):
                _atomic_write(path, dict(record, peak=seen))
        waiting = _waiting(directory, now)
        choice = _choose(kind, want, goal, limits, live, observed.get(kind, []), waiting, now)
        previous = _read_json(os.path.join(directory, "last.json"), {})
        if isinstance(choice, Hold):
            if limits.mode != "enforce":
                log_event(directory, {"event": "would_hold", "kind": kind, "goal": goal, "reason": choice.reason})
                choice = (want, 0)
        if isinstance(choice, Hold):
            if kind == "verify":
                waiting[goal] = {"kind": kind, "since": waiting.get(goal, {}).get("since", now), "until": now + WAIT_TTL}
            _atomic_write(os.path.join(directory, "waiting.json"), waiting)
            since = previous.get("since") if previous.get("state") == "hold" else now
            _atomic_write(os.path.join(directory, "last.json"), {
                "state": "hold", "since": since, "reason": choice.reason, "kind": kind, "goal": goal, "at": now,
                "sample": sample.as_dict()})
            log_event(directory, {"event": "hold", "kind": kind, "goal": goal, "reason": choice.reason, "at": now})
            return choice
        n, est = choice
        lease_id = uuid.uuid4().hex[:12]
        record = {"id": lease_id, "kind": kind, "pid": pid, "pgid": _pgid(pid), "start": now,
                  "repo_hash": hashlib.sha256(str(repo).encode()).hexdigest()[:12], "goal": goal, "est_mem": est,
                  "workers": n, "ttl": ttl if guard._number(ttl) and ttl > 0 else DEFAULT_TTL, "peak": 0}
        _atomic_write(os.path.join(directory, "leases", lease_id + ".json"), record)
        waiting.pop(goal, None) if kind == "verify" else None
        _atomic_write(os.path.join(directory, "waiting.json"), waiting)
        _atomic_write(os.path.join(directory, "last.json"), {
            "state": "open", "since": now, "reason": "ok", "kind": kind, "goal": goal, "at": now,
            "sample": sample.as_dict()})
        log_event(directory, {"event": "grant", "id": lease_id, "kind": kind, "goal": goal, "n": n, "at": now})
        return Grant(n, lease_id)


def _pgid(pid):
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def release(lease_id, env=None):
    """Measure the unit, record it into its ring, remove its lease. False when unknown or the lock stayed busy."""
    directory = state_dir(env)
    path = os.path.join(directory, "leases", os.path.basename(str(lease_id)) + ".json")
    with _locked(directory) as got:
        if not got or not os.path.exists(path):
            return False
        _finish(directory, path, _load_one(path), "release")
        return True


def _load_one(path):
    record = _read_json(path, None)
    return record if isinstance(record, dict) and "kind" in record and "pid" in record else None


def status(env=None, config=None, now=None):
    """Read-only snapshot: leases, per-kind p90, headroom, hold state and age, swap-out rate."""
    directory = state_dir(env)
    now = time.time() if now is None else now
    sample, limits, live, observed = _view(directory, now, config)
    last = _read_json(os.path.join(directory, "last.json"), {})
    hold = {"state": last.get("state", "open"), "since": last.get("since"), "reason": last.get("reason")}
    hold["age"] = round(now - hold["since"], 1) if hold["state"] == "hold" and guard._number(hold["since"]) else None
    rate = None
    prev = last.get("sample")
    if isinstance(prev, dict):
        try:
            rate = guard.swap_rate([guard.Sample(**prev), sample])
        except (TypeError, KeyError):
            rate = None
    return {"leases": [r for _, r in live],
            "p90": {k: learned_cost(v) for k, v in observed.items() if isinstance(v, list)},
            "headroom": limits.headroom, "reserve": limits.reserve, "hold": hold, "swapout_rate": rate,
            "uncalibrated": True}


def wait(max_s, kind="goal", env=None, config=None):
    """0 as soon as a unit of `kind` would be admitted, 10 at the cap after max_s. Never blocks past max_s + 1."""
    directory = state_dir(env)
    deadline = time.monotonic() + max(0.0, min(float(max_s), 3600.0))
    while True:
        _, limits, live, observed = _view(directory, time.time(), config)
        cost = learned_cost(observed.get(kind, []))
        room = _room(kind, limits)
        open_ = not limits.critical and (room > 0 if cost is None else room >= cost)
        if kind == "goal" and limits.goal_block:
            open_ = False
        if open_:
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return EXIT_HELD
        time.sleep(min(POLL, remaining))


# ------------------------------------------------------------------------------------------ CLI
def _parser():
    parser = argparse.ArgumentParser(prog="resource_lease.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd")
    a = sub.add_parser("admit", help="ask for up to --want units; prints the grant, or the hold with exit 10")
    a.add_argument("--kind", required=True, choices=("verify", "session", "goal"))
    a.add_argument("--want", type=int, default=1)
    a.add_argument("--repo", default=os.getcwd())
    a.add_argument("--goal", default="")
    a.add_argument("--pid", type=int, default=os.getppid(), help="process the lease belongs to (default: the caller)")
    a.add_argument("--ttl", type=float, default=None, help="seconds before the lease is stale")
    r = sub.add_parser("release", help="release a lease and record what the unit used")
    r.add_argument("id")
    s = sub.add_parser("status", help="leases, p90 per kind, headroom, hold age, swap-out rate")
    s.add_argument("--json", action="store_true")
    w = sub.add_parser("wait", help="exit 0 when open, 10 at the cap, within --max N (+1) seconds")
    w.add_argument("--max", type=float, default=0, dest="max_s")
    w.add_argument("--kind", default="goal", choices=("verify", "session", "goal"))
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.cmd:
        parser.print_help()
        return 0
    try:
        if args.cmd == "admit":
            got = admit(args.kind, args.want, args.repo, args.goal, pid=args.pid, ttl=args.ttl)
            sys.stdout.write(json.dumps(got.as_dict(), sort_keys=True) + "\n")
            return 0 if isinstance(got, Grant) else EXIT_HELD
        if args.cmd == "release":
            if release(args.id):
                return 0
            sys.stderr.write("resource_lease: no such lease, or the lock stayed busy\n")
            return 1
        if args.cmd == "status":
            snap = status()
            if args.json:
                sys.stdout.write(json.dumps(snap, sort_keys=True) + "\n")
            else:
                sys.stdout.write("leases=%d headroom=%s hold=%s age=%s swapout_rate=%s p90=%s (uncalibrated)\n" % (
                    len(snap["leases"]), snap["headroom"], snap["hold"]["state"], snap["hold"]["age"],
                    snap["swapout_rate"], json.dumps(snap["p90"], sort_keys=True)))
            return 0
        return wait(args.max_s, args.kind)
    except guard.Unsupported as refusal:
        sys.stderr.write("resource_lease: unsupported: %s\n" % refusal.reason)
        return guard.EXIT_UNSUPPORTED


if __name__ == "__main__":
    sys.exit(main())
