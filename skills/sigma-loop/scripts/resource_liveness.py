"""Resource guard liveness (#1604): is admission held, is the sampler alive, and who is told.

A library with no command line of its own; `resource_guard.py sample --record <sdlc_dir>` is its entry point and
the watcher tick calls that through run_with_timeout.py. It joins landed pieces and invents no field of its own:
`resource_guard` (Sample, derive, decide, hold.json) and `resource_lease` (last.json, the lease table, the
admit.lock). AGENTS.md LIVENESS: age is the tell, so every reader here reasons on a timestamp, never on an error.

  * `refresh(sdlc_dir, config)`: one sample, stale leases reclaimed under admit.lock, `last.json` stamped with the
    sample time (`at`), `hold.json` rewritten from `decide`, and (only when `ledger.enabled`) one ledger note when a
    hold starts and one when it ends, each rate limited to one per `hold_alert_minutes` (default 10, uncalibrated).
    `resource.mode` off returns None and touches nothing. An unsupported platform raises `Unsupported` and writes
    nothing.
  * `hold_view`, `snapshot`, `sample_age_ok`, `sample_stale_after`: read-only helpers the doctor rows use.

The unit cost a refresh decides on is one model session (`Limits.session_cost`): the smallest thing the loop admits.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resource_guard as guard  # noqa: E402
import resource_lease as lease  # noqa: E402

DEFAULT_ALERT_MINUTES = 10
NOTES_FILE = "resource-hold-notes.json"
_N_FLAG = re.compile(r"(?<![\w-])(?:-n|--numprocesses)(?:\s+|=)(\d+|auto)(?![\w.-])")


def block(config):
    value = config.get("resource") if isinstance(config, dict) else None
    return value if isinstance(value, dict) else {}


def effective_mode(config, env=None):
    """enforce | observe | off: `resource_guard.resolve_mode`, the one rule for every surface (#1759)."""
    return guard.resolve_mode(config, env)


def alert_minutes(config):
    value = guard.resolve_config(block(config) or None)[0]["hold_alert_minutes"]
    return value if value else DEFAULT_ALERT_MINUTES


def hold_view(env=None, now=None):
    """{"age", "reason", "resource"} while admission is held, else None. hold.json first, last.json when it is absent."""
    now = time.time() if now is None else now
    record, _ = guard.read_hold(guard.hold_path(env))
    if record.get("state") != "hold" or not guard._number(record.get("since")):
        if record.get("state") == "open" and record.get("since") is not None:
            return None
        last = lease._read_json(os.path.join(lease.state_dir(env), "last.json"), {})
        record = last if isinstance(last, dict) and last.get("state") == "hold" else None
    if not record or not guard._number(record.get("since")):
        return None
    return {"age": max(0.0, now - record["since"]), "reason": record.get("reason"), "resource": record.get("resource")}


def newest_heartbeat(sdlc_dir):
    """The newest managing-loop `last_seen` under <sdlc_dir>/state/heartbeat, or None."""
    newest = None
    directory = os.path.join(str(sdlc_dir), "state", "heartbeat")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    for name in names:
        if name.endswith(".json"):
            seen = lease._read_json(os.path.join(directory, name), {})
            seen = seen.get("last_seen") if isinstance(seen, dict) else None
            if guard._number(seen):
                newest = seen if newest is None else max(newest, seen)
    return newest


def sample_stale_after(config):
    """The watcher-derived staleness bound in seconds (sync.py owns the rule: 3 x interval, floored)."""
    import sync
    return sync.stale_after_seconds(sync.watch_interval_seconds(config if isinstance(config, dict) else {}))


def sample_age_ok(last_at, heartbeat, now, bound):
    """False only when a loop heartbeat is fresh (something is running) and the last sample is missing or older
    than `bound`. With no fresh heartbeat nothing is expected to sample, so an old sample is not a fault."""
    if heartbeat is None or now - heartbeat > bound:
        return True
    if not guard._number(last_at):
        return False
    return now - last_at <= bound


def snapshot(config, env=None, now=None, sampler=None):
    """Read-only facts for the doctor. Raises `Unsupported` from the sampler. No lock, no write, no reclaim."""
    now = time.time() if now is None else now
    sample = (sampler or lease.take_sample)()
    directory = lease.state_dir(env)
    found = lease._load_leases(directory)
    live = [r for _, r in found if not lease._stale(r, now)]
    limits = guard.derive(sample, block(config) or None)
    limits.leased = sum(r["est_mem"] for r in live)
    limits.unmaterialised = sum(lease.remainder(r["est_mem"], r.get("peak", 0)) for r in live)
    limits.headroom = limits.headroom_for(sample.available)
    last = lease._read_json(os.path.join(directory, "last.json"), {})
    last = last if isinstance(last, dict) else {}
    return {"sample": sample, "limits": limits, "live": len(live), "stale": len(found) - len(live),
            "hold": hold_view(env, now), "last_at": last.get("at") if guard._number(last.get("at")) else None}


def goal_cap(snap, config):
    """The most concurrent goals this machine supports: shares of memory, cores left after the reserve, max_goals."""
    limits, sample = snap["limits"], snap["sample"]
    caps = [max(1, int(limits.share_cap // max(1, limits.session_cost))), max(1, sample.cpu_count - limits.cpu_reserve)]
    if limits.max_goals:
        caps.append(limits.max_goals)
    return min(caps)


def verify_workers(snap):
    limits, sample = snap["limits"], snap["sample"]
    want = max(1, sample.cpu_count - limits.cpu_reserve)
    cap = limits.max_verify_workers
    return min(want, cap) if cap else want


def verify_pin(command):
    """The largest `-n <int>` (an int), or "auto" when only `-n auto` is pinned, else None."""
    found = _N_FLAG.findall(command or "")
    ints = [int(x) for x in found if x.isdigit()]
    if ints:
        return max(ints)
    return "auto" if found else None


# ------------------------------------------------------------------------------------------- refresh
def _stamps(sdlc_dir):
    path = os.path.join(str(sdlc_dir), "state", NOTES_FILE)
    data = lease._read_json(path, {})
    return path, data if isinstance(data, dict) else {}


def _note(sdlc_dir, config, kind, text, now, append):
    """One ledger note, only when `ledger.enabled`, at most one per `kind` per hold_alert_minutes."""
    import ledger
    if not ledger.enabled(config):
        return False
    path, stamps = _stamps(sdlc_dir)
    prior = stamps.get(kind)
    if guard._number(prior) and now - prior < alert_minutes(config) * 60:
        return False
    (append or ledger.safe_append)(str(sdlc_dir), "note", "resource-guard", config=config, why=text)
    stamps[kind] = now
    lease._atomic_write(path, stamps)
    return True


def refresh(sdlc_dir, config=None, env=None, now=None, append=None):
    """Sample, reclaim stale leases, stamp last.json and hold.json, note hold transitions. -> dict, or None when off."""
    config = config if isinstance(config, dict) else {}
    if effective_mode(config, env) == "off":
        return None
    now = time.time() if now is None else now
    directory = lease.state_dir(env)
    cfg = dict(block(config), mode=effective_mode(config, env))
    sample = lease.take_sample()                     # Unsupported propagates before anything is written
    with lease._locked(directory) as got:
        live = []
        if got:
            _, _, live, observed = lease._view(directory, now, cfg, reclaim=True)
        else:
            observed = {}
    limits = guard.derive(sample, cfg, [(r["est_mem"], False) for _, r in live], observed)
    previous = lease._read_json(os.path.join(directory, "last.json"), {})
    previous = previous if isinstance(previous, dict) else {}
    old, _ = guard.read_hold(guard.hold_path(env))
    history = []
    prev_sample = previous.get("sample")
    if isinstance(prev_sample, dict):
        try:
            history.append(guard.Sample(**prev_sample))
        except (TypeError, KeyError):
            pass
    history.append(sample)
    decision = guard.decide(history, limits, limits.session_cost, prior=old.get("state", "open"), now=now,
                            prior_since=old.get("since"))
    guard.write_hold(guard.hold_path(env), decision)
    held = decision.state == "hold"
    keep = previous.get("state") == ("hold" if held else "open") and guard._number(previous.get("since"))
    lease._atomic_write(os.path.join(directory, "last.json"), dict(
        previous, state="hold" if held else "open", reason=decision.reason if held else "ok", at=now,
        since=previous["since"] if keep else (decision.since if held else now),
        kind=previous.get("kind", "sample"), goal=previous.get("goal", ""), sample=sample.as_dict(),
        critical=bool(limits.critical)))
    guard.note_transition(directory, bool(previous.get("critical")), bool(limits.critical), now=now)   # #1914
    was = old.get("state") == "hold"
    if held and not was:
        _note(sdlc_dir, config, "start", "admission HELD: %s (%s)" % (decision.reason, decision.resource), now, append)
    elif was and not held:
        started = old.get("since")
        span = " after %dm" % ((now - started) // 60) if guard._number(started) else ""
        _note(sdlc_dir, config, "end", "admission open again%s" % span, now, append)
    return {"state": decision.state, "since": decision.since if held else None, "reason": decision.reason,
            "resource": decision.resource, "leases": len(live), "notes": limits.notes}
