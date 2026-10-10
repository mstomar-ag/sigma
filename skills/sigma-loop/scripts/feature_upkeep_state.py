"""Per-unit upkeep state and the due rule: when is a long-lived feature branch due for a maintenance pass?

LIBRARY ONLY, AND NOTHING CALLS IT YET. A later slice (the pass, then the scheduler) calls it behind the upkeep gate in
`feature_upkeep.py`; with that gate closed nothing here runs. It never reads the gate's block: the caller hands it the FLAT settings
of `feature_upkeep.read(config)` and `schedule` checks the three keys it uses with the gate's own `check`.

THE FILE. One small JSON document per unit, at the path `unit_state_path` returns (below the project's `.sdlc/state/` directory),
named by the unit's folded key (the same fold every other per-unit address uses). The path is ONE string constant joined with a
visible `/`, so the unit-key fold inventory in the feature-registry tests discovers it and demands its classification. The document
holds: when the last attempt and the last success happened (epoch seconds), the unit and base tips after the last success (the
ANCHOR the drift measure counts from), the last outcome, why the last attempt started, and a bounded count of consecutive failures.
Its schema id is `upkeep-unit/1`; any other id, a missing or extra field, a boolean where a number belongs, a name that does not
fold to the file's key, or more than MAX_STATE_CHARS characters reads as unreadable.

UNREADABLE MEANS OVERDUE, AND IS REPORTED. `read_state` answers ok, missing (never attempted: no cooldown, no success) or
unreadable with a reason word; `decide` turns unreadable into a due unit whose reason is `state-unreadable` and whose detail is
that word. READS NEVER WRITE: a read creates no directory and no file, on an unadopted project too. A stamp more than
FUTURE_SLACK_SECONDS ahead of `now` is unreadable (overdue, and the next record replaces it), because a cooldown measured from a
stamp in the future would not end for as long as the clock is behind it; a stamp inside the slack counts as `now`, so it extends a
cooldown or a backstop by at most the slack.

THE DUE RULE, in this order (`decide`):
  1. state unreadable                         -> due, `state-unreadable` (nothing is known, so no cooldown applies)
  2. last ATTEMPT less than the cooldown ago  -> not due, `cooldown` (a failed or parked pass restarts ONLY this clock)
  3. the unit is known to be level with base  -> not due, `not-behind`
  4. drift known and at or over the threshold -> due, `drift` (UNKNOWN drift, or a trigger that is off, never fires)
  5. no success yet, or last SUCCESS at least a backstop ago -> due, `backstop`; the longer dormant backstop for a unit with no
     arrival on its own side inside the dormant window (`backstop-dormant`). An UNKNOWN behind count counts as behind, an UNKNOWN
     dormancy as not dormant (the more frequent backstop).
  otherwise                                   -> not due, `within-backstop`.

THE PROTOCOL THAT KEEPS A UNIT THAT CANNOT BE RECORDED FROM RUNNING EVERY TICK. `claim` is the one call a pass or scheduler makes:
it reads, decides and, only when due, RECORDS THE ATTEMPT BEFORE THE WORK. `Claim.started` is true only when that record was
written; a caller starts a pass only when it is. A state directory that cannot be written (a read-only disk, a file where a
directory belongs, a symlink, a name too long for the file system) therefore starts nothing, and reports `state-unwritable` with
the cause; a corrupt or future-dated file is overwritten by the same record, so it heals itself. A crash between the record and the
outcome costs at most one cooldown. `claim` is not a lock: two processes can both read, decide and record in the same few
milliseconds and both start a pass, and the per-unit rebase lock the pass already takes is what excludes the second one (it
reports `busy`, a neutral outcome). Only the holder of that lock should call `record_outcome`. A record merges into the document it
read and never moves a time backwards (while the clock has not stepped back by more than the slack: beyond that the newer stamps
read as unreadable and the next record starts a fresh document), so a late writer cannot shorten a cooldown or a backstop; but a
whole-document replace can still lose a field that a concurrent writer set between the read and the rename (a success
overwritten by an older attempt record). The cost is one extra, cheap pass; the single-writer rule is the cure.

OUTCOMES. The sixteen outcomes of the rebase engine are held here as a closed set (a test compares it with the engine's own, so a
new engine outcome fails until it is classified): SUCCESS (rebased, current) resets the backstop and the failure count and records
the tips; FAILURE (occupied, unverifiable, direct-commits, conflict, lease-refused, failed, would-drop) adds one to the failure
count and leaves the backstop alone; NEUTRAL (disabled, no-unit, not-adopted, no-base, no-branch, remote-unreadable, busy) means
the pass did not judge the unit and changes nothing but the recorded outcome.

THE FIVE PROPERTIES. Reliability: every branch of the due rule and every unreadable shape is exercised, including the second either
side of each boundary, and each guard was seen red against a deliberately broken copy. Scalability: one small file per unit, found
by name (the directory is never listed), so the cost per unit does not grow with the number of units: 0.077 ms per read and 0.4 ms
per record over 1,000 units (one machine, macOS, where an fsync is cheap; a Linux disk will cost more per record, unmeasured);
281 bytes of content per document (measured, a 9-character unit name) and one file-system block (4 KiB) each, so 100 units and
10,000 units occupy about 400 KiB and 39 MiB (extrapolated, not measured). Nothing prunes the directory: the growth disposition
records it as intentionally unbounded, with the lever (delete a unit's file; the unit then reads as never attempted and is
re-evaluated, which is not an outage). Resiliency: the protocol above. Safety: no write primitive appears in this module; both
writes go through the shared helpers in `state.py`, which refuse a symlink anywhere on the path and publish with a same-directory
temporary file, an fsync and one rename; no value is echoed. Liveness: every refusal and every hold has a reason word, and a
unit whose state cannot be recorded is reported (`state-unwritable`) on every tick rather than silent.

DEFAULT NUMBERS AND WHERE THEY COME FROM. See `SOURCES`; a test pins that every number this module reads or holds has an entry.
"""
import collections
import importlib.util
import json
import numbers
import pathlib
import re

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_LOADED = {}


