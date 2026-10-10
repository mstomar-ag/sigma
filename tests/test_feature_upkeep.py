"""#917: the upkeep gate (the feature_upkeep module of the loop scripts), proven end to end.

PLANNED TESTS of the pull-request red-to-green gate. Every test in this file is listed in the plan, is silent,
unparametrised, and starts by calling a `upkeep_support` helper that asserts the thing under test exists, so that
before the gate is written each one fails by AssertionError. The file is bound by its whole-file hash: it is not
edited after the red verify. Tests that are green before the gate exists live in `test_upkeep_harness.py`."""
import ast
import functools
import importlib
import json
import math
import random

import pytest

import upkeep_support as support

OPEN_ENV = support.OPEN_ENV

EXPECTED_SCHEMA = {   # key -> (kind, lo, hi, min_items)
    "enabled": ("bool", None, None, None),
    "units.include": ("name_list", None, None, 1),
    "units.exclude": ("name_list", None, None, 0),
    "triggers.drift_merges": ("auto_int_null", 1, 1000, None),
    "triggers.every_hours": ("int", 1, 8760, None),
    "triggers.min_interval_minutes": ("int", 1, 10080, None),
    "triggers.dormant_days": ("int", 1, 3650, None),
    "triggers.dormant_every_hours": ("int", 1, 8760, None),
    "auto.window_days": ("int", 1, 365, None),
    "auto.burst_window_hours": ("int", 1, 720, None),
    "auto.target_hours": ("int", 1, 720, None),
    "auto.floor": ("int", 1, 1000, None),
    "auto.ceiling": ("int", 1, 1000, None),
    "verify.clean_rebase": ("bool", None, None, None),
    "verify.timeout_minutes": ("int", 1, 1440, None),
    "backup.keep_days": ("int", 1, 3650, None),
    "backup.keep_last": ("int", 1, 1000, None),
    "conflicts.resolve": ("enum", None, None, None),
    "conflicts.mechanical_without_verify": ("bool", None, None, None),
    "backup.former_prefixes": ("name_list", None, None, 0),
}
DEFAULT_TABLE = {
    "enabled": False, "units.include": ["*"], "units.exclude": [], "triggers.drift_merges": "auto",
    "triggers.every_hours": 24, "triggers.min_interval_minutes": 120, "triggers.dormant_days": 14,
    "triggers.dormant_every_hours": 72, "auto.window_days": 21, "auto.burst_window_hours": 24,
    "auto.target_hours": 12, "auto.floor": 3, "auto.ceiling": 40, "verify.clean_rebase": False,
    "verify.timeout_minutes": 60, "backup.keep_days": 14, "backup.keep_last": 5,
    "conflicts.resolve": "off", "conflicts.mechanical_without_verify": False,
    "backup.former_prefixes": [],
}
#: The acceptance matrix (None, True, 0, -1, 1.5, nan, inf, "", "x", [], [1], {}, {"x": 1}) plus the values that
#: are real traps: False, 1, the text forms of a boolean, -inf, "auto", a name list, a 400-digit integer.
BAD_VALUES = [None, True, False, 0, 1, -1, 1.5, float("nan"), float("inf"), float("-inf"), "", "x", "true",
              "yes", "auto", [], [1], ["a"], ["*"], {}, {"x": 1}, 10 ** 400]
SECTIONS = ("units", "triggers", "auto", "verify", "backup", "conflicts")


def valid_value(kind, lo, hi, min_items, v):          # the oracle, independent of the module
    if kind == "bool":
        return type(v) is bool
    if kind == "enum":
        return type(v) is str and v in ("off", "mechanical", "agent")
    if kind == "name_list":
        return type(v) is list and len(v) >= min_items and all(type(x) is str and x for x in v)
    if kind == "auto_int_null" and (v is None or (type(v) is str and v == "auto")):
        return True
    return type(v) is int and lo <= v <= hi


