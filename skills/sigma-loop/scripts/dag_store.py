"""The stored dependency map (#1610): `.sdlc/state/dag.json`, its schema, its lock and its off switch.

The map is a graph of goals and pull requests (`dag_core.py` is the pure side). This module is the
file: schema string `dag/1`, published with `state.atomic_write_text` (a crash leaves the whole old
file or the whole new one), gitignored through the existing `.sdlc/state/` entry.

What the file holds, and only this: a header (repo, `built_at`, `refreshed_at`, `last_ok_at`,
`last_error` as a class NAME, `partial`, the change-check `cursor`, `etag` and `checked_at`), the
nodes (number, kind, state, life, prio, in_corpus, updated_at, an optional local-only `unit` token)
and the edges (blocker, blocked, a source CODE). Every value is a number, a boolean, a timestamp or a
code from a closed set; `save` rebuilds the document from those fields alone, so a title, a label
text, an author or a comment phrase handed to it never reaches the disk. `load` applies the same
filter to what it reads.

`load(sdlc)` returns `(map, "ok")` or `(None, reason)` and never raises. The reasons are distinct:
`missing`, `truncated` (starts like the document, ends early or is empty), `corrupt` (anything else
unparseable or the wrong shape) and `schema` (parses, but is another schema). A damaged file is
never repaired in place: the next refresh overwrites it.

`refresh_lock(sdlc)` is a non-blocking `fcntl.flock`, as `sync.py`'s knowledge-channel lock: a
context manager yielding `held`, `busy` (a sibling refresher holds it; read the existing file and do
not wait) or `open` (no `fcntl`, fail open). The lock is released the instant the holder dies.

`usable(map, now, ttl_minutes)` returns `(ok, reason)`; the clauses are separate and each one is
tested on its own: the schema, built, not partial, and an age of at most twice the TTL (or a change
check within ten minutes). `now` is epoch seconds.

`mode(config)` reads `discovery.dag.mode`: `off`, `shadow` (default) or `on`. A missing or
unrecognised value, including a stray case such as `OFF`, reads as `shadow`: the direction that
never changes the picker. With mode `off` nothing here reads or writes anything: `load`, `save` and
`refresh_lock` return before touching the disk, and no state directory is created. `config` is an
optional argument of those three; left out, the project config is read from `sdlc`. A caller that
holds the config should pass it.

Scale: one file read and one parse per `load`, linear in nodes plus edges; at 10x (about 2,000
nodes) the file is a few hundred KB, at 100x a few MB. `save` rewrites the whole file, one writer at
a time by the lock, so the refresh is a serialised chokepoint by design. Nothing grows without bound:
the file is replaced, never appended to. Not measured here; the sizes are arithmetic.
"""
import calendar
import contextlib
import importlib.util
import json
import os
import pathlib
import re
import time

try:                    # POSIX only; without it the refresh lock fails OPEN, as sync.py's does
    import fcntl
except ImportError:
    fcntl = None

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


state = _load("state")                  # atomic_write_text, load_config

SCHEMA = "dag/1"
FILE = "dag.json"
LOCK = "dag.lock"
DEFAULT_TTL_MINUTES = 60
CHANGE_CHECK_SECONDS = 600              # a change check this recent keeps an old map usable
SKEW_SECONDS = 300                      # a stamp this far ahead of `now` is clock skew, not the future
MODES = ("off", "shadow", "on")
ORDERS = ("chain", "off")
LOAD_REASONS = ("ok", "missing", "truncated", "corrupt", "schema", "off")

# The closed vocabulary. Every string in the file is a key below, a member of one of these sets, a
# timestamp, the repo name, an opaque token (cursor/etag) or the unit token.
KINDS = ("issue", "pr")
STATES = ("open", "closed", "merged", "closed-unmerged", "unknown")
LIVES = ("goal", "in-progress", "blocked", "blocking", "parked", "needs-label", "needs-unit", "none", "unknown")
EDGE_SOURCES = ("body", "comment", "timeline", "other")
TOP_KEYS = ("schema", "header", "nodes", "edges")
HEADER_KEYS = ("repo", "built_at", "refreshed_at", "last_ok_at", "last_error", "partial", "cursor",
               "etag", "checked_at")