def _sibling(name):
    """A sibling module, loaded once per process (`_load` re-executes on every call and never caches)."""
    if name not in _LOADED:
        _LOADED[name] = _load(name)
    return _LOADED[name]


SCHEMA_ID = "upkeep-unit/1"
#: Where the files live, below the `.sdlc` directory. ONE constant on purpose: a join of three segments would make the middle one
#: a string constant equal to the gate's block name, which the "only the gate names the block" test refuses.
STATE_REL = "state/upkeep/units"
SUFFIX = ".json"
MAX_STATE_CHARS = 4096
FAILURES_CAP = 1000
FUTURE_SLACK_SECONDS = 300

READS = ("triggers.min_interval_minutes", "triggers.every_hours", "triggers.dormant_every_hours")

_DESIGN = ("design default; derived on the predecessor's history, which this repository does not hold, so it is a starting "
           "point and not a measurement made here")
SOURCES = {   # every number this module reads or holds -> where it comes from
    "triggers.min_interval_minutes": "120 minutes of cooldown after an attempt: " + _DESIGN,
    "triggers.every_hours": "24 hours of backstop after a success: " + _DESIGN,
    "triggers.dormant_every_hours": "72 hours of backstop for a dormant unit: " + _DESIGN,
    "MAX_STATE_CHARS": ("4096 characters read at most: a real document is 281 bytes (measured, a 9-character unit name and two "
                        "40-hex tips), so this is fourteen times that and a hand-planted large file cannot be read whole"),
    "FAILURES_CAP": "1000 consecutive failures counted at most: a bound on a number, not a measurement; nothing branches on it yet",
    "FUTURE_SLACK_SECONDS": ("300 seconds: a judgement and not a measurement; more than ordinary clock skew between machines, "
                             "so a stamp from a wrong clock is overwritten by the next record instead of holding a cooldown"),
}

#: The rebase engine's outcomes, classified. A test compares the union with the engine's own set.
SUCCESS = ("rebased", "current")
FAILURE = ("occupied", "unverifiable", "direct-commits", "conflict", "lease-refused", "failed", "would-drop")
NEUTRAL = ("disabled", "no-unit", "not-adopted", "no-base", "no-branch", "remote-unreadable", "busy")
OUTCOMES = SUCCESS + FAILURE + NEUTRAL
DUE_REASONS = ("drift", "backstop", "backstop-dormant", "state-unreadable")
HOLD_REASONS = ("cooldown", "not-behind", "within-backstop")
FIELDS = ("schema", "unit", "last_attempt_at", "last_success_at", "unit_tip", "base_tip", "outcome", "due_reason",
          "consecutive_failures")

_HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

Schedule = collections.namedtuple("Schedule", "cooldown backstop dormant_backstop")      # seconds
StateRead = collections.namedtuple("StateRead", "kind doc reason")                          # ok / missing / unreadable
WriteResult = collections.namedtuple("WriteResult", "ok reason")
Decision = collections.namedtuple("Decision", "due reason dormant detail")
Claim = collections.namedtuple("Claim", "started due reason detail")