def cross_ok(eff):                                    # the cross-checks, restated independently
    return (eff["auto.floor"] <= eff["auto.ceiling"]
            and eff["triggers.min_interval_minutes"] <= eff["triggers.every_hours"] * 60
            <= eff["triggers.dormant_every_hours"] * 60)


def block_for(key, v, flag):
    block = {"enabled": flag}
    if key == "enabled":
        block["enabled"] = v
    else:
        head, _, tail = key.rpartition(".")
        block[head] = {tail: v}
    return {"upkeep": block}


def expected_clean(key, v):                           # value valid AND cross-checks hold
    eff = dict(DEFAULT_TABLE)
    eff[key] = v
    return valid_value(*EXPECTED_SCHEMA[key], v) and cross_ok(eff)


def matrix_failures(g):
    wrong = []
    for key in sorted(g.SCHEMA):
        for v in BAD_VALUES:
            for flag in (True, False):
                config = block_for(key, v, flag)
                machine = dict(config, ledger={"enabled": True})
                try:
                    got = g.enabled(config)
                    got_machine = g.machine_enabled(machine, OPEN_ENV)
                    reading = g.read(config)
                except Exception as exc:                      # a raise is the finding
                    wrong.append((key, repr(v)[:24], flag, "raised " + type(exc).__name__))
                    continue
                clean = expected_clean(key, v)
                want = clean and (v is True if key == "enabled" else flag)
                if got is not want or got_machine is not want:
                    wrong.append((key, repr(v)[:24], flag, "opened=%r machine=%r want=%r" % (got, got_machine, want)))
                if bool(reading.problems) == clean:
                    wrong.append((key, repr(v)[:24], flag, "problems=%r but clean=%r" % (reading.problems, clean)))
    return wrong


def edge_failures(g):
    wrong = []
    for key, (kind, lo, hi, min_items) in EXPECTED_SCHEMA.items():
        cases = []
        if kind in ("int", "auto_int_null"):
            cases += [(lo - 1, False), (lo, True), (hi, True), (hi + 1, False)]
        if kind == "auto_int_null":
            cases += [("auto", True), (None, True), ("autoo", False), ("AUTO", False), (" auto", False)]
        if kind == "name_list":
            cases += [(["a"] * 200, True), (["a"] * 201, False), ([""], False), (["a" * 200], True),
                      (["a" * 201], False), (["a"] * min_items, True)]
            if min_items:
                cases.append(([], False))
            else:
                cases.append(([], True))
        for value, ok in cases:
            if (g.check(key, value) is None) != ok:
                wrong.append((key, repr(value)[:20], ok))
    return wrong


def blk(**sections):
    return {"upkeep": dict({"enabled": True}, **sections)}


def assert_enabled_is_strict(g):
    assert g.enabled({"upkeep": {"enabled": True}}) is True
    for v in (False, None, 0, 1, 1.0, 2, -1, "true", "True", "TRUE", "yes", "on", "1", "", " true", [True], {"x": True}, 10 ** 400):
        assert g.enabled({"upkeep": {"enabled": v}}) is False, repr(v)[:30]
    assert g.enabled({"upkeep": {}}) is False and g.enabled({}) is False


STRICT = "return type(value) is bool"
STRICT_MUTANTS = ["return bool(value)", "return value == True or value == False", "return isinstance(value, int)",
                  "return value in (True, False, 1, 0)", "return True"]


# ------------------------------------------------------------------------------------------ schema and defaults

def test_schema_is_locked():
    """The schema is exactly the 20 keys the gate reads, with the documented kinds and bounds."""
    g = support.gate()
    got = {k: (s["kind"], s.get("lo"), s.get("hi"), s.get("min_items")) for k, s in g.SCHEMA.items()}
    assert got == EXPECTED_SCHEMA
    assert {k.split(".")[0] for k in g.SCHEMA if "." in k} == set(SECTIONS)
    assert [k for k in g.SCHEMA if "." not in k] == ["enabled"]


