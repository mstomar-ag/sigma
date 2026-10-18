"""Chain run width and the single-active-chain lock (#1590). Library only; no main.

WIDTH. `chain.parallel` (an int >= 2) lets one chain run claim several independent members in a cycle. It is
bounded by `parallel.goals.max_concurrent` and needs goal-level parallelism on (`parallel.goals.enabled is
True`, and `work.enabled`, the same two rules `loop.py next-batch` applies); otherwise the width is 1, which
is today's behaviour. Unset means 1.

READY SET. `ready_set` returns members with no open in-chain blocker, never one already in flight, at most
`width - in flight`, in the order given.

WIDENING. `widen_ok` refuses a width past 1 when the remaining `chain.max_usd` does not cover the goals that
would be in flight at the p90 cost per goal, or when there is a cap and no p90. The estimate is
`concurrent x p90` (PROVISIONAL).

LOCK. `.sdlc/state/chain-active.json` holds `{chain: {pid, heartbeat, pool}}`. `acquire` refuses a pool that
intersects another active chain's, and a count past `chain.max_active` (default 1). An entry is reclaimed
only when its pid is dead AND its heartbeat is older than `STALE_AFTER` seconds (PROVISIONAL: 900, well above
the default pick timeout, to be ratified by the owner), each clause alone being
insufficient: a dead pid with a fresh heartbeat may be a driver between cycles, a live pid with a stale
heartbeat is a hung driver a person should look at. Recovery of a file that cannot be read is manual and
named in the refusal. The read-modify-write runs under a `flock` on a sidecar file; without `fcntl` the lock
refuses loudly. Ceiling: one small JSON read and one `flock` per run cycle, at most `chain.max_active`
entries.
"""
import contextlib
import importlib.util
import json
import os
import pathlib
import time

try:
    import fcntl
except ImportError:         # pragma: no cover - not on the supported hosts
    fcntl = None

STALE_AFTER = 900
DEFAULT_GOALS_MAX_CONCURRENT = 3        # the value loop.py's own default names
LOCK_WAIT = 5.0
_HERE = pathlib.Path(__file__).resolve().parent


class ChainRefused(Exception):
    pass


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def parallel_set(config):
    """-> the configured `chain.parallel` when it is a whole number >= 2, else None."""
    block = (config or {}).get("chain")
    n = _int(block.get("parallel")) if isinstance(block, dict) else None
    return n if n is not None and n >= 2 else None


def max_active(config):
    block = (config or {}).get("chain")
    n = _int(block.get("max_active")) if isinstance(block, dict) else None
    return n if n is not None and n >= 1 else 1


def engaged(config):
    """True when this run uses the lock: `chain.parallel` or `chain.max_active` is configured."""
    block = (config or {}).get("chain")
    return isinstance(block, dict) and (parallel_set(config) is not None or block.get("max_active") is not None)


def width(config):
    """-> concurrent members a chain cycle may hold: 1 unless `chain.parallel` >= 2 AND goal-level parallelism
    is on; then the smaller of `chain.parallel` and `parallel.goals.max_concurrent`."""
    n = parallel_set(config)
    if n is None:
        return 1
    goals = ((config.get("parallel") or {}).get("goals")) or {}
    if goals.get("enabled") is not True or not (config.get("work") or {}).get("enabled"):
        return 1
    try:
        cap = int(goals.get("max_concurrent", DEFAULT_GOALS_MAX_CONCURRENT))
    except (TypeError, ValueError):
        cap = DEFAULT_GOALS_MAX_CONCURRENT
    return min(n, max(1, cap))


def ready_set(members, blockers, in_flight, width):
    """Members with no open in-chain blocker, none already in flight, at most `width - len(in_flight)`."""
    room = width - len(in_flight)
    if room <= 0:
        return []
    out = []
    for m in members:
        if m in in_flight or blockers.get(m):
            continue
        out.append(m)
        if len(out) == room:
            break
    return out


