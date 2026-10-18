"""#1594: doctor row `chain: <state> (<age>)` -- local record read only; absent when no chain record exists.

Controls: the absent-when-none clause (a row on an empty repo), and the stuck/stale verdict.
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_doctor as td  # noqa: E402


def _iso(ago_s):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago_s))


def _chain_rows(tmp_path, record=None, cfg=None):
    base = td._sdlc(tmp_path, cfg or {})
    if record is not None:
        d = pathlib.Path(base) / "state" / "chains"
        d.mkdir(parents=True)
        (d / "c1.json").write_text(json.dumps(record))
    d = td._doc()
    return [c for c in d.check(base, run=td._runner()) if c["name"].startswith("chain:")]


def _rec(run):
    return {"schema": "chain-plan/1", "goal": 1, "state": "approved", "armed_at": _iso(9999),
            "members": [{"number": 1, "verdict": "armed", "waits_on": []}], "answers": {}, "run": run}


def test_row_absent_when_no_chain_record(tmp_path):
    assert _chain_rows(tmp_path) == []


def test_row_absent_for_an_unreadable_record(tmp_path):
    base = td._sdlc(tmp_path, {})
    d = pathlib.Path(base) / "state" / "chains"
    d.mkdir(parents=True)
    (d / "c1.json").write_text("{bad")
    assert [c for c in td._doc().check(base, run=td._runner()) if c["name"].startswith("chain:")] == []


def test_row_present_informational_for_a_fresh_running_chain(tmp_path):
    rows = _chain_rows(tmp_path, _rec({"state": "running", "heartbeat": _iso(120)}))
    assert len(rows) == 1 and rows[0]["ok"] is True and "running" in rows[0]["name"] and "(2m)" in rows[0]["name"]


def test_row_fails_for_stale_or_stuck_with_the_fix(tmp_path_factory):
    for run in ({"state": "running", "heartbeat": _iso(7200)},
                {"state": "stuck", "stuck": "decision", "reason": "#4 is parked", "heartbeat": _iso(60)}):
        rows = _chain_rows(tmp_path_factory.mktemp("c"), _rec(run))
        assert len(rows) == 1 and rows[0]["ok"] is False and "chain.py status" in rows[0]["fix"]


def test_row_for_a_done_chain_is_ok_however_old(tmp_path):
    rows = _chain_rows(tmp_path, _rec({"state": "done", "heartbeat": _iso(90 * 86400)}))
    assert rows[0]["ok"] is True and "STALE" not in rows[0]["name"]
