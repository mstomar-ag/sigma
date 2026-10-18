"""#1588: `chain.py run`, `stop` and `resume` -- a foreground driver, one cycle per call, no daemon.

A stateful fake `gh` (the arm tests' fake) serves every read; the pick is a fake that behaves like the
loop's scoped pick (label scoped, lease based, deepest first). No process is ever started: the default pick
is checked by its argv and through a patched `subprocess.run`.

Controls (each clause alone, then restored): drop the label scope from the pick; map an empty pick with open
members to done; skip the admit check; do not stamp the heartbeat when a member closes; record a member
again that the record already holds as picked.
"""
import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_chain_arm as tca  # noqa: E402
import test_label_answer as tla  # noqa: E402
from test_label_drain_arm import DW  # noqa: E402

NOW = tca.NOW
LABEL = "chain:1"


def _sweep(*_a):
    return ()


def _chain_backlog():
    """1 waits on 2, 2 waits on 3: the order is 3, 2, 1. Goal 9 is an armed goal outside the chain."""
    return [tla.issue(1, "sdlc:goal", assignees=("alice",), body="Blocked by #2\n\n" + DW),
            tla.issue(2, body="Blocked by #3\n\n" + DW), tla.issue(3, body=DW),
            tla.issue(9, "sdlc:goal", assignees=("alice",), body=DW)]


def _armed(tmp_path, issues=None):
    ch = tca._chain()
    base, fk = tca._scene(tmp_path, issues=issues or _chain_backlog())
    ch.arm(base, 1, run=fk, now=NOW)
    assert tca._rec(base).get("armed_at")
    fk.calls.clear()
    return ch, base, fk


class FakePick:
    """The loop's scoped pick, as far as the driver can tell: label scoped, leased, deepest first."""

    def __init__(self, fk, base, lease=True, batch=1):
        self.fk, self.base, self.lease, self.batch = fk, base, lease, batch
        self.labels, self.skips, self.claims = [], [], []

    def __call__(self, label, skip):
        self.labels.append(label)
        self.skips.append(set(skip))
        rows = {r["number"]: r for r in tca._rec(self.base)["members"]}
        order = list(rows) + [n for n in self.fk.by if n not in rows]
        got = []
        for n in order:
            issue = self.fk.by[n]
            names = {l["name"] for l in issue["labels"]}
            if issue["state"] == "closed" or (label and label not in names) or (self.lease and n in self.claims) \
                    or "sdlc:goal" not in names or names & {"story", "sdlc:parked"}:
                continue
            if any(self.fk.by[w]["state"] != "closed" for w in rows.get(n, {}).get("waits_on", [])):
                continue
            got.append(n)
            self.claims.append(n)
            if len(got) == self.batch:
                break
        return got


def _close(fk, *nums):
    for n in nums:
        fk.by[n]["state"] = "closed"


def _run(ch, base, fk, pick, at=NOW, **kw):
    return ch.run(base, 1, pick=pick, now=at, run=fk, sweep=kw.pop("sweep", _sweep), **kw)


# 1 -- label scoped, deepest first, one claim each -----------------------------------------------------------

def test_order_and_exclusivity(tmp_path):
    ch, base, fk = _armed(tmp_path)
    pick = FakePick(fk, base)
    seen = []
    for i in range(3):
        res = _run(ch, base, fk, pick, at=NOW + i, sweep=lambda *a: {"7"})
        assert res["exit"] == 0 and res["state"] == "running", res
        seen += res["picked"]
        _close(fk, *res["picked"])
    assert seen == [3, 2, 1]
    assert 9 not in pick.claims, "a goal outside the label was claimed"
    assert pick.labels == [LABEL] * 3, pick.labels
    assert all(s == {"7"} for s in pick.skips), "the sweep's lifted goals are not handed to the pick as a skip"
    res = _run(ch, base, fk, pick, at=NOW + 9)
    assert res["state"] == "done" and res["exit"] == 0, res
    assert tca._rec(base)["run"]["picked"] == [1, 2, 3]
    assert fk.writes() == [], "a run cycle wrote to GitHub itself"