def test_defaults_pinned():
    """DEFAULTS are the documented defaults, valid, mutation-safe, and a closed reading never carries a partial merge."""
    g = support.gate()
    assert g.DEFAULTS == DEFAULT_TABLE
    assert [k for k, v in g.DEFAULTS.items() if g.check(k, v)] == []
    assert cross_ok(g.DEFAULTS)
    first = g.read({}).settings
    first["units.include"].append("mutated")
    assert g.DEFAULTS["units.include"] == ["*"] and g.read({}).settings["units.include"] == ["*"]
    closed = g.read({"upkeep": {"enabled": True, "auto": {"floor": "x", "ceiling": 5}}})
    assert closed.enabled is False and closed.problems and closed.settings == g.DEFAULTS
    merged = g.read({"upkeep": {"enabled": True, "auto": {"floor": 2}}})
    assert merged.enabled is True and merged.problems == ()
    assert merged.settings == dict(DEFAULT_TABLE, enabled=True, **{"auto.floor": 2})


def test_range_edges():
    """Every bound is inclusive, one past either edge is refused, and the name lists are capped."""
    g = support.gate()
    assert edge_failures(g) == []


def test_bad_numbers():
    """A boolean, fraction, NaN, infinity, text or huge integer is never a number; an unknown key never raises."""
    g = support.gate()
    wrong = []
    for key, (kind, *_rest) in EXPECTED_SCHEMA.items():
        if kind in ("int", "auto_int_null"):
            for v in (True, False, 1.0, 2.0, math.nan, math.inf, "5", "1", 10 ** 400, [], {}):
                if g.check(key, v) is None:
                    wrong.append((key, repr(v)[:20]))
    assert wrong == []
    assert g.check("not.a.key", 1) and g.check(["list"], 1) and g.check(None, 1)


def test_matrix_no_raise():
    """The acceptance matrix: every key, every bad value, enabled true and false, project and machine door."""
    g = support.gate()
    assert matrix_failures(g) == []


def test_bad_blocks_closed():
    """A bad config, a bad block, or an oversized block reads closed and never raises."""
    g = support.gate()
    for top in (None, True, 1, "x", [], [1], {}, 1.5, math.nan):
        assert g.read(top).enabled is False and g.read(top).problems == ()
        reading = g.read({"upkeep": top})
        assert reading.enabled is False and reading.settings == g.DEFAULTS
        if isinstance(top, dict):
            assert reading.problems == ()
        else:
            assert len(reading.problems) == 1 and reading.problems[0].startswith("upkeep: expected an object")

    class NoIteration(dict):
        def __iter__(self):
            raise RuntimeError("an oversized block must be refused before it is walked")
    big = NoIteration({"k%d" % i: 1 for i in range(65)})
    reading = g.read({"upkeep": big})
    assert reading.enabled is False and len(reading.problems) == 1 and "more than 64 keys" in reading.problems[0]


def test_json_edge_values():
    """NaN, Infinity, 1e999 and a 400-digit integer, as json.loads really produces them, read closed."""
    g = support.gate()
    wrong = []
    for literal in ("NaN", "Infinity", "-Infinity", "1e999", "1" + "0" * 399):
        config = json.loads('{"upkeep": {"enabled": true, "auto": {"floor": %s}}}' % literal)
        reading = g.read(config)
        if reading.enabled or not any(p.startswith("upkeep.auto.floor") for p in reading.problems):
            wrong.append(literal[:12])
    assert wrong == []


