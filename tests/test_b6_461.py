"""#461 -- B6: the event journal, the time store and the ledger stream files.

`docs/launch/dispositions/461.json` claims two stores are capped by existing pruners and that the
ledger streams are unbounded by decision.  A claim in a JSON file proves nothing, so each node
here drives the REAL write path (`ledger.append`, `timing_store.append`) against an aged file and
checks what survives.  The audit node runs the gesture copied out of `docs/launch/growth-audit.md`
in a scratch copy of the repository (the gesture overwrites the committed snapshot).
"""
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = ROOT / "skills" / "sigma-loop" / "scripts"
FILE = ROOT / "docs" / "launch" / "dispositions" / "461.json"
DOC = ROOT / "docs" / "launch" / "growth-audit.md"
DAY = 86400


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ledger = _mod("ledger")
timing_store = _mod("timing_store")

EVENTS = [".sdlc/events", ".sdlc/events/", ".sdlc/events/.last-prune",
          ".sdlc/events/<value>-<alias>.<value>.jsonl"]
TIME = [".sdlc/state/time", ".sdlc/state/time/", ".sdlc/state/time/.last-prune",
        ".sdlc/state/time/<goal>", ".sdlc/state/time/<goal>/",
        ".sdlc/state/time/<goal>/<value>.jsonl", ".sdlc/state/time/_sessions/<text>/",
        ".sdlc/state/time/_sessions/<text>/<value>.jsonl",
        ".sdlc/state/time/_sessions/<text>/turns.jsonl"]
UNBOUNDED = [".sdlc/ledger/", ".sdlc/ledger/<stream>", ".sdlc/ledger/<stream>/",
             ".sdlc/ledger/<stream>/<value>-<alias>.<value>.jsonl"]
DERIVED = [".sdlc/ledger/TEAM.md"]
ISSUE_PATTERNS = set(EVENTS + TIME + UNBOUNDED + DERIVED)

CONFIG = {"journal": {"enabled": True}, "ledger": {"enabled": True, "actor": "dana"}}


def _entries():
    """{} while the file is absent, so every node below fails by assertion rather than by error."""
    if not FILE.exists():
        return {}
    return {e["pattern"]: e for e in json.loads(FILE.read_text(encoding="utf-8"))}


def _claims(patterns, decision):
    entries = _entries()
    for pattern in patterns:
        assert entries.get(pattern, {}).get("decision", "").startswith(decision), pattern


@pytest.fixture(autouse=True)
def _fresh_process():
    """Both pruners are once-per-process behind a module flag; every node is a fresh process."""
    ledger._journal_pruned_this_process = False
    timing_store._pruned_this_process = False
    yield
    ledger._journal_pruned_this_process = False
    timing_store._pruned_this_process = False


def _aged(path, days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind":"x"}\n', encoding="utf-8")
    when = time.time() - days * DAY
    os.utime(path, (when, when))
    return path


def test_covers():
    entries = _entries()
    assert set(entries) == ISSUE_PATTERNS
    assert not any("unscanned" in e for e in entries.values())


def test_named():
    """The pruner a capped entry names must still exist, or the entry is a lie."""
    _claims(EVENTS + TIME, "capped")
    for pattern in EVENTS + TIME:
        cap = _entries()[pattern]["pruner_or_cap"]
        name = re.match(r"(ledger|timing_store)\.(\w+):", cap)
        assert name, cap
        assert callable(getattr(globals()[name.group(1)], name.group(2), None)), cap


def test_events(tmp_path):
    _claims(EVENTS, "capped")
    d = tmp_path / ".sdlc"
    events = d / "events"
    old = _aged(events / "ann-aaaaaaaa.11.jsonl", 31)
    keep = _aged(events / "bob-bbbbbbbb.12.jsonl", 29)
    # Another actor's file carrying THIS process's writer suffix: only the suffix guard keeps it, because
    # the append below writes `dana-<token>.jsonl`, not this path (a same-actor plant would be recreated).
    mine = _aged(events / f"ann-{ledger._instance_token()}.jsonl", 90)
    legacy = _aged(d / "ledger" / "events" / "ann-aaaaaaaa.11.jsonl", 400)
    ledger.append(d, CONFIG, "phase", "461", stream=ledger.EVENTS, phase="implement", state="end")
    assert not old.exists()
    assert keep.exists() and mine.exists() and legacy.exists()
    assert (events / ".last-prune").exists()


def test_time(tmp_path):
    _claims(TIME, "capped")
    d = tmp_path / ".sdlc"
    root = timing_store.store_dir(d)
    stale = timing_store.RETENTION_DAYS + 1
    old_goal = _aged(root / "9001" / "1.jsonl", stale)
    live_goal = _aged(root / "9002" / "1.jsonl", stale)
    _aged(root / "9002" / "2.jsonl", 1)
    old_session = _aged(root / "_sessions" / "s-old" / "turns.jsonl", stale)
    foreign = _aged(root / "9001" / "notes.txt", stale)
    timing_store.append(d, "461", "phase", "implement", 1000, started=1)
    assert not old_goal.exists() and not old_session.exists()
    assert live_goal.exists()
    assert foreign.exists()
    assert (root / ".last-prune").exists()


def test_unbounded(tmp_path):
    _claims(UNBOUNDED, "intentionally unbounded")
    _claims(DERIVED, "derived")
    d = tmp_path / ".sdlc"
    entry = _aged(d / "ledger" / "entries" / "ann-aaaaaaaa.11.jsonl", 400)
    team = _aged(d / "ledger" / "TEAM.md", 400)
    legacy = _aged(d / "ledger" / "events" / "ann-aaaaaaaa.11.jsonl", 400)
    _aged(d / "events" / "ann-aaaaaaaa.12.jsonl", 40)       # makes the journal sweep really run
    ledger.append(d, CONFIG, "note", "461", why="x")
    ledger.append(d, CONFIG, "phase", "461", stream=ledger.EVENTS, phase="implement", state="end")
    timing_store.append(d, "461", "phase", "implement", 1000, started=1)
    assert not (d / "events" / "ann-aaaaaaaa.12.jsonl").exists()      # the sweep ran ...
    assert entry.exists() and team.exists() and legacy.exists()       # ... and stopped at the ledger


def _gesture():
    block = re.search(r"```sh\n(.*?)```", DOC.read_text(encoding="utf-8"), re.S).group(1)
    line = next(l for l in block.splitlines() if "tools/readiness/growth_audit.py" in l)
    argv = shlex.split(line)
    assert argv[:2] == ["python3", "tools/readiness/growth_audit.py"]
    return [sys.executable] + argv[1:]


def test_audit(tmp_path):
    for name in ("skills", "hooks", "tools", "contract"):
        if (ROOT / name).exists():
            shutil.copytree(ROOT / name, tmp_path / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "docs" / "launch").parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "docs" / "launch", tmp_path / "docs" / "launch")
    (tmp_path / ".sdlc" / "state").mkdir(parents=True)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-C", str(tmp_path)]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "seed"]):
        subprocess.run(git + args, check=True, capture_output=True)
    done = subprocess.run(_gesture(), cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    result = json.loads((tmp_path / "docs" / "launch" / "growth-audit.json").read_text())
    unresolved = set(result["b6_disposition"]["unresolved_patterns"])
    assert not unresolved & ISSUE_PATTERNS
    rows = {r["pattern"]: r for r in result["store_measurements"]}
    for pattern in ISSUE_PATTERNS:
        assert rows[pattern]["pruner_or_cap"] == _entries()[pattern]["pruner_or_cap"]
        assert rows[pattern]["decision"] == _entries()[pattern]["decision"]
