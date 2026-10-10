"""#924: the opt-in audit of upkeep part A, written to be independent of the registry it audits.

`upkeep_support.REGISTERED_ENTRY_POINTS` and its static guard (`entry_point_offenders`) are the first line. Their
holes are known (measured): an aliased import (`from feature_upkeep import gated as door`), the assignment form
(`run = gated("project")(run)`) and a hand-written `feature_upkeep.enabled(config)` check are all invisible to
that guard. This file therefore enumerates by a different route (every reference to the gate, however spelled), checks
the enumeration against hand-built sources that use each spelling (the control), and then runs the near-enabled matrix
on EVERY real entry point with an enabled control per driver, so a closed result is not an early return for another reason.

What it does not prove: anything about a hosting service. Everything here is local and offline."""
import ast
import re
import types

import pytest

import test_upkeep_proof as proof
import upkeep_support as support

GATE_STEM = "feature_upkeep"
READERS = {"enabled", "evaluate", "machine_enabled", "conflict_level", "read"}
DOORS = ("project", "machine")

#: shipped files that read the gate by a hand-written check (not through the decorator), each with the reason. An
#: unlisted reader is an unaudited reader; a listed file that stops reading is a stale entry. Edit this on purpose.
KNOWN_READERS = {
    "skills/sigma-loop/scripts/feature_rebase.py": "pick-time pass: pin_env, backup push, restore and prune verbs",
    "skills/sigma-loop/scripts/work.py": "pick-time: goal replay base, conflict level, the readiness hint",
    "skills/sigma-loop/scripts/drift_watch.py": "reads the project door only to size the drift note",
    "skills/sigma-loop/scripts/feature_backup.py": "backup push, restore and prune: each asks the project door itself first",
    "skills/sigma-loop/scripts/feature_upkeep_sched.py": "the scheduler's own doctor rows and status; the tick itself is decorated",
    "skills/sigma-loop/scripts/feature_land_approval.py": "landing approval check, project door, closed means the old path",
    "skills/sigma-loop/scripts/feature_upkeep_landing.py": "landing helper behind the project door",
    "skills/sigma-loop/scripts/unit_completion.py": "unit completion report, closed gate keeps the old report",
    "skills/sigma-rebase/scripts/rebase_brief.py": "attended rebase brief, closed gate keeps the old brief",
    "skills/sigma-loop/scripts/feature_land.py": "landing engine front half, checks the project door first; closed refuses before any read",
    "skills/sigma-loop/scripts/feature_upkeep_launcher.py": "resolver launcher, checks the project door and the resolve level itself; closed returns before any read",
}


# ------------------------------------------------------------------------------------------ the enumerator

def _mentions(node, aliases):
    """Does an expression refer to the gate module or one of its names, by any spelling?"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in aliases:
            return True
        if isinstance(sub, ast.Constant) and sub.value == GATE_STEM:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == GATE_STEM:
            return True
    return False


def _aliases(tree):
    """Names bound to the gate module or to something taken from it, to a fixed point. Includes the helper functions
    that load it (a function whose body names the gate)."""
    names = {GATE_STEM}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == GATE_STEM:
                targets = [a.asname or a.name for a in node.names]
            elif isinstance(node, ast.Import):
                targets = [a.asname or a.name for a in node.names if a.name.split(".")[-1] == GATE_STEM]
            elif isinstance(node, ast.Assign) and _mentions(node.value, names):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    isinstance(s, ast.Constant) and s.value == GATE_STEM for s in ast.walk(node)):
                targets = [node.name]
            for name in targets:
                if name not in names:
                    names.add(name)
                    changed = True
    return names


def enumerate_gate_use(src):
    """-> (gated, readers): the names of functions the gate wraps (decorator OR assignment form, any alias), and the names
    of the functions holding a hand-written call of one of the gate's readers."""
    tree = ast.parse(src)
    aliases = _aliases(tree)
    gated_names = {"gated"}
    imported = {}                                    # local name -> original name, for `from feature_upkeep import x as y`
    for node in ast.walk(tree):                      # `from feature_upkeep import gated as door` and `door = x.gated`
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == GATE_STEM:
            gated_names.update(a.asname for a in node.names if a.name == "gated" and a.asname)
            imported.update({(a.asname or a.name): a.name for a in node.names})
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
            ref = node.value.attr if isinstance(node.value, ast.Attribute) else node.value.id
            if ref in gated_names:
                gated_names.update(t.id for t in node.targets if isinstance(t, ast.Name))

    def gated_ref(node):
        if isinstance(node, ast.Call):
            node = node.func
        return (isinstance(node, ast.Attribute) and node.attr in gated_names) or (
            isinstance(node, ast.Name) and node.id in gated_names)

    gated, readers = {}, set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if gated_ref(deco):
                    gated[node.name] = _door(deco)
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr in READERS
                        and _mentions(sub.func.value, aliases)):
                    readers.add(node.name)
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and imported.get(sub.func.id) in READERS):
                    readers.add(node.name)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Call) and gated_ref(node.func)
                and node.args and isinstance(node.args[0], ast.Name)):        # gated("project")(run)
            gated[node.args[0].id] = _door(node.func)
    return gated, readers


