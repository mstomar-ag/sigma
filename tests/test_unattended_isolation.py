"""#920: while the upkeep gate is closed the new runners are inert, and no shared helper arms a ledger claim.

PLANNED TESTS of the pull-request red-to-green gate. Both start by asking `unattended_support` for the new modules,
which asserts that they exist, so before they are written each fails by AssertionError. The file is bound by its
whole-file hash and is not edited after the red verify.

The runners are libraries with no gate of their own (a gated entry point would force a registry entry, a trap driver
and a rewrite of the reserved note in the config template), so "nothing changes while the gate is closed" is shown
structurally: no shipped code loads them, they load nothing that can arm a claim, importing them does nothing, and
running them leaves the project state untouched. Each scan has a planted counter-example that it must flag."""
import ast
import pathlib

import attempt_trap
import unattended_support as support

#: Relative paths of the two modules; they are exempt from the caller scan by PATH, never by file name.
OWN = ("skills/sigma-loop/scripts/bounded_run.py", "skills/sigma-loop/scripts/unattended_git.py")
#: Siblings the new modules may load. Anything else (the loop script, autowatch, the ledger, the work script) is not.
#: Registered callers, exempt by PATH: the shared "landed" predicate (#930) builds its read-only runner on `bounded_run`
#: and is itself a library with no caller until its gated callers ship.
REGISTERED_CALLERS = (
    "skills/sigma-loop/scripts/feature_landed.py",
    "skills/sigma-loop/scripts/feature_upkeep_pass.py",
    # the landing engine front half (#937): gated on the upkeep gate (refuses while closed) and rehearsal-only
    "skills/sigma-loop/scripts/feature_land.py",
    "skills/sigma-loop/scripts/feature_upkeep_launcher.py",
    "skills/sigma-loop/scripts/feature_upkeep_job.py",
    "skills/sigma-loop/scripts/feature_upkeep_sched.py",
)
#: Registered callers that run only behind the upkeep gate: `feature_upkeep_prove.py` is loaded by `feature_rebase.py`
#: only after `gate.enabled(config)` and the `conflicts.resolve` level check pass (see `_level1_inputs`), so with the
#: gate closed it is never reached. Any further caller must be added here in the same change that gates it.
GATED_CALLERS = (
    "skills/sigma-loop/scripts/feature_upkeep_prove.py:186",
    "skills/sigma-loop/scripts/feature_upkeep_prove.py:193",
)
ALLOWED_LOADS = {"shell_policy", "bounded_run"}
#: Names of the one place a claim is armed and its helpers.
ARMING = {"_ensure_claimed", "_ARMS_CLAIM", "_ensure_unit_tracking", "safe_append", "claim_lock"}


def references(src, stems):
    """Sorted (line, stem) for every import of, name of, or string that is exactly `stem` or `stem.py`."""
    found = set()
    for node in ast.walk(ast.parse(src)):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]] + [a.name for a in node.names]
        elif isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value[:-3] if node.value.endswith(".py") else node.value]
        found.update((node.lineno, n) for n in names if n in stems and hasattr(node, "lineno"))
    return sorted(found)


def callers(root):
    """['relative/path:line'] for every shipped Python file under `root` that refers to either new module."""
    root = pathlib.Path(root)
    found = []
    for pattern in ("skills/**/*.py", "hooks/**/*.py", "tools/**/*.py", "evals/**/*.py", "contract/**/*.py"):
        for path in sorted(root.glob(pattern)):
            rel = path.relative_to(root).as_posix()
            if rel in OWN or rel in REGISTERED_CALLERS or "node_modules" in path.parts:
                continue
            found += ["%s:%d" % (rel, line) for line, _stem in references(path.read_text(encoding="utf-8"), set(support.NAMES))]
    return sorted(found)


def arming_or_loading(src):
    """Reasons a module source loads something it must not or names an arming helper; [] when clean."""
    reasons = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_load" and node.args:
            arg = node.args[0]
            if not (isinstance(arg, ast.Constant) and arg.value in ALLOWED_LOADS):
                reasons.append("loads %r" % (getattr(arg, "value", "?"),))
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            wanted = [a.name.split(".")[0] for a in node.names] + [(getattr(node, "module", None) or "").split(".")[0]]
            reasons += ["imports %s" % n for n in wanted if n in {"loop", "autowatch", "ledger", "work", "state", "handoff"}]
        if isinstance(node, ast.Name) and node.id in ARMING or isinstance(node, ast.Attribute) and node.attr in ARMING:
            reasons.append("names %s" % (getattr(node, "id", None) or node.attr))
    return sorted(set(reasons))


