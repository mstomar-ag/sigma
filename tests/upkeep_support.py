"""Support for the #917 upkeep tests. No test lives here, and nothing here asserts at import time.

It loads the upkeep gate by path (never by import, so a missing gate is an assertion inside a test, never a
collection error), rebuilds it with a source substitution for the mutation controls, holds the entry-point probe,
the static first-call guard, the registry of shipped entry points and the scan for readers of the block."""
import ast
import importlib.util
import json
import pathlib
import types

import attempt_trap

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
GATE = SCRIPTS / "feature_upkeep.py"
TEMPLATE = ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl"
GATE_REL = GATE.relative_to(ROOT).as_posix()      # the gate is exempt from the reader scan by this PATH, never by its file name
OPEN_ENV = {"SIGMA_UPKEEP_JOB": "1"}

NO_GATE = "feature_upkeep.py does not exist yet: the upkeep gate has not been written"
NO_BLOCK = 'the scaffolded config has no top-level "upkeep" block'
NOT_REGISTERED = "SIGMA_UPKEEP_JOB is not registered in POST_RENAME_ENV"

_MEMO = {}


# ------------------------------------------------------------------------------------------ loading

def source():
    assert GATE.is_file(), NO_GATE
    return GATE.read_text(encoding="utf-8")


def build(src, name="feature_upkeep_variant"):
    namespace = {"__name__": name, "__file__": str(GATE)}
    exec(compile(src, str(GATE), "exec"), namespace)          # noqa: S102 - test-only
    return types.SimpleNamespace(**namespace)


def gate():
    """The real gate, built once per process."""
    if "real" not in _MEMO:
        _MEMO["real"] = build(source())
    return _MEMO["real"]


def gate_with(old, new):
    """The gate rebuilt with a source substitution. The target must exist and EVERY occurrence is replaced (the
    lesson recorded at tests/test_feature_registry.py `_mod_with`); each caller still asserts the mutant's
    behaviour differs."""
    src = source()
    assert old in src, "mutation target has drifted out of the source: %r" % (old,)
    return build(src.replace(old, new))


def script(stem, edit=None):
    """Any loop script, loaded by path under a variant name; `edit` is an optional (old, new) substitution."""
    path = SCRIPTS / (stem + ".py")
    src = path.read_text(encoding="utf-8")
    if edit:
        old, new = edit
        assert old in src, "mutation target has drifted out of the source: %r" % (old,)
        src = src.replace(old, new)
    namespace = {"__name__": stem + "_variant", "__file__": str(path)}
    exec(compile(src, str(path), "exec"), namespace)          # noqa: S102 - test-only
    return types.SimpleNamespace(**namespace)


