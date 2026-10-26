"""#2020: under `model_selection: auto`, reviewer phases never resolve below a floor tier.

The floor lives at ONE seam, `predict.phase_tier`, which `resolve-phase` (the CLI) calls too."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

import resource_mode

ROOT = pathlib.Path(__file__).resolve().parent.parent
P = ROOT / "skills" / "sigma-model" / "scripts" / "predict.py"
TMPL = ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl"
REVIEWERS = ("plan-review", "review", "pr-review")


def _mod():
    spec = importlib.util.spec_from_file_location("predict", P)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sdlc(tmp_path, cfg):
    d = tmp_path / ".sdlc"
    d.mkdir()
    (d / "config.json").write_text(json.dumps(cfg))
    return str(d)


@pytest.mark.parametrize("phase", REVIEWERS)
def test_reviewer_phase_is_raised_to_the_default_floor(phase):
    assert _mod().phase_tier("haiku", phase, {"model_selection": "auto"}) == "sonnet"


@pytest.mark.parametrize("phase", ("plan", "implement", "research", "verify"))
def test_other_phases_are_not_floored(phase):
    assert _mod().phase_tier("haiku", phase, {"model_selection": "auto"}) == "haiku"


def test_a_signal_above_the_floor_is_kept():
    m = _mod()
    assert m.phase_tier("opus", "review", {"model_selection": "auto"}) == "opus"
    assert m.phase_tier("haiku", "review",
                        {"model_selection": "auto", "model_selection_reviewer_floor": "opus"}) == "opus"


def test_the_ceiling_wins_over_the_floor():
    cfg = {"model_selection": "auto", "model_selection_reviewer_floor": "opus",
           "model_selection_max_tier": "sonnet"}
    assert _mod().phase_tier("haiku", "pr-review", cfg) == "sonnet"


def test_off_or_absent_key_is_unchanged():
    m = _mod()
    assert m.phase_tier("haiku", "review", {"model_selection": "off"}) == "haiku"
    assert m.phase_tier("haiku", "review", {}) == "haiku"


def test_unknown_floor_falls_back_to_the_default():
    cfg = {"model_selection": "auto", "model_selection_reviewer_floor": "gpt"}
    assert _mod().phase_tier("haiku", "review", cfg) == "sonnet"


def test_cli_resolve_phase(tmp_path):
    sdlc = _sdlc(tmp_path, {"model_selection": "auto"})
    env = resource_mode.carry(dict(os.environ))

    def run(*args):
        r = subprocess.run([sys.executable, str(P), "resolve-phase", *args],
                           capture_output=True, text=True, env=env)
        return r.returncode, r.stdout.strip()
    assert run("review", "haiku", sdlc) == (0, "sonnet")
    assert run("implement", "haiku", sdlc) == (0, "haiku")
    (tmp_path / ".sdlc" / "config.json").write_text(json.dumps({}))
    assert run("review", "haiku", sdlc) == (0, "off")
    assert run("review", "turbo", sdlc)[0] == 2


def test_template_ships_auto_for_new_installs():
    cfg = json.loads(TMPL.read_text())
    assert cfg["model_selection"] == "auto"
    assert cfg["model_selection_reviewer_floor"] == "sonnet"


def test_existing_install_without_the_key_reads_off(tmp_path):
    m = _mod()
    sdlc = _sdlc(tmp_path, {"mode": {"default": "goal"}})
    assert m._model_selection(m._cfg(sdlc)) == "off"
