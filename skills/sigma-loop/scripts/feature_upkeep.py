"""The upkeep gate: the ONE reader of the `upkeep` block of the project config.

WHAT IT IS. A pure function over an already-loaded config dict (and an injectable environment mapping). It decides
whether the optional, automatic upkeep of long-lived feature branches may run at all. It does no I/O and spawns
nothing. So far two things read the block through it: the pick-time rebase pass of feature_rebase.py, which asks the
project door before it pushes a unit branch (to keep the old tip as a backup ref), and the restore and prune
commands of feature_backup.py; the rest of the block is read by later releases. A test proves a closed gate does
nothing. Projects scaffolded earlier never receive the block (the scaffold never overwrites an existing config);
they copy it in if they want it.

TOTAL. No input makes it raise: a missing or non-object config, a non-object block, a wrong type, NaN, infinity, a
huge integer, an unknown key or an oversized block all read CLOSED. A closed reading carries the DEFAULTS as its
settings, never a partially trusted merge.

STRICT. `enabled` opens the gate only as the JSON boolean true: the text "true", the text "yes" and the number 1 all
stay closed. Every number is a whole number inside a guard-rail range (a boolean is not a number). ONE invalid key, an
unknown key (one that does not start with an underscore), a section that is not an object, or a pair of settings that
contradicts itself closes the WHOLE block, and `problems` names the key path and the type found. A value is never
echoed (a typo that pastes a secret into the wrong key must not reach a log). An unknown KEY is echoed only as a
"did you mean" hint: when it is ASCII, at most 26 characters and within two edits of a known key name at that level.
Any other unknown key is described by its position and type, so a credential pasted in key position is never read back.

INTERFACE, the contract later slices consume: BLOCK, ENV_MACHINE, DOORS, SCHEMA, DEFAULTS, CROSS_CHECKS;
check(key, value); read(config) -> Reading(enabled, settings, problems); enabled(config);
machine_enabled(config, environ=None); evaluate(config, door="project", environ=None) -> a dict with open, door,
missing and problems; gated(door="project"), the decorator that makes the gate the first action of an entry point.
Settings are a flat dict keyed by dotted path (for example "auto.floor"), the same shape as DEFAULTS and SCHEMA.

THREE OPT-INS, all off by default. (1) The project: `upkeep.enabled` is the boolean true. (2) The machine: the
environment variable SIGMA_UPKEEP_JOB is exactly the text 1, and `ledger.enabled` is exactly true (restated here
because the ledger module's own reader raises on a truthy non-object block, and a gate must not). (3) Spend: not
yet. The "project" door needs (1); the "machine" door needs all of (1) and (2).

PRECEDENCE WITH THE OLDER SWITCH `work.rebase_upkeep` (ON by default; an unrecognised value reads ON)
The two switches are independent and have OPPOSITE typo rules: the older one fails open, this gate fails closed.
  Pick time       absent or not enabled: exactly today, `work.rebase_upkeep` decides.
                  enabled: today's trigger; `work.rebase_upkeep: "off"` still turns this door off.   (door "project")
  Scheduler       absent or not enabled: does not exist; no new subprocess, file or output.
                  enabled + machine variable + ledger: the decision step.                               (door "machine")
  A person's request (rebase now / land)
                  absent or not enabled: today's attended skill unchanged. enabled: the engine.         (door "project")
  Chat            absent or not enabled: unchanged. enabled: the engine.                               (door "project")

KEEP IT PATH-FREE. The module takes a dict, so it joins no filesystem path. A function here that did would be
an address builder for the unit-key fold inventory in the feature-registry tests and would need a classification
there; the tests pin that it has none (no division operator appears anywhere in this file).
"""
import collections
import functools
import inspect
import os

BLOCK = "upkeep"
ENV_MACHINE = "SIGMA_UPKEEP_JOB"
DOORS = ("project", "machine")

MAX_BLOCK_KEYS = 64
MAX_LIST_ITEMS = 200
MAX_NAME_CHARS = 200
MAX_HINT_KEY_CHARS = 26
MAX_NEAR_EDITS = 2