def _unique_pairs(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key in the template: %r" % (key,))
        seen[key] = value
    return seen


def parse_template(text=None):
    """The shipped config template as a dict; a duplicate key (which JSON would resolve silently) is a ValueError."""
    return json.loads(TEMPLATE.read_text(encoding="utf-8") if text is None else text, object_pairs_hook=_unique_pairs)


def template_cfg():
    """The parsed template, after asserting it has the block. Every template-reading test calls THIS first, so
    that before the block exists the failure is an AssertionError and never a KeyError."""
    cfg = parse_template()
    assert "upkeep" in cfg, NO_BLOCK
    return cfg


# ------------------------------------------------------------------------------------------ the probe

#: stem -> names of the functions shipped code has gated. EMPTY in this slice: nothing in shipped code is an entry
#: point yet. A later slice that gates one adds it here AND adds a trap driver in `drivers()`; two tests make
#: forgetting either red.
REGISTERED_ENTRY_POINTS = {}

PROBE_SOURCE = '''
import os
import socket
import subprocess


def _attempt(action):
    try:
        action()
    except Exception:
        pass


def _everything(sdlc_dir):
    _attempt(lambda: subprocess.run(["upkeep-probe-tool", "--probe"]))
    _attempt(lambda: subprocess.Popen(["upkeep-probe-model", "-p", "probe"]))
    _attempt(lambda: socket.getaddrinfo("upkeep-probe.invalid", 443))
    state = os.path.join(sdlc_dir, "state", "upkeep")
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "probe.json"), "w") as handle:
        handle.write("{}")
    return {"ran": True}


@feature_upkeep.gated("project")
def attempt_project(config, sdlc_dir):
    return _everything(sdlc_dir)


@feature_upkeep.gated("machine")
def attempt_machine(config, sdlc_dir, *, environ=None):
    return _everything(sdlc_dir)
'''
PROBE_NAMES = {"attempt_project", "attempt_machine"}
PROBE_MODEL = "upkeep-probe-model"      # a stand-in for a model CLI: classified as one, and not installed anywhere


def probe(gate_module, src=PROBE_SOURCE):
    namespace = {"feature_upkeep": gate_module, "__name__": "upkeep_probe"}
    exec(compile(src, "<upkeep-probe>", "exec"), namespace)    # noqa: S102 - test-only
    return types.SimpleNamespace(**namespace)


def drivers(gate_module):
    """(stem, function) -> callable(config, sdlc_dir, environ). Shipped entry points add theirs here (see
    REGISTERED_ENTRY_POINTS)."""
    p = probe(gate_module)
    return {("upkeep_probe", "attempt_project"): lambda c, s, e: p.attempt_project(c, s),
            ("upkeep_probe", "attempt_machine"): lambda c, s, e: p.attempt_machine(c, s, environ=e)}


def run_case(tmp_path, monkeypatch, driver, config, environ=None):
    """Run one driver under the trap in a fresh project directory and a fake HOME.
    -> (result, trap, (added, removed, changed), project directory)."""
    project, home = tmp_path / "project", tmp_path / "home"
    (project / ".sdlc").mkdir(parents=True)
    home.mkdir()
    (project / ".sdlc" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    before = attempt_trap.snapshot(project, home)
    with attempt_trap.AttemptTrap(model_programs=(PROBE_MODEL,)) as trap:
        result = driver(config, str(project / ".sdlc"), environ)
    return result, trap, attempt_trap.tree_diff(before, attempt_trap.snapshot(project, home)), project


# ------------------------------------------------------------------------------------------ the static guard

def _mark(decorator):
    node = decorator
    called = isinstance(node, ast.Call)
    if called:
        node = node.func
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
    if name != "gated":
        return None
    return "call" if called else "bare"


def entry_point_offenders(src, registered):
    """[] when every gated function is registered, module-level, sync, called as gated(door) and OUTERMOST, and
    every registered name is gated. Otherwise a sorted list of reasons."""
    tree = ast.parse(src)
    top = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        marks = [_mark(d) for d in node.decorator_list]
        if not any(marks):
            continue
        name = node.name
        if top.get(name) is not node:
            problems.append("%s: a gated entry point must be a module-level function" % name)
            continue
        if isinstance(node, ast.AsyncFunctionDef):
            problems.append("%s: an async function cannot be gated" % name)
        if "bare" in marks:
            problems.append("%s: write @gated(door), not @gated" % name)
        if marks[0] != "call":
            problems.append("%s: the gate must be the outermost decorator" % name)
        if name not in registered:
            problems.append("%s: gated but not registered" % name)
    for name in sorted(registered):
        node = top.get(name)
        if node is None or not any(_mark(d) for d in node.decorator_list):
            problems.append("%s: registered but not gated" % name)
    return sorted(set(problems))


def shipped_scripts():
    """Every shipped Python script the static guards cover."""
    found = list((ROOT / "skills").glob("*/scripts/*.py")) + list((ROOT / "hooks").glob("*.py"))
    return sorted(set(found))


# ------------------------------------------------------------------------------------------ readers of the block

def upkeep_literals(src):
    """Line numbers of every string constant exactly `upkeep` in `src`, except the right-hand side of an
    `argv[<n>] == "upkeep"` comparison (the one command-line verb of feature_rebase.py, which names no config key)."""
    tree = ast.parse(src)
    verbs = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.left, ast.Subscript) and isinstance(node.left.value, ast.Name)
                and node.left.value.id == "argv"):
            verbs.update(id(c) for c in node.comparators)
    return sorted(n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and n.value == "upkeep" and id(n) not in verbs)


def readers_outside_the_gate(root=ROOT):
    """['relative/path:line'] for every `upkeep` string constant in shipped Python under `root` other than the gate module
    itself (identified by its relative path: a file of the same name anywhere else is scanned)."""
    found = []
    for pattern in ("skills/**/*.py", "hooks/**/*.py", "tools/**/*.py", "evals/**/*.py", "contract/**/*.py"):
        for path in sorted(pathlib.Path(root).glob(pattern)):
            rel = path.relative_to(root).as_posix()
            if rel == GATE_REL or "node_modules" in path.parts:
                continue
            for line in upkeep_literals(path.read_text(encoding="utf-8")):
                found.append("%s:%d" % (rel, line))
    return sorted(found)


# ------------------------------------------------------------------------------------------ the template pin

def flat(block, prefix=""):
    """{dotted.key: value} of a config block; keys starting with an underscore are notes and are skipped."""
    out = {}
    for key, value in block.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict):
            out.update(flat(value, prefix + key + "."))
        else:
            out[prefix + key] = value
    return out


def key_gaps(block, gate_keys):
    """(keys the gate reads that the block does not carry, keys the block carries that the gate does not read)."""
    carried = set(flat(block))
    return sorted(set(gate_keys) - carried), sorted(carried - set(gate_keys))