def test_no_caller_and_nothing_that_arms_a_claim(tmp_path):
    """Only the registered, gate-guarded callers reach the runners, and neither runner loads the loop, the ledger or a claim helper."""
    support.load("bounded_run")
    support.load("unattended_git")
    assert callers(support.ROOT) == list(GATED_CALLERS), "a shipped caller must be gated and registered in the same change"
    for stem in support.NAMES:
        assert arming_or_loading(support.source(stem)) == [], stem
    planted = {"skills/x/scripts/y.py": 'import unattended_git\nm = _load("bounded_run")\n',
               "hooks/h.py": 'p = "unattended_git.py"\n', "tools/bounded_run.py": "x = 1\nbounded_run.run_group()\n",
               OWN[0]: "bounded_run = 1\n", "skills/x/scripts/z.py": 'text = "see bounded_run.py for the runner"\n'}
    for rel, text in planted.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    assert callers(tmp_path) == ["hooks/h.py:1", "skills/x/scripts/y.py:1", "skills/x/scripts/y.py:2",
                                 "tools/bounded_run.py:2"]
    bad = ['loop = _load("loop")\n', "import autowatch\n", "from ledger import safe_append\n",
           "x = loop._ensure_claimed(a)\n", "y = _ARMS_CLAIM\n", 'shell_policy = _load("shell_policy")\n']
    assert [bool(arming_or_loading(src)) for src in bad] == [True, True, True, True, True, False]


def test_import_and_a_run_leave_no_state_behind(tmp_path, monkeypatch):
    """Loading the modules starts, sends and writes nothing; a verify and a git read leave .sdlc and the home untouched."""
    monkeypatch.setattr(support.sys, "dont_write_bytecode", True)
    with attempt_trap.AttemptTrap() as trap:
        br = support.build("bounded_run", support.source("bounded_run"))
        ug = support.build("unattended_git", support.source("unattended_git"))
    assert trap.quiet, (trap.processes, trap.network, trap.writes)
    support.hermetic(tmp_path, monkeypatch)
    main = support.make_repo(tmp_path)
    support.trust(main)
    scratch = support.add_worktree(main, tmp_path / "scratch")
    state = main / ".sdlc" / "state"
    state.mkdir(parents=True)
    (main / ".sdlc" / "config.json").write_text("{}", encoding="utf-8")
    watched = (main / ".sdlc", tmp_path / "home")
    before = attempt_trap.snapshot(*watched)
    assert br.run_verify("exit 0", scratch, 30).outcome == br.OK
    assert ug.make_runner({"default": 30})(scratch, ["git", "rev-parse", "HEAD"])
    assert attempt_trap.tree_diff(before, attempt_trap.snapshot(*watched)) == ([], [], [])
    br.run_verify("echo claimed > %s" % (state / "claim.json"), scratch, 30)
    assert attempt_trap.tree_diff(before, attempt_trap.snapshot(*watched))[0], "the snapshot must see a planted claim file"


def test_a_gated_caller_is_quiet_while_closed(tmp_path, monkeypatch):
    """A caller written the documented way (gate, then the settings, then the runner) never reaches the runner closed."""
    br = support.load("bounded_run")
    fu = support.real("feature_upkeep")
    support.hermetic(tmp_path, monkeypatch)
    main = support.make_repo(tmp_path)
    support.trust(main)
    scratch = support.add_worktree(main, tmp_path / "scratch")
    marker = tmp_path / "ran"
    asked = []
    real = br.run_verify
    monkeypatch.setattr(br, "run_verify", lambda *a, **k: asked.append(a[2]) or real(*a, **k))

    @fu.gated("project")
    def verify_unit(config, worktree, command):
        return br.run_verify(command, worktree, fu.read(config).settings["verify.timeout_minutes"] * 60)

    command = "touch %s" % marker
    closed = [{}, {"upkeep": {"enabled": False}}, {"upkeep": {"enabled": "true"}},
              {"upkeep": {"enabled": True, "verify": {"timeout_minutes": 0}}},
              {"upkeep": {"enabled": True, "verify": {"timeout_minutes": True}}}]
    with attempt_trap.AttemptTrap() as trap:
        got = [verify_unit(config, scratch, command) for config in closed]
    assert trap.quiet and [g.get("closed") for g in got] == [True] * len(closed), (trap.processes, got)
    assert asked == [] and not marker.exists(), "a closed gate must never reach the runner"
    spec = fu.SCHEMA["verify.timeout_minutes"]
    assert br.seconds(spec["lo"] * 60) == 60.0 and br.seconds(spec["hi"] * 60) == float(br.MAX_SECONDS)
    assert fu.DEFAULTS["verify.timeout_minutes"] * 60 == 3600, "the gate's default budget is an hour"
    opened = verify_unit({"upkeep": {"enabled": True, "verify": {"timeout_minutes": 2}}}, scratch, command)
    assert (opened.outcome, marker.exists(), asked) == (br.OK, True, [120]), tuple(opened)
    assert verify_unit({"upkeep": {"enabled": True}}, scratch, "exit 0").outcome == br.OK and asked == [120, 3600]
    with attempt_trap.AttemptTrap() as ungated_trap:        # the control: without the gate the same call is not quiet
        try:
            verify_unit.__wrapped__({}, scratch, command)
        except Exception:                                    # noqa: BLE001 - the trap vetoes the launch
            pass
    assert not ungated_trap.quiet and ungated_trap.processes, "the trap must see a caller that skips the gate"
