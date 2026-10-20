"""#1939: GitHub's GraphQL quota belongs to the ACCOUNT, not to one Sigma process.

Library only (no CLI). Three jobs, all stdlib:

1. Read a refusal. `parse_refusal` / `wait_seconds` take the text of a REFUSED response (`gh api -i`
   stdout, or gh's stderr) and read `X-RateLimit-Remaining`, `X-RateLimit-Reset` and `Retry-After` from
   it, plus the words "secondary"/"abuse". A wait is NEVER derived from `/rate_limit`: that counter can
   disagree with the response that actually refused the call (it reported thousands left while a
   secondary limit held the account). `run=` is accepted only so a test can prove it is never called.
2. Name the kind. A primary limit (hourly allowance spent, resets at a stated time) and a secondary
   limit (burst or concurrency abuse guard, `Retry-After` seconds) need different advice.
3. Share a budget. One bucket file, `gh-graphql-bucket.json`, under the user state folder
   (`resource_lease.state_dir()`: SIGMA_RESOURCE_DIR, else the XDG state home), outside every
   repository, serialised by the lease folder's `admit.lock`. Per account: refill one point per
   REFILL_SECONDS up to BURST (about 1800 points an hour when steady), plus `hold_until` written from
   the last refusal so every watcher on the machine stops together. Damage fails OPEN: a missing,
   corrupt or locked file never blocks a pick, because a budget that can stall the fleet is worse than
   the limit it guards (unmeasured against a live secondary limit; tests use fakes).
"""
import importlib.util
import json
import math
import os
import re
import time
from pathlib import Path

BUCKET_FILE = "gh-graphql-bucket.json"
BURST = 20                    # most points a quiet account banks
REFILL_SECONDS = 2.0          # one point per interval: 1800 points an hour at a steady rate
TICK_SHARE = 5                # points one watcher tick may spend (documented share; callers stay under it)
DEFAULT_SECONDARY = 60        # bounded wait when a secondary refusal carries no Retry-After
DEFAULT_PRIMARY = 300         # bounded wait when a primary refusal carries no reset header
MAX_WAIT = 3600               # no wait is ever longer than one hour
DEFAULT_ACCOUNT = "default"

_REMAINING = re.compile(r"(?im)^\s*x-ratelimit-remaining:\s*(\d+)")
_RESET = re.compile(r"(?im)^\s*x-ratelimit-reset:\s*(\d+)")
_RETRY = re.compile(r"(?im)^\s*retry-after:\s*(\d+)")
_SECONDARY = re.compile(r"secondary|abuse", re.I)
_PRIMARY = re.compile(r"rate limit|too many requests|http 429", re.I)


