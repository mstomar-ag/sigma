"""#1399: with a project board on, an unconfirmed gh login fails BOTH "gh auth" and
"gh project scope". That pair is still the transient case: one retry, then the "could not confirm"
step (never `gh auth login`), cached briefly."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "skills" / "sigma-init" / "scripts"))
import setup_wizard  # noqa: E402
import pytest  # noqa: E402

AUTH = {"name": "gh auth", "ok": False, "fix": "run: gh auth login"}
SCOPE = {"name": "gh project scope", "ok": False, "fix": "run: gh auth refresh -s project"}
OTHER = {"name": "project layer", "ok": False, "fix": "run: sigma-init"}
CLEAN = [{"name": "gh auth", "ok": True, "fix": ""},
         {"name": "gh project scope", "ok": True, "fix": ""}]


@pytest.fixture(autouse=True)
def _adopted(monkeypatch):
    monkeypatch.setattr(setup_wizard, "adopted_by_sigma", lambda sdlc_dir: True)


def _sdlc(tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    return str(sdlc)


def _script(monkeypatch, reads):
    calls = []

    def fake(**kwargs):
        calls.append(1)
        return reads[min(len(calls), len(reads)) - 1]
    monkeypatch.setattr(setup_wizard, "_doctor_check", fake)
    return calls


def test_both_rows_failing_on_both_reads_is_the_transient_step(monkeypatch, tmp_path):
    calls = _script(monkeypatch, [[AUTH, SCOPE]])
    status = setup_wizard.wizard_status(_sdlc(tmp_path), dismissed=set(), now=1000.0)
    assert calls == [1, 1]
    assert [s["name"] for s in status["steps"]] == ["gh auth"]
    assert status["steps"][0]["fix"] == setup_wizard._TRANSIENT_FIX
    assert "gh auth login" not in status["steps"][0]["fix"]


def test_both_rows_failing_once_then_passing_returns_clean(monkeypatch, tmp_path):
    calls = _script(monkeypatch, [[AUTH, SCOPE], CLEAN])
    status = setup_wizard.wizard_status(_sdlc(tmp_path), dismissed=set(), now=1000.0)
    assert calls == [1, 1]
    assert status == {"needs_wizard": False, "steps": []}


def test_scope_alone_still_yields_the_scope_step(monkeypatch, tmp_path):
    calls = _script(monkeypatch, [[SCOPE]])
    status = setup_wizard.wizard_status(_sdlc(tmp_path), dismissed=set(), now=1000.0)
    assert calls == [1]
    assert [s["name"] for s in status["steps"]] == ["gh project scope"]


def test_auth_plus_another_row_yields_the_normal_steps(monkeypatch, tmp_path):
    calls = _script(monkeypatch, [[AUTH, SCOPE, OTHER]])
    status = setup_wizard.wizard_status(_sdlc(tmp_path), dismissed=set(), now=1000.0)
    assert calls == [1]
    assert [s["name"] for s in status["steps"]] == ["gh auth", "gh project scope", "project layer"]
    assert status["steps"][0]["fix"] == "run: gh auth login"


def test_transient_window_is_cached_and_served_without_a_doctor_call(monkeypatch, tmp_path):
    sdlc = _sdlc(tmp_path)
    calls = _script(monkeypatch, [[AUTH, SCOPE]])
    setup_wizard.wizard_status(sdlc, dismissed=set(), now=1000.0)
    cache = json.loads(setup_wizard._cache_path(sdlc).read_text(encoding="utf-8"))
    assert cache["transient_until"] == 1000.0 + setup_wizard._TRANSIENT_TTL_SECONDS
    before = len(calls)
    second = setup_wizard.wizard_status(sdlc, dismissed=set(), now=1000.0 + 10)
    assert len(calls) == before, "second start called doctor inside the transient window"
    assert second["steps"][0]["fix"] == setup_wizard._TRANSIENT_FIX