SCHEMA = {   # dotted key -> spec; the lo/hi are guard rails against typos, not tuning advice
    "enabled": {"kind": "bool"},
    "units.include": {"kind": "name_list", "min_items": 1},
    "units.exclude": {"kind": "name_list", "min_items": 0},
    "triggers.drift_merges": {"kind": "auto_int_null", "lo": 1, "hi": 1000},
    "triggers.every_hours": {"kind": "int", "lo": 1, "hi": 8760},
    "triggers.min_interval_minutes": {"kind": "int", "lo": 1, "hi": 10080},
    "triggers.dormant_days": {"kind": "int", "lo": 1, "hi": 3650},
    "triggers.dormant_every_hours": {"kind": "int", "lo": 1, "hi": 8760},
    "auto.window_days": {"kind": "int", "lo": 1, "hi": 365},
    "auto.burst_window_hours": {"kind": "int", "lo": 1, "hi": 720},
    "auto.target_hours": {"kind": "int", "lo": 1, "hi": 720},
    "auto.floor": {"kind": "int", "lo": 1, "hi": 1000},
    "auto.ceiling": {"kind": "int", "lo": 1, "hi": 1000},
    "verify.clean_rebase": {"kind": "bool"},
    "verify.timeout_minutes": {"kind": "int", "lo": 1, "hi": 1440},
    "backup.keep_days": {"kind": "int", "lo": 1, "hi": 3650},
    "backup.keep_last": {"kind": "int", "lo": 1, "hi": 1000},
    "conflicts.resolve": {"kind": "enum", "values": ("off", "mechanical", "agent")},
    "conflicts.mechanical_without_verify": {"kind": "bool"},
    "backup.former_prefixes": {"kind": "name_list", "min_items": 0},
}
DEFAULTS = {   # the one place the defaults live; the shipped template must equal this
    "enabled": False, "units.include": ["*"], "units.exclude": [], "triggers.drift_merges": "auto",
    "triggers.every_hours": 24, "triggers.min_interval_minutes": 120, "triggers.dormant_days": 14,
    "triggers.dormant_every_hours": 72, "auto.window_days": 21, "auto.burst_window_hours": 24,
    "auto.target_hours": 12, "auto.floor": 3, "auto.ceiling": 40, "verify.clean_rebase": False,
    "verify.timeout_minutes": 60, "backup.keep_days": 14, "backup.keep_last": 5,
    "conflicts.resolve": "off", "conflicts.mechanical_without_verify": False,
    "backup.former_prefixes": [],
}
CROSS_CHECKS = (   # run only after every per-key check, and only when the keys involved passed theirs
    (("auto.floor", "auto.ceiling"), lambda floor, ceiling: floor <= ceiling,
     "auto.floor must not exceed auto.ceiling"),
    (("triggers.min_interval_minutes", "triggers.every_hours", "triggers.dormant_every_hours"),
     lambda low, mid, high: low <= mid * 60 <= high * 60,
     "triggers must satisfy min_interval_minutes <= every_hours*60 <= dormant_every_hours*60"),
)

_ABSENT = object()
_BROKEN = object()
_SECTIONS = tuple(sorted({key.split(".")[0] for key in SCHEMA if "." in key}))

Reading = collections.namedtuple("Reading", "enabled settings problems")


def _tname(value):
    return type(value).__name__


def _edits(left, right):
    """Edit distance (insert, delete, replace) between two short texts, or None when it exceeds MAX_NEAR_EDITS."""
    if abs(len(left) - len(right)) > MAX_NEAR_EDITS:
        return None
    row = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        nxt = [i]
        for j, b in enumerate(right, 1):
            nxt.append(min(row[j] + 1, nxt[j - 1] + 1, row[j - 1] + (a != b)))
        row = nxt
    return row[-1] if row[-1] <= MAX_NEAR_EDITS else None


