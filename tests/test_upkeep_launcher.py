"""#949: the resolver launcher, run only against a FAKE `claude` the tests write into tmp_path. No real model, no network.

The five controls (each run once with the guarded line broken, seen red, then restored) are tests 1 to 5 in the order the
plan lists them. The gesture is the documented one: `python -m pytest tests/test_upkeep_launcher.py -q`.
"""
import importlib.util
import json
import os
import pathlib
import stat
import sys

import pytest

import attempt_trap

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
PATH = SCRIPTS / "feature_upkeep_launcher.py"
MODEL = "test-model-id-1"
CAP = 2.0
METERED = 0.5            # 100,000 input tokens at the shipped list price for the model the fake transcript names

FAKE = '''#!%(python)s
import json, os, pathlib, sys, subprocess, time
here = pathlib.Path(%(dir)r)
mode = json.loads((here / "mode.json").read_text())
stdin = sys.stdin.read()
(here / "marker.json").write_text(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ), "stdin": stdin, "cwd": os.getcwd()}))
work = pathlib.Path(os.getcwd())
if mode.get("edit_listed"):
    (work / "a.txt").write_text("resolved\\n")
if mode.get("edit_unlisted"):
    (work / "other.txt").write_text("tampered\\n")
if mode.get("add_file"):
    (work / "new.txt").write_text("new\\n")
if mode.get("symlink"):
    os.remove(work / "b.txt")
    os.symlink("a.txt", work / "b.txt")
if mode.get("chmod"):
    os.chmod(work / "b.txt", 0o755)
if not mode.get("no_transcript"):
    d = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "x"
    d.mkdir(parents=True)
    usage = {"input_tokens": 100000, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
    line = {"type": "assistant", "timestamp": "2026-09-01T00:00:00Z",
            "message": {"role": "assistant", "model": "claude-opus-5", "id": "m1", "usage": usage}}
    (d / "s.jsonl").write_text(json.dumps(line) + "\\n")
if mode.get("sleep"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    (here / "child.pid").write_text(str(child.pid))
    time.sleep(120)
print("done")
'''


class Rig:
    def __init__(self, tmp, monkeypatch):
        self.tmp = tmp
        self.fakedir = tmp / "fake"
        self.fakedir.mkdir()
        self.binary = self.fakedir / "claude"
        self.binary.write_text(FAKE % {"python": sys.executable, "dir": str(self.fakedir)})
        self.binary.chmod(0o755)
        self.work = tmp / "work" / "dir"
        self.work.mkdir(parents=True)
        (self.work / "a.txt").write_text("conflict\n")
        (self.work / "b.txt").write_text("b\n")
        (self.work / "other.txt").write_text("o\n")
        for name in ("state", "sdlc", "scratch", "repo", "home"):
            (tmp / name).mkdir()
        self.mode({})
        self.launcher = load()

    def mode(self, mapping):
        (self.fakedir / "mode.json").write_text(json.dumps(mapping))

    def marker(self):
        path = self.fakedir / "marker.json"
        return json.loads(path.read_text()) if path.exists() else None

    def request(self, **over):
        config = {"upkeep": {"enabled": True, "conflicts": {"resolve": "agent"}}, "ledger": {"enabled": True}}
        fields = dict(config=config, sdlc_dir=str(self.tmp / "sdlc"), state_dir=str(self.tmp / "state"),
                      repo_root=str(self.tmp / "repo"), directory=str(self.work), conflicted=["a.txt"],
                      binary=str(self.binary), prompt="Resolve the conflict.", blocks=[("hunks", "SECRET-HUNK-TEXT")],
                      timeout=20, cap_usd=CAP, model=MODEL, flags=(), credential_var=None,
                      scratch_parent=str(self.tmp / "scratch"), rates=None, ceiling_machine_usd=10.0,
                      ceiling_team_usd=10.0, budget_seconds=5.0, home=str(self.tmp / "home"))
        fields.update(over)
        return self.launcher.Request(**fields)


def load(source=None, edit=None):
    src = PATH.read_text(encoding="utf-8")
    if edit:
        old, new = edit
        assert old in src, "mutation target has drifted out of the source: %r" % (old,)
        src = src.replace(old, new)
    spec = importlib.util.spec_from_file_location("feature_upkeep_launcher_t", PATH)
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__file__"] = str(PATH)
    exec(compile(src, str(PATH), "exec"), module.__dict__)          # noqa: S102 - test-only
    return module


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGMA_UPKEEP_JOB", raising=False)
    return Rig(tmp_path, monkeypatch)


# ------------------------------------------------------------------------------------------ control 1: confinement

