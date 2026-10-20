"""The chain reads "in progress" from a live holder, not from the label alone (#1945).

A leftover in-progress label (the worker died, nothing released it) used to make the planner treat a member as
held by someone, so the sheet offered only `wait for them` for work nobody was doing. A member now counts as in
progress when the label is backed by a live holder: a registered worker with a live process on this machine, or an
unexpired claim in the team ledger. A label with neither is reported as stale on the sheet, with the release
gesture. With the ledger off and no marker the planner cannot tell another machine's work from a leftover, so the
member stays in progress. Any failure reading the evidence also leaves it in progress (the old behaviour).

Controls (each clause alone, then restored; every one seen red): the ledger lease check removed; the ttl removed
(an expired claim counts as live); the marker-alive clause removed; the dead-marker clause removed; the fail-open
removed (a helper exception escapes). No network: a stateful fake `gh`."""
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402,F401
import test_chain_arm as tca  # noqa: E402
import test_label_answer as tla  # noqa: E402
from test_label_drain_arm import DW, _mod  # noqa: E402

NOW = tca.NOW
LABEL = "sdlc:in-progress"


def _goal(n, *waits, labels=()):
    text = "".join("Blocked by #%d\n" % w for w in waits) + "\n" + DW
    return tla.issue(n, "sdlc:goal", *labels, assignees=("alice",), body=text)


def _scene(tmp_path, ledger_on, claims=(), ttl_hours=None, label=True, tweak=None):
    """Plan fixture issue 1, which waits on fixture issue 2 (labelled in progress when `label`).
    `claims` are `(goal, kind, hours_ago)` rows written to the ledger. -> (chain module, base, plan record)."""
    ch = tca._chain()
    base = tca._repo(tmp_path, "alice")
    cp = pathlib.Path(base) / "config.json"
    cfg = json.loads(cp.read_text())
    cfg["discovery"]["dag"] = {"mode": "off"}
    if ledger_on:
        cfg["ledger"] = {"enabled": True, "actor": "alice"}
        if ttl_hours is not None:
            cfg["ledger"]["lease"] = {"ttl_hours": ttl_hours}
    cp.write_text(json.dumps(cfg))
    ent = pathlib.Path(base) / "ledger" / "entries"
    ent.mkdir(parents=True)
    with (ent / "alice.jsonl").open("a") as f:
        for i, (goal, kind, hours_ago) in enumerate(claims):
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours_ago * 3600))
            f.write(json.dumps({"id": "alice:%d" % i, "ts": ts, "actor": "alice", "kind": kind,
                                "goal": str(goal)}) + "\n")
    if tweak:
        tweak(base, cfg)
    fk = tca.ChainFake([_goal(1, 2), _goal(2, labels=(LABEL,) if label else ())])
    ch.plan(base, 1, run=fk, now=NOW)
    return ch, base, tca._rec(base)


def _row(rec, n=2):
    return next(r for r in rec["members"] if r["number"] == n)


def _marker(base, pid):
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    assert _mod("loop").agent_start(base, 2, pid, cfg), "marker not written"


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_a_stale_label_with_the_ledger_on_and_no_claim_is_not_in_progress(tmp_path):
    _ch, _base, rec = _scene(tmp_path, True)
    assert _row(rec)["in_progress"] is False
    assert _row(rec)["stale_label"] is True


def test_an_unexpired_ledger_claim_keeps_the_member_in_progress(tmp_path):
    _ch, _base, rec = _scene(tmp_path, True, claims=[(2, "claimed", 1)])
    assert _row(rec)["in_progress"] is True
    assert not _row(rec).get("stale_label")


def test_an_expired_claim_is_stale(tmp_path):
    _ch, _base, rec = _scene(tmp_path, True, claims=[(2, "claimed", 13)], ttl_hours=12)
    assert _row(rec)["in_progress"] is False
    assert _row(rec)["stale_label"] is True


def test_a_claim_that_was_released_is_stale(tmp_path):
    _ch, _base, rec = _scene(tmp_path, True, claims=[(2, "claimed", 2), (2, "release", 1)])
    assert _row(rec)["stale_label"] is True


def test_a_live_worker_marker_keeps_it_in_progress_with_the_ledger_on_and_off(tmp_path):
    for on, sub in ((False, "off"), (True, "on")):
        d = tmp_path / sub
        d.mkdir()
        _ch, _base, rec = _scene(d, on, tweak=lambda b, c: _marker(b, os.getpid()))
        assert _row(rec)["in_progress"] is True, sub


def test_a_dead_marker_with_the_ledger_off_is_stale(tmp_path):
    _ch, _base, rec = _scene(tmp_path, False, tweak=lambda b, c: _marker(b, _dead_pid()))
    assert _row(rec)["in_progress"] is False
    assert _row(rec)["stale_label"] is True


def test_no_marker_and_the_ledger_off_stays_in_progress(tmp_path):
    _ch, _base, rec = _scene(tmp_path, False)
    assert _row(rec)["in_progress"] is True
    assert not _row(rec).get("stale_label")


def test_an_unlabelled_member_is_untouched_and_the_ledger_is_not_read(tmp_path, monkeypatch):
    reads = []
    real = _mod("ledger").read_all
    monkeypatch.setattr(_mod("ledger"), "read_all", lambda *a, **k: reads.append(1) or real(*a, **k))
    _ch, _base, rec = _scene(tmp_path, True, label=False)
    assert _row(rec)["in_progress"] is False
    assert not _row(rec).get("stale_label")
    assert reads == []


def test_the_sheet_names_a_stale_member_and_the_release_gesture(tmp_path):
    ch, base, _rec = _scene(tmp_path, True)
    text = ch.sheet(base, 1, now=NOW, config=json.loads((pathlib.Path(base) / "config.json").read_text()))
    line = next(x for x in text.splitlines() if x.startswith("Stale in-progress label"))
    assert "#2" in line and "loop.py release" in line


def test_the_sheet_has_no_stale_line_when_none_is_stale(tmp_path):
    ch, base, _rec = _scene(tmp_path, True, claims=[(2, "claimed", 1)])
    text = ch.sheet(base, 1, now=NOW, config=json.loads((pathlib.Path(base) / "config.json").read_text()))
    assert "Stale in-progress label" not in text


def test_a_helper_failure_leaves_the_member_in_progress(tmp_path, monkeypatch):
    ch = tca._chain()

    def boom(*a, **k):
        raise RuntimeError("ledger unreadable")
    monkeypatch.setattr(ch, "_label_holders", boom)
    base = tca._repo(tmp_path, "alice")
    cp = pathlib.Path(base) / "config.json"
    cfg = json.loads(cp.read_text())
    cfg["discovery"]["dag"] = {"mode": "off"}
    cfg["ledger"] = {"enabled": True, "actor": "alice"}
    cp.write_text(json.dumps(cfg))
    ch.plan(base, 1, run=tca.ChainFake([_goal(1, 2), _goal(2, labels=(LABEL,))]), now=NOW)
    assert _row(tca._rec(base))["in_progress"] is True
