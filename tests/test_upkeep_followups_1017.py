"""Goal 1017: the three upkeep follow-ups. No network, no model."""
import importlib.util
import json
import pathlib

import upkeep_support as support

ROOT = pathlib.Path(__file__).resolve().parents[1]
OPEN = {"upkeep": {"enabled": True}}
CLOSED = {}


def _brief():
    path = ROOT / "skills" / "sigma-rebase" / "scripts" / "rebase_brief.py"
    spec = importlib.util.spec_from_file_location("rebase_brief_1017", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _rebase_world(tmp_path, work):
    sdlc = tmp_path / ".sdlc"
    rec = sdlc / "state" / "work" / "5.json"
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"worktree": str(tmp_path), "branch": "sdlc/5", "base": "main", "remote": "origin"}))
    return str(sdlc)


def _run_rebase(tmp_path, monkeypatch, config):
    work = support.script("work")
    sdlc = _rebase_world(tmp_path, work)
    calls = []
    monkeypatch.setattr(work.state, "reanchor_content", lambda s, g: calls.append((s, g)) or True)
    monkeypatch.setattr(work, "_replay_would_lose", lambda *a, **k: None)
    monkeypatch.setattr(work, "_push_refused", lambda *a, **k: None)
    monkeypatch.setattr(work, "_rerecord_cut_tip", lambda *a, **k: None)
    out = work.rebase(sdlc, config, "5", run=lambda cwd, argv: "")
    return out, calls


def test_goal_rebase_reanchors_with_the_gate_open(tmp_path, monkeypatch):
    out, calls = _run_rebase(tmp_path, monkeypatch, OPEN)
    assert out == "rebased"
    assert len(calls) == 1 and calls[0][1] == "5"


def test_goal_rebase_leaves_evidence_alone_with_the_gate_closed(tmp_path, monkeypatch):
    out, calls = _run_rebase(tmp_path, monkeypatch, CLOSED)
    assert out == "rebased"
    assert calls == []


def _acks(sdlc):
    path = sdlc / "state" / "upkeep" / "acks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"units": {"u": [{"sha": "a" * 40, "patch_id": "pid"}]}}))


def _boom(cwd, argv):
    raise RuntimeError("x")


def test_leftover_runtime_acks_are_ignored_once_the_gate_is_closed(tmp_path):
    rebase = support.script("feature_rebase")
    sdlc = tmp_path / ".sdlc"
    _acks(sdlc)
    closed = rebase._acked(str(sdlc), _boom, str(tmp_path), "u", "origin/main", config=CLOSED)
    assert closed == (set(), set())
    opened = rebase._acked(str(sdlc), _boom, str(tmp_path), "u", "origin/main", config=OPEN)
    assert opened == ({"pid"}, {"a" * 40})


def test_attempt_rebase_never_raises_when_the_lease_tip_cannot_be_read(tmp_path):
    m = _brief()

    def run(cwd, argv):
        if argv[:2] == ["git", "rev-list"]:
            return "1"
        if argv[:2] == ["git", "for-each-ref"]:
            raise RuntimeError("cannot read refs")
        return "abc"

    report = m.attempt_rebase(run, str(tmp_path), "origin", "feature/x", "main", lease=True)
    assert report["outcome"] == m.FAILED
    assert "cannot read refs" in report["why"]