def widen_ok(remaining_cents, concurrent, p90):
    """False when `concurrent` goals (past 1) are not covered by the remaining cap at p90. `remaining_cents`
    None means no cap is set. A cap with an unknown p90 refuses."""
    if concurrent <= 1 or remaining_cents is None:
        return True
    if p90 is None:
        return False
    return concurrent * p90 <= remaining_cents


# -- the lock ----------------------------------------------------------------------------------------------

def _paths(sdlc_dir):
    base = pathlib.Path(sdlc_dir) / "state"
    return base / "chain-active.json", base / "chain-active.lock"


@contextlib.contextmanager
def _locked(sdlc_dir, wait=LOCK_WAIT):
    if fcntl is None:
        raise ChainRefused("the chain lock needs fcntl, which this platform does not have; "
                           "chain.parallel and chain.max_active are not supported here")
    path, lock = _paths(sdlc_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ChainRefused("the chain lock is busy (another run is inside it); try again")
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


def _read(sdlc_dir):
    path, _ = _paths(sdlc_dir)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ChainRefused("%s cannot be read; remove that file if no chain run is active" % path.name)
    return data


def _write(sdlc_dir, data):
    path, _ = _paths(sdlc_dir)
    state = _load_state()
    state.atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _load_state():
    spec = importlib.util.spec_from_file_location("state", _HERE / "state.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _pid_alive(pid):
    spec = importlib.util.spec_from_file_location("resource_lease", _HERE / "resource_lease.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.pid_alive(pid)


def _beat(entry):
    b = entry.get("heartbeat") if isinstance(entry, dict) else None
    return float(b) if isinstance(b, (int, float)) and not isinstance(b, bool) else None


def _stale(entry, now, stale_after):
    """Dead pid AND an old heartbeat. A missing or unreadable heartbeat is NOT old: it never reclaims."""
    beat = _beat(entry)
    if beat is None or now - beat <= stale_after:
        return False
    pid = entry.get("pid")
    return not _pid_alive(pid)


def active(sdlc_dir):
    """-> the recorded entries as stored (nothing reclaimed, nothing written)."""
    return _read(sdlc_dir)


def acquire(sdlc_dir, chain, pool, config, pid, now, lock_wait=LOCK_WAIT, stale_after=STALE_AFTER):
    """Take or refresh the lock for `chain` over `pool`. Raises `ChainRefused` naming the holder when a pool
    intersects or `chain.max_active` is reached; reclaims entries that are dead AND stale."""
    mine, pool = str(chain), sorted(int(x) for x in pool)
    with _locked(sdlc_dir, lock_wait):
        data = _read(sdlc_dir)
        data = {k: v for k, v in data.items() if k == mine or not _stale(v, now, stale_after)}
        others = {k: v for k, v in data.items() if k != mine}
        for k in sorted(others, key=lambda x: (len(x), x)):
            theirs = others[k].get("pool") if isinstance(others[k], dict) else None
            if set(pool) & set(theirs or ()):
                raise ChainRefused("chain #%s is running on an intersecting pool; nothing was started" % k)
        if len(others) + 1 > max_active(config):
            first = sorted(others, key=lambda x: (len(x), x))[0]
            raise ChainRefused("chain #%s is running and chain.max_active is %d; nothing was started"
                               % (first, max_active(config)))
        data[mine] = {"pid": pid, "heartbeat": now, "pool": pool}
        _write(sdlc_dir, data)


def release(sdlc_dir, chain, lock_wait=LOCK_WAIT):
    """Drop `chain`'s entry. Harmless when absent or when the file does not exist."""
    path, _ = _paths(sdlc_dir)
    if not path.exists():
        return
    with _locked(sdlc_dir, lock_wait):
        data = _read(sdlc_dir)
        if str(chain) in data:
            del data[str(chain)]
            _write(sdlc_dir, data)
