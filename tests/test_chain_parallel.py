"""#1590: a chain run can claim independent members together (`chain.parallel`), and only one chain at a time
holds a pool (`chain-active.json`). Offline: the arm tests' stateful fake `gh`, a fake pick, no process.

Controls (each clause alone, then restored): `chain.parallel` ignored; the `parallel.goals.max_concurrent`
cap ignored; the lock's pool-intersection check; the `chain.max_active` count; the stale rule's dead-pid
clause and its old-heartbeat clause each alone; the lock's mutex; the widening refusal (p90 unknown, and
remaining below the estimate each alone); a blocked member left in the ready set.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_chain_arm as tca  # noqa: E402
import test_chain_run as tcr  # noqa: E402
import test_label_answer as tla  # noqa: E402
import test_session_unstarted_release as tsur  # noqa: E402
from test_label_drain_arm import DW  # noqa: E402

S = pathlib.Path(__file__).resolve().parents[1] / "skills" / "sigma-loop" / "scripts"
_spec = importlib.util.spec_from_file_location("chain_parallel_t1590", S / "chain_parallel.py")
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

NOW = tca.NOW


def _backlog(free=(2, 3, 4, 5)):
    """Goal 1 waits on every free member; the free ones are independent of each other."""
    body = "".join("Blocked by #%d\n" % n for n in free) + "\n" + DW
    return [tla.issue(1, "sdlc:goal", assignees=("alice",), body=body)] + [tla.issue(n, body=DW) for n in free]


def _armed(tmp_path, free=(2, 3, 4, 5)):
    ch, base, fk = tcr._armed(tmp_path, issues=_backlog(free))
    return ch, base, fk


def _cfg(ch, base, parallel=None, cap=None, enabled=True, **chain):
    cfg = ch._load("state").load_config(base)
    if parallel is not None:
        chain["parallel"] = parallel
    if chain:
        cfg["chain"] = dict(cfg.get("chain") or {}, **chain)
    if cap is not None:
        cfg["parallel"] = {"goals": {"enabled": enabled, "max_concurrent": cap}}
        cfg["work"] = dict(cfg.get("work") or {}, enabled=True)
    return cfg


class Pick(tcr.FakePick):
    """The scoped pick that also honours `max_concurrent` and marks what it claims as in progress."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.asked = []

    def __call__(self, label, skip, max_concurrent=None):
        self.asked.append(max_concurrent)
        self.batch = max_concurrent or 1
        got = super().__call__(label, skip)
        for n in got:
            self.fk.by[n]["labels"].append({"name": "sdlc:in-progress"})
        return got


def _go(ch, base, fk, pick, cfg, at=NOW, **kw):
    return ch.run(base, 1, pick=pick, now=at, run=fk, sweep=tcr._sweep, config=cfg, **kw)


def _lockfile(base):
    return pathlib.Path(base) / "state" / "chain-active.json"


# 1 -- the ready set ---------------------------------------------------------------------------------------

def test_ready_set(tmp_path):
    members, blockers = [2, 3, 1], {1: [2, 3]}
    assert cp.ready_set(members, blockers, set(), 2) == [2, 3]
    assert cp.ready_set(members, blockers, set(), 1) == [2]
    assert cp.ready_set(members, blockers, {2}, 2) == [3], "a member in flight is offered again or uses no slot"
    assert cp.ready_set(members, blockers, {2, 3}, 2) == []
    ch, base, fk = _armed(tmp_path)
    pick = Pick(fk, base)
    res = _go(ch, base, fk, pick, _cfg(ch, base, parallel=2, cap=3))
    assert res["picked"] == [2, 3] and res["state"] == "running", res
    assert pick.asked == [2]
    ch2, base2, fk2 = _armed(tmp_path / "seq")
    pick2 = Pick(fk2, base2)
    res2 = _go(ch2, base2, fk2, pick2, _cfg(ch2, base2))
    assert len(res2["picked"]) == 1 and pick2.asked == [None], (res2, pick2.asked)