def _door(deco):
    if isinstance(deco, ast.Call) and deco.args and isinstance(deco.args[0], ast.Constant):
        return deco.args[0].value
    return "project"


def shipped_enumeration():
    """({stem: {function: door}}, {relative path: reader function names}) over every shipped script."""
    entry, readers = {}, {}
    for path in support.shipped_scripts():
        if path == support.GATE:
            continue
        gated, read = enumerate_gate_use(path.read_text(encoding="utf-8"))
        rel = path.relative_to(support.ROOT).as_posix()
        if gated:
            entry[path.stem] = gated
        if read:
            readers[rel] = read
    return entry, readers


# ------------------------------------------------------------------------------------------ the enumeration

ALIASED = '''
from feature_upkeep import gated as door

@door("project")
def aliased(config, sdlc_dir):
    return 1
'''
ASSIGNED = '''
import feature_upkeep

def plain(config, sdlc_dir):
    return 1

plain = feature_upkeep.gated("machine")(plain)
'''
HAND_WRITTEN = '''
def sneaky(config, sdlc_dir):
    gate = _sibling("feature_upkeep")
    if gate.enabled(config):
        return spawn()
'''
IMPORTED_READER = '''
from feature_upkeep import enabled as on

def sneaky(config):
    return on(config)
'''


def test_the_enumerator_sees_the_spellings_the_registry_guard_misses():
    """Control: each hole of `entry_point_offenders` is seen by this file's enumeration, and the old guard still misses it."""
    for src, name in ((ALIASED, "aliased"), (ASSIGNED, "plain")):
        gated, _readers = enumerate_gate_use(src)
        assert name in gated, src
        assert support.entry_point_offenders(src, set()) == [], "the registry guard now sees it: tighten this control"
    assert enumerate_gate_use(ASSIGNED)[0]["plain"] == "machine"
    assert enumerate_gate_use(HAND_WRITTEN)[1] == {"sneaky"}
    assert enumerate_gate_use(IMPORTED_READER)[1] == {"sneaky"}
    assert enumerate_gate_use("def f(config):\n    return config.get('enabled')\n") == ({}, set())


def test_every_entry_point_is_registered_and_driven():
    """The enumeration over the whole shipped tree equals the registry, door by door, and each has a driver."""
    entry, _readers = shipped_enumeration()
    found = {(stem, fn) for stem, fns in entry.items() for fn in fns}
    registered = {(stem, fn) for stem, fns in support.REGISTERED_ENTRY_POINTS.items() for fn in fns}
    assert found == registered, "an entry point exists that the registry does not list, or the other way round"
    have = set(support.drivers(support.gate())) - {("upkeep_probe", n) for n in support.PROBE_NAMES}
    assert have == found
    assert all(door in DOORS for fns in entry.values() for door in fns.values())


def test_every_reader_of_the_gate_outside_the_decorator_is_known():
    """A hand-written check of the gate is allowed only in a file listed, with its reason, in KNOWN_READERS."""
    _entry, readers = shipped_enumeration()
    unknown = sorted(set(readers) - set(KNOWN_READERS))
    stale = sorted(set(KNOWN_READERS) - set(readers))
    assert unknown == [], "unaudited readers of the upkeep gate: %s" % unknown
    assert stale == [], "KNOWN_READERS lists files that no longer read the gate: %s" % stale
    assert all(reason.strip() for reason in KNOWN_READERS.values())