def schedule(settings):
    """-> Schedule(cooldown, backstop, dormant_backstop) in seconds, from the flat settings of `feature_upkeep.read(config)`.
    The three keys go through the gate's own `check` and its ordering rule; a missing or invalid one raises ValueError naming
    the key (never the value). The caller owns whether the gate is open: a closed reading carries the defaults."""
    gate = _sibling("feature_upkeep")
    if not isinstance(settings, dict):
        raise ValueError("settings must be the flat dict of feature_upkeep.read")
    for key in READS:
        if key not in settings:
            raise ValueError("missing setting " + key)
        why = gate.check(key, settings[key])
        if why:
            raise ValueError("%s: %s" % (key, why))
    for keys, fits, why in gate.CROSS_CHECKS:
        if all(k in READS for k in keys) and not fits(*[settings[k] for k in keys]):
            raise ValueError(why)
    return Schedule(settings["triggers.min_interval_minutes"] * 60, settings["triggers.every_hours"] * 3600,
                    settings["triggers.dormant_every_hours"] * 3600)


def unit_state_path(sdlc_dir, name):
    """The one place a unit NAME becomes the path of its upkeep state file. Refuses what `feature_registry.unit_path` refuses
    (same predicate, same exception) and folds the name the same way, AFTER the refusal, so `Voice` and `voice` are one file."""
    registry = _sibling("feature_registry")
    if not (isinstance(name, str) and registry.is_unit_name(name)):
        raise registry.InvalidUnitName("%r is not a unit name, so it has no upkeep record" % (name,))
    return pathlib.Path(sdlc_dir) / STATE_REL / (registry.unit_key(name) + SUFFIX)


def _int(value, low=0):
    return type(value) is int and value >= low


def _now(now):
    if not _int(now):
        raise ValueError("now must be whole epoch seconds")


def _refuse_constant(_name):
    raise ValueError("not a JSON number")


def _no_duplicates(pairs):
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _problem(doc, key, now):
    """None when `doc` is a valid document for the file `key`, else a reason word. Never echoes a value."""
    registry = _sibling("feature_registry")
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA_ID:
        return "bad-schema"
    if set(doc) != set(FIELDS):
        return "bad-field"
    for field in ("last_attempt_at", "last_success_at"):
        value = doc[field]
        if value is not None and not _int(value):
            return "bad-field"
        if value is not None and value > now + FUTURE_SLACK_SECONDS:
            return "future-stamp"
    for field in ("unit_tip", "base_tip"):
        if doc[field] is not None and not (isinstance(doc[field], str) and _HEX.fullmatch(doc[field])):
            return "bad-field"
    if doc["outcome"] is not None and doc["outcome"] not in OUTCOMES:
        return "bad-field"
    if doc["due_reason"] is not None and doc["due_reason"] not in DUE_REASONS:
        return "bad-field"
    if not (_int(doc["consecutive_failures"]) and doc["consecutive_failures"] <= FAILURES_CAP):
        return "bad-field"
    unit = doc["unit"]
    if not (isinstance(unit, str) and registry.is_unit_name(unit) and registry.unit_key(unit) + SUFFIX == key):
        return "wrong-unit"
    return None


def read_state(sdlc_dir, name, now):
    """-> StateRead(kind, doc, reason). kind is `ok` (doc is valid), `missing` (no file: never attempted) or `unreadable` (reason
    is one of bad-unit, unsafe-path, io, too-big, not-json, bad-schema, bad-field, future-stamp, wrong-unit). Writes nothing."""
    _now(now)
    try:
        path = unit_state_path(sdlc_dir, name)
    except ValueError:
        return StateRead("unreadable", None, "bad-unit")
    state = _sibling("state")
    try:
        handle = state.safe_state_open(sdlc_dir, path.relative_to(pathlib.Path(sdlc_dir)), "r")
    except FileNotFoundError:
        return StateRead("missing", None, None)
    except state.UnsafeStatePath:
        return StateRead("unreadable", None, "unsafe-path")
    except OSError:
        return StateRead("unreadable", None, "io")
    try:
        with handle:
            text = handle.read(MAX_STATE_CHARS + 1)
    except (OSError, ValueError):
        return StateRead("unreadable", None, "io")
    if len(text) > MAX_STATE_CHARS:
        return StateRead("unreadable", None, "too-big")
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        return StateRead("unreadable", None, "not-json")
    why = _problem(doc, path.name, now)
    return StateRead("unreadable", None, why) if why else StateRead("ok", doc, None)


def _fresh(name):
    return {"schema": SCHEMA_ID, "unit": name, "last_attempt_at": None, "last_success_at": None, "unit_tip": None,
            "base_tip": None, "outcome": None, "due_reason": None, "consecutive_failures": 0}


def _later(old, new):
    return new if old is None else max(old, new)