def _lease():
    spec = importlib.util.spec_from_file_location("resource_lease", Path(__file__).with_name("resource_lease.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_refusal(text, now=None):
    """-> {"kind": "secondary"|"primary"|None, "retry_at": epoch|None} from a refused response's text.

    Secondary: the wording says secondary/abuse, or `Retry-After` arrives while allowance remains.
    Primary: remaining 0 (or plain rate-limit wording) with no secondary tell; `retry_at` is the
    `X-RateLimit-Reset` epoch. `Retry-After` is relative, so it needs `now`."""
    text = str(text or "")
    now = time.time() if now is None else now
    rem, reset, retry = (_REMAINING.search(text), _RESET.search(text), _RETRY.search(text))
    remaining = int(rem.group(1)) if rem else None
    if _SECONDARY.search(text) or (retry and remaining != 0):
        kind = "secondary"
    elif remaining == 0 or _PRIMARY.search(text):
        kind = "primary"
    else:
        return {"kind": None, "retry_at": None}
    if retry:
        at = now + int(retry.group(1))
    elif reset:
        at = int(reset.group(1))
    else:
        at = None
    return {"kind": kind, "retry_at": at}


def wait_seconds(text, now=None, run=None):
    """Seconds to wait, read from the REFUSED response only: Retry-After, else the reset header, else a
    bounded default for the kind. 0 when `text` is not a rate refusal. `run` is never called."""
    now = time.time() if now is None else now
    got = parse_refusal(text, now)
    if got["kind"] is None:
        return 0
    if got["retry_at"] is None:
        return DEFAULT_SECONDARY if got["kind"] == "secondary" else DEFAULT_PRIMARY
    return int(min(MAX_WAIT, max(0, math.ceil(got["retry_at"] - now))))


def graphql_shaped(args):
    """Does `gh <args>` spend GraphQL points? `gh api graphql`, and the verbs gh itself drives through
    GraphQL (`project`, `issue`/`pr` view|list|edit|create|comment|close ...). `gh api <rest path>` does not."""
    args = [str(a) for a in args]
    if not args:
        return False
    if args[0] == "api":
        return len(args) > 1 and args[1] == "graphql"
    return args[0] in ("project", "issue", "pr")


def account_key(env=None):
    env = os.environ if env is None else env
    return (env.get("SIGMA_GH_ACCOUNT") or "").strip() or DEFAULT_ACCOUNT


def bucket_path(env=None):
    return os.path.join(_lease().state_dir(env), BUCKET_FILE)


def _read(path):
    try:
        with open(path, "r") as handle:
            data = json.loads(handle.read())
        return data if isinstance(data, dict) and isinstance(data.get("accounts"), dict) else {"accounts": {}}
    except (OSError, ValueError):
        return {"accounts": {}}


def _num(value, default):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _update(account, mutate, env=None):
    """Run `mutate(entry) -> result` on the account's entry under the lock; persist it. Fails open:
    any problem (no flock, busy lock, unwritable folder) returns None and changes nothing."""
    try:
        lease = _lease()
        directory = lease.state_dir(env)
        with lease._locked(directory) as got:
            if not got:
                return None
            path = os.path.join(directory, BUCKET_FILE)
            data = _read(path)
            entry = data["accounts"].get(account)
            entry = entry if isinstance(entry, dict) else {}
            result = mutate(entry)
            data["accounts"][account] = entry
            lease._atomic_write(path, data)
            return result
    except Exception:                                   # noqa: BLE001 - damage never blocks a pick
        return None


def acquire(account=None, now=None, cost=1, env=None):
    """-> (ok, wait_seconds). Takes `cost` points when the account is not held and has them."""
    account = account or account_key(env)
    now = time.time() if now is None else now

    def take(entry):
        hold = _num(entry.get("hold_until"), 0.0)
        if hold > now:
            return (False, int(math.ceil(hold - now)))
        last = _num(entry.get("updated"), now)
        tokens = min(float(BURST), _num(entry.get("tokens"), float(BURST)) + max(0.0, now - last) / REFILL_SECONDS)
        entry["updated"] = now
        if tokens >= cost:
            entry["tokens"] = tokens - cost
            return (True, 0)
        entry["tokens"] = tokens
        return (False, int(math.ceil((cost - tokens) * REFILL_SECONDS)))

    got = _update(account, take, env)
    return (True, 0) if got is None else got


def note_refusal(account, text, now=None, env=None):
    """Record a refusal: every caller on this machine is held until the response's own retry time."""
    account = account or account_key(env)
    now = time.time() if now is None else now
    got = parse_refusal(text, now)
    if got["kind"] is None:
        return None
    secs = wait_seconds(text, now)

    def write(entry):
        entry["hold_until"] = now + secs
        entry["kind"] = got["kind"]
        entry["updated"] = now
        entry.setdefault("tokens", 0.0)
        return got["kind"]

    return _update(account, write, env)


def held(account=None, now=None, env=None):
    """Seconds the account is still held by a recorded refusal (0 when free or unreadable)."""
    account = account or account_key(env)
    now = time.time() if now is None else now
    try:
        entry = _read(bucket_path(env))["accounts"].get(account) or {}
        return max(0, int(math.ceil(_num(entry.get("hold_until"), 0.0) - now)))
    except Exception:                                   # noqa: BLE001
        return 0


def held_kind(account=None, env=None):
    """The kind ('secondary'/'primary') of the recorded hold, or None."""
    account = account or account_key(env)
    try:
        return (_read(bucket_path(env))["accounts"].get(account) or {}).get("kind")
    except Exception:                                   # noqa: BLE001
        return None
