"""#1769 - the controls runner detects a red, and refuses a break that does not apply or a test that stays green.

Control: make run_control return ok on a green test and test_a_test_that_stays_green_is_not_ok goes red."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("resource_controls", ROOT / "tools" / "resource_controls.py")
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)


def _fake_root(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "mod.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'skills'))\n"
        "import mod\n\ndef test_value():\n    assert mod.VALUE == 1\n\ndef test_always():\n    assert True\n")
    return tmp_path


def _control(**over):
    base = {"id": "x", "file": "skills/mod.py", "old": "VALUE = 1", "new": "VALUE = 2",
            "test": "tests/test_mod.py::test_value"}
    base.update(over)
    return base


def test_a_known_good_break_is_detected_red(tmp_path):
    ok, detail = rc.run_control(_control(), root=_fake_root(tmp_path))
    assert ok, detail


def test_a_test_that_stays_green_is_not_ok(tmp_path):
    ok, detail = rc.run_control(_control(test="tests/test_mod.py::test_always"), root=_fake_root(tmp_path))
    assert not ok and "green" in detail


def test_a_patch_that_does_not_apply_is_not_ok(tmp_path):
    ok, detail = rc.run_control(_control(old="NOT THERE"), root=_fake_root(tmp_path))
    assert not ok and "did not apply" in detail


def test_the_checkout_is_never_written(tmp_path):
    root = _fake_root(tmp_path)
    rc.run_control(_control(), root=root)
    assert (root / "skills" / "mod.py").read_text() == "VALUE = 1\n"


def test_every_listed_control_names_an_existing_test_and_an_applicable_patch(tmp_path):
    for control in rc.load():
        assert (ROOT / control["test"].split("::")[0]).is_file(), control["id"]
        text = (ROOT / control["file"]).read_text(encoding="utf-8")
        assert text.count(control["old"]) == 1, control["id"]


def test_a_control_whose_baseline_fails_is_rejected(tmp_path):
    root = _fake_root(tmp_path)
    (root / "skills" / "mod.py").write_text("VALUE = 5\n")
    ok, detail = rc.run_control(_control(old="VALUE = 5", new="VALUE = 6"), root=root)
    assert not ok and "baseline not green" in detail


def test_a_break_that_errors_instead_of_failing_is_rejected(tmp_path):
    ok, detail = rc.run_control(_control(new="VALUE = (\n"), root=_fake_root(tmp_path))
    assert not ok and "no FAILED line" in detail


def test_failed_line_matches_the_exact_node_id_not_a_prefix_name():
    """#1787: a FAILED line for test_ab must not count as a red for test_a."""
    out = "FAILED tests/test_mod.py::test_ab - assert False\n"
    assert not rc._failed_line(out, "tests/test_mod.py::test_a")
    assert rc._failed_line(out, "tests/test_mod.py::test_ab")
    assert rc._failed_line("FAILED tests/test_mod.py::test_a\n", "tests/test_mod.py::test_a")
    assert rc._failed_line("FAILED tests/test_mod.py::test_p[x] - boom\n", "tests/test_mod.py::test_p")
    assert not rc._failed_line("FAILED tests/other.py::test_a - boom\n", "tests/test_mod.py::test_a")


def test_every_table_row_has_a_control_and_every_control_a_row():
    """#1797: the runner covers each row of the controls table; each row names its control ids."""
    import re
    text = (ROOT / "docs" / "launch" / "evidence" / "resource-guard-controls.md").read_text()
    rows = [l for l in text.splitlines() if l.startswith("| ") and "tests/test_" in l]
    ids = {c["id"] for c in rc.load()}
    seen = set()
    for row in rows:
        found = set(re.findall(r"--only ([\w-]+)", row))
        assert found and found <= ids, row
        seen |= found
    assert seen == ids
