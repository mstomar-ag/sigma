"""#1594: `chain.py status` -- closed states with the age of last progress, STALE, one root-cause line,
`--rebuild` from the label and the recorded answers, `--leftovers`, and slot facts the renderer accepts.

Records are built by hand for the state tests (no gh at all); rebuild and leftovers use the arm tests' fake.

Controls (each clause alone, then restored): the stale threshold comparison; the done-never-stale clause
(only a running chain can be stale); the root-cause line; the stale_minutes config.
"""
import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402,F401
import test_chain_arm as tca  # noqa: E402
from test_label_drain_arm import _mod  # noqa: E402

NOW = tca.NOW
ROOT = pathlib.Path(__file__).resolve().parent.parent
RENDER = ROOT / "skills" / "sigma-loop" / "scripts" / "render.py"


def _iso(ago_s):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - ago_s))


def _rec(run=None, state="approved", armed=True):
    rec = {"schema": "chain-plan/1", "goal": 1, "state": state, "armed_at": _iso(7200) if armed else None,
           "members": [{"number": 1, "verdict": "armed", "waits_on": [2, 3]},
                       {"number": 2, "verdict": "armed", "waits_on": [4]},
                       {"number": 3, "verdict": "armed", "waits_on": [4]},
                       {"number": 4, "verdict": "parked", "waits_on": []}], "answers": {}}
    if run is not None:
        rec["run"] = run
    return rec


def _st(rec, config=None):
    return _mod("chain_status").status_of(rec, config, NOW)


def _word(rec, config=None):
    return _mod("chain_status").state_word(_st(rec, config))


# 1 -- the closed state set, each with its age -------------------------------------------------------------

@pytest.mark.parametrize("rec,word", [
    (_rec(state="awaiting-answers"), "planned"),
    (_rec(), "approved"),
    (_rec({"state": "running", "heartbeat": _iso(300)}), "running"),
    (_rec({"state": "approved-running", "heartbeat": _iso(300)}), "running"),
    (_rec({"state": "stuck", "stuck": "held-gate", "reason": "#2 is held by a gate", "heartbeat": _iso(300)}),
     "stuck(held-gate)"),
    (_rec({"state": "stuck", "stuck": "decision", "reason": "#4 is parked and needs an answer",
           "heartbeat": _iso(300)}), "paused(decision)"),
    (_rec({"state": "capped", "heartbeat": _iso(300)}), "capped"),
    (_rec({"state": "done", "heartbeat": _iso(300)}), "done"),
    (_rec({"state": "stopped", "heartbeat": _iso(300)}), "stopped"),
])
def test_states(rec, word):
    assert _word(rec) == word
    line = _mod("chain_status").render_line(_st(rec))
    assert word in line and "4 members" in line
    if rec.get("run"):
        assert "last progress 5m ago" in line
    else:
        assert "no progress recorded" in line


# 2 -- STALE: each clause alone --------------------------------------------------------------------------------

def test_stale_running_reads_stale_and_fresh_does_not():
    assert _word(_rec({"state": "running", "heartbeat": _iso(31 * 60)})) == "running STALE"
    assert _word(_rec({"state": "running", "heartbeat": _iso(29 * 60)})) == "running"


def test_stale_threshold_is_strictly_older_than():
    assert _word(_rec({"state": "running", "heartbeat": _iso(30 * 60)})) == "running"


def test_stale_minutes_config_is_honoured_and_invalid_reads_default():
    run = {"state": "running", "heartbeat": _iso(10 * 60)}
    assert _word(_rec(run), {"chain": {"stale_minutes": 5}}) == "running STALE"
    assert _word(_rec(run), {"chain": {"stale_minutes": 120}}) == "running"
    for bad in (0, -3, "x", True, None):
        assert _word(_rec(run), {"chain": {"stale_minutes": bad}}) == "running"


@pytest.mark.parametrize("state", ["done", "stopped", "capped", "stuck"])
def test_only_running_is_ever_stale(state):
    run = {"state": state, "heartbeat": _iso(10 * 86400), "stuck": "claimed", "reason": "#2 is in progress"}
    assert "STALE" not in _word(_rec(run))


