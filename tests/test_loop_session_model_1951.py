"""#1951: the loop session's model is chosen (supervisor --model), observed (transcript) and checked.

Controls (each clause removed alone, seen red): the explicit-command guard, the key validation, the
above-tier comparison, the unavailable branch, the no-dispatched-tiers branch, the doctor row.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import resource_mode  # noqa: E402
import test_doctor as td  # noqa: E402

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _lsm():
    spec = importlib.util.spec_from_file_location("lsm", S / "loop_session_model.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---- effective(): the key ---------------------------------------------------------------------
def test_key_wins_over_the_auto_default():
    assert _lsm().effective({"model_selection": "auto", "loop": {"session_model": "opus"}}) == "opus"


def test_auto_defaults_to_sonnet():
    assert _lsm().effective({"model_selection": "auto"}) == "sonnet"


def test_unset_without_auto_is_no_flag():
    assert _lsm().effective({}) is None
    assert _lsm().effective({"model_selection": "off"}) is None


@pytest.mark.parametrize("bad", ["opus; rm", "a b", "", "--x y", 5])
def test_invalid_key_is_refused(bad):
    with pytest.raises(ValueError):
        _lsm().effective({"loop": {"session_model": bad}})


# ---- supervisor argv --------------------------------------------------------------------------
def _supervise(tmp_path, cfg, explicit=None):
    base = tmp_path / ".sdlc"
    (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps(cfg))
    seen = tmp_path / "argv.txt"
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(f'#!/usr/bin/env bash\necho "$@" > "{seen}"\necho "LOOP STOP: backlog-empty"\n')
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "SIGMA_CLAUDE_CMD"}
    env.update({"PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}", "SIGMA_HOST": "claude",
                "SIGMA_SUPERVISE_MAX_RUNS": "1", "SIGMA_SUPERVISE_SLEEP_SCALE": "0"})
    resource_mode.carry(env)
    if explicit is not None:
        env["SIGMA_CLAUDE_CMD"] = explicit.replace("FAKE", str(fake))
    r = subprocess.run([sys.executable, str(S / "supervise_daemon.py"), str(base)],
                       capture_output=True, text=True, env=env, timeout=60)
    return r, (seen.read_text().strip() if seen.exists() else None)


def test_supervisor_passes_the_key_as_model(tmp_path):
    r, argv = _supervise(tmp_path, {"loop": {"session_model": "opus"}})
    assert r.returncode == 0, r.stderr
    assert argv == "--model opus -p /sigma-loop"


def test_supervisor_defaults_to_sonnet_under_auto(tmp_path):
    r, argv = _supervise(tmp_path, {"model_selection": "auto"})
    assert argv == "--model sonnet -p /sigma-loop"


def test_supervisor_has_no_flag_when_unset(tmp_path):
    r, argv = _supervise(tmp_path, {})
    assert argv == "-p /sigma-loop"


def test_explicit_command_is_never_rewritten(tmp_path):
    r, argv = _supervise(tmp_path, {"loop": {"session_model": "opus"}}, explicit="FAKE --keep")
    assert argv == "--keep"


def test_invalid_key_refuses_loudly(tmp_path):
    r, argv = _supervise(tmp_path, {"loop": {"session_model": "bad value"}})
    assert r.returncode == 2 and "loop.session_model" in r.stderr and argv is None


def test_unreadable_config_does_not_crash_the_supervisor(tmp_path):
    base = tmp_path / ".sdlc"
    (base / "state").mkdir(parents=True)
    (base / "config.json").write_text("{bad")
    env = {**os.environ, "SIGMA_CLAUDE_CMD": "true", "SIGMA_SUPERVISE_MAX_RUNS": "1",
           "SIGMA_SUPERVISE_SLEEP_SCALE": "0"}
    resource_mode.carry(env)
    r = subprocess.run([sys.executable, str(S / "supervise_daemon.py"), str(base)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert "Traceback" not in r.stderr


# ---- verdict ----------------------------------------------------------------------------------
def test_opus_over_sonnet_only_warns():
    assert _lsm().verdict("claude-opus-5", {"sonnet"})[0] == "above-tier"


def test_opus_is_fine_when_opus_was_dispatched():
    assert _lsm().verdict("claude-opus-5", {"sonnet", "opus"})[0] == "ok"


def test_sonnet_over_sonnet_is_quiet():
    assert _lsm().verdict("claude-sonnet-5", {"sonnet"})[0] == "ok"


def test_unavailable_names_its_reason():
    st, text = _lsm().verdict(None, {"sonnet"})
    assert st == "unavailable" and "not exposed by this host" in text


def test_no_dispatched_tiers_is_quiet():
    assert _lsm().verdict("claude-opus-5", set())[0] == "ok"


def test_unknown_model_is_unavailable_not_a_guess():
    assert _lsm().verdict("gpt-x", {"sonnet"})[0] == "unavailable"


# ---- observed + dispatched_tiers from disk ----------------------------------------------------
def _session(home, sid, models):
    d = home / ".claude" / "projects" / "proj"
    d.mkdir(parents=True)
    rows = [json.dumps({"type": "assistant", "timestamp": f"2026-01-01T00:00:0{i}Z",
                        "message": {"role": "assistant", "model": m, "id": f"m{i}", "usage": {}}})
            for i, m in enumerate(models)]
    (d / f"{sid}.jsonl").write_text("\n".join(rows) + "\n")


def _log(sdlc, tiers):
    d = sdlc / "state" / "log"
    d.mkdir(parents=True)
    (d / "1.jsonl").write_text("\n".join(json.dumps({"kind": "model_choice", "model": t}) for t in tiers) + "\n")


def test_observed_is_the_latest_turn(tmp_path):
    _session(tmp_path, "sess1", ["claude-sonnet-5", "claude-opus-5"])
    assert _lsm().observed("sess1", tmp_path) == "claude-opus-5"
    assert _lsm().observed("missing", tmp_path) is None


def test_dispatched_tiers_reads_model_choice_rows(tmp_path):
    _log(tmp_path, ["sonnet", "haiku"])
    assert _lsm().dispatched_tiers(tmp_path) == {"sonnet", "haiku"}


# ---- doctor row -------------------------------------------------------------------------------
def _rows(tmp_path, monkeypatch, models, tiers, sid="sess1"):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if sid:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    else:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    if models:
        _session(home, "sess1", models)
    base = td._sdlc(tmp_path, {})
    if tiers:
        _log(pathlib.Path(base), tiers)
    return [c for c in td._doc().check(base, run=td._runner()) if c["name"].startswith("loop session model")]


def test_doctor_row_warns_above_tier(tmp_path, monkeypatch):
    rows = _rows(tmp_path, monkeypatch, ["claude-opus-5"], ["sonnet"])
    assert len(rows) == 1 and rows[0]["ok"] is False and "loop.session_model" in rows[0]["fix"]


def test_doctor_row_quiet_within_tiers(tmp_path, monkeypatch):
    rows = _rows(tmp_path, monkeypatch, ["claude-sonnet-5"], ["sonnet"])
    assert len(rows) == 1 and rows[0]["ok"] is True


def test_doctor_row_says_unavailable(tmp_path, monkeypatch):
    rows = _rows(tmp_path, monkeypatch, None, ["sonnet"], sid="")
    assert len(rows) == 1 and rows[0]["ok"] is True and "not exposed by this host" in rows[0]["name"]


# ---- #1961: the bounded read, the pick-time record, the empty command ------------------------------
def test_an_empty_session_command_is_left_empty_not_rewritten(tmp_path):
    r, argv = _supervise(tmp_path, {"loop": {"session_model": "opus"}}, explicit="")
    assert argv is None                                           # the session was never launched with a rewritten command
    assert "Traceback" not in r.stderr


def test_the_read_is_capped_to_the_newest_files_and_the_last_bytes(tmp_path):
    lsm = _lsm()
    d = tmp_path / "state" / "log"
    d.mkdir(parents=True)
    old = d / "old.jsonl"
    old.write_text(json.dumps({"kind": "model_choice", "model": "opus"}) + "\n")
    os.utime(old, (1, 1))
    for i in range(lsm.MAX_STREAM_FILES):
        (d / f"g{i}.jsonl").write_text(json.dumps({"kind": "model_choice", "model": "haiku"}) + "\n")
    assert lsm.dispatched_tiers(tmp_path) == {"haiku"}            # the one old opus row is past the file cap
    big = d / "g0.jsonl"
    big.write_text("junk\n" + json.dumps({"kind": "model_choice", "model": "fable"}) + "\n" + "x" * (lsm.MAX_STREAM_BYTES + 10) + "\n")
    assert "fable" not in lsm.dispatched_tiers(tmp_path)           # a row before the last MAX_STREAM_BYTES is not read


def test_a_pick_time_record_in_the_events_stream_counts(tmp_path, monkeypatch):
    ev = tmp_path / "events"
    ev.mkdir()
    (ev / "a.jsonl").write_text(json.dumps({"kind": "model_choice", "model": "sonnet", "goal": "7"}) + "\n")
    assert _lsm().dispatched_tiers(tmp_path) == {"sonnet"}
    assert _lsm().verdict("claude-opus-5", _lsm().dispatched_tiers(tmp_path))[0] == "above-tier"