# 2 -- an empty pick is never "done" while a member is open ---------------------------------------------------

def test_stuck_not_done(tmp_path):
    ch, base, fk = tca._chain(), *tca._scene(tmp_path / "a", issues=tca._backlog(bob=False))
    ch.arm(base, 1, run=fk, now=NOW)
    pick = FakePick(fk, base)
    for _ in range(2):                                   # 2 and 3 are armed and free; 6 is an upper-tier story
        res = _run(ch, base, fk, pick)
        _close(fk, *res["picked"])
    res = _run(ch, base, fk, pick)
    assert res["exit"] == 3 and res["state"] == "stuck" and res["reason"] == "decision", res
    assert res["reason"] in ch.STUCK_REASONS and "#6" in res["detail"]
    assert tca._rec(base)["run"]["state"] == "stuck"
    assert ch.main(["chain.py", "run", base, "1"], run=fk, now=NOW, pick=pick) == 3
    # an open member that lost its label is not closed: the label listing is empty, the run is not done
    ch2, base2, fk2 = _armed(tmp_path / "b")
    _close(fk2, 1, 2)
    fk2.by[3]["labels"] = [l for l in fk2.by[3]["labels"] if l["name"] != LABEL]
    res = _run(ch2, base2, fk2, FakePick(fk2, base2, batch=0))
    assert res["state"] == "stuck" and res["reason"] == "degraded-read" and "#3" in res["detail"], res
    # every member closed but the pick itself failed: not done, the failure is the reason
    ch3, base3, fk3 = _armed(tmp_path / "c")
    _close(fk3, 1, 2, 3)

    def limited(label, skip):
        raise RuntimeError("HTTP 403 secondary rate limit")
    res = _run(ch3, base3, fk3, limited)
    assert res["state"] == "stuck" and res["reason"] == "rate-limit", res


# 3 -- the cap stops the claim and resume lifts it -------------------------------------------------------------

def test_capped_resumable(tmp_path):
    ch, base, fk = _armed(tmp_path)
    cp = pathlib.Path(base) / "config.json"
    cfg = json.loads(cp.read_text())
    cfg["chain"] = {"max_goals": 1}
    cp.write_text(json.dumps(cfg))
    calls = []
    pick = FakePick(fk, base)
    res = _run(ch, base, fk, lambda *a: calls.append(a) or pick(*a))
    assert res["state"] == "capped" and res["exit"] == 3 and res["reason"] == "capped", res
    assert calls == [], "a capped cycle still called the pick"
    assert tca._rec(base)["run"]["state"] == "capped"
    assert _run(ch, base, fk, pick)["state"] == "capped"           # the config cap still holds
    with pytest.raises(ch.ChainRefused):
        ch.resume(base, 1, cap=0)
    assert ch.resume(base, 1, cap=10) == {"goal": 1, "state": "approved-running", "cap": 10}
    res = _run(ch, base, fk, pick)
    assert res["state"] == "running" and res["picked"] == [3], res
    assert tca._rec(base)["run"]["cap"] == 10
    ch.stop(base, 1)
    assert tca._rec(base)["run"]["state"] == "stopped"
    assert ch.resume(base, 1)["state"] == "approved-running"
    with pytest.raises(ch.ChainRefused):
        ch.resume(base, 1)                                          # already running: nothing to resume


# 4 -- the heartbeat moves when a claim is made and when a member closes --------------------------------------

def test_heartbeat(tmp_path):
    ch, base, fk = _armed(tmp_path)
    pick = FakePick(fk, base)
    _run(ch, base, fk, pick, at=NOW)
    assert tca._rec(base)["run"]["heartbeat"] == ch._iso(NOW)
    _close(fk, 1, 2, 3)                                             # merged elsewhere between two cycles
    res = _run(ch, base, fk, pick, at=NOW + 600)
    assert res["state"] == "done" and res["closed"] == [1, 2, 3], res
    assert tca._rec(base)["run"]["heartbeat"] == ch._iso(NOW + 600)