def test_the_template_block_is_read_by_no_shipped_script_but_the_gate():
    """The config block `upkeep` has one reader: the gate. (The pick-time module's verb `upkeep` is not a key.)"""
    assert support.readers_outside_the_gate() == []


# ------------------------------------------------------------------------------------------ the matrix

def real_drivers():
    entry, _readers = shipped_enumeration()
    drivers = support.drivers(support.gate())
    return [(stem, fn, door, drivers[(stem, fn)]) for stem, fns in sorted(entry.items()) for fn, door in sorted(fns.items())]


def test_there_are_real_drivers_to_audit():
    assert len(real_drivers()) >= 6, "the matrix must not shrink to the probe"


@pytest.mark.parametrize("stem,fn,door", [(s, f, d) for s, f, d, _ in real_drivers()])
def test_near_enabled_matrix_per_real_driver(tmp_path, monkeypatch, stem, fn, door):
    """Every near-enabled config (and each withheld machine opt-in) leaves a REAL entry point closed: no process, model,
    network, write or file change."""
    driver = support.drivers(support.gate())[(stem, fn)]
    if door == "project":
        cases = [(n, c, None) for n, c in proof.project_cases()]
    else:
        cases = proof.machine_cases()
    assert len(cases) >= 20
    assert proof.offences(tmp_path, monkeypatch, driver, cases) == [], "%s.%s" % (stem, fn)


def test_enabled_control_per_real_driver(tmp_path, monkeypatch):
    """Non-vacuous: with every opt-in on, each real driver reaches its body (a result that is not the closed one), so the
    closed runs above are not an early return for some other reason. Where the body acts, the trap records it."""
    acted = 0
    for n, (stem, fn, door, driver) in enumerate(real_drivers()):
        config = {"upkeep": {"enabled": True}, "ledger": {"enabled": True}}
        result, trap, diff, _project = support.run_case(tmp_path / ("c%d" % n), monkeypatch, driver, config,
                                                        support.OPEN_ENV if door == "machine" else None)
        assert isinstance(result, dict) and result.get("closed") is not True, "%s.%s stayed closed when opened" % (stem, fn)
        acted += bool(trap.processes or trap.network or trap.writes or diff[0])
    assert acted >= 4, "most real drivers should leave a trace when open; the control is vacuous otherwise"


def _without_decorator(fn):
    """A loader for loop scripts that strips the gate decorator from `fn` (the mutation: the gate is gone)."""
    def load(stem):
        path = support.SCRIPTS / (stem + ".py")
        text = path.read_text(encoding="utf-8")
        pattern = re.compile(r'@feature_upkeep\.gated\("\w+"\)\n(def %s\()' % re.escape(fn))
        if not pattern.search(text):
            return ORIGINAL_SCRIPT(stem)
        namespace = {"__name__": stem + "_stripped", "__file__": str(path)}
        exec(compile(pattern.sub(r"\1", text), str(path), "exec"), namespace)      # noqa: S102 - test-only
        return types.SimpleNamespace(**namespace)
    return load


ORIGINAL_SCRIPT = support.script


def test_control_a_stripped_decorator_is_seen_red_for_every_real_driver(tmp_path, monkeypatch):
    """The mutation, per real entry point: remove its decorator and the closed run is no longer closed (or the body
    raises). Without this the matrix above could pass for a driver whose gate is not what keeps it closed."""
    escaped = []
    for n, (stem, fn, door, _driver) in enumerate(real_drivers()):
        monkeypatch.setattr(support, "script", _without_decorator(fn))
        driver = support.drivers(support.gate())[(stem, fn)]
        try:
            result, _trap, _diff, _project = support.run_case(tmp_path / ("s%d" % n), monkeypatch, driver, {}, {})
            opened = not (isinstance(result, dict) and result.get("closed") is True)
        except Exception:
            opened = True
        if not opened:
            escaped.append((stem, fn))
    monkeypatch.setattr(support, "script", ORIGINAL_SCRIPT)
    assert escaped == [], "removing the decorator changed nothing for: %s" % escaped