def _hint(key, known):
    """The known key name nearest to `key` within MAX_NEAR_EDITS edits, else None. Only an ASCII text key of at most
    MAX_HINT_KEY_CHARS characters is compared at all, so nothing else is ever echoed."""
    if not (isinstance(key, str) and key.isascii() and len(key) <= MAX_HINT_KEY_CHARS):
        return None
    best = None
    for name in sorted(known):
        distance = _edits(key, name)
        if distance is not None and (best is None or distance < best[0]):
            best = (distance, name)
    return None if best is None else best[1]


def _is_bool(value):
    return type(value) is bool


def _is_int(value, lo, hi):
    return type(value) is int and lo <= value <= hi


def check(key, value):
    """None when `value` is acceptable for the dotted `key`, else a short reason. Never raises; never echoes the value."""
    spec = SCHEMA.get(key) if isinstance(key, str) else None
    if spec is None:
        return "not a key the gate reads"
    kind = spec["kind"]
    if kind == "bool":
        if _is_bool(value):
            return None
        return "expected the JSON boolean true or false, got " + _tname(value)
    if kind == "enum":
        if type(value) is str and value in spec["values"]:
            return None
        return "expected one of the texts %s, got %s" % (", ".join(spec["values"]), _tname(value))
    if kind == "name_list":
        if (type(value) is list and spec["min_items"] <= len(value) <= MAX_LIST_ITEMS
                and all(type(item) is str and 0 < len(item) <= MAX_NAME_CHARS for item in value)):
            return None
        return "expected a list of %d to %d non-empty names, got %s" % (spec["min_items"], MAX_LIST_ITEMS, _tname(value))
    extra = ""
    if kind == "auto_int_null":
        if value is None or (type(value) is str and value == "auto"):
            return None
        extra = " (or the text auto, or null)"
    if _is_int(value, spec["lo"], spec["hi"]):
        return None
    return "expected a whole number from %d to %d%s, got %s" % (spec["lo"], spec["hi"], extra, _tname(value))


def _defaults():
    return {key: (list(value) if isinstance(value, list) else value) for key, value in DEFAULTS.items()}


def _unknown(label, mapping, known):
    found = []
    for position, key in enumerate(mapping, 1):
        if isinstance(key, str) and (key.startswith("_") or key in known):
            continue
        hint = _hint(key, known)
        if hint is not None:
            found.append("%s: unknown key %r (did you mean %r?)" % (label, key, hint))
        elif isinstance(key, str):
            found.append("%s: unknown key at position %d (text, %d characters)" % (label, position, len(key)))
        else:
            found.append("%s: unknown key at position %d (%s)" % (label, position, _tname(key)))
    return found


def read(config):
    """The whole decision, total. -> Reading(enabled, settings, problems). Closed readings carry DEFAULTS as settings."""
    block = config.get(BLOCK, _ABSENT) if isinstance(config, dict) else _ABSENT
    defaults = _defaults()
    if block is _ABSENT:
        return Reading(False, defaults, ())
    if not isinstance(block, dict):
        return Reading(False, defaults, ("upkeep: expected an object, got " + _tname(block),))
    if len(block) > MAX_BLOCK_KEYS:
        return Reading(False, defaults, ("upkeep: more than %d keys" % MAX_BLOCK_KEYS,))
    problems = _unknown("upkeep", block, {"enabled"} | set(_SECTIONS))
    sections = {}
    for name in _SECTIONS:
        got = block.get(name, _ABSENT)
        if got is _ABSENT or (isinstance(got, dict) and len(got) <= MAX_BLOCK_KEYS):
            sections[name] = got
            if got is not _ABSENT:
                known = {key.split(".")[1] for key in SCHEMA if key.startswith(name + ".")}
                problems += _unknown("upkeep." + name, got, known)
        else:
            sections[name] = _BROKEN
            problems.append("upkeep.%s: expected an object of at most %d keys, got %s" % (name, MAX_BLOCK_KEYS, _tname(got)))
    values, bad = {}, set()
    for key in SCHEMA:                                    # per-key checks: ALL of them, before any cross-check
        scope, _, name = key.rpartition(".")
        holder = sections[scope] if scope else block
        if holder is _BROKEN:
            bad.add(key)
            continue
        raw = _ABSENT if holder is _ABSENT else holder.get(name, _ABSENT)
        if raw is _ABSENT:
            values[key] = defaults[key]
            continue
        why = check(key, raw)
        if why:
            problems.append("upkeep.%s: %s" % (key, why))
            bad.add(key)
            continue
        values[key] = list(raw) if isinstance(raw, list) else raw
    for keys, fits, why in CROSS_CHECKS:
        if any(k in bad for k in keys):
            continue                                      # already reported; comparing would raise on a wrong type
        if not fits(*[values[k] for k in keys]):
            problems.append(why)
    if problems:
        return Reading(False, defaults, tuple(problems))
    return Reading(values["enabled"], values, ())          # validated to be a bool, so this IS the boolean