@pytest.mark.parametrize("mode", ["edit_unlisted", "add_file", "symlink", "chmod"])
def test_confinement_discards_and_charges(rig, mode):
    rig.mode({"edit_listed": True, mode: True})
    result = rig.launcher.launch(rig.request())
    assert result.outcome == "discarded", result
    assert result.changed, result
    assert result.charged_usd == pytest.approx(METERED)


def test_confinement_allows_the_conflicted_file_alone(rig):
    rig.mode({"edit_listed": True})
    result = rig.launcher.launch(rig.request())
    assert (result.outcome, result.changed) == ("ok", ())
    assert result.charged_usd == pytest.approx(METERED)
    assert (rig.work / "a.txt").read_text() == "resolved\n"


def test_charge_is_written_locally_and_as_a_note(rig):
    rig.mode({"edit_listed": True})
    rig.launcher.launch(rig.request())
    records = json.loads((rig.tmp / "state" / rig.launcher.SPEND_FILE).read_text())["records"]
    assert [r["cost_usd"] for r in records] == [pytest.approx(METERED)]
    notes = [e for e in rig.launcher._sibling("ledger").read_all(str(rig.tmp / "sdlc")) if str(e.get("ref", "")).startswith("upkeep:resolver-spend:")]
    assert len(notes) == 1 and "addressed" not in notes[0]
    assert not list((rig.tmp / "scratch").iterdir()), "the scratch tree this run made is removed"


# ------------------------------------------------------------------------------------------ control 2: killed means the full cap

def test_timeout_kills_the_group_and_charges_the_cap(rig):
    rig.mode({"sleep": True})
    result = rig.launcher.launch(rig.request(timeout=1))
    assert result.outcome == "killed", result
    assert result.charged_usd == CAP and result.charged_usd != METERED
    pid = int((rig.fakedir / "child.pid").read_text())
    with pytest.raises(OSError):
        for _ in range(50):
            os.kill(pid, 0)
            import time
            time.sleep(0.1)


def test_a_missing_transcript_charges_the_cap(rig):
    rig.mode({"no_transcript": True})
    result = rig.launcher.launch(rig.request())
    assert result.charged_usd == CAP


# ------------------------------------------------------------------------------------------ control 3: confirmed flags only

@pytest.mark.parametrize("flag", ["--max-budget-usd", "--permission-mode", "--not-a-real-flag"])
def test_an_unverified_flag_is_refused_before_the_spawn(rig, flag):
    result = rig.launcher.launch(rig.request(flags=((flag, "1"),)))
    assert result.outcome == "refused" and flag in result.reason and "UNVERIFIED" in result.reason
    assert rig.marker() is None, "the fake ran: the flag check did not come first"


def test_a_confirmed_flag_passes_and_the_table_is_labelled(rig):
    result = rig.launcher.launch(rig.request(flags=(("--output-format", "json"),)))
    assert result.outcome == "ok" or result.outcome == "failed" or result.outcome == "discarded"
    assert rig.marker() is not None
    src = PATH.read_text(encoding="utf-8")
    assert "UNVERIFIED" in src and all(f in rig.launcher.UNVERIFIED_FLAGS for f in ("--max-budget-usd", "--allowedTools"))
    assert not set(rig.launcher.CONFIRMED_FLAGS) & set(rig.launcher.UNVERIFIED_FLAGS)


# ------------------------------------------------------------------------------------------ control 4: ceilings and the guard

def test_the_machine_ceiling_refuses_before_the_spawn(rig):
    result = rig.launcher.launch(rig.request(ceiling_machine_usd=CAP - 0.01))
    assert result.outcome == "refused" and "ceiling" in result.reason
    assert rig.marker() is None


def test_an_unreadable_machine_store_is_closed(rig):
    (rig.tmp / "state" / rig.launcher.SPEND_FILE).write_text("{not json")
    result = rig.launcher.launch(rig.request())
    assert result.outcome == "refused" and "unreadable" in result.reason and rig.marker() is None


def test_the_entry_file_guard_refuses_without_reading_the_ledger(rig, monkeypatch):
    entries = pathlib.Path(rig.launcher._sibling("ledger").entries_dir(str(rig.tmp / "sdlc")))
    entries.mkdir(parents=True)
    for n in range(10001):
        (entries / ("w%05d.jsonl" % n)).write_bytes(b"")
    calls = []
    ledger = rig.launcher._sibling("ledger")
    real = ledger.read_all
    monkeypatch.setattr(ledger, "read_all", lambda *a, **k: calls.append(1) or real(*a, **k))
    result = rig.launcher.launch(rig.request())
    assert result.outcome == "refused" and "parked" in result.reason
    assert calls == [] and rig.marker() is None


