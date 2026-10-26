"""#2023: REST core reserve in gh_quota, deferral of the auto-unpark and reconcile sweeps below it, and the
doctor row. Zero network: header text fixtures and fake runners only.

Controls (each run alone, then restored): reserve as a constant; unknown quota treated as below the reserve.
"""
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"
NOW = 1_800_000_000
RESET = NOW + 1200


def _mod(name, base=LOOP):
    spec = importlib.util.spec_from_file_location(name + "_t2023q", base / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def headers(remaining, limit, resource="core", reset=RESET):
    return ("HTTP/2.0 200 OK\nX-Ratelimit-Limit: %d\nX-Ratelimit-Remaining: %d\nX-Ratelimit-Reset: %d\n"
            "X-Ratelimit-Resource: %s\n\n{}" % (limit, remaining, reset, resource))


@pytest.fixture
def q(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(tmp_path / "state"))
    return _mod("gh_quota")


def test_headers_are_recorded_per_resource(q):
    assert q.note_headers(headers(4000, 5000), now=NOW) == "core"
    q.note_headers(headers(90, 100, resource="graphql"), now=NOW)
    got = q.recorded_quota()
    assert got["core"]["remaining"] == 4000 and got["graphql"]["limit"] == 100


def test_unknown_quota_defers_nothing(q):
    assert q.recorded_quota() == {} and q.rest_deferral({}, now=NOW) is None


def test_below_reserve_defers_with_reason_and_reset(q):
    q.note_headers(headers(400, 5000), now=NOW)             # reserve 500
    why = q.rest_deferral({}, now=NOW)
    assert why and "400 of 5000" in why and str(RESET) in why


def test_at_or_above_reserve_does_not_defer(q):
    q.note_headers(headers(500, 5000), now=NOW)
    assert q.rest_deferral({}, now=NOW) is None


def test_reserve_is_derived_from_the_reported_limit(q):
    q.note_headers(headers(300, 15000), now=NOW)            # reserve 1500: defers
    assert q.rest_deferral({}, now=NOW)
    q.note_headers(headers(300, 1000), now=NOW)             # reserve 100: does not
    assert q.rest_deferral({}, now=NOW) is None


def test_reserve_percent_comes_from_config_and_bad_values_fall_back(q):
    q.note_headers(headers(400, 5000), now=NOW)
    assert q.rest_deferral({"gh_quota": {"rest_reserve_percent": 5}}, now=NOW) is None     # reserve 250
    assert q.reserve_percent({"gh_quota": {"rest_reserve_percent": "x"}}) == q.DEFAULT_REST_RESERVE_PERCENT
    assert q.reserve_percent({"gh_quota": {"rest_reserve_percent": 400}}) == q.DEFAULT_REST_RESERVE_PERCENT


def test_a_passed_reset_means_the_figure_is_stale_and_defers_nothing(q):
    q.note_headers(headers(1, 5000), now=NOW)
    assert q.rest_deferral({}, now=RESET + 1) is None


def test_incomplete_headers_record_nothing(q):
    assert q.note_headers("HTTP/2.0 200 OK\nX-Ratelimit-Remaining: 3\n\n{}", now=NOW) is None
    assert q.recorded_quota() == {}


def test_conditional_get_records_quota_from_the_response_it_already_has(q):
    api = _mod("gh_api")
    api.conditional_get("repos/o/r/issues", run=lambda args: (0, headers(10, 5000) , ""))
    assert q.recorded_quota()["core"]["remaining"] == 10


# --- sweeps ----------------------------------------------------------------------------------------------------

def _low(q):
    import time
    now = time.time()
    q.note_headers(headers(1, 5000, reset=int(now) + 1200), now=now)


def test_auto_unpark_sweep_defers_below_reserve_and_logs(q, capsys, monkeypatch):
    lp = _mod("loop")
    _low(lp.sources.gh_quota)
    calls = []
    monkeypatch.setattr(lp, "_load", lambda name: calls.append(name))
    monkeypatch.setattr(lp.sources, "_auto_unpark", lambda cfg: "on")
    assert lp._auto_unpark_sweep("/x/.sdlc", {}) == frozenset()
    err = capsys.readouterr().err
    assert err.count("deferred") == 1 and "resets at epoch" in err and calls == []


def test_auto_unpark_sweep_runs_when_quota_unknown(q, monkeypatch):
    lp = _mod("loop")
    ran = []

    class Fake:
        def sweep_unpark(self, *a, **k):
            ran.append(1)
            return {"unparked": ["7"]}

    monkeypatch.setattr(lp, "_load", lambda name: Fake())
    monkeypatch.setattr(lp.sources, "_auto_unpark", lambda cfg: "on")
    assert lp._auto_unpark_sweep("/x/.sdlc", {}) == frozenset(["7"]) and ran


def test_reconcile_sweep_defers_below_reserve_and_does_not_stamp(q, capsys, monkeypatch):
    lp = _mod("loop")
    _low(lp.sources.gh_quota)
    monkeypatch.setattr(lp, "_reconcile_mode", lambda cfg: "on")
    monkeypatch.setattr(lp, "_reconcile_due", lambda *a, **k: True)
    stamped, loaded = [], []
    monkeypatch.setattr(lp, "_reconcile_stamp", lambda *a, **k: stamped.append(1))
    monkeypatch.setattr(lp, "_load", lambda name: loaded.append(name))
    assert lp._reconcile_sweep("/x/.sdlc", {}) == frozenset()
    err = capsys.readouterr().err
    assert err.count("deferred") == 1 and "resets at epoch" in err and not stamped and not loaded


# --- doctor row ------------------------------------------------------------------------------------------------

def test_doctor_row_prints_recorded_quota(q):
    doc = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts")
    q.note_headers(headers(4000, 5000), now=NOW)
    q.note_headers(headers(90, 100, resource="graphql"), now=NOW)
    (row,) = doc._quota_rows()
    assert row["ok"] is True
    assert "REST core: 4000 of 5000" in row["fix"] and "GraphQL: 90 of 100" in row["fix"] and str(RESET) in row["fix"]


def test_doctor_row_says_not_recorded_yet(q):
    doc = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts")
    (row,) = doc._quota_rows()
    assert "REST core: not recorded yet" in row["fix"] and "GraphQL: not recorded yet" in row["fix"]
