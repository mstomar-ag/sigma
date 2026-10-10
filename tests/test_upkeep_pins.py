"""#919: how the two new library modules sit in the repository's pins and ratchets, and the proof that a closed gate means
nothing changes (the modules have no caller).

THE LISTED TESTS (every one red by assertion before the modules exist, green after) are silent and unparametrised. The tests
named test_fixture_* are NOT listed: they pass at baseline because they use only existing tools."""
import ast
import importlib
import importlib.util
import json
import shutil
import sys
from unittest import mock

import attempt_trap
import upkeep_drift_support as S

TEN_KEYS = {"triggers.drift_merges", "triggers.dormant_days", "auto.window_days", "auto.burst_window_hours", "auto.target_hours",
            "auto.floor", "auto.ceiling", "triggers.min_interval_minutes", "triggers.every_hours", "triggers.dormant_every_hours"}
STDLIB_OK = {"collections", "fractions", "importlib", "json", "numbers", "pathlib", "re"}
BANNED_CALLS = {"write_text", "write_bytes", "mkdir", "touch", "replace", "rename", "unlink", "rmdir", "makedirs", "symlink", "link",
                "chmod", "rmtree", "copytree", "fsync", "Popen", "system", "urlopen", "connect", "getaddrinfo"}
DISPOSITION = S.ROOT / "docs" / "launch" / "dispositions" / "919.json"
DIR_PATTERN = ".sdlc/state/upkeep/units/"
FILE_PATTERN = ".sdlc/state/upkeep/units/<unit>.json"