def test_never_stale_before_a_run_and_running_without_heartbeat_uses_armed_at():
    assert "STALE" not in _word(_rec())
    assert _word(_rec({"state": "running"})) == "running STALE"      # armed 2h ago, no heartbeat
    assert _st(_rec({"state": "running", "heartbeat": _iso(-500)}))["age_s"] == 0     # clock skew clamps


# 3 -- one root-cause line ---------------------------------------------------------------------------------------

def test_root_cause_golden():
    rec = _rec({"state": "stuck", "stuck": "decision", "reason": "#4 is parked and needs an answer",
                "heartbeat": _iso(60)})
    text = _mod("chain_status").render_status(_st(rec))
    assert text.splitlines()[1] == "#1, #2, #3 wait on #4's decision; /sigma-unpark #4"
    assert len(text.splitlines()) == 2


def test_root_cause_other_reason_and_stopped_and_none_when_healthy():
    held = _rec({"state": "stuck", "stuck": "held-gate", "reason": "#2 is held by a gate"})
    assert _st(held)["root_cause"] == "held-gate at #2"
    assert "resume" in _st(_rec({"state": "stopped"}))["root_cause"]
    assert _st(_rec({"state": "running", "heartbeat": _iso(5)}))["root_cause"] is None


# 4 -- slot facts go through the renderer -----------------------------------------------------------------------

def test_render_accepts_slot_facts_for_every_state():
    cs = _mod("chain_status")
    for run in (None, {"state": "running", "heartbeat": _iso(5)}, {"state": "done"}, {"state": "capped"},
                {"state": "stopped"}, {"state": "stuck", "stuck": "decision", "reason": "#4 is parked"}):
        facts = {"headline": "Chain status:", "slots": [cs.slot_facts(_st(_rec(run)))], "tail": "Nothing else running."}
        p = subprocess.run([sys.executable, str(RENDER), "status"], input=json.dumps(facts), capture_output=True,
                           text=True)
        assert p.returncode == 0, p.stderr
        assert "P5 IMPLEMENT" in p.stdout and "REFUSED" not in p.stderr


# 5 -- the verb: all records, one chain, a malformed record ------------------------------------------------------

def test_main_status_prints_every_record_and_survives_a_bad_one(tmp_path, capsys):
    base = tmp_path / "s"
    d = base / "state" / "chains"
    d.mkdir(parents=True)
    (d / "c1.json").write_text(json.dumps(_rec({"state": "done"})))
    (d / "c7.json").write_text("{not json")
    ch = tca._chain()
    assert ch.main(["chain.py", "status", str(base)], now=NOW) == 0
    out = capsys.readouterr().out
    assert "chain #1: done" in out and "c7.json" in out
    assert ch.main(["chain.py", "status", str(base), "1", "--json"], now=NOW) == 0
    j = json.loads(capsys.readouterr().out)
    assert j[0]["state"] == "done" and j[0]["goal"] == 1
    assert ch.main(["chain.py", "status", str(base), "9"], now=NOW) == 2
    assert ch.main(["chain.py", "status", str(tmp_path / "none")], now=NOW) == 0


# 6 -- rebuild -----------------------------------------------------------------------------------------------------

def _keep(rec):
    return {"goal": rec["goal"], "members": rec["members"], "answers": rec["answers"]}


def test_rebuild_regenerates_an_equal_record(tmp_path):
    base, fk = tca._scene(tmp_path)
    before = tca._rec(base)
    assert before["answers"] and before["state"] == "approved"
    tca._rec_path(base).unlink()
    res = tca._chain().rebuild(base, 1, answers=before["answers"], run=fk, now=NOW)
    after = tca._rec(base)
    assert _keep(after) == _keep(before) and res["state"] == "approved"
    assert fk.writes() == []


def test_rebuild_keeps_the_old_records_answers_and_drops_the_run_block(tmp_path):
    base, fk = tca._scene(tmp_path)
    rec = tca._rec(base)
    rec["run"] = {"state": "running", "heartbeat": _iso(5)}
    tca._put(base, rec)
    tca._chain().rebuild(base, 1, run=fk, now=NOW)
    after = tca._rec(base)
    assert after["answers"] == rec["answers"] and "run" not in after


