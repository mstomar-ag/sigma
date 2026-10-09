"""#917: the harness around the upkeep gate -- tests that are GREEN before the gate exists.

NOT planned tests of the pull-request red-to-green gate (they are not listed in the plan, because a test that
passes at baseline can never carry a recorded red): the recording trap's own controls, the static first-call guard
and its planted counter-examples, the ungated probe, the pick-time control, the scrub of the machine variable, the
template parse, the scan for readers of the block, and the rules the three planned files must obey. They run in the
orchestrator's full verify. Tests that need the gate live in the three planned files."""
import ast
import os
import pathlib
import socket
import subprocess
import threading

import pytest

import attempt_trap
from attempt_trap import AttemptTrap, Vetoed
import upkeep_support as support

TESTS = pathlib.Path(__file__).resolve().parent
PLANNED_FILES = ("test_feature_upkeep.py", "test_upkeep_tmpl.py", "test_upkeep_proof.py")
MAX_NODE_ID = 53


# ------------------------------------------------------------------------------------------ the trap itself

def test_trap_vetoes_process_launches(tmp_path):
    """A process launch is recorded and refused, so a control never runs a real command."""
    marker = tmp_path / "marker"
    with AttemptTrap() as trap:
        for attempt in (lambda: subprocess.run(["upkeep-trap-control-tool", "--flag"]),
                        lambda: os.system("touch " + str(marker)),
                        lambda: subprocess.Popen(["touch", str(marker)])):
            with pytest.raises(Vetoed):
                attempt()
    assert [program for _event, program in trap.processes] == ["upkeep-trap-control-tool", "touch", "touch"]
    assert not marker.exists(), "the veto must stop the command from running"


def test_trap_classifies_model_launches():
    """A model launch is classified apart, also through a shell string, `sh -c` and `bash -lc`; the launch is always recorded.

    Real launches use a stand-in name that is installed nowhere; the built-in names are classified from the audit
    shapes the real calls produce, so no test can start a real model even if the veto were broken."""
    stand_in = "upkeep-model-stand-in"
    with AttemptTrap(model_programs=(stand_in,)) as trap:
        for attempt in (lambda: subprocess.run(["/nonexistent/" + stand_in, "-p", "x"]),
                        lambda: subprocess.run(stand_in + " -p x", shell=True),
                        lambda: subprocess.run(["bash", "-lc", stand_in + " run"]),
                        lambda: os.system(stand_in + " run"),
                        lambda: subprocess.run(["upkeep-trap-control-tool"]),
                        lambda: subprocess.run("echo hello && " + stand_in, shell=True)):
            with pytest.raises(Vetoed):
                attempt()
    assert trap.models == [stand_in] * 4
    assert len(trap.processes) == 6                               # the launch is always recorded, whatever its class
    shapes = [("subprocess.Popen", ("/usr/local/bin/claude", ["/usr/local/bin/claude", "-p", "x"], None, None), "claude"),
              ("subprocess.Popen", ("/bin/sh", ["/bin/sh", "-c", "codex exec x"], None, None), "codex"),
              ("subprocess.Popen", ("/bin/bash", ["bash", "-lc", "cursor-agent run"], None, None), "cursor-agent"),
              ("os.system", (b"claude -p x",), "claude"), ("os.posix_spawn", ("/bin/echo", ["echo", "x"], {}), None)]
    for event, args, program in shapes:
        built_in = AttemptTrap()
        with pytest.raises(Vetoed):
            built_in._see(event, args)
        assert built_in.models == ([program] if program else []), (event, args)


def test_trap_vetoes_network_attempts():
    """A name lookup and a request are recorded and refused, so a control never leaves the machine."""
    import urllib.request
    address = "http" + "://upkeep-trap.invalid/"
    with AttemptTrap() as trap:
        with pytest.raises(Vetoed):
            socket.getaddrinfo("upkeep-trap.invalid", 443)
        with pytest.raises(Vetoed):
            urllib.request.urlopen(address)
    assert [event for event, _arg in trap.network] == ["socket.getaddrinfo", "urllib.Request"]