def tool(name):
    path = S.ROOT / "tools" / "readiness" / (name + ".py")
    spec = importlib.util.spec_from_file_location("readiness_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def module_paths():
    return [S.SCRIPTS / (S.DRIFT + ".py"), S.SCRIPTS / (S.STATE + ".py")]


def shipped_python():
    found = list(S.ROOT.glob("skills/*/scripts/*.py")) + list(S.ROOT.glob("hooks/*.py"))
    for top in ("tools", "evals", "contract"):
        found += [p for p in (S.ROOT / top).rglob("*.py") if "node_modules" not in p.parts]
    return sorted(set(found))


def int_constants(tree):
    return {t.id for node in tree.body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int for t in node.targets if isinstance(t, ast.Name)}


# ------------------------------------------------------------------------------------------ the checks

def classification_failures(inv, listing):
    bad = []
    S.expect(bad, "the state path builder folds two casings", (S.STATE, "unit_state_path") in inv._FOLDS, True)
    for stem in (S.DRIFT, S.STATE):
        S.expect(bad, stem + " sibling importer is declared free of unit names", (stem, "_load") in inv._NO_UNIT_NAME, True)
        S.expect(bad, stem + " is a library", stem in listing.LIBRARY_ONLY, True)
        S.expect(bad, stem + " has no command-line entry", listing._has_main_block(S.SCRIPTS / (stem + ".py")), False)
    ours = sorted(k for k in inv._address_builders() if k[0] in (S.DRIFT, S.STATE))
    S.expect(bad, "the only address builders in the new modules", ours, [(S.DRIFT, "_load"), (S.STATE, "_load"), (S.STATE, "unit_state_path")])
    S.expect(bad, "every discovered builder is classified", sorted(set(inv._address_builders()) ^ inv._classified()), [])
    S.expect(bad, "no unit-name vocabulary in a declared unit-name-free builder", inv._declared_free_but_naming(inv._address_builders(), inv._NO_UNIT_NAME), [])
    return bad


def source_failures():
    d, s = S.drift(), S.ustate()
    gate = S.sibling("feature_upkeep")
    bad = []
    S.expect(bad, "the two modules read the ten keys, once each", (set(d.READS) | set(s.READS), len(d.READS) + len(s.READS)), (TEN_KEYS, 10))
    S.expect(bad, "every key is one the gate validates and defaults", sorted(k for k in TEN_KEYS if k not in gate.SCHEMA or k not in gate.DEFAULTS), [])
    for name, module, path in (("drift", d, module_paths()[0]), ("state", s, module_paths()[1])):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        named = set(module.READS) | int_constants(tree)
        S.expect(bad, name + ": every number it reads or holds has a stated source", sorted(k for k in named if k not in module.SOURCES), [])
        S.expect(bad, name + ": no source is empty", sorted(k for k, v in module.SOURCES.items() if not (isinstance(v, str) and len(v) > 40)), [])
        S.expect(bad, name + ": no source for a number it does not have", sorted(set(module.SOURCES) - named), [])
        for key in module.READS:
            text = module.SOURCES[key]
            S.expect(bad, name + ": " + key + " says it is the design's and not a measurement", ("predecessor" in text, "not a measurement" in text), (True, True))
    S.expect(bad, "the walk cap says what it measured", "measured" in d.SOURCES["WALK_CAP"] and "UNKNOWN" in d.SOURCES["WALK_CAP"], True)
    S.expect(bad, "the two slacks agree", d.FUTURE_SLACK_SECONDS, s.FUTURE_SLACK_SECONDS)
    S.expect(bad, "the tuning of the defaults", tuple(d.tuning(gate.DEFAULTS)), (504, 24, 12, 3, 40, "auto", 336))
    S.expect(bad, "the schedule of the defaults: 2 hours, 24 hours, 72 hours", tuple(s.schedule(gate.DEFAULTS)), (7200, 86400, 259200))
    base = dict(gate.DEFAULTS)
    moved = (("triggers.min_interval_minutes", 121, 0, 7260), ("triggers.every_hours", 25, 1, 90000), ("triggers.dormant_every_hours", 73, 2, 262800))
    for key, value, slot, want in moved:
        got = s.schedule(dict(base, **{key: value}))
        S.expect(bad, key + " feeds exactly one field", (got[slot], tuple(got[:slot]) + tuple(got[slot + 1:])),
                 (want, tuple(x for i, x in enumerate((7200, 86400, 259200)) if i != slot)))
    S.expect(bad, "the dormant window feeds the dormant hours", d.tuning(dict(base, **{"triggers.dormant_days": 15})).dormant_hours, 360)
    return bad


def disposition_failures(tmp):
    bad = []
    s = S.ustate()
    audit = tool("growth_audit")
    assert DISPOSITION.is_file(), "no growth disposition file for the upkeep state store yet"
    entries = json.loads(DISPOSITION.read_text(encoding="utf-8"))
    S.expect(bad, "patterns", [e.get("pattern") for e in entries], [DIR_PATTERN, FILE_PATTERN])
    S.expect(bad, "the pattern is the module's path", s.STATE_REL in FILE_PATTERN and FILE_PATTERN.startswith(".sdlc/"), True)
    for e in entries:
        S.expect(bad, e["pattern"] + ": keys", sorted(e), sorted(audit._DISPOSITION_KEYS + audit._DISPOSITION_OPTIONAL))
        S.expect(bad, e["pattern"] + ": issue and flag", (e["issue"], e["unscanned"]), ("#919", True))
    store = {e["pattern"]: e for e in entries}[FILE_PATTERN]
    sdlc = tmp / "size" / ".sdlc"
    s.record_attempt(sdlc, "unit-0000", S.at(100), "backstop")
    s.record_outcome(sdlc, "unit-0000", S.at(100) + 5, "rebased", unit_tip="a" * 40, base_tip="b" * 40)
    size = len(s.unit_state_path(sdlc, "unit-0000").read_bytes())
    text = store["pruner_or_cap"]
    S.expect(bad, "the retention rule", store["decision"].startswith("intentionally unbounded"), True)
    S.expect(bad, "the measured size is the real size of a document with a 9-character name", ("%d bytes" % size) in text, True)
    for word in ("lever", "extrapolations and not measurements", "follow-up", "never listed"):
        S.expect(bad, "the disposition says: " + word, word in text, True)
    copy = tmp / "copy" / "docs" / "launch" / "dispositions"
    copy.mkdir(parents=True)
    shutil.copy(DISPOSITION, copy / "919.json")
    S.expect(bad, "the real loader accepts it", sorted(audit.load_dispositions(tmp / "copy", [])), sorted([DIR_PATTERN, FILE_PATTERN]))
    stripped = [{k: v for k, v in e.items() if k != "unscanned"} for e in entries]
    (copy / "919.json").write_text(json.dumps(stripped), encoding="utf-8")
    try:
        audit.load_dispositions(tmp / "copy", [])
    except Exception as exc:                    # noqa: BLE001
        S.expect(bad, "without the flag the loader refuses (the scan does not produce the pattern)", type(exc).__name__, "DispositionError")
    else:
        bad.append("the loader accepted the entries without the unscanned flag")
    return bad


def inert_failures(tmp):
    bad = []
    S.drift()
    S.ustate()
    own = {p.resolve() for p in module_paths()}
    callers = [p.relative_to(S.ROOT).as_posix() for p in shipped_python() if p.resolve() not in own
               and any(stem in p.read_text(encoding="utf-8") for stem in (S.DRIFT, S.STATE))]
    S.expect(bad, "callers of the new modules (the pass and the scheduler add theirs here, in the slices that write them)", callers,
             ["skills/sigma-loop/scripts/feature_upkeep_job.py",           # #923: the detached job
              "skills/sigma-loop/scripts/feature_upkeep_pass.py",          # #922: the pass, the first live caller
              "skills/sigma-loop/scripts/feature_upkeep_sched.py"])        # #923: the scheduler
    support = importlib.import_module("upkeep_support")
    S.expect(bad, "the registered entry points (#922: the pass, its acks file, its ledger note)", support.REGISTERED_ENTRY_POINTS,
             {"feature_upkeep_pass": {"upkeep_pass", "ack_union", "ledger_note"},
              "feature_upkeep_sched": {"scheduler_tick"},                  # #923
              "feature_upkeep_job": {"run_job", "run_engine"}})
    ws = tool("write_surface")
    S.expect(bad, "the write-surface scanner sees no write site", ws.scan_paths(S.ROOT, module_paths()), [])
    for path in module_paths():
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                roots.add((node.module or "").split(".")[0])
        calls = {n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name))}
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        S.expect(bad, path.stem + ": imports", sorted(roots - STDLIB_OK), [])
        S.expect(bad, path.stem + ": no write, process or network call", sorted(calls & BANNED_CALLS), [])
        S.expect(bad, path.stem + ": no open()", "open" in calls, False)
        S.expect(bad, path.stem + ": the gate decorator and doors are not used", sorted(names & {"gated", "evaluate", "machine_enabled"}), [])
        S.expect(bad, path.stem + ": no string constant equal to the block name", support.upkeep_literals(src), [])
        S.expect(bad, path.stem + ": parses as Python 3.10", bool(ast.parse(src, feature_version=(3, 10))), True)
    sched_settings = S.settings()
    with mock.patch.object(sys, "dont_write_bytecode", True), attempt_trap.AttemptTrap() as trap:
        d2, s2 = S.build(S.DRIFT), S.build(S.STATE)
        sched = s2.schedule(sched_settings)
        tune = d2.tuning(sched_settings)
        measured = d2.Drift(3, 4, 3, False, ())
        verdict = s2.decide(sched, s2.StateRead("missing", None, None), measured, S.at(1))
        count = d2.window_count(d2.Walk((), False, None), S.at(1), 24, lambda subject: True)
    S.expect(bad, "importing and calling the pure functions attempts nothing", (trap.processes, trap.models, trap.network, trap.writes),
             ([], [], [], []))
    S.expect(bad, "...and they work", (tuple(sched), tune.floor, verdict.due, count.value), ((7200, 86400, 259200), 3, True, 0))
    return bad


