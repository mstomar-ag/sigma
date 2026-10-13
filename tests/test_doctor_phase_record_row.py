"""#1343: doctor row for a config that lacks the `gates.phase_record` key (absent = gate off)."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_doctor as td  # noqa: E402

NAME = "phase-record gate configured"


def _row(tmp_path, cfg, **kw):
    d = td._doc()
    return td._by_name(d.check(td._sdlc(tmp_path, cfg), run=td._runner(), **kw)).get(NAME)


def test_absent_key_fails_with_fix_naming_the_line(tmp_path):
    row = _row(tmp_path, {})
    assert row is not None and row["ok"] is False
    assert "phase_record" in row["fix"] and "action_log" in row["fix"]


def test_absent_key_with_other_gates_still_fails(tmp_path):
    row = _row(tmp_path, {"gates": {"plan": {"enabled": True}}})
    assert row is not None and row["ok"] is False


def test_present_key_passes_enabled_or_explicit_off(tmp_path_factory):
    for value in ({"enabled": True}, {"enabled": False}):
        row = _row(tmp_path_factory.mktemp("c"), {"gates": {"phase_record": value}})
        assert row is not None and row["ok"] is True and row["fix"] == ""


def test_runs_under_cheap_only(tmp_path):
    row = _row(tmp_path, {}, cheap_only=True)
    assert row is not None and row["ok"] is False


def test_not_a_wizard_step(monkeypatch, tmp_path):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-init" / "scripts"))
    import setup_wizard
    assert setup_wizard._MODES and NAME not in setup_wizard._MODES
