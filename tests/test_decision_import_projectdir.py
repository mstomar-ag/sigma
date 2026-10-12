"""Goal 1244: --project-dir takes a dash-leading folder name; scan, plan, apply share one option set."""
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("decision_import_t1244", ROOT / "skills/sigma-loop/scripts/decision_import.py")
DI = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DI)

NAME = "-srv-demo-work-repo"


def _world(tmp_path):
    sd = tmp_path / "proj" / ".sdlc"
    sd.mkdir(parents=True)
    (sd / "config.json").write_text("{}")
    prof = tmp_path / "prof"
    (prof / "projects" / NAME).mkdir(parents=True)
    (prof / "projects" / NAME / "a.jsonl").write_text("")
    return str(sd), str(prof)


def _run(capsys, verb, sd, *rest):
    rc = DI.main([verb, sd, *rest])
    return rc, capsys.readouterr().out


@pytest.mark.parametrize("verb", ["scan", "plan", "apply"])
def test_two_word_dash_name_is_accepted(tmp_path, capsys, verb):
    sd, prof = _world(tmp_path)
    rc, out = _run(capsys, verb, sd, "--profile", prof, "--project-dir", NAME)
    assert "not found" not in out and "usage" not in out
    assert rc in (0, 1)  # apply with the records part closed exits 1 by design


def test_dash_name_is_read_in_both_forms(tmp_path, capsys):
    sd, prof = _world(tmp_path)
    _, two = _run(capsys, "scan", sd, "--profile", prof, "--project-dir", NAME)
    _, one = _run(capsys, "scan", sd, "--profile", prof, "--project-dir=" + NAME)
    assert "files_read=1" in two and two == one


def test_path_form_derives_the_folder_name(tmp_path, capsys):
    sd, prof = _world(tmp_path)
    other = tmp_path / "Work" / "repo"
    other.mkdir(parents=True)
    import re
    (pathlib.Path(prof) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(other.resolve()))).mkdir()
    rc, out = _run(capsys, "scan", sd, "--profile", prof, "--project-dir", str(other))
    assert rc == 0 and "not found" not in out


def test_missing_folder_is_reported_not_raised(tmp_path, capsys):
    sd, prof = _world(tmp_path)
    rc, out = _run(capsys, "scan", sd, "--profile", prof, "--project-dir", "-srv-demo-work-nowhere")
    assert rc == 0 and "refused: project folder -srv-demo-work-nowhere not found" in out


def test_a_real_option_after_the_flag_is_not_swallowed(tmp_path):
    sd, prof = _world(tmp_path)
    with pytest.raises(SystemExit) as e:
        DI.main(["scan", sd, "--project-dir", "--profile=" + prof])
    assert e.value.code == 2  # argparse still says --project-dir lacks a value


def test_three_verbs_share_one_option_set():
    import argparse
    seen = {}
    for verb in ("scan", "plan", "apply"):
        v = argparse.ArgumentParser().add_subparsers().add_parser(verb)
        DI._add_options(v)
        seen[verb] = {k: (a.option_strings, a.help) for k, a in v._option_string_actions.items()}
    assert seen["scan"] == seen["plan"] == seen["apply"]
    assert "--project-dir" in seen["scan"] and "-srv-demo-work-repo" in seen["scan"]["--project-dir"][1]


def test_main_builds_every_verb_through_the_helper(monkeypatch):
    calls = []
    real = DI._add_options
    monkeypatch.setattr(DI, "_add_options", lambda v: (calls.append(v), real(v))[1])
    with pytest.raises(SystemExit):
        DI.main(["scan", "--help"])
    assert len(calls) == 3