# ------------------------------------------------------------------------------------------ the listed tests

def test_classifications():
    S.drift(), S.ustate()
    bad = classification_failures(importlib.import_module("test_feature_registry"), importlib.import_module("test_script_help"))
    assert not bad, "; ".join(bad)


def test_sources_and_keys():
    bad = source_failures()
    assert not bad, "; ".join(bad)


def test_growth_disposition(tmp_path):
    bad = disposition_failures(tmp_path)
    assert not bad, "; ".join(bad)


def test_gate_closed_inert(tmp_path):
    bad = inert_failures(tmp_path)
    assert not bad, "; ".join(bad)


def test_changelog_entry():
    text = (S.ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    head = text.split("\n## ", 2)
    assert text.startswith("# Changelog") and len(head) > 1 and head[1].startswith("Unreleased"), "no Unreleased section"
    # every later release moves the entries under its own dated heading, so the entry may sit in any section: it is the
    # bold-headed entry that names this goal's own module, found anywhere in the file
    entries = [e for e in ("\n" + text).split("\n- **") if "#919" in e and "feature_upkeep_drift.py" in e]
    assert len(entries) == 1, "the changelog needs exactly one entry for this goal"
    entry = " ".join(entries[0].split())
    bad = []
    for word in ("feature_upkeep_drift.py", "feature_upkeep_state.py", "UNKNOWN", "author date", "no behaviour change", "nothing calls"):
        S.expect(bad, "the entry says " + word, word in entry, True)
    assert not bad, "; ".join(bad)


# ------------------------------------------------------------------------------------------ unlisted: facts about existing tools

def test_fixture_unscanned_flag_is_required(tmp_path):
    """The real loader refuses a disposition whose pattern the scan does not produce unless it carries `unscanned: true`."""
    audit = tool("growth_audit")
    folder = tmp_path / "docs" / "launch" / "dispositions"
    folder.mkdir(parents=True)
    entry = {"pattern": ".sdlc/state/nowhere/<unit>.json", "issue": "#1", "decision": "d", "pruner_or_cap": "p", "evidence": "e"}
    (folder / "1.json").write_text(json.dumps([entry]), encoding="utf-8")
    try:
        audit.load_dispositions(tmp_path, [])
    except Exception as exc:                    # noqa: BLE001
        assert type(exc).__name__ == "DispositionError"
    else:
        raise AssertionError("an unproduced pattern was accepted without the flag")
    (folder / "1.json").write_text(json.dumps([dict(entry, unscanned=True)]), encoding="utf-8")
    assert list(audit.load_dispositions(tmp_path, [])) == [entry["pattern"]]


def test_fixture_write_surface_sees_staged_writes(tmp_path):
    """The write-surface scanner reads names: a function that calls mkdir or os.replace is a site, and a call through the
    shared helper `state.atomic_write_text` is not (which is why the new state module needs no inventory row)."""
    ws = tool("write_surface")
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n\ndef a(p):\n    p.mkdir()\n\ndef b(p, q):\n    os.replace(p, q)\n\ndef c(state, p):\n"
                      "    state.atomic_write_text(p, 'x')\n    state.refuse_symlinks(p, 'y', create_parents=True)\n", encoding="utf-8")
    rows = ws.scan_paths(tmp_path, [sample])
    assert sorted((r["function"], r["rule"]) for r in rows) == [("a", "fs-write"), ("b", "fs-remove")]