def test_blocked_never_batched(tmp_path):
    assert cp.ready_set([1, 2], {1: [2]}, {2}, 5) == [], "a member behind an in-flight blocker was offered"
    assert cp.ready_set([1, 2, 3], {1: [2], 2: [3]}, set(), 5) == [3]
    ch, base, fk = _armed(tmp_path, free=(2, 3))
    pick = Pick(fk, base)
    cfg = _cfg(ch, base, parallel=3, cap=3)
    res = _go(ch, base, fk, pick, cfg)
    assert res["picked"] == [2, 3] and pick.asked == [2], "goal 1 waits on both, so only 2 are ready"
    res = _go(ch, base, fk, pick, cfg, at=NOW + 1)           # both still in flight: nothing new is asked for
    assert res["picked"] == [] and res["state"] == "running" and res["exit"] == 0, res
    assert pick.asked == [2], "the pick was called while the only open member was blocked"
    tcr._close(fk, 2, 3)
    res = _go(ch, base, fk, pick, cfg, at=NOW + 2)
    assert res["picked"] == [1]


# 2 -- the bounds ------------------------------------------------------------------------------------------

def test_bounds(tmp_path):
    assert cp.width({}) == 1
    W = {"work": {"enabled": True}}
    assert cp.width({**W, "chain": {"parallel": 4}, "parallel": {"goals": {"enabled": True, "max_concurrent": 2}}}) == 2
    assert cp.width({**W, "chain": {"parallel": 2}, "parallel": {"goals": {"enabled": True, "max_concurrent": 5}}}) == 2
    assert cp.width({**W, "chain": {"parallel": 4}, "parallel": {"goals": {"enabled": False, "max_concurrent": 5}}}) == 1
    assert cp.width({**W, "chain": {"parallel": 4}, "parallel": {"goals": {"enabled": "yes", "max_concurrent": 5}}}) == 1
    for bad in (1, 0, -3, True, "3", 2.5, None):
        assert cp.width({**W, "chain": {"parallel": bad}, "parallel": {"goals": {"enabled": True,
                                                                           "max_concurrent": 5}}}) == 1, bad
    assert cp.width({**W, "chain": {"parallel": 3}}) == 1, "goal-level parallelism is off by default"
    assert cp.width({"chain": {"parallel": 4}, "parallel": {"goals": {"enabled": True, "max_concurrent": 5}}}) == 1, \
        "goal parallelism without work.enabled shares one checkout"
    for parallel, cap, want in ((2, 5, 2), (5, 2, 2)):
        ch, base, fk = _armed(tmp_path / ("a%d_%d" % (parallel, cap)))
        pick = Pick(fk, base)
        res = _go(ch, base, fk, pick, _cfg(ch, base, parallel=parallel, cap=cap))
        assert pick.asked == [want] and len(res["picked"]) == want, (parallel, cap, pick.asked)
    ch, base, fk = _armed(tmp_path / "off")
    pick = Pick(fk, base)
    res = _go(ch, base, fk, pick, _cfg(ch, base, parallel=3, cap=3, enabled=False))
    assert pick.asked == [None] and len(res["picked"]) == 1


def test_loop_clamp_never_above_the_configured_cap():
    lp = tsur._loop()
    on = {"work": {"enabled": True}, "parallel": {"goals": {"enabled": True, "max_concurrent": 3}}}
    assert lp.clamp_batch_width({"parallel": on["parallel"]}, 2) == 1, "no worktrees: goals would share a checkout"
    assert lp.clamp_batch_width(on, 2) == 2
    assert lp.clamp_batch_width(on, 9) == 3
    assert lp.clamp_batch_width({}, 4) == 1
    assert lp.clamp_batch_width({"work": {"enabled": True}, "parallel": {"goals": {"enabled": False}}}, 4) == 1


# 3 -- the single-active-chain lock --------------------------------------------------------------------------