def _publish(sdlc_dir, name, doc):
    """The one write: refuse a symlink anywhere on the path (creating missing directories one level at a time), then publish
    through the shared atomic helper. -> WriteResult. A failure of the file system is `unwritable`, never an exception."""
    state = _sibling("state")
    try:
        path = unit_state_path(sdlc_dir, name)
    except ValueError:
        return WriteResult(False, "bad-unit")
    try:
        state.refuse_symlinks(sdlc_dir, path.relative_to(pathlib.Path(sdlc_dir)), create_parents=True)
        state.atomic_write_text(path, json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError:
        return WriteResult(False, "unwritable")
    return WriteResult(True, None)


def _record_attempt(sdlc_dir, name, now, reason, read):
    doc = dict(read.doc) if read.kind == "ok" else _fresh(name)
    doc["unit"] = name
    doc["last_attempt_at"] = _later(doc["last_attempt_at"], now)
    doc["due_reason"] = reason
    return _publish(sdlc_dir, name, doc)


def record_attempt(sdlc_dir, name, now, reason):
    """Record that a pass is about to start, BEFORE it does. Restarts the cooldown; touches nothing else except why it started.
    An unreadable existing file is replaced by a fresh document (it heals). -> WriteResult; start the pass only when `ok`."""
    _now(now)
    if reason not in DUE_REASONS:
        raise ValueError("reason must be one of " + ", ".join(DUE_REASONS))
    return _record_attempt(sdlc_dir, name, now, reason, read_state(sdlc_dir, name, now))


def record_outcome(sdlc_dir, name, now, outcome, unit_tip=None, base_tip=None):
    """Record how a pass ended. SUCCESS resets the backstop (`last_success_at`) and the failure count and stores the tips given
    (the unit tip is the anchor the drift measure counts from: pass the tip AFTER the rebase); FAILURE adds one to the failure
    count; NEUTRAL records only the outcome. The attempt time is not touched. -> WriteResult."""
    _now(now)
    if outcome not in OUTCOMES:
        raise ValueError("outcome must be one of the engine's sixteen: " + ", ".join(OUTCOMES))
    for tip in (unit_tip, base_tip):
        if tip is not None and not (isinstance(tip, str) and _HEX.fullmatch(tip)):
            raise ValueError("a tip is a lowercase hex object name or None")
    read = read_state(sdlc_dir, name, now)
    doc = dict(read.doc) if read.kind == "ok" else _fresh(name)
    doc["unit"] = name
    doc["outcome"] = outcome
    if outcome in SUCCESS:
        doc["last_success_at"] = _later(doc["last_success_at"], now)
        doc["consecutive_failures"] = 0
        doc["unit_tip"] = unit_tip if unit_tip is not None else doc["unit_tip"]
        doc["base_tip"] = base_tip if base_tip is not None else doc["base_tip"]
    elif outcome in FAILURE:
        doc["consecutive_failures"] = min(doc["consecutive_failures"] + 1, FAILURES_CAP)
    return _publish(sdlc_dir, name, doc)


def _number(value):
    return isinstance(value, numbers.Rational) and type(value) is not bool


def decide(sched, read, drift, now):
    """The due rule, pure. `read` is a StateRead; `drift` is anything with `behind`, `drift`, `threshold` and `dormant`
    attributes (a feature_upkeep_drift.Drift): a value that is not a whole number (or a bool, for dormant) is UNKNOWN.
    -> Decision(due, reason, dormant, detail)."""
    _now(now)
    dormant = drift.dormant is True
    if read.kind == "unreadable":
        return Decision(True, "state-unreadable", dormant, read.reason)
    doc = read.doc or _fresh("")
    attempt, success = doc["last_attempt_at"], doc["last_success_at"]
    if attempt is not None and max(0, now - attempt) < sched.cooldown:
        return Decision(False, "cooldown", dormant, None)
    if type(drift.behind) is int and drift.behind == 0:
        return Decision(False, "not-behind", dormant, None)
    if type(drift.drift) is int and _number(drift.threshold) and drift.drift >= drift.threshold:
        return Decision(True, "drift", dormant, None)
    backstop = sched.dormant_backstop if dormant else sched.backstop
    if success is None or max(0, now - success) >= backstop:
        return Decision(True, "backstop-dormant" if dormant else "backstop", dormant, None)
    return Decision(False, "within-backstop", dormant, None)


def claim(sdlc_dir, name, sched, drift, now):
    """Read, decide and, when due, record the attempt BEFORE the work -> Claim(started, due, reason, detail). Start a pass only
    when `started`. `due` with `started` false means the attempt could not be recorded (`state-unwritable`, detail is the
    cause), so the pass must not run: a unit whose state cannot be written is reported every tick and started never."""
    read = read_state(sdlc_dir, name, now)
    verdict = decide(sched, read, drift, now)
    if not verdict.due:
        return Claim(False, False, verdict.reason, verdict.detail)
    wrote = _record_attempt(sdlc_dir, name, now, verdict.reason, read)
    if not wrote.ok:
        return Claim(False, True, "state-unwritable", wrote.reason)
    return Claim(True, True, verdict.reason, verdict.detail)