NODE_KEYS = ("number", "kind", "state", "life", "prio", "in_corpus", "updated_at", "unit")
EDGE_KEYS = ("blocker", "blocked", "source")
PRIOS = ("P0", "P1", "P2", "P3", "P4")
VOCAB = (SCHEMA,) + TOP_KEYS + HEADER_KEYS + NODE_KEYS + EDGE_KEYS + KINDS + STATES + LIVES + EDGE_SOURCES + PRIOS

_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")
_TOKEN = re.compile(r'^[A-Za-z0-9._:+/="-]{1,128}$')
_UNIT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


# ------------------------------------------------------------------ config

def _block(config):
    d = (config or {}).get("discovery") if isinstance(config, dict) else None
    b = d.get("dag") if isinstance(d, dict) else None
    return b if isinstance(b, dict) else {}


def _key(config, name):
    return _block(config).get(name)


def mode(config):
    """`off`, `shadow` or `on`; anything else (missing, `OFF`, a typo, a non-string) is `shadow`."""
    v = _key(config, "mode")
    return v if isinstance(v, str) and v in MODES else "shadow"


def ttl_minutes(config):
    v = _key(config, "ttl_minutes")
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else DEFAULT_TTL_MINUTES


def order(config):
    v = _key(config, "order")
    return v if isinstance(v, str) and v in ORDERS else "chain"


def cycle_comment(config):
    v = _key(config, "cycle_comment")
    return v if isinstance(v, bool) else True


def _mode_for(sdlc, config):
    if config is None:
        try:
            config = state.load_config(sdlc)
        except Exception:
            config = {}
    return mode(config)


# ------------------------------------------------------------------ paths and I/O seams

def path(sdlc):
    return pathlib.Path(sdlc) / "state" / FILE