def test_trap_records_file_mutations(tmp_path):
    """A write, a mkdir, a rename and an os.open write are recorded and really happen."""
    with AttemptTrap() as trap:
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "d").mkdir()
        (tmp_path / "a.txt").replace(tmp_path / "b.txt")
        os.close(os.open(str(tmp_path / "c.bin"), os.O_WRONLY | os.O_CREAT))
    assert str(tmp_path / "a.txt") in trap.writes and str(tmp_path / "d") in trap.writes
    assert str(tmp_path / "c.bin") in trap.writes, "a write through os.open flags is a write"
    assert (tmp_path / "b.txt").exists() and (tmp_path / "d").is_dir(), "writes are recorded, never vetoed"


def test_trap_ignores_reads_threads_and_the_time_after_it_closes(tmp_path):
    """A read, another thread and the time after the window closes are none of the trap's business."""
    target = tmp_path / "r.txt"
    target.write_text("x")
    with AttemptTrap() as trap:
        target.read_text()
        worker = threading.Thread(target=lambda: (tmp_path / "t.txt").write_text("y"))
        worker.start()
        worker.join()
        assert trap.quiet
    (tmp_path / "after.txt").write_text("z")
    assert trap.quiet and trap.writes == []


# ------------------------------------------------------------------------------------------ the static guard

def test_static_guard_passes_the_shipped_tree_and_the_probe():
    """No shipped function is gated yet and none is registered; the probe and the clean one-liners pass."""
    offenders = []
    for path in support.shipped_scripts():
        text = path.read_text(encoding="utf-8")
        registered = support.REGISTERED_ENTRY_POINTS.get(path.stem, set())
        if "gated" in text or registered:
            offenders += [path.name + ": " + reason for reason in support.entry_point_offenders(text, registered)]
    for stem in support.REGISTERED_ENTRY_POINTS:
        if not any(p.stem == stem for p in support.shipped_scripts()):
            offenders.append(stem + ": registered but the file is missing")
    assert offenders == []
    assert support.entry_point_offenders(support.PROBE_SOURCE, support.PROBE_NAMES) == []
    assert support.entry_point_offenders("@feature_upkeep.gated('project')\ndef run(config):\n    pass\n", {"run"}) == []
    assert support.entry_point_offenders("from feature_upkeep import gated\n@gated('machine')\ndef run(config, *, environ=None):\n    pass\n", {"run"}) == []


@pytest.mark.parametrize("source,registered,expected", [
    ("def run(config):\n    return 1\n", {"run"}, "registered but not gated"),
    ("@other()\n@feature_upkeep.gated('project')\ndef run(config):\n    pass\n", {"run"}, "outermost decorator"),
    ("@feature_upkeep.gated\ndef run(config):\n    pass\n", {"run"}, "write @gated(door)"),
    ("@feature_upkeep.gated('project')\ndef other(config):\n    pass\n", {"run"}, "gated but not registered"),
    ("class C:\n    @feature_upkeep.gated('project')\n    def run(self, config):\n        pass\n", set(), "module-level"),
    ("@feature_upkeep.gated('project')\nasync def run(config):\n    pass\n", {"run"}, "async"),
], ids=["no_decorator", "decorator_outside", "bare_decorator", "unregistered", "inside_class", "async"])
def test_static_guard_flags_each_planted_counter_example(source, registered, expected):
    """Each planted counter-example is flagged for its own reason (the guard seen red)."""
    reasons = support.entry_point_offenders(source, registered)
    assert any(expected in reason for reason in reasons), reasons


def test_an_ungated_probe_is_caught_by_the_trap_and_the_static_guard(tmp_path, monkeypatch):
    """A probe with its decorators stripped makes every attempt, and the static guard flags it."""
    stripped = "\n".join(line for line in support.PROBE_SOURCE.splitlines() if "@feature_upkeep.gated" not in line)
    assert stripped != support.PROBE_SOURCE
    probe = support.probe(None, stripped)
    result, trap, diff, _project = support.run_case(tmp_path, monkeypatch, lambda c, s, e: probe.attempt_project(c, s), {})
    assert result == {"ran": True}
    assert trap.processes and trap.models and trap.network and trap.writes and diff[0]
    assert support.entry_point_offenders(stripped, support.PROBE_NAMES)


# ------------------------------------------------------------------------------------------ the pick-time control

