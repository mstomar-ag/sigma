"""The upkeep window: the one cheap question "has the sweep window passed on this machine?" (#1387).

Upkeep runs on a window (`upkeep.window_hours`, default 12). Whichever user or machine reaches an open window first starts ONE
sweep of every unit; a unit another person's sweep already brought current needs no work, because the engine reports it
`current` after a bounded fetch. This module holds only the per-machine stamp and the arithmetic over it.

STAMP. `state/upkeep/window.json`: `last_at` (when the last sweep was attempted or finished), `unreached` (units the last
sweep ran out of time before), `outcome`. It is written by the scheduler when a window opens (an attempt) and rewritten by
the sweep when it ends.

READ-ONLY CHECK. `is_open` reads one small file and does arithmetic: no git, no gh, no unit listing. A missing, unreadable or
future-dated stamp reads as OPEN (a clock set far ahead must not silence upkeep for good; the margin tolerates ordinary skew).
Stdlib only.
"""
import importlib.util
import json
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent
_LOADED = {}

STAMP_REL = "state/upkeep/window.json"
SCHEMA = "upkeep-window/1"
FUTURE_SKEW_SECONDS = 300
MAX_UNREACHED = 500
STALE_WINDOWS = 2                     # the doctor warns when the last sweep is older than this many windows


def _sibling(name):
    if name not in _LOADED:
        spec = importlib.util.spec_from_file_location(name, _HERE.joinpath(name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LOADED[name] = module
    return _LOADED[name]


def _path(sdlc_dir):
    return pathlib.Path(sdlc_dir).joinpath(*STAMP_REL.split("/"))


def hours_of(settings):
    value = settings.get("window_hours") if isinstance(settings, dict) else None
    return value if type(value) is int and value >= 1 else 12


def read(sdlc_dir):
    """-> the stamp dict, or None when absent, unreadable or malformed. Never raises."""
    try:
        text = _path(sdlc_dir).read_text(encoding="utf-8")
        doc = json.loads(text) if len(text) <= 65536 else None
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or type(doc.get("last_at")) is not int:
        return None
    return doc


def is_open(settings, sdlc_dir, now):
    """True when no usable stamp exists, the stamp is dated beyond the skew margin, or a whole window has passed."""
    doc = read(sdlc_dir)
    if doc is None:
        return True
    age = int(now) - doc["last_at"]
    if age < -FUTURE_SKEW_SECONDS:
        return True
    return age >= hours_of(settings) * 3600


def stamp(sdlc_dir, now, outcome, unreached=()):
    """Write the stamp atomically (symlink-refusing). -> bool; never raises."""
    doc = {"schema": SCHEMA, "last_at": int(now), "outcome": str(outcome)[:40],
           "unreached": [str(u)[:200] for u in list(unreached)[:MAX_UNREACHED]]}
    try:
        state = _sibling("state")
        state.refuse_symlinks(sdlc_dir, STAMP_REL, True)
        state.atomic_write_text(_path(sdlc_dir), json.dumps(doc, sort_keys=True))
        return True
    except Exception:                                       # noqa: BLE001 - an unwritable stamp is reported, not raised
        return False


def _span(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)


def describe(settings, sdlc_dir, now):
    """-> one reporting line: when the last sweep was and when the next window opens. Reads one file; nothing else."""
    doc = read(sdlc_dir)
    if doc is None:
        return "upkeep: no sweep yet on this machine; the window is open"
    age = int(now) - doc["last_at"]
    left = hours_of(settings) * 3600 - age
    if age < -FUTURE_SKEW_SECONDS or left <= 0:
        return "upkeep: last sweep %s ago; the window is open" % _span(age) if age >= 0 else "upkeep: last sweep is dated in the future; the window is open"
    return "upkeep: last sweep %s ago, next window opens in %s" % (_span(age), _span(left))


def stamp_age(settings, sdlc_dir, now):
    """-> (older_than_two_windows, age_seconds or None): True when a stamp exists and is older than STALE_WINDOWS windows."""
    doc = read(sdlc_dir)
    if doc is None:
        return False, None
    age = int(now) - doc["last_at"]
    return age > STALE_WINDOWS * hours_of(settings) * 3600, max(0, age)