def _read_text(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------ the closed-vocabulary filter

def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _ts(v):
    return v if isinstance(v, str) and _TS.match(v) else None


def _clean_header(h):
    out = {}
    if not isinstance(h, dict):
        return out
    for k in HEADER_KEYS:
        if k not in h:
            continue
        v = h[k]
        if k == "repo":
            v = v if isinstance(v, str) and _REPO.match(v) else None
        elif k in ("built_at", "refreshed_at", "last_ok_at", "checked_at"):
            v = _ts(v)
        elif k == "last_error":
            v = v if isinstance(v, str) and _CLASS.match(v) else None
        elif k == "partial":
            v = bool(v)
        else:                                   # cursor, etag
            v = v if isinstance(v, str) and _TOKEN.match(v) else None
        out[k] = v
    return out


def _clean_node(n):
    num = _int(n.get("number")) if isinstance(n, dict) else None
    if num is None:
        return None
    out = {"number": num}
    if "kind" in n:
        out["kind"] = n["kind"] if n["kind"] in KINDS else "issue"
    if "state" in n:
        out["state"] = n["state"] if n["state"] in STATES else "unknown"
    if "life" in n:
        out["life"] = n["life"] if n["life"] in LIVES else "unknown"
    if "prio" in n:
        out["prio"] = n["prio"] if n["prio"] in PRIOS else None
    if "in_corpus" in n:
        out["in_corpus"] = bool(n["in_corpus"])
    if "updated_at" in n:
        out["updated_at"] = _ts(n["updated_at"])
    if "read_at" in n:
        out["read_at"] = _ts(n["read_at"])
    u = n.get("unit")
    if isinstance(u, str) and _UNIT.match(u):
        out["unit"] = u
    return out


def _clean_edge(e):
    if not isinstance(e, dict):
        return None
    a, b = _int(e.get("blocker")), _int(e.get("blocked"))
    if a is None or b is None:
        return None
    src = e.get("source")
    return {"blocker": a, "blocked": b, "source": src if src in EDGE_SOURCES else "other"}


def clean(m):
    """The map rebuilt from the whitelisted fields alone (no schema check; see `load`)."""
    m = m if isinstance(m, dict) else {}
    nodes = [c for c in (_clean_node(n) for n in (m.get("nodes") or []) if isinstance(n, dict)) if c]
    edges = [c for c in (_clean_edge(e) for e in (m.get("edges") or [])) if c]
    return {"schema": m.get("schema"), "header": _clean_header(m.get("header")),
            "nodes": nodes, "edges": edges}


# ------------------------------------------------------------------ save / load

def save(sdlc, m, config=None):
    """Publish `m` atomically. Returns `(True, "ok")`, `(False, "off")` or `(False, "error")`."""
    if _mode_for(sdlc, config) == "off":
        return False, "off"
    try:
        doc = clean(m)
        doc["schema"] = SCHEMA
        p = path(sdlc)
        os.makedirs(p.parent, exist_ok=True)
        state.atomic_write_text(p, json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
        return True, "ok"
    except Exception:
        return False, "error"


def load(sdlc, config=None):
    """`(map, "ok")` or `(None, reason)`; never raises. Mode `off` reads nothing: `(None, "off")`."""
    if _mode_for(sdlc, config) == "off":
        return None, "off"
    p = path(sdlc)
    try:
        text = _read_text(p)
    except FileNotFoundError:
        return None, "missing"
    except Exception:
        return None, "corrupt"
    try:
        doc = json.loads(text)
    except ValueError:
        s = text.rstrip()
        return None, ("truncated" if (not s or (s.startswith("{") and not s.endswith("}"))) else "corrupt")
    if not isinstance(doc, dict):
        return None, "corrupt"
    if doc.get("schema") != SCHEMA:
        return None, "schema"
    if (not isinstance(doc.get("header"), dict) or not isinstance(doc.get("nodes"), list)
            or not isinstance(doc.get("edges"), list)):
        return None, "corrupt"
    return clean(doc), "ok"


# ------------------------------------------------------------------ the single-refresher lock

@contextlib.contextmanager
def refresh_lock(sdlc, config=None):
    """Yields `held`, `busy`, `open` (no fcntl or an unwritable state directory; fail open) or `off`
    (mode off; nothing touched)."""
    if _mode_for(sdlc, config) == "off":
        yield "off"
        return
    if fcntl is None:
        yield "open"
        return
    p = pathlib.Path(sdlc) / "state" / LOCK
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = open(p, "a+")
    except OSError:                             # unwritable state directory: read the map, do not raise
        yield "open"
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield "busy"
            return
        yield "held"
    finally:
        fd.close()                              # closing releases the flock


# ------------------------------------------------------------------ freshness

def parse_ts(v):
    """Epoch seconds for a `YYYY-MM-DDTHH:MM:SSZ` stamp, else None."""
    if not isinstance(v, str) or not _TS.match(v):
        return None
    try:
        return float(calendar.timegm(time.strptime(v, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, OverflowError):
        return None


def stamp(m):
    """The freshness stamp of a map: `last_ok_at`, else `built_at`; epoch seconds or None."""
    h = m.get("header") if isinstance(m, dict) else None
    if not isinstance(h, dict):
        return None
    t = parse_ts(h.get("last_ok_at"))
    return t if t is not None else parse_ts(h.get("built_at"))


def usable(m, now, ttl):
    """`(ok, reason)`. Clauses, each separate: schema, built, not partial, age <= 2 * ttl (minutes)
    or a change check within ten minutes. Reasons: ok, missing, corrupt, schema, not built, partial,
    future, stale."""
    if m is None:
        return False, "missing"
    if not isinstance(m, dict) or not isinstance(m.get("header"), dict):
        return False, "corrupt"
    h = m["header"]
    if m.get("schema") != SCHEMA:
        return False, "schema"
    if parse_ts(h.get("built_at")) is None:
        return False, "not built"
    if h.get("partial"):
        return False, "partial"
    t = stamp(m)
    age = now - t
    if age < -SKEW_SECONDS:
        return False, "future"
    if age > 2 * ttl * 60:
        c = parse_ts(h.get("checked_at"))
        if c is None or not (-SKEW_SECONDS <= now - c <= CHANGE_CHECK_SECONDS):
            return False, "stale"
    return True, "ok"