def test_the_pick_time_half_notices_an_added_call(tmp_path):
    """The control for the pick-time proof: an added call under an enabled block changes the call list."""
    import test_docs as docs

    def add_a_call(rebase, calls):
        real = rebase.switch

        def switch(config):
            if (config.get("upkeep") or {}).get("enabled") is True:
                calls.append("git fetch --extra")
            return real(config)
        rebase.switch = switch
    base = docs._calls_for_one_started_goal(tmp_path / "base", True)
    assert len(base) == 17
    changed = docs._calls_for_one_started_goal(tmp_path / "changed", True, upkeep={"enabled": True}, hook=add_a_call)
    assert "git fetch --extra" in changed and len(changed) == 18
    assert len(docs._calls_for_one_started_goal(tmp_path / "off", True, upkeep={"enabled": False}, hook=add_a_call)) == 17


# ------------------------------------------------------------------------------------------ the environment and the template

def test_the_suite_scrubs_the_machine_variable():
    """Green now; the conftest fixture keeps it green in a shell that exports the variable (--noconftest shows it red)."""
    assert "SIGMA_UPKEEP_JOB" not in os.environ


def test_the_template_parses_and_a_duplicate_key_is_refused():
    """The shipped template parses with no duplicate key, and a duplicate would be refused."""
    assert isinstance(support.parse_template(), dict)
    with pytest.raises(ValueError):
        support.parse_template('{"a": 1, "a": 2}')


# ------------------------------------------------------------------------------------------ readers of the block

def test_no_shipped_script_other_than_the_gate_names_the_block():
    """No shipped Python other than the gate names the upkeep key (green before and after the gate exists)."""
    assert support.readers_outside_the_gate() == []


def test_the_reader_scan_sees_planted_reads_and_skips_the_one_command_verb():
    """The scan flags a subscript, get, pop, setdefault and membership test, and skips only the command verb."""
    flagged = [
        'x = config["upkeep"]\n', 'x = config.get("upkeep")\n', 'x = config.pop("upkeep", None)\n',
        'if "upkeep" in config:\n    pass\n', 'x = state.load_config(d).setdefault("upkeep", {})\n',
    ]
    assert [support.upkeep_literals(src) for src in flagged] == [[1], [1], [1], [1], [1]]
    assert support.upkeep_literals('if len(argv) >= 4 and argv[1] == "upkeep":\n    pass\n') == []
    assert support.upkeep_literals('if verb == "upkeep":\n    pass\n') == [1]
    real = (support.SCRIPTS / "feature_rebase.py").read_text(encoding="utf-8")
    assert 'argv[1] == "upkeep"' in real and support.upkeep_literals(real) == []


def test_the_tree_scan_finds_a_planted_reader_in_each_shipped_directory(tmp_path):
    """The tree scan finds a planted reader under skills, hooks and tools, and exempts only the real gate path."""
    files = {"skills/x/scripts/y.py": 'cfg["upkeep"]\n', "hooks/h.py": 'cfg.get("upkeep")\n',
             "tools/deep/t.py": 'x = 1\n"upkeep" in cfg\n', "tools/ok.py": 'if argv[1] == "upkeep":\n    pass\n',
             "skills/sigma-loop/scripts/feature_upkeep.py": 'BLOCK = "upkeep"\n',
             "tools/feature_upkeep.py": 'cfg["upkeep"]\n', "skills/x/scripts/feature_upkeep.py": 'cfg.get("upkeep")\n'}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert support.readers_outside_the_gate(tmp_path) == ["hooks/h.py:1", "skills/x/scripts/feature_upkeep.py:1",
                                                         "skills/x/scripts/y.py:1", "tools/deep/t.py:2",
                                                         "tools/feature_upkeep.py:1"]


# ------------------------------------------------------------------------------------------ the rules the planned files obey

def _planned_functions():
    for name in PLANNED_FILES:
        tree = ast.parse((TESTS / name).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                yield name, node


def test_the_planned_files_obey_the_red_to_green_rules():
    """No parametrisation, no print, no skip, a short node id: what lets the pull-request gate credit a red."""
    offences = []
    for name, node in _planned_functions():
        node_id = "tests/%s::%s" % (name, node.name)
        if len(node_id) > MAX_NODE_ID:
            offences.append("%s is %d characters (limit %d)" % (node_id, len(node_id), MAX_NODE_ID))
        if node.decorator_list:
            offences.append(node_id + " is decorated (no parametrise, no skip, no xfail)")
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "print":
                offences.append(node_id + " prints")
            if isinstance(sub, ast.Attribute) and sub.attr in ("skip", "skipif", "xfail", "importorskip"):
                offences.append(node_id + " skips")
    for name in PLANNED_FILES:
        assert (TESTS / name).is_file(), name
    assert offences == []
    assert len(list(_planned_functions())) >= 40
