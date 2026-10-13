"""#1319: Ready before Parked on a board migration, a rate limit named as one, doctor progress lines."""
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills"


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / rel)
    m = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS / rel).rsplit("/", 1)[0])
    spec.loader.exec_module(m)
    return m


# --- (1) Ready goes before Parked ------------------------------------------------------------------

OPTS = [{"id": "o1", "name": "Backlog", "color": "GRAY", "description": ""},
        {"id": "o2", "name": "In Progress", "color": "YELLOW", "description": ""},
        {"id": "o3", "name": "Parked", "color": "RED", "description": ""}]


def test_ready_is_placed_before_parked():
    m = _load("sigma-doctor/scripts/board_migrate.py", "bm1319")
    q = m.build_options_mutation("F", OPTS, "Ready")
    assert q.index('name: "In Progress"') < q.index('name: "Ready"') < q.index('name: "Parked"')
    assert q.count("id: ") == len(OPTS)             # every existing option still keeps its id


def test_ready_is_still_appended_without_a_parked_lane():
    m = _load("sigma-doctor/scripts/board_migrate.py", "bm1319b")
    q = m.build_options_mutation("F", OPTS[:2], "Ready")
    assert q.index('name: "Ready"') > q.index('name: "In Progress"')
    assert q.count('name: "Ready"') == 1


def test_apply_gesture_sends_ready_before_parked():
    m = _load("sigma-doctor/scripts/board_migrate.py", "bm1319c")
    sent = []

    def run(args):
        q = next((a for a in args if a.startswith("query=")), "")
        if "updateProjectV2Field" in q:
            sent.append(q)
            return json.dumps({"data": {"updateProjectV2Field": {"projectV2Field": {"id": "F"}}}})
        if "items(" in q:
            return json.dumps({"data": {"repositoryOwner": {"projectV2": {"items": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        return json.dumps({"data": {"repositoryOwner": {"projectV2": {"id": "P", "field": {
            "id": "F", "name": "Status", "options": OPTS}}}}})

    assert m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run) == 0
    assert len(sent) == 1 and sent[0].index('name: "Ready"') < sent[0].index('name: "Parked"')


# --- (2) a rate limit is named as one --------------------------------------------------------------

def _source(run):
    src = _load("sigma-loop/scripts/sources.py", "src1319")
    gh = src.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._RETRY_BASE = 0
    return gh


def _failing(text):
    def run(args):
        raise RuntimeError(text)
    return run


def test_a_rate_limited_owner_lookup_is_named_a_rate_limit():
    gh = _source(_failing("gh: unknown owner type (API rate limit exceeded for user ID 1)"))
    with pytest.raises(Exception) as ei:
        gh._run(["project", "view", "3", "--owner", "acme"])
    msg = str(ei.value)
    assert "rate limit" in msg.lower() and msg.count("unknown owner type") <= 1 and "gh may report" in msg


def test_a_plain_unknown_owner_type_is_left_as_it_was():
    gh = _source(_failing("unknown owner type"))
    with pytest.raises(Exception) as ei:
        gh._run(["project", "view", "3", "--owner", "acme"])
    assert "unknown owner type" in str(ei.value)


# --- (3) doctor progress lines ---------------------------------------------------------------------

def _doctor(tmp_path, env_extra):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "SIGMA_DOCTOR_PROGRESS"}
    env.update(env_extra)
    return subprocess.run([sys.executable, str(SCRIPTS / "sigma-doctor/scripts/doctor.py"), "check", str(sdlc)],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=300)


def test_progress_lines_appear_only_when_opted_in(tmp_path):
    on = _doctor(tmp_path, {"SIGMA_DOCTOR_PROGRESS": "1"})
    off = _doctor(tmp_path, {})
    assert "sigma-doctor: checking" in on.stderr
    assert "sigma-doctor: checking" not in off.stderr
    assert on.stdout == off.stdout                  # stdout is byte-identical either way


def test_a_tty_stderr_turns_progress_on(tmp_path, monkeypatch):
    d = _load("sigma-doctor/scripts/doctor.py", "doc1319")
    monkeypatch.delenv("SIGMA_DOCTOR_PROGRESS", raising=False)

    class Tty(io.StringIO):
        def isatty(self):
            return True

    err = Tty()
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(d, "check", lambda *a, **k: (d._progress("x"), [])[1])
    d.main(["doctor.py", "check", str(tmp_path / ".sdlc")])
    assert "sigma-doctor: checking x" in err.getvalue()
    assert d._PROGRESS_ON is False                   # restored after the run