def test_rebuild_without_any_answers_is_a_plain_plan(tmp_path):
    base, fk = tca._scene(tmp_path, approve=False)
    res = tca._chain().rebuild(base, 1, run=fk, now=NOW)
    assert res["state"] == "planned" and tca._rec(base)["answers"] == {}


# 7 -- leftovers ---------------------------------------------------------------------------------------------------

def test_leftovers_names_labels_with_no_open_member(tmp_path):
    base, fk = tca._scene(tmp_path)
    ch = tca._chain()
    ch.arm(base, 1, run=fk, now=NOW)
    assert ch.leftovers(base, run=fk) == []
    for n in (1, 2, 3, 5, 6):
        if n in fk.by:
            fk.by[n]["state"] = "closed"
    assert ch.leftovers(base, run=fk) == ["chain:1"]
    writes = len(fk.writes())
    ch.leftovers(base, run=fk)
    assert len(fk.writes()) == writes


# 8 -- help / usage ---------------------------------------------------------------------------------------------------

def test_usage_lists_status(capsys):
    assert tca._chain().main(["chain.py", "--help"]) == 0
    assert "chain.py status" in capsys.readouterr().out


# 9 -- #1864: rebuild guard, never-armed leftovers, --json unreadable, malformed record ---------------------------

def _chains_dir(tmp_path):
    base = tmp_path / "s"
    (base / "state" / "chains").mkdir(parents=True)
    return base, base / "state" / "chains"


@pytest.mark.parametrize("rec", [_rec({"state": "running", "heartbeat": _iso(5)}, armed=False), _rec()])
def test_rebuild_of_a_running_or_armed_chain_refuses_without_force(tmp_path, capsys, rec):
    base, d = _chains_dir(tmp_path)
    (d / "c1.json").write_text(json.dumps(rec))
    ch = tca._chain()
    assert ch.main(["chain.py", "status", str(base), "1", "--rebuild"], now=NOW) == 2
    assert "--force" in capsys.readouterr().err
    assert json.loads((d / "c1.json").read_text()) == rec


def test_rebuild_force_flag_is_accepted(tmp_path, capsys):
    base, d = _chains_dir(tmp_path)
    (d / "c1.json").write_text(json.dumps(_rec({"state": "running", "heartbeat": _iso(5)})))
    ch = tca._chain()
    ch.main(["chain.py", "status", str(base), "1", "--rebuild", "--force"], now=NOW)
    assert "--force" not in capsys.readouterr().err


def test_leftovers_skips_a_never_armed_chain(tmp_path):
    base, fk = tca._scene(tmp_path)
    ch = tca._chain()
    assert ch.leftovers(base, run=fk) == []          # planned, never armed: no label yet, no leftover
    rec = tca._rec(base)
    rec["armed_at"] = "2026-01-01T00:00:00Z"
    tca._put(base, rec)
    for n in (1, 2, 3, 5, 6):
        if n in fk.by:
            fk.by[n]["state"] = "closed"
    assert ch.leftovers(base, run=fk) == ["chain:1"]


def test_json_reports_an_unreadable_record_like_text(tmp_path, capsys):
    base, d = _chains_dir(tmp_path)
    (d / "c1.json").write_text(json.dumps(_rec({"state": "done"})))
    (d / "c7.json").write_text("{not json")
    assert tca._chain().main(["chain.py", "status", str(base), "--json"], now=NOW) == 0
    j = json.loads(capsys.readouterr().out)
    assert {"file": "c7.json", "state": "unreadable"} in j and any(r.get("goal") == 1 for r in j)


def test_malformed_member_list_prints_an_error_line(tmp_path, capsys):
    base, d = _chains_dir(tmp_path)
    bad = _rec({"state": "stuck", "stuck": "decision", "reason": "#4 is parked"})
    bad["members"] = [5, "x"]
    (d / "c1.json").write_text(json.dumps(bad))
    assert tca._chain().main(["chain.py", "status", str(base)], now=NOW) == 0
    assert "c1.json: unreadable chain record" in capsys.readouterr().out