# 5 -- a killed run resumes without claiming anything twice ----------------------------------------------------

class Crash(BaseException):
    pass


def test_resume_after_kill(tmp_path):
    ch, base, fk = _armed(tmp_path)
    pick = FakePick(fk, base)
    real = ch._save

    def die(*a):
        raise Crash("killed after the claim, before the record")
    ch._save = die
    with pytest.raises(Crash):
        _run(ch, base, fk, pick)
    ch._save = real
    assert pick.claims == [3] and "picked" not in (tca._rec(base).get("run") or {})
    _close(fk, 3)                                                    # its worker finished meanwhile
    res = _run(ch, base, fk, pick, at=NOW + 5)                       # the lease keeps 3 from being claimed again
    assert res["picked"] == [2], res
    assert sorted(pick.claims) == [2, 3] and len(set(pick.claims)) == len(pick.claims)
    # a pick without a lease returns a number the record already holds: it is recorded once
    def again(label, skip):
        return [2]
    _run(ch, base, fk, again, at=NOW + 6)
    _run(ch, base, fk, again, at=NOW + 7)
    picked = tca._rec(base)["run"]["picked"]
    assert picked.count(2) == 1 and len(picked) == len(set(picked)), picked


# 6 -- every refusal writes nothing and starts nothing --------------------------------------------------------

def test_refuses_and_spawns_nothing(tmp_path, monkeypatch):
    def no_spawn(*a, **k):
        raise AssertionError("a process was started: %s" % (a,))
    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)

    def boom(*a):
        raise AssertionError("the pick was called")
    ch = tca._chain()
    cases = []
    base, fk = tca._scene(tmp_path / "none", approve=False)
    tca._rec_path(base).unlink()
    cases.append(("no record", base, fk))
    cases.append(("planned",) + tca._scene(tmp_path / "planned", approve=False))
    ch3, base3, fk3 = _armed(tmp_path / "replanned")
    rec = tca._rec(base3)
    rec["state"] = "planned"                                        # armed once, then no longer approved
    tca._put(base3, rec)
    cases.append(("armed but not approved", base3, fk3))
    cases.append(("approved, not armed",) + tca._scene(tmp_path / "unarmed"))
    ch2, base2, fk2 = _armed(tmp_path / "stopped")
    ch2.stop(base2, 1)
    cases.append(("stopped", base2, fk2))
    for name, base, fk in cases:
        path = tca._rec_path(base)
        before = path.read_bytes() if path.exists() else None
        with pytest.raises(ch.ChainRefused):
            ch.run(base, 1, pick=boom, now=NOW, run=fk, sweep=boom)
        assert ch.main(["chain.py", "run", base, "1"], run=fk, now=NOW, pick=boom) == 2, name
        assert fk.calls == [], "%s: touched GitHub: %s" % (name, fk.calls[:2])
        assert (path.read_bytes() if path.exists() else None) == before, name
    assert ch.main(["chain.py", "run", base2, "1", "--bogus"], run=fk2) == 2


# 7 -- the default pick is the loop's own scoped call -----------------------------------------------------------

def test_default_pick_argv(tmp_path, monkeypatch):
    ch, base, fk = _armed(tmp_path)
    loop = str(pathlib.Path(ch.__file__).resolve().parent / "loop.py")
    want = [sys.executable, loop, "next-batch", base, "--label", LABEL, "--session-pid", "4242"]
    assert ch.pick_argv(base, LABEL, 4242) == want
    assert ch.pick_argv(base, LABEL, 4242, skip={"8", "7"}) == want + ["--skip", "7,8"]
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="3\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    res = ch.run(base, 1, session_pid=4242, now=NOW, run=fk, sweep=_sweep)
    assert seen == [want] and res["picked"] == [3], seen
    # a failed pick is a stuck cycle with a reason, never a crash and never done
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "HTTP 429 rate limit"))
    res = ch.run(base, 1, session_pid=4242, now=NOW + 1, run=fk, sweep=_sweep)
    assert res["state"] == "stuck" and res["reason"] == "rate-limit", res