def _fuzz_value(rng, depth=0):
    keys = list(SECTIONS) + ["enabled", "flor", "_note", "floor", "ceiling", "include", "exclude", 1, None]
    roll = rng.random()
    if depth < 3 and roll < 0.25:
        return {rng.choice(keys): _fuzz_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}
    if depth < 3 and roll < 0.4:
        return [_fuzz_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return rng.choice(BAD_VALUES + [5, 24, 3, "auto", ["x"]])


def test_seeded_fuzz():
    """3000 seeded nested configs: nothing raises, and every OPEN reading is clean and fully valid."""
    g = support.gate()
    rng = random.Random(917)
    opened = 0
    for n in range(3000):
        if n % 2:
            block = _fuzz_value(rng)
        else:
            block = {"enabled": True}
            for _ in range(rng.randint(0, 2)):
                block[rng.choice(SECTIONS)] = {rng.choice(["floor", "ceiling", "include", "keep_last", "flor"]): _fuzz_value(rng, 2)}
        config = {"upkeep": block}
        if rng.random() < 0.5:
            config["ledger"] = _fuzz_value(rng, 2)
        reading = g.read(config)
        g.evaluate(config, "machine", OPEN_ENV)
        if g.enabled(config):
            opened += 1
            assert reading.problems == () and all(g.check(k, v) is None for k, v in reading.settings.items())
        elif reading.problems:
            assert reading.settings == g.DEFAULTS
    assert opened >= 100, "the fuzz must reach the open branch, or its implication is vacuous"


# ------------------------------------------------------------------------------------------ cross-checks, unknown keys

def test_cross_checks_fire():
    """Each cross-check closes a block whose keys are individually valid; the equal edges are open."""
    g = support.gate()
    closed = {
        "floor above ceiling": blk(auto={"floor": 10, "ceiling": 5}),
        "min interval above every_hours": blk(triggers={"min_interval_minutes": 2000}),
        "every_hours above dormant": blk(triggers={"every_hours": 100}),
        "every_hours below min interval": blk(triggers={"every_hours": 1}),
        "dormant below every_hours": blk(triggers={"dormant_every_hours": 10}),
    }
    open_ = {
        "floor equals ceiling": blk(auto={"floor": 5, "ceiling": 5}),
        "min interval equals every_hours": blk(triggers={"min_interval_minutes": 1440}),
        "every_hours equals dormant": blk(triggers={"every_hours": 72}),
    }
    wrong = [name for name, c in closed.items() if g.enabled(c) or not g.read(c).problems]
    wrong += [name for name, c in open_.items() if not g.enabled(c)]
    assert wrong == []
    assert any("auto.floor must not exceed" in p for p in g.read(closed["floor above ceiling"]).problems)


def test_cross_checks_total():
    """Over every pair and triple of bad values the cross-checks never raise and open only when all is valid."""
    g = support.gate()
    wrong = []
    for a in BAD_VALUES:
        for b in BAD_VALUES:
            c = blk(auto={"floor": a, "ceiling": b})
            eff = dict(DEFAULT_TABLE, **{"auto.floor": a, "auto.ceiling": b})
            ok = (valid_value(*EXPECTED_SCHEMA["auto.floor"], a) and valid_value(*EXPECTED_SCHEMA["auto.ceiling"], b)
                  and a <= b)
            if g.enabled(c) is not ok:
                wrong.append(("floor/ceiling", repr(a)[:12], repr(b)[:12]))
    keys = ("min_interval_minutes", "every_hours", "dormant_every_hours")
    for a in BAD_VALUES:
        for b in BAD_VALUES:
            for c3 in BAD_VALUES:
                c = blk(triggers=dict(zip(keys, (a, b, c3))))
                ok = (all(valid_value(*EXPECTED_SCHEMA["triggers." + k], v) for k, v in zip(keys, (a, b, c3)))
                      and a <= b * 60 <= c3 * 60)
                if g.enabled(c) is not ok:
                    wrong.append(("triggers", repr(a)[:12], repr(b)[:12], repr(c3)[:12]))
    assert wrong == []


def test_unknown_keys_close():
    """An unknown key closes the whole block; an underscore note does not; a section that is not an object closes."""
    g = support.gate()
    for bad in (blk(flor=3), blk(auto={"flor": 3}), {"upkeep": {"enabled": True, 1: 2}}, blk(auto={2: 3})):
        reading = g.read(bad)
        assert reading.enabled is False and any("unknown key" in p for p in reading.problems), bad
    for note in ("text", {"a": 1}, None):
        assert g.enabled({"upkeep": {"enabled": True, "_note": note, "auto": {"_note": note, "floor": 2}}}) is True
    for bad in (blk(units=[]), blk(auto=None), blk(backup="x")):
        reading = g.read(bad)
        assert reading.enabled is False and reading.problems, bad


def test_problems_no_echo():
    """`problems` names key paths and type names; it never echoes a value, and echoes an unknown key only as a near-miss hint."""
    g = support.gate()
    sentinel = "ECHO-SENTINEL-917"
    leaks, unnamed = [], []
    for key in g.SCHEMA:
        for value in (sentinel, [sentinel], {sentinel: sentinel}):
            config = block_for(key, value, True)
            if key in ("units.include", "units.exclude") and value == [sentinel]:
                continue                                  # a valid list of one name: no problem to read
            reading = g.read(config)
            if any(sentinel in p for p in reading.problems):
                leaks.append((key, repr(value)[:12]))
            if not any(("upkeep." + key) in p for p in reading.problems) and not valid_value(*EXPECTED_SCHEMA[key], value):
                unnamed.append((key, repr(value)[:12]))
    assert leaks == [] and unnamed == []
    credential = "Zq7Lm2Vx9Rt4Wk8Hd1Pa"                         # identifier-shaped, 20 characters, near no known key
    for odd in (sentinel, "x" * 30, "floer" + chr(246), "a b", credential, "floorxyz"):   # never echoed
        for config in ({"upkeep": {odd: 1}}, {"upkeep": {"auto": {odd: 1}}}):
            problems = g.read(config).problems
            assert len(problems) == 1 and "at position 1 (text, %d characters)" % len(odd) in problems[0], problems
            assert odd not in problems[0] and odd[:8] not in problems[0], problems
    near = {"upkeep": {"auto": {"flor": 1}}}, {"upkeep": {"enabeld": True}}, {"upkeep": {"auto": {"fl\nor": 1}}}
    shown = [g.read(config).problems for config in near]
    assert shown == [("upkeep.auto: unknown key 'flor' (did you mean 'floor'?)",),
                     ("upkeep: unknown key 'enabeld' (did you mean 'enabled'?)",),
                     ("upkeep.auto: unknown key 'fl\\nor' (did you mean 'floor'?)",)], shown
    assert g.read({"upkeep": {"enabled": True, 1: 2}}).problems == ("upkeep: unknown key at position 2 (int)",)
    assert g.read({"upkeep": {"enabled": True, "zz": 1}}).problems == ("upkeep: unknown key at position 2 (text, 2 characters)",)


# ------------------------------------------------------------------------------------------ the strict boolean

def test_enabled_is_strict():
    """`enabled` opens the gate only for the JSON boolean true."""
    assert_enabled_is_strict(support.gate())


def test_control_strict():
    """Each way of loosening the boolean check is caught by the strictness test."""
    support.gate()
    escaped = []
    for mutant in STRICT_MUTANTS:
        broken = support.gate_with(STRICT, mutant)
        try:
            assert_enabled_is_strict(broken)
        except AssertionError:
            continue
        escaped.append(mutant)
    assert escaped == []


def test_control_bool_int():
    """Counting a boolean as a number turns the matrix red."""
    support.gate()
    assert matrix_failures(support.gate_with("type(value) is int", "isinstance(value, int)")) != []


def test_control_cross():
    """Removing the guard that skips cross-checks on bad keys makes the matrix raise."""
    support.gate()
    broken = support.gate_with("if any(k in bad for k in keys):", "if False:")
    assert any("raised" in w[3] for w in matrix_failures(broken))


def test_control_range_edge():
    """An exclusive lower bound turns the edge test red."""
    support.gate()
    assert edge_failures(support.gate_with("lo <= value <= hi", "lo < value <= hi")) != []


# ------------------------------------------------------------------------------------------ the machine door

LEDGER_CASES = [({}, False), ({"ledger": {"enabled": True}}, True), ({"ledger": {"enabled": False}}, False),
                ({"ledger": {"enabled": None}}, False), ({"ledger": {"enabled": "true"}}, False),
                ({"ledger": {"enabled": 1}}, False), ({"ledger": {"enabled": 0}}, False), ({"ledger": {}}, False),
                ({"ledger": True}, False), ({"ledger": []}, False), ({"ledger": "x"}, False), ({"ledger": None}, False)]
ENV_CASES = [({}, False)] + [({"SIGMA_UPKEEP_JOB": v}, v == "1") for v in
                             ("1", "", "0", "true", "yes", "on", " 1", "1 ", "11", "01", 1, True, None)]


def machine_failures(g):
    wrong = []
    base = {"upkeep": {"enabled": True}}
    for ledger, ledger_on in LEDGER_CASES:
        for env, env_on in ENV_CASES:
            config = dict(base, **ledger)
            verdict = g.evaluate(config, "machine", env)
            want_missing = (["machine"] if not env_on else []) + (["ledger"] if not ledger_on else [])
            if verdict["open"] is not (ledger_on and env_on) or verdict["missing"] != want_missing:
                wrong.append((repr(ledger)[:30], repr(env)[:30], verdict["open"], verdict["missing"]))
            if g.machine_enabled(config, env) is not (ledger_on and env_on):
                wrong.append((repr(ledger)[:30], repr(env)[:30], "machine_enabled"))
    for bad in ({"upkeep": {"enabled": "true"}}, {"upkeep": {"enabled": True, "flor": 1}}, {"upkeep": {"enabled": False}}):
        config = dict(bad, ledger={"enabled": True})
        verdict = g.evaluate(config, "machine", OPEN_ENV)
        if verdict["open"] or verdict["missing"] != ["project"] or (bad["upkeep"]["enabled"] is not False and not verdict["problems"]):
            wrong.append((repr(bad)[:40], verdict["missing"]))
    return wrong


def test_machine_opt_ins():
    """The machine door opens only with the project block, the variable exactly `1`, and `ledger.enabled` exactly true."""
    assert machine_failures(support.gate()) == []


def test_machine_env_source(monkeypatch):
    """The variable comes from the injected mapping, else the process environment; a non-mapping is closed."""
    g = support.gate()
    config = {"upkeep": {"enabled": True}, "ledger": {"enabled": True}}
    monkeypatch.setenv("SIGMA_UPKEEP_JOB", "1")
    assert g.machine_enabled(config) is True
    assert g.machine_enabled(config, {}) is False                 # the injected mapping wins
    assert g.machine_enabled(config, 7) is False and g.machine_enabled(config, "1") is False
    monkeypatch.delenv("SIGMA_UPKEEP_JOB")
    assert g.machine_enabled(config) is False
    assert g.machine_enabled(config, OPEN_ENV) is True


def test_ledger_rule_agrees():
    """The ledger rule restated in the gate equals ledger.enabled on well-formed input, and both are total on a truthy non-object block."""
    g = support.gate()
    ledger = support.script("ledger")
    base = {"upkeep": {"enabled": True}}
    for config in [{}, {"ledger": {}}] + [{"ledger": {"enabled": v}} for v in (True, False, None, "true", 1)]:
        assert g.machine_enabled(dict(base, **config), OPEN_ENV) is ledger.enabled(config), config
    for config in ({"ledger": True}, {"ledger": "x"}, {"ledger": [1]}):
        assert g.machine_enabled(dict(base, **config), OPEN_ENV) is False
        assert ledger.enabled(config) is False        # total since #966: the reader no longer raises here


def test_control_machine():
    """A truthy ledger value or a truthy variable would open the machine door, and the machine test sees it."""
    assert machine_failures(support.gate()) == []
    for old, new in (('ledger.get("enabled") is True', 'ledger.get("enabled")'),
                     ('getter(ENV_MACHINE) == "1"', 'getter(ENV_MACHINE)')):
        assert machine_failures(support.gate_with(old, new)) != [], old


def test_machine_reading():
    """The machine door is not a second reading of the block: same problems, as lists."""
    g = support.gate()
    wrong = []
    for key in g.SCHEMA:
        for v in BAD_VALUES:
            config = dict(block_for(key, v, True), ledger={"enabled": True})
            if list(g.evaluate(config, "machine", OPEN_ENV)["problems"]) != list(g.read(config).problems):
                wrong.append((key, repr(v)[:20]))
    assert wrong == []


def test_env_registered():
    """The machine variable is registered as post-rename: never a fallback name, never an internal one."""
    name = "SIGMA_UPKEEP_JOB"
    legacy = support.script("legacy")
    assert name in legacy.POST_RENAME_ENV, support.NOT_REGISTERED
    assert name not in legacy.FALLBACK_ENV and name not in legacy.INTERNAL_ENV
    retired = ("loop" + "smith").upper() + "_UPKEEP_JOB"           # a previous-prefix spelling is never consulted
    assert legacy.getenv(name, environ={retired: "1"}) is None
    assert support.gate().ENV_MACHINE == name


# ------------------------------------------------------------------------------------------ the decorator

def test_gated_closed():
    """A closed gate returns the closed dict and never calls the body; an open one calls it with the same arguments."""
    g = support.gate()
    calls = []

    @g.gated("project")
    def body(config, extra=None):
        calls.append((config, extra))
        return "ran"
    for config in ({}, None, {"upkeep": {"enabled": "true"}}, {"upkeep": []}, {"upkeep": {"enabled": True, "auto": {"floor": "x"}}}):
        got = body(config, extra=1)
        assert got["closed"] is True and got["door"] == "project" and got["missing"] == ["project"], config
        assert isinstance(got["problems"], list) and set(got) == {"closed", "door", "missing", "problems"}
    assert calls == []
    assert body({"upkeep": {"enabled": True}}, extra=2) == "ran" and calls == [({"upkeep": {"enabled": True}}, 2)]


def test_gated_machine_door():
    """The machine door needs the variable and the ledger; the `environ` keyword reaches the body."""
    g = support.gate()

    @g.gated("machine")
    def tick(config, *, environ=None):
        return ("ran", environ)
    project_only = {"upkeep": {"enabled": True}}
    closed = tick(project_only, environ={})
    assert closed["closed"] is True and closed["missing"] == ["machine", "ledger"]
    config = dict(project_only, ledger={"enabled": True})
    assert tick(config, environ=OPEN_ENV) == ("ran", OPEN_ENV)
    assert tick(config, environ={"SIGMA_UPKEEP_JOB": "yes"})["missing"] == ["machine"]


def test_gated_bad_door():
    """An unknown door is a ValueError at decoration time, and in evaluate."""
    g = support.gate()
    for door in ("both", print, None, ""):
        with pytest.raises(ValueError):
            g.gated(door)
    with pytest.raises(ValueError):
        g.evaluate({}, "both")


def test_gated_env_kwonly():
    """A machine-door entry point must take `environ` as a keyword-only parameter, so none can be passed positionally and ignored."""
    g = support.gate()

    def positional(config, environ=None):
        return 1

    def absent(config):
        return 1

    def swallowed(config, **kwargs):
        return 1

    def keyword_only(config, *, environ=None):
        return 1

    def wrapper_of(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            return fn(*args, **kwargs)
        return inner
    refused = []
    for name, fn in (("positional", positional), ("absent", absent), ("swallowed", swallowed)):
        try:
            g.gated("machine")(fn)
        except ValueError:
            continue
        refused.append(name)
    assert refused == [], "these shapes were accepted: %s" % refused
    assert g.gated("machine")(keyword_only)({"upkeep": {"enabled": True}}, environ={})["closed"] is True
    assert g.gated("machine")(wrapper_of(keyword_only)) is not None            # the signature is followed through functools.wraps
    for fn in (positional, absent, swallowed):
        assert g.gated("project")(fn) is not None                               # the project door never reads `environ`


def test_gated_identity():
    """The decorated function keeps its name and docstring and records its door."""
    g = support.gate()

    @g.gated("project")
    def documented(config):
        """the docstring"""
    assert documented.__name__ == "documented" and documented.__doc__ == "the docstring" and documented.upkeep_door == "project"


# ------------------------------------------------------------------------------------------ precedence, fold, hygiene

_UNSET = object()
#: (work.rebase_upkeep, upkeep block, pick-time switch, upkeep gate open). The older switch fails OPEN on a typo, the gate fails CLOSED.
PRECEDENCE_ROWS = [
    (_UNSET, _UNSET, "ON", False),
    ("on", _UNSET, "ON", False),
    ("off", _UNSET, "OFF", False),
    ("on", {"enabled": False}, "ON", False),
    ("on", {"enabled": True}, "ON", True),
    ("off", {"enabled": True}, "OFF", True),         # the older switch still turns the pick-time door off
    (False, {"enabled": True}, "OFF", True),
    ("onn", {"enabled": "tru"}, "ON", False),         # opposite typo rules
    ("onn", {"enabled": True}, "ON", True),
    ("off", {"enabled": "yes"}, "OFF", False),
]


def _row_config(work_value, block):
    config = {}
    if work_value is not _UNSET:
        config["work"] = {"rebase_upkeep": work_value}
    if block is not _UNSET:
        config["upkeep"] = dict(block)
    return config


def precedence_failures(rebase, g):
    wrong = []
    for work_value, block, want_switch, want_open in PRECEDENCE_ROWS:
        config = _row_config(work_value, block)
        if rebase.switch(config) != getattr(rebase, want_switch) or g.enabled(config) is not want_open:
            wrong.append((work_value, block))
    return wrong


def test_precedence_rows():
    """The precedence table, row by row, against the real pick-time switch."""
    g = support.gate()
    assert precedence_failures(support.script("feature_rebase"), g) == []


def test_reads_independent():
    """The pick-time switch never reads the upkeep block and the gate never reads `work`."""
    g = support.gate()
    rebase = support.script("feature_rebase")
    wrong = []
    for work_value, block, _switch, _open in PRECEDENCE_ROWS:
        config = _row_config(work_value, block)
        without_upkeep = {k: v for k, v in config.items() if k != "upkeep"}
        without_work = {k: v for k, v in config.items() if k != "work"}
        if rebase.switch(config) != rebase.switch(without_upkeep) or g.read(config) != g.read(without_work):
            wrong.append((work_value, block))
    assert wrong == []


def test_control_precedence():
    """A switch that reads the upkeep block turns the precedence test red."""
    g = support.gate()
    old = 'value = _settings(config).get("rebase_upkeep")'
    new = 'value = False if isinstance(config.get("upkeep"), dict) else _settings(config).get("rebase_upkeep")'
    assert precedence_failures(support.script("feature_rebase", (old, new)), g) != []


def test_docstring_table():
    """The module docstring carries the precedence table and says the two typo rules are opposite."""
    support.gate()
    doc = ast.get_docstring(ast.parse(support.source()))
    for phrase in ("work.rebase_upkeep", "Pick time", "Scheduler", "A person's request", "Chat"):
        assert phrase in doc, phrase
    assert "opposite" in doc.lower()


def test_imports_hygiene():
    """The module imports nothing that can spawn, connect or write; it touches only `os.environ`; it holds no marker literal."""
    support.gate()
    tree = ast.parse(support.source())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert roots <= {"collections", "functools", "inspect", "os"}, roots
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os"}
    assert attrs <= {"environ"}, attrs
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    markers = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and id(n) not in docstrings and n.value.startswith(("sigma-", "sigma:"))]
    assert markers == []


def test_parses_as_310():
    """The module parses as Python 3.10, the oldest version CI covers."""
    ast.parse(support.source(), feature_version=(3, 10))


def test_one_reader():
    """The gate reads the block exactly once, through BLOCK; no other shipped script names the key."""
    g = support.gate()
    assert g.BLOCK == "upkeep"
    reads = [n for n in ast.walk(ast.parse(support.source())) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "get" and n.args
             and isinstance(n.args[0], ast.Name) and n.args[0].id == "BLOCK"]
    assert len(reads) == 1, "the gate must read the upkeep block in exactly one place"
    assert support.readers_outside_the_gate() == [], "only feature_upkeep.py may read the upkeep block"