# --------------------------------------------------------------------------- #947: Level 1 on the unit path

LEVEL1_SITES = {("feature_rebase.py", "_write_runtime_acks")}


def test_947_the_runtime_ack_write_is_registered_in_the_write_surface_inventory():
    rows = json.loads((S.ROOT / "docs" / "launch" / "write-surface.json").read_text())["entries"]
    got = {(r["path"].rsplit("/", 1)[-1], r["function"]) for r in rows}
    assert LEVEL1_SITES <= got
    assert "_write_runtime_acks" in (S.ROOT / "docs" / "launch" / "write-surface.md").read_text()


def test_947_the_level_1_module_is_pure_orchestration_with_no_config_read_and_no_push():
    text = (S.SCRIPTS / "feature_upkeep_prove.py").read_text()
    assert '"push"' not in text and "gate.read" not in text and "conflict_level" not in text
    tree = ast.parse(text)
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (called & {"write_text", "write_bytes", "mkdir", "unlink", "rmtree", "urlopen"})


def test_947_the_runtime_ack_file_lives_under_state_and_is_never_the_tracked_store():
    spec = importlib.util.spec_from_file_location("feature_rebase_947", S.SCRIPTS / "feature_rebase.py")
    rebase = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rebase)
    runtime = rebase.runtime_ack_path("/x/.sdlc", "Billing")
    assert runtime != rebase.ack_path("/x/.sdlc", "Billing") and runtime.parts[-3:-1] == ("state", "rebase-acks")
    assert runtime.name == "billing.json"
