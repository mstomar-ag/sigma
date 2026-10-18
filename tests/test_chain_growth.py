"""#1589: a running chain may grow, within bounds; every addition beyond them is asked, once, as a delta sheet.

Fakes only: the closure read, the pull, the filed list and the pick are injected, so no process starts and no
network is touched. The base chain is the run tests' (3 <- 2 <- 1; goal 1 is the root).

Controls (each clause alone, then restored): remove the max_added bound; ignore freeze; allow a fourth pause;
let a follow-up join; skip no dependents of a paused prerequisite in the pick; re-parent the root on a
decomposition.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_chain_arm as tca  # noqa: E402
import test_chain_run as tcr  # noqa: E402

NOW = tca.NOW


def _row(n, verdict="armed"):
    return {"number": n, "verdict": verdict, "waits_on": [], "labels": ["sdlc:goal"], "kind": None,
            "spend": False}


def _closure(base, new=(), edges=()):
    """-> a closure fake: the recorded members plus `new` rows; `edges` are (blocker, blocked) pairs."""
    def read(*_a, **_k):
        rows = [dict(r) for r in tca._rec(base)["members"]] + [_row(n) for n in new]
        es = [{"from": b, "to": r["number"], "tag": "explicit"} for r in rows for b in r.get("waits_on") or []]
        es += [{"from": b, "to": n, "tag": "explicit"} for b, n in edges]
        return {"members": rows, "edges": es, "externals": []}
    return read


class FakePull:
    def __init__(self, result="pulled"):
        self.calls, self.result = [], result

    def __call__(self, sdlc_dir, label, ref, for_goal, config=None, run=None):
        self.calls.append((label, str(ref).lstrip("#"), int(for_goal)))
        return {"ref": str(ref), "result": self.result, "reason": "", "actions": []}


def _cfg(base, **chain):
    cp = pathlib.Path(base) / "config.json"
    cfg = json.loads(cp.read_text())
    cfg["chain"] = dict(cfg.get("chain") or {}, **chain)
    cp.write_text(json.dumps(cfg))


def _go(ch, base, fk, pick, at=NOW, **kw):
    return tcr._run(ch, base, fk, pick, at=at, **kw)


def _idle(label, skip):
    return [3]


# 1 -- a free prerequisite joins by itself, with one line and under the root ----------------------------------

def test_prerequisite_joins(tmp_path):
    ch, base, fk = tcr._armed(tmp_path)
    pull = FakePull()
    res = _go(ch, base, fk, _idle, closure=_closure(base, [4], [(4, 3)]), pull=pull)
    assert pull.calls == [("chain:1", "4", 1)], pull.calls
    assert res["joined"] == [4] and res["delta"] == "" and res["exit"] == 0, res
    assert tca._rec(base)["run"]["joined"] == [4]
    out = ch.render_run(res)
    assert [l for l in out.splitlines() if l.startswith("joined")] == ["joined #4 (a new prerequisite of #1)"], out
    assert "added to the label" not in out


# 2 -- anything else filed from a member stays out and is listed ----------------------------------------------

def test_followup_stays_out(tmp_path):
    ch, base, fk = tcr._armed(tmp_path)
    pull = FakePull()
    tcr._close(fk, 1, 2, 3)
    res = _go(ch, base, fk, lambda l, s: [], closure=_closure(base), pull=pull, filed=lambda *a: [77])
    assert pull.calls == [] and res["joined"] == [], "a follow-up joined the chain"
    assert res["filed"] == [77] and res["state"] == "done", res
    out = ch.render_run(res)
    assert "filed, not in the chain: #77" in out, out
    assert tca._rec(base)["run"].get("joined") in (None, [])


# 3 -- the sixth addition is asked, and the delta carries only the new question -------------------------------

def test_bound_and_delta(tmp_path):
    ch, base, fk = tcr._armed(tmp_path)
    pull = FakePull()
    new = [11, 12, 13, 14, 15, 16]
    res = _go(ch, base, fk, _idle, closure=_closure(base, new, [(n, 3) for n in new]), pull=pull)
    assert [c[1] for c in pull.calls] == ["11", "12", "13", "14", "15"], pull.calls
    assert res["joined"] == [11, 12, 13, 14, 15] and res["paused"] == [16], res
    delta = res["delta"]
    assert "grow:16" in delta and "#16" in delta, delta
    assert "cap:" not in delta and "label_set" not in delta and "Writes that follow" not in delta, delta
    assert not any(("#%d" % n) in delta for n in (11, 12, 13, 14, 15)), delta     # only what is new and asked
    blk = tca._rec(base)["run"]
    assert blk["pauses"] == 1 and list(blk["paused"]) == ["16"] and blk["paused"]["16"], blk
    # the question is on the sheet and answerable; "add it" lets that one join despite the bound
    got = ch.answer(base, 1, {"grow:16": "add it"}, run=fk)
    assert got["errors"] == [] and tca._rec(base)["state"] == "approved", got
    # the run kept going on the independent branch, so there is nothing to resume: the next cycle takes the answer
    pull2 = FakePull()
    res = _go(ch, base, fk, _idle, at=NOW + 5, closure=_closure(base, new, [(n, 3) for n in new]), pull=pull2)
    assert [c[1] for c in pull2.calls] == ["16"] and res["joined"] == [16] and res["paused"] == [], res


# 4 -- freeze: nothing joins, each addition is asked ------------------------------------------------------------

def test_freeze(tmp_path):
    for how in ("config", "flag"):
        ch, base, fk = tcr._armed(tmp_path / how)
        if how == "config":
            _cfg(base, freeze=True)
        pull = FakePull()
        res = _go(ch, base, fk, _idle, closure=_closure(base, [4], [(4, 3)]), pull=pull, no_grow=how == "flag")
        assert pull.calls == [] and res["joined"] == [] and res["paused"] == [4], (how, res)
        assert "grow:4" in res["delta"], res["delta"]


def test_no_grow_flag_reaches_run(tmp_path, monkeypatch):
    ch, base, fk = tcr._armed(tmp_path)
    seen = {}

    def spy(*a, **k):
        seen.update(k)
        return {"goal": 1, "state": "running", "exit": 0, "reason": None, "detail": "", "picked": [],
                "added": [], "closed": [], "cycle": 1}
    monkeypatch.setattr(ch, "run", spy)
    assert ch.main(["chain.py", "run", base, "1", "--no-grow"], run=fk, now=NOW) == 0
    assert seen.get("no_grow") is True
    seen.clear()
    assert ch.main(["chain.py", "run", base, "1"], run=fk, now=NOW) == 0
    assert not seen.get("no_grow")


# 5 -- a fourth pause ends the run stuck ------------------------------------------------------------------------

def test_reask_bound(tmp_path):
    ch, base, fk = tcr._armed(tmp_path)
    _cfg(base, freeze=True)
    calls = []

    def pick(label, skip):
        calls.append(1)
        return [3]
    for i, n in enumerate((4, 5, 6)):
        res = _go(ch, base, fk, pick, at=NOW + i, closure=_closure(base, [4, 5, 6][:i + 1]))
        assert res["state"] == "running" and res["delta"], (i, res)
    assert tca._rec(base)["run"]["pauses"] == 3 and len(calls) == 3
    res = _go(ch, base, fk, pick, at=NOW + 9, closure=_closure(base, [4, 5, 6, 7]))
    assert res["state"] == "stuck" and res["reason"] == "decision" and res["exit"] == 3, res
    assert "max_reasks" in res["detail"] and len(calls) == 3, "the fourth pause still picked"
    assert tca._rec(base)["run"]["stuck"] == "decision"


# 6 -- the root is never re-parented when the end goal decomposes -------------------------------------------------

def test_root_not_reparented(tmp_path):
    ch, base, fk = tcr._armed(tmp_path)
    before = [r["number"] for r in tca._rec(base)["members"]]
    pull = FakePull()
    res = _go(ch, base, fk, _idle, closure=_closure(base, [21, 22], [(21, 1), (22, 1)]), pull=pull)
    assert res["joined"] == [21, 22], res
    assert {c[2] for c in pull.calls} == {1}, "a slice was pulled for a goal other than the root"
    rec = tca._rec(base)
    assert rec["goal"] == 1 and [r["number"] for r in rec["members"]] == before
    assert tca._rec_path(base).name == "c1.json"


# 7 -- a paused prerequisite holds back its dependents only -------------------------------------------------------

def test_subtree_pause(tmp_path):
    ch, base, fk = tcr._armed(tmp_path)
    _cfg(base, freeze=True)
    skips = []

    def pick(label, skip):
        skips.append(set(skip))
        return [3]
    res = _go(ch, base, fk, pick, closure=_closure(base, [4], [(4, 2)]))
    assert res["paused"] == [4]
    assert skips == [{"1", "2"}], skips                       # 1 waits on 2 which waits on 4; 3 is independent
    assert tca._rec(base)["run"]["paused"] == {"4": [1, 2]}
    # nothing else can run: the empty pick with members left is STUCK(decision)
    res = _go(ch, base, fk, lambda l, s: [], at=NOW + 3, closure=_closure(base, [4], [(4, 2)]))
    assert res["state"] == "stuck" and res["reason"] == "decision" and "#4" in res["detail"], res


# 8 -- when nothing grows the output is what it was ---------------------------------------------------------------

def test_no_growth_unchanged(tmp_path):
    ch, base, fk = tcr._armed(tmp_path)
    seen = []
    res = _go(ch, base, fk, lambda l, s: seen.append(set(s)) or [3], closure=_closure(base), pull=FakePull())
    assert ch.render_run(res) == "chain #1: running (cycle 1)\nclaimed: #3\n"
    assert seen == [set()] and res["joined"] == [] and res["paused"] == [] and res["filed"] == []
    blk = tca._rec(base)["run"]
    assert not any(k in blk for k in ("joined", "paused", "delta", "pauses", "filed")), blk
    # a read that fails is a cycle with no growth, never a crash
    def boom(*_a, **_k):
        raise RuntimeError("HTTP 500")
    res = _go(ch, base, fk, lambda l, s: [3], at=NOW + 1, closure=boom)
    assert ch.render_run(res) == "chain #1: running (cycle 2)\nclaimed: #3\n"
