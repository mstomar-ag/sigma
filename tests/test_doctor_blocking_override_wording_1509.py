"""#1509: the doctor row describes the within-tier order of the blocking override."""
import importlib.util
import pathlib

D = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py"


def _doctor():
    spec = importlib.util.spec_from_file_location("doctor_1509", D)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_on_row_describes_within_tier_order():
    text = _doctor()._blocking_priority_override_feature_state({"discovery": {"source": "github"}})
    assert text.startswith("ON (default)")
    assert "within its own priority tier" in text
    assert "raised to the tier of what it blocks" in text
    assert "regardless of priority tier" not in text


def test_off_and_local_rows_are_unchanged():
    d = _doctor()
    off = d._blocking_priority_override_feature_state(
        {"discovery": {"source": "github", "blocking_priority_override": False}})
    assert off.startswith("off")
    assert d._blocking_priority_override_feature_state({}).startswith("n/a")
