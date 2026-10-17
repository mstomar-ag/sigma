"""#1586: chain cost estimate and chain-level spend cap (`chain_spend.py`). Offline; fixture ledgers only.

Controls (each clause alone, then restored): drop the member filter; drop the `since` filter; drop the
unreadable-ledger clause; drop `spent >= cap`; drop the no-p90 refusal."""
import importlib.util
import json
import pathlib
import time

S = pathlib.Path(__file__).resolve().parents[1] / "skills" / "sigma-loop" / "scripts"
spec = importlib.util.spec_from_file_location("chain_spend_t1586", S / "chain_spend.py")
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

SINCE = 1_800_000_000.0


def _ts(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _write(sdlc, rows):
    d = pathlib.Path(sdlc) / "events"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "a-1.jsonl", "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _spend(goal, cents, phase="implement", at=SINCE + 10):
    return {"kind": "spend", "goal": str(goal), "phase": phase, "cost_cents": str(cents), "ts": _ts(at)}


def _phase(goal, phase, at=SINCE + 10):
    return {"kind": "phase", "goal": str(goal), "phase": phase, "state": "end", "ts": _ts(at)}


def _history(sdlc, costs):
    _write(sdlc, [_spend(100 + i, c, at=SINCE - 1000) for i, c in enumerate(costs)])


def test_estimate_basis(tmp_path):
    _history(tmp_path, [100, 200, 300, 400, 1000])
    e = cs.estimate(tmp_path, ["1", "2"])
    assert e["p50"] == 300 and e["p90"] == 1000 and e["basis"] == 5 and e["text"] == ""
    two = tmp_path / "two"
    _history(two, [100, 200])
    e = cs.estimate(two, ["1"])
    assert e["p50"] is None and e["p90"] is None and e["basis"] == 2
    assert e["text"] == "no estimate: only 2 measured goals"


def test_attribution(tmp_path):
    _write(tmp_path, [_spend(1, 100), _spend(2, 50), _spend(9, 999), _spend(1, 700, at=SINCE - 5),
                      _spend(2, 30, phase="review", at=SINCE)])
    assert cs.spent(tmp_path, ["1", "2"], SINCE) == (180, True)
    assert cs.spent(tmp_path, ["1", "2"], _ts(SINCE)) == (180, True)


def test_cap_fail_closed(tmp_path):
    cfg = {"chain": {"max_usd": 1.0}}
    chain = {"members": ["1"], "approved_at": SINCE}
    _write(tmp_path, [_spend(1, 99)])
    assert cs.admit(tmp_path, cfg, chain)[0] is True
    _write(tmp_path, [_spend(1, 1)])
    ok, why = cs.admit(tmp_path, cfg, chain)
    assert not ok and why.startswith("capped:") and "spent" in why
    bad = tmp_path / "bad"
    (bad / "events").mkdir(parents=True)
    (bad / "events" / "x.jsonl").write_bytes(b"{}\n")
    (bad / "events" / "x.jsonl").chmod(0)
    try:
        ok, why = cs.admit(bad, cfg, chain)
    finally:
        (bad / "events" / "x.jsonl").chmod(0o644)
    assert not ok and "unreadable" in why
    assert cs.admit(bad, {}, chain) == (True, "")   # no cap set: nothing is read
    dd = tmp_path / "dir"
    (dd / "events").mkdir(parents=True)
    (dd / "events").chmod(0)
    try:
        ok, why = cs.admit(dd, cfg, chain)
    finally:
        (dd / "events").chmod(0o755)
    assert not ok and "unreadable" in why   # an unreadable directory is not an empty ledger
    for bad_cap in ("5", 0, -1, True):
        ok, why = cs.admit(tmp_path, {"chain": {"max_usd": bad_cap}}, chain)
        assert not ok and "invalid chain.max_usd" in why   # a mistyped cap is never "no cap"
    ok, why = cs.admit(tmp_path, {"chain": {"max_minutes": 5}}, {"members": ["1"]})
    assert not ok and "approval stamp" in why
    ok, why = cs.admit(tmp_path, {"chain": {"max_goals": 2, "max_added": 0}}, {"members": ["1", "2", "3"]})
    assert not ok and "goals" in why


def test_unpriced(tmp_path):
    cfg = {"chain": {"max_usd": 10.0}}
    chain = {"members": ["1"], "approved_at": SINCE}
    _write(tmp_path, [_phase(1, "implement")])
    ok, why = cs.admit(tmp_path, cfg, chain)
    assert not ok and "no p90" in why
    _write(tmp_path, [_spend(7, 400, at=SINCE - 99), _spend(8, 600, at=SINCE - 99), _spend(9, 900, at=SINCE - 99)])
    assert cs.admit(tmp_path, {"chain": {"max_usd": 10.0}}, chain)[0] is True
    ok, why = cs.admit(tmp_path, {"chain": {"max_usd": 9.0}}, chain)
    assert not ok and "unpriced" in why   # 900 cents at p90 reaches the 9.00 cap


def test_since_edges(tmp_path):
    assert cs._epoch("2026-10-18T10:00:00.5Z") == cs._epoch("2026-10-18T10:00:00Z")
    _write(tmp_path, [_phase(1, "implement", at=SINCE - 50)])   # before approval: not an unpriced phase now
    assert cs.admit(tmp_path, {"chain": {"max_usd": 10.0}}, {"members": ["1"], "approved_at": SINCE})[0] is True


def test_overshoot_sentence():
    assert "checked before each claim" in cs.OVERSHOOT_SENTENCE
    assert "overshot" in cs.OVERSHOOT_SENTENCE and "in flight" in cs.OVERSHOOT_SENTENCE


def test_measured_scan(tmp_path):
    for n in (1000, 10000):
        d = tmp_path / str(n)
        _write(d, [_spend(i % 50, 5, at=SINCE + i) for i in range(n)])
        t = time.perf_counter()
        cs.spent(d, [str(i) for i in range(10)], SINCE)
        print("spent scan", n, "records: %.1f ms" % ((time.perf_counter() - t) * 1000))