def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_lock(tmp_path):
    sd = str(tmp_path)
    live = os.getpid()
    cp.acquire(sd, 7, {1, 2, 3}, {}, live, 1000.0)
    with pytest.raises(cp.ChainRefused) as e:
        cp.acquire(sd, 8, {3, 4}, {"chain": {"max_active": 5}}, live, 1001.0)
    assert "#7" in str(e.value), "the refusal does not name the chain holding the pool"
    with pytest.raises(cp.ChainRefused) as e:
        cp.acquire(sd, 8, {4, 5}, {}, live, 1001.0)                      # disjoint, but max_active defaults to 1
    assert "#7" in str(e.value)
    cp.acquire(sd, 8, {4, 5}, {"chain": {"max_active": 2}}, live, 1002.0)
    with pytest.raises(cp.ChainRefused):                                  # a third, disjoint, past max_active 2
        cp.acquire(sd, 9, {6}, {"chain": {"max_active": 2}}, live, 1003.0)
    cp.acquire(sd, 7, {1, 2, 3}, {"chain": {"max_active": 2}}, live, 1004.0)   # the holder itself refreshes
    held = json.loads((pathlib.Path(sd) / "state" / "chain-active.json").read_text())
    assert held["7"]["heartbeat"] == 1004.0 and set(held) == {"7", "8"}
    cp.release(sd, 7)
    cp.acquire(sd, 9, {1, 2}, {"chain": {"max_active": 2}}, live, 1005.0)
    cp.release(sd, 9)
    cp.release(sd, 8)
    cp.release(sd, 8)                                                     # releasing twice is harmless
    assert cp.active(sd) == {}


def test_lock_is_one_read_modify_write(tmp_path):
    """The mutex seam: while another holder is inside the critical section, acquire waits and gives up."""
    with cp._locked(str(tmp_path), wait=0):
        with pytest.raises(cp.ChainRefused) as e:
            cp.acquire(str(tmp_path), 7, {1}, {}, os.getpid(), 1000.0, lock_wait=0.2)
        assert "busy" in str(e.value)
    cp.acquire(str(tmp_path), 7, {1}, {}, os.getpid(), 1000.0, lock_wait=0.2)


def test_run_refused_by_the_lock_starts_nothing(tmp_path):
    ch, base, fk = _armed(tmp_path)
    cp.acquire(base, 7, {2, 9}, {}, os.getpid(), NOW)
    pick = Pick(fk, base)
    before = tca._rec_path(base).read_text()
    with pytest.raises(ch.ChainRefused) as e:
        _go(ch, base, fk, pick, _cfg(ch, base, parallel=2, cap=3))
    assert "#7" in str(e.value) and "nothing was started" in str(e.value)
    assert pick.asked == [] and tca._rec_path(base).read_text() == before and fk.writes() == []


def test_run_holds_and_releases_the_lock(tmp_path):
    ch, base, fk = _armed(tmp_path, free=(2, 3))
    pick = Pick(fk, base)
    cfg = _cfg(ch, base, parallel=2, cap=2)
    _go(ch, base, fk, pick, cfg)
    assert set(cp.active(base)) == {"1"}
    ch.stop(base, 1)
    assert cp.active(base) == {}, "stop left the lock behind"
    ch.resume(base, 1)
    tcr._close(fk, 2, 3)
    _go(ch, base, fk, pick, cfg, at=NOW + 5)
    tcr._close(fk, 1)
    res = _go(ch, base, fk, pick, cfg, at=NOW + 9)
    assert res["state"] == "done" and cp.active(base) == {}, "done left the lock behind"


# 4 -- the stale rule, each clause alone ---------------------------------------------------------------------

