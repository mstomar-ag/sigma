"""#1507: the upkeep session line and the trigger share one decision (an attempt stamp holding an open window)."""
import importlib.util
import json
import pathlib
import time

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
NOW = 1_800_000_000
SETTINGS = {"window_hours": 12, "retry_minutes": 60}


@pytest.fixture
def window():
    spec = importlib.util.spec_from_file_location("feature_upkeep_window", SCRIPTS / "feature_upkeep_window.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stamp(tmp_path, window, last_at, attempt_at):
    path = tmp_path / window.STAMP_REL
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": window.SCHEMA, "last_at": last_at, "attempt_at": attempt_at,
                                "outcome": "x", "unreached": []}))


def _hm(t):
    return time.strftime("%H:%M", time.localtime(t))


def test_recent_attempt_names_retry_time(tmp_path, window):
    _stamp(tmp_path, window, NOW - 13 * 3600, NOW - 600)
    line = window.describe(SETTINGS, tmp_path, NOW)
    assert "window open, retry after %s (last attempt %s)" % (_hm(NOW + 3000), _hm(NOW - 600)) in line
    assert window.is_open(SETTINGS, tmp_path, NOW) is False       # the trigger agrees


def test_no_prior_sweep_with_recent_attempt_names_retry_time(tmp_path, window):
    _stamp(tmp_path, window, 0, NOW - 600)
    assert "retry after %s" % _hm(NOW + 3000) in window.describe(SETTINGS, tmp_path, NOW)


def test_no_recent_attempt_line_unchanged(tmp_path, window):
    _stamp(tmp_path, window, NOW - 13 * 3600, NOW - 2 * 3600)
    assert window.describe(SETTINGS, tmp_path, NOW) == "upkeep: last sweep 13h00m ago; the window is open"
    assert window.is_open(SETTINGS, tmp_path, NOW) is True


def test_closed_window_line_unchanged(tmp_path, window):
    _stamp(tmp_path, window, NOW - 3600, NOW - 600)
    assert window.describe(SETTINGS, tmp_path, NOW) == "upkeep: last sweep 1h00m ago, next window opens in 11h00m"
