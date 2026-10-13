"""#1316: the doctor's upkeep readiness row reads SIGMA_UPKEEP_JOB from the doctor's own shell. The watcher records no
environment of its own, so the row may report only what THIS shell shows and must never claim the watcher lacks it."""
import json
import os
import pathlib

import test_upkeep_sched as T
import upkeep_support as S


def _row(rows):
    return [r for r in rows if r["name"].startswith("upkeep scheduler readiness")]


def _run(argv):
    return "git version 2.54.0" if "--version" in argv else "true"


def _rows(tmp_path, cfg, monkeypatch, env=None):
    monkeypatch.delenv("SIGMA_UPKEEP_JOB", raising=False)
    if env:
        monkeypatch.setenv("SIGMA_UPKEEP_JOB", env)
    doctor, sdlc = T._doctor(), pathlib.Path(T.project(tmp_path))
    return doctor._upkeep_rows(sdlc, cfg, _run, lambda _n: "/bin/git", True, True)


def test_missing_machine_row_says_it_checked_this_shell_only(tmp_path, monkeypatch):
    row = _row(_rows(tmp_path, T.open_config(), monkeypatch))[0]
    text = row["name"]
    assert row["ok"] is False and "machine" in text
    assert "this shell" in text and "watcher" in text, text
    assert "not ready on this machine" not in text, text


def test_present_variable_row_is_unchanged_green(tmp_path, monkeypatch):
    row = _row(_rows(tmp_path, T.open_config(), monkeypatch, env="1"))[0]
    assert row["ok"] is True and "all opt-ins present in this shell" in row["name"]


def test_closed_gate_output_is_byte_identical_whatever_the_shell(tmp_path, monkeypatch):
    a = json.dumps(_rows(tmp_path, S.template_cfg(), monkeypatch), sort_keys=True)
    b = json.dumps(_rows(tmp_path, S.template_cfg(), monkeypatch, env="1"), sort_keys=True)
    assert a == b == "[]"