def test_an_unreadable_team_ledger_is_closed(rig, monkeypatch):
    ledger = rig.launcher._sibling("ledger")

    def boom(*a, **k):
        raise OSError("gone")
    monkeypatch.setattr(ledger, "read_all", boom)
    result = rig.launcher.launch(rig.request())
    assert result.outcome == "refused" and "unreadable" in result.reason and rig.marker() is None


def test_the_team_ceiling_is_summed_from_notes(rig):
    ledger = rig.launcher._sibling("ledger")
    ledger.safe_append(str(rig.tmp / "sdlc"), "note", "upkeep-resolver", config=rig.request().config,
                       ref="upkeep:resolver-spend:9.900000:old")
    result = rig.launcher.launch(rig.request())
    assert result.outcome == "refused" and "team" in result.reason and rig.marker() is None


# ------------------------------------------------------------------------------------------ control 5: gate closed, environment

def test_gate_closed_attempts_nothing_and_import_is_inert(rig):
    closed = rig.request(config={"upkeep": {"enabled": False}})
    with attempt_trap.AttemptTrap() as trap:
        module = load()
        result = module.launch(module.Request(**closed._asdict()))
    assert result.outcome == "closed"
    assert (trap.processes, trap.models, trap.network, trap.writes) == ([], [], [], [])
    assert rig.marker() is None and not list((rig.tmp / "state").iterdir())


def test_the_environment_is_built_from_nothing_and_the_prompt_is_on_stdin(rig, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("GIT_DIR", "/nowhere")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/nowhere")
    monkeypatch.setenv("SOME_OTHER_VARIABLE", "x")
    rig.launcher.launch(rig.request())
    seen = rig.marker()
    assert not [k for k in seen["env"] if k.startswith("GIT_") and k != "GIT_TERMINAL_PROMPT"]
    assert "MODEL_API_KEY" not in seen["env"] and "CLAUDE_PROJECT_DIR" not in seen["env"]
    assert "SOME_OTHER_VARIABLE" not in seen["env"]
    assert "Resolve the conflict." in seen["stdin"] and "SECRET-HUNK-TEXT" in seen["stdin"]
    assert "-BEGIN>>>" in seen["stdin"] and "-END>>>" in seen["stdin"]
    assert not any("Resolve" in a or "HUNK" in a for a in seen["argv"]), seen["argv"]
    assert seen["argv"][:3] == ["-p", "--output-format", "json"] and seen["argv"][-2:] == ["--model", MODEL]


def test_the_credential_is_passed_only_when_chosen(rig, monkeypatch):
    monkeypatch.setenv("CHOSEN_KEY", "chosen-value")
    monkeypatch.setenv("MODEL_API_KEY", "other")
    rig.launcher.launch(rig.request(credential_var="CHOSEN_KEY"))
    env = rig.marker()["env"]
    assert env.get("CHOSEN_KEY") == "chosen-value" and "MODEL_API_KEY" not in env
    result = rig.launcher.launch(rig.request(credential_var="UNSET_KEY_NAME"))
    assert result.outcome == "refused"


# ------------------------------------------------------------------------------------------ the other refusals and pins

def test_refusals_spawn_nothing(rig, tmp_path):
    cases = {
        "ancestor": (rig.request(directory=str(rig.work)), lambda: (rig.work.parent / "CLAUDE.md").write_text("x")),
        "placeholder": (rig.request(model=None), lambda: None),
        "repo": (rig.request(repo_root=str(rig.tmp / "work")), lambda: None),
        "home": (rig.request(home=str(rig.tmp / "work")), lambda: None),
    }
    for name, (request, setup) in cases.items():
        setup()
        result = rig.launcher.launch(request)
        assert result.outcome == "refused", (name, result)
        assert rig.marker() is None, name


def test_the_ancestor_list_equals_the_bench_launchers():
    spec = importlib.util.spec_from_file_location("bench_launcher_t", ROOT / "evals" / "bench" / "launcher" / "sigma_bench_launcher.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    assert tuple(load().INSTRUCTION_FILES) == tuple(bench.INSTRUCTION_FILES)


def test_the_catalog_ships_closed():
    module = load()
    assert set(module.CATALOG.values()) == {module.PLACEHOLDER_MODEL}
    assert module.resolve_model({}, None)[0] is None
    assert module.resolve_model({}, "bad id!")[0] is None
    assert module.resolve_model({}, MODEL) == (MODEL, None)
