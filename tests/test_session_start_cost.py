"""#1334: the SessionStart wizard path is bounded, cheap, and honest about a transient gh failure."""
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "sigma-init" / "scripts"))
import setup_wizard  # noqa: E402

REAL_ADOPTED = setup_wizard.adopted_by_sigma


@pytest.fixture(autouse=True)
def _adopted(monkeypatch):
    monkeypatch.setattr(setup_wizard, "adopted_by_sigma", lambda sdlc_dir: True)


def _auth_row(ok):
    return {"name": "gh auth", "ok": ok, "fix": "" if ok else "run: gh auth login"}


def _sequence(rows_per_call, calls):
    def _check(sdlc_dir=".sdlc", run=None, scheduled_tasks_dir=None, site_packages_dirs=None,
              cheap_only=False):
        i = min(len(calls), len(rows_per_call) - 1)
        calls.append(1)
        return rows_per_call[i]
    return _check


def test_session_start_hook_has_a_timeout():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    entry = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    assert isinstance(entry.get("timeout"), (int, float)) and 0 < entry["timeout"] <= 30


def test_one_transient_auth_failure_is_retried_and_says_nothing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(setup_wizard, "_doctor_check",
                        _sequence([[_auth_row(False)], [_auth_row(True)]], calls))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"), allow_cache=False)
    assert status == {"needs_wizard": False, "steps": []}
    assert len(calls) == 2


def test_a_failed_auth_check_says_could_not_confirm_not_login(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(setup_wizard, "_doctor_check",
                        _sequence([[_auth_row(False)]], calls))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"), allow_cache=False)
    assert len(calls) == 2
    (step,) = status["steps"]
    assert step["name"] == "gh auth"
    assert "could not confirm" in step["fix"]
    assert "gh auth login" not in step["fix"]


def test_other_failing_rows_are_not_retried(monkeypatch, tmp_path):
    calls = []
    rows = [{"name": "project layer", "ok": False, "fix": "run /sigma-init"}]
    monkeypatch.setattr(setup_wizard, "_doctor_check", _sequence([rows], calls))
    setup_wizard.wizard_status(str(tmp_path / ".sdlc"), allow_cache=False)
    assert len(calls) == 1


def test_transient_result_is_cached_for_a_short_window(monkeypatch, tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text("{}")
    calls = []
    monkeypatch.setattr(setup_wizard, "_doctor_check", _sequence([[_auth_row(False)]], calls))
    first = setup_wizard.wizard_status(str(sdlc), now=1000.0)
    n = len(calls)
    second = setup_wizard.wizard_status(str(sdlc), now=1060.0)
    assert len(calls) == n                      # zero further gh work inside the window
    assert second == first
    setup_wizard.wizard_status(str(sdlc), now=1000.0 + 121)
    assert len(calls) > n                       # window over: really checked again


def test_a_real_non_auth_failure_is_never_served_from_cache(monkeypatch, tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text("{}")
    calls = []
    rows = [{"name": "project layer", "ok": False, "fix": "run /sigma-init"}]
    monkeypatch.setattr(setup_wizard, "_doctor_check", _sequence([rows], calls))
    setup_wizard.wizard_status(str(sdlc), now=1000.0)
    setup_wizard.wizard_status(str(sdlc), now=1010.0)
    assert len(calls) == 2


def test_wizard_path_makes_no_issue_list_calls_in_github_mode(tmp_path):
    """The cost was ~29 REST issue reads for rows the wizard allow-list never uses."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text(json.dumps({
        "discovery": {"source": "github", "github": {"project": {"enabled": True, "number": 3}}},
        "work": {"enabled": True}}))
    calls = []

    def run(argv, *a, **k):
        calls.append(" ".join(argv))
        return ""
    setup_wizard._doctor_check(sdlc_dir=str(sdlc), run=run)
    assert [c for c in calls if "/issues" in c] == []


def test_auth_failing_alongside_another_real_failure_is_not_retried(monkeypatch, tmp_path):
    calls = []
    rows = [_auth_row(False), {"name": "project layer", "ok": False, "fix": "run /sigma-init"}]
    monkeypatch.setattr(setup_wizard, "_doctor_check", _sequence([rows], calls))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"), allow_cache=False)
    assert len(calls) == 1
    assert [s["name"] for s in status["steps"]] == ["gh auth", "project layer"]