def enabled(config):
    """The project opt-in: True only for a valid block whose `enabled` is the boolean true."""
    return read(config).enabled


def _ledger_on(config):
    """`ledger.enabled` is exactly true. Restated here because the ledger module's own reader raises on a truthy
    non-object block, and a gate must not (it is pinned equal on well-formed input)."""
    ledger = config.get("ledger") if isinstance(config, dict) else None
    return isinstance(ledger, dict) and ledger.get("enabled") is True


def _env_on(environ):
    """The machine variable is exactly the text 1, read from the injected mapping else the process environment."""
    env = os.environ if environ is None else environ
    getter = getattr(env, "get", None)
    return callable(getter) and getter(ENV_MACHINE) == "1"


def evaluate(config, door="project", environ=None):
    """-> {"open": bool, "door": door, "missing": [...], "problems": [...]}. `missing` lists the absent opt-ins in the order
    project, machine, ledger; the machine door needs all three. A closed gate with `problems` is invalid, one without is simply off."""
    if door not in DOORS:
        raise ValueError("unknown door %r; expected one of %s" % (door, DOORS))
    reading = read(config)
    missing = [] if reading.enabled else ["project"]
    if door == "machine":
        if not _env_on(environ):
            missing.append("machine")
        if not _ledger_on(config):
            missing.append("ledger")
    return {"open": not missing, "door": door, "missing": missing, "problems": list(reading.problems)}


def conflict_level(config):
    """How far a unit conflict may be resolved: "off" (the default), "mechanical" or "agent". Anything the block does not
    say plainly, and any closed or invalid block, reads "off": the level is a setting of the project opt-in, never beside it."""
    reading = read(config)
    return reading.settings["conflicts.resolve"] if reading.enabled else "off"


def machine_enabled(config, environ=None):
    """The scheduler's question: project opt-in AND the machine variable AND the shared ledger switch."""
    return evaluate(config, "machine", environ)["open"]


def gated(door="project"):
    """Decorator for an entry point `fn(config, ...)`: the gate is its first action, and a closed gate returns
    {"closed": True, "door", "missing", "problems"} WITHOUT calling the body. Write it with parentheses and put it
    OUTERMOST (a static test enforces both). A machine-door entry point must declare `environ` as a keyword-only
    parameter, which is where the gate reads it from; any other shape is refused when the function is decorated, so an
    `environ` passed positionally can never be silently ignored."""
    if door not in DOORS:
        raise ValueError("unknown door %r; expected one of %s" % (door, DOORS))

    def decorate(fn):
        if door == "machine":
            parameter = inspect.signature(fn).parameters.get("environ")
            if parameter is None or parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
                raise ValueError("a machine-door entry point must declare environ as a keyword-only parameter: "
                                 + getattr(fn, "__name__", "?"))

        @functools.wraps(fn)
        def guarded(config, *args, **kwargs):
            verdict = evaluate(config, door, kwargs.get("environ") if door == "machine" else None)
            if not verdict["open"]:
                return {"closed": True, "door": door, "missing": verdict["missing"], "problems": verdict["problems"]}
            return fn(config, *args, **kwargs)
        guarded.upkeep_door = door
        return guarded
    return decorate