def test_stale_lock(tmp_path):
    dead, live = _dead_pid(), os.getpid()
    old, now = 1000.0, 1000.0 + cp.STALE_AFTER + 1
    fresh = now - 5

    def held(sub, pid, beat):
        sd = str(tmp_path / sub)
        cp.acquire(sd, 7, {1}, {}, pid, beat)
        return sd

    sd = held("dead-stale", dead, old)
    cp.acquire(sd, 8, {1}, {}, live, now)                                 # reclaimed
    assert set(cp.active(sd)) == {"8"}
    for sub, pid, beat in (("live-fresh", live, fresh), ("dead-fresh", dead, fresh), ("live-stale", live, old)):
        sd = held(sub, pid, beat)
        with pytest.raises(cp.ChainRefused) as e:
            cp.acquire(sd, 8, {1}, {}, live, now)
        assert "#7" in str(e.value), sub
        assert set(cp.active(sd)) == {"7"}, sub
    sd = held("edge", dead, old)                                          # exactly at the age: not yet stale
    with pytest.raises(cp.ChainRefused):
        cp.acquire(sd, 8, {1}, {}, live, old + cp.STALE_AFTER)


# 5 -- widening against the remaining cap --------------------------------------------------------------------

def test_widen_vs_cap(tmp_path):
    assert cp.widen_ok(1000, 2, 400) is True                              # 2 x 400 fits in 1000
    assert cp.widen_ok(800, 2, 400) is True                               # exactly covered
    assert cp.widen_ok(799, 2, 400) is False
    assert cp.widen_ok(1000, 2, None) is False, "an unknown p90 widened the run"
    assert cp.widen_ok(0, 1, 400) is True and cp.widen_ok(0, 1, None) is True, "width 1 is never a widening"
    assert cp.widen_ok(None, 3, None) is True, "no usd cap: nothing to cover"
    ev = pathlib.Path(tmp_path) / "ev"

    def scene(sub, usd, history):
        ch, base, fk = _armed(tmp_path / sub)
        d = pathlib.Path(base) / "events"
        d.mkdir(exist_ok=True)
        lines = []
        for g in range(100, 100 + history):
            lines += [{"kind": "spend", "goal": g, "cost_cents": 400, "ts": "2026-01-01T00:00:00Z"},
                      {"kind": "retro", "goal": g, "ts": "2026-01-01T00:00:00Z"}]
        (d / "a.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        return ch, base, fk, _cfg(ch, base, parallel=2, cap=2, max_usd=usd)

    ch, base, fk, cfg = scene("tight", 5.0, 4)                            # 500 cents < 2 x 400
    pick = Pick(fk, base)
    res = _go(ch, base, fk, pick, cfg)
    assert pick.asked == [1] and len(res["picked"]) == 1, (pick.asked, res)
    assert "widen" in res["detail"], res
    ch, base, fk, cfg = scene("room", 20.0, 4)
    pick = Pick(fk, base)
    res = _go(ch, base, fk, pick, cfg)
    assert pick.asked == [2] and len(res["picked"]) == 2, (pick.asked, res)
    ch, base, fk, cfg = scene("unknown", 20.0, 0)                         # a cap, and no p90 to size it by
    pick = Pick(fk, base)
    res = _go(ch, base, fk, pick, cfg)
    assert pick.asked == [1] and len(res["picked"]) == 1


# 6 -- the default is untouched ------------------------------------------------------------------------------

def test_default_unchanged(tmp_path):
    ch, base, fk = tcr._armed(tmp_path)
    seen = []

    class TwoArg(tcr.FakePick):
        def __call__(self, label, skip):                                   # a third argument would raise
            seen.append((label, sorted(skip)))
            return super().__call__(label, skip)

    pick = TwoArg(fk, base)
    res = tcr._run(ch, base, fk, pick)
    assert res == {"goal": 1, "state": "running", "exit": 0, "reason": None, "detail": "", "picked": [3],
                   "added": [], "closed": [], "cycle": 1, "joined": [], "paused": [], "delta": "", "filed": []}, res
    assert not _lockfile(base).exists(), "the lock was written with chain.parallel unset"
    assert seen == [("chain:1", [])]
    a = ch.pick_argv(base, "chain:1", 5, ())
    assert "--max-concurrent" not in a
    b = ch.pick_argv(base, "chain:1", 5, (), max_concurrent=2)
    assert b[:len(a)] == a and b[-2:] == ["--max-concurrent", "2"]
