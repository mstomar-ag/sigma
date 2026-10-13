"""#1228: the `action` and `permission_use` log kinds, so the record-done hard-stop net has text to read.

Controls run once each (named in the docstrings): the net call removed from `loop.py record done` (refusal test red),
`permission_use` dropped from the stop test (release test red), the required-text check removed (missing-text test
red), the guard's own permission_use write removed (park test red).
"""
import importlib.util
import json
import pathlib
import subprocess
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
LOOP = SCRIPTS / "loop.py"
SKILL = ROOT / "skills" / "sigma-loop" / "references" / "running.md"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


actionlog = _mod("actionlog")
G = _mod("hard_stop_guard")

ACTION = "terraform apply -auto-approve"
OPEN = {"action_log": {"enabled": True}, "rulebook": {"hard_stops": {"enabled": True}}}
CLOSED = {"action_log": {"enabled": True}}


def _sdlc(tmp_path, config):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(config))
    (d / "state" / "STATE.md").write_text("# Loop State\niteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return str(d)


def _loop(*args):
    return subprocess.run([sys.executable, str(LOOP), *args], capture_output=True, text=True)


def _log(d, kind, text):
    return _loop("log", d, "7-x.md", kind, "--action", text)


def _done(d):
    return _loop("record", d, "7-x.md", "done")


def test_cli_accepts_both_kinds_and_stores_the_text(tmp_path):
    d = _sdlc(tmp_path, OPEN)
    assert _log(d, "action", ACTION).returncode == 0
    assert _log(d, "permission_use", ACTION).returncode == 0
    rows = actionlog.read_goal(d, "7-x.md")
    assert [(r["kind"], r["action"]) for r in rows] == [("action", ACTION), ("permission_use", ACTION)]


def test_missing_or_empty_action_is_refused(tmp_path):
    """Red against the broken control (required-text check removed): an empty entry would be written."""
    d = _sdlc(tmp_path, OPEN)
    for kind in ("action", "permission_use"):
        assert _loop("log", d, "7-x.md", kind).returncode == 2
        assert _loop("log", d, "7-x.md", kind, "--action", "  ").returncode == 2
    assert actionlog.read_goal(d, "7-x.md") == []


def test_unreleased_hard_stop_action_refuses_record_done(tmp_path):
    """Red against the broken control (net call removed from record done)."""
    d = _sdlc(tmp_path, OPEN)
    _log(d, "action", ACTION)
    r = _done(d)
    assert r.returncode == 4 and "hard-stop" in r.stderr


def test_permission_use_with_same_text_releases(tmp_path):
    """Red against the broken control (permission kinds not counted as stops)."""
    d = _sdlc(tmp_path, OPEN)
    _log(d, "action", ACTION)
    _log(d, "permission_use", ACTION)
    r = _done(d)
    assert r.returncode != 4 and "hard-stop" not in r.stderr


def test_permission_use_with_other_text_does_not_release(tmp_path):
    """Red against the broken control (stops matched by kind only, not by text)."""
    d = _sdlc(tmp_path, OPEN)
    _log(d, "action", ACTION)
    _log(d, "permission_use", "something else entirely")
    assert _done(d).returncode == 4


def test_park_entry_with_same_text_releases():
    log = [{"kind": "action", "action": ACTION}, {"kind": "park", "action": ACTION}]
    assert G.record_done_net(log, OPEN) == []
    assert G.record_done_net(log[:1], OPEN) != []


def test_harmless_action_passes(tmp_path):
    d = _sdlc(tmp_path, OPEN)
    _log(d, "action", "ls -la")
    assert "hard-stop" not in _done(d).stderr


def test_gate_closed_reads_nothing(tmp_path):
    d = _sdlc(tmp_path, CLOSED)
    _log(d, "action", ACTION)
    assert "hard-stop" not in _done(d).stderr
    assert G.record_done_net(actionlog.read_goal(d, "7-x.md"), CLOSED) == []


def test_secret_in_the_text_is_scrubbed_in_the_stored_entry(tmp_path):
    d = _sdlc(tmp_path, OPEN)
    shape = "".join(["ghp", "_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"])
    _log(d, "action", "terraform apply --token " + shape)
    stored = actionlog.log_path(d, "7-x.md").read_text()
    assert shape not in stored


class _Src:
    def __init__(self, sdlc_dir):
        self.sdlc_dir = sdlc_dir
        self.parked = []

    def park(self, issue, reason):
        self.parked.append((issue, reason))


def test_guard_park_writes_one_permission_use_entry(tmp_path):
    """Red against the broken control (the permission_use write removed from park_issue)."""
    d = _sdlc(tmp_path, OPEN)
    src = _Src(d)
    verdict = G.Verdict(True, "production_change", "x", "q", "hard stop: production_change (x)")
    G.park_issue(src, "7-x.md", verdict)
    rows = actionlog.read_goal(d, "7-x.md")
    assert src.parked and [r["kind"] for r in rows] == ["permission_use"]
    assert rows[0]["action"] == verdict.reason


def test_guard_park_without_sdlc_dir_still_parks_and_never_raises():
    src = types.SimpleNamespace(park=lambda i, r: None)
    G.park_issue(src, "7", G.Verdict(True, "c", "p", "q", "hard stop: c (p)"))


def test_skill_text_names_both_kinds():
    text = SKILL.read_text()
    assert "`log .sdlc \"$goal\" action --action" in text and "`permission_use`" in text


def test_guard_park_carries_the_original_action_text_and_releases_the_net(tmp_path):
    """#1240. Red against the broken control (the verdict reason written again instead of the action text)."""
    d = _sdlc(tmp_path, OPEN)
    src = _Src(d)
    _log(d, "action", ACTION)
    verdict = G.guard(ACTION, {}, OPEN)
    assert verdict.blocked and verdict.action == ACTION
    G.park_issue(src, "7-x.md", verdict)
    rows = actionlog.read_goal(d, "7-x.md")
    assert [r["kind"] for r in rows] == ["action", "permission_use"]
    assert rows[1]["action"] == ACTION
    assert G.record_done_net(rows, OPEN) == []
    assert _done(d).returncode != 4


def test_guard_park_without_action_text_keeps_the_reason(tmp_path):
    d = _sdlc(tmp_path, OPEN)
    G.park_issue(_Src(d), "7-x.md", G.Verdict(True, "c", "p", "q", "hard stop: c (p)"))
    assert actionlog.read_goal(d, "7-x.md")[0]["action"] == "hard stop: c (p)"
