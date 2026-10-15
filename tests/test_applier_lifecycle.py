"""Goal 1499: the applier ask lifecycle -- sync of existing parks, no stale asks, armed windows, dry run."""
import datetime
import json
import os
import subprocess
import sys

from test_applier import AP, AU, DS, RB, SCRIPTS, T0, MIN, Src, _cfg, _sd, _setup, _train, _rule

DAY = datetime.timedelta(days=1)
PARK = "Parked by Sigma — needs human review: stuck\nsigma-qkind: %s"


class LSrc(Src):
    """A source with comments: `parks` maps issue -> list of (comment id, qkind) park comments."""

    def __init__(self, parks=None, labels=("area:docs",), fail=False):
        super().__init__()
        self.parks, self.labels, self.fail, self.reads = parks or {}, list(labels), fail, 0

    def _read_issue(self, goal, fields, comment_limit=None):
        self.reads += 1
        if self.fail:
            raise OSError("down")
        cs = [{"id": cid, "body": PARK % k, "createdAt": T0.isoformat()} for cid, k in self.parks.get(goal, [])]
        return {"labels": [{"name": n} for n in self.labels + [self.parked_label]], "comments": cs}


def _fetch(numbers, complete=True):
    return lambda src: ([{"number": n, "labels": [{"name": "sdlc:parked"}]} for n in numbers], complete)


def _dep_setup(tmp_path, kind="dependency", option="wait for it"):
    cfg = _cfg()
    sd = _sd(tmp_path)
    for _ in range(8):
        DS.append(sd, {"how": "human", "qkind": kind, "area": "docs", "choice": option}, cfg)
        AU.record_watch(sd, kind, "docs", option, option, cfg, now=T0, machine="m1")
    rid = _rule(sd, cfg, qkind=kind, option=option)
    return sd, cfg, rid


def _states(sd):
    return sorted(a["state"] for a in AP._asks(sd))


def test_sync_opens_an_ask_for_an_existing_park_once(tmp_path):
    sd, cfg, _ = _dep_setup(tmp_path)
    src = LSrc({7: [("c1", "dependency")]})
    assert len(AP.sync(sd, cfg, src, now=T0, parked_fn=_fetch([7]))) == 1
    assert AP.sync(sd, cfg, src, now=T0 + MIN, parked_fn=_fetch([7])) == []
    asks = AP._asks(sd)
    assert len(asks) == 1 and asks[0]["park"] == "c1" and asks[0]["gesture"] == "keep-parked"


def test_sync_skips_none_gesture_kinds_and_survives_read_failure(tmp_path):
    sd, cfg, _ = _setup(tmp_path, qkind="merge_conflict")
    # 'start fresh' is a none-gesture option: a rule on it opens nothing
    assert AP.sync(sd, cfg, LSrc({7: [("c1", "needs_decision")]}), now=T0, parked_fn=_fetch([7])) == []
    assert AP.sync(sd, cfg, LSrc({7: [("c1", "merge_conflict")]}, fail=True), now=T0, parked_fn=_fetch([7])) == []
    assert AP._asks(sd) == []


def test_sync_reads_are_bounded_per_tick(tmp_path):
    sd, cfg, _ = _dep_setup(tmp_path)
    src = LSrc({n: [("c%d" % n, "dependency")] for n in range(1, 60)})
    AP.sync(sd, cfg, src, now=T0, parked_fn=_fetch(range(1, 60)))
    assert src.reads <= AP.SYNC_MAX_READS and len(AP._asks(sd)) == AP.SYNC_MAX_READS


def test_a_new_park_of_another_kind_supersedes_the_pending_ask(tmp_path):
    sd, cfg, _ = _dep_setup(tmp_path)
    src = LSrc({7: [("c1", "dependency")]})
    AP.sync(sd, cfg, src, now=T0, parked_fn=_fetch([7]))
    src.parks[7].append(("c2", "dependency"))     # parked again later, same kind: a new park
    AP.sync(sd, cfg, src, now=T0 + MIN, parked_fn=_fetch([7]))
    assert _states(sd) == ["pending", "superseded"]
    assert [a["park"] for a in AP._asks(sd) if a["state"] == "pending"] == ["c2"]


def test_sync_closes_asks_of_an_issue_no_longer_parked(tmp_path):
    sd, cfg, _ = _dep_setup(tmp_path)
    AP.sync(sd, cfg, LSrc({7: [("c1", "dependency")]}), now=T0, parked_fn=_fetch([7]))
    AP.sync(sd, cfg, LSrc(), now=T0 + MIN, parked_fn=_fetch([], complete=False))   # a blip closes nothing
    assert _states(sd) == ["pending"]
    AP.sync(sd, cfg, LSrc(), now=T0 + MIN, parked_fn=_fetch([]))
    assert "pending" not in _states(sd)


def test_unpark_by_the_sweep_closes_the_pending_ask(tmp_path):
    sd, cfg, _ = _dep_setup(tmp_path)
    AP.open_ask(sd, 7, "dependency", "docs", ["wait for it"], cfg, T0, gesture="keep-parked")
    AP.close_for_issue(sd, cfg, 7, "auto-unpark", now=T0 + MIN)
    assert _states(sd) == ["answered"]


def test_superseded_asks_are_pruned_after_two_days(tmp_path):
    sd, cfg, _ = _dep_setup(tmp_path)
    aid = AP.open_ask(sd, 7, "dependency", "docs", ["wait for it"], cfg, T0, gesture="keep-parked")
    AP._supersede(sd, next(a for a in AP._asks(sd) if a["id"] == aid), T0)
    AP.tick(sd, cfg, T0 + DAY + MIN, source=LSrc())
    assert len(AP._asks(sd)) == 1
    AP.tick(sd, cfg, T0 + 2 * DAY + MIN, source=LSrc())
    assert AP._asks(sd) == []


def test_keep_parked_applies_once_across_ticks_and_syncs(tmp_path):
    sd, cfg, _ = _dep_setup(tmp_path)
    src = LSrc({7: [("c1", "dependency")]})
    for i in range(0, 6):
        now = T0 + i * 30 * MIN
        AP.sync(sd, cfg, src, now=now, parked_fn=_fetch([7]))
        AP.tick(sd, cfg, now, source=src)
    applied = [n for _, n in src.notes if "Applied automatically" in n]
    asks = AP._asks(sd)
    assert len(applied) == 1 and [a["state"] for a in asks if a["state"] == "applied"] == ["applied"]
    assert len(asks) <= 2 and sum(1 for a in asks if a["state"] == "pending") == 0


def test_an_ask_opened_below_level_3_gets_a_fresh_window_and_one_notice(tmp_path):
    sd, cfg, rid = _dep_setup(tmp_path)
    level = {"v": 2}
    AP.level_fn = lambda *a: level["v"]
    try:
        src = LSrc({7: [("c1", "dependency")]})
        AP.sync(sd, cfg, src, now=T0, parked_fn=_fetch([7]))
        assert AP._asks(sd)[0]["armed_at"] is None
        assert AP.tick(sd, cfg, T0 + 120 * MIN, source=src) == []
        level["v"] = 3
        acts = AP.tick(sd, cfg, T0 + 121 * MIN, source=src)        # the level just rose: arm, never apply
        assert [a["kind"] for a in acts] == ["arm"] and src.swaps == []
        assert sum(1 for _, n in src.notes if rid in n and "will apply" in n) == 1
        assert AP.tick(sd, cfg, T0 + 150 * MIN, source=src) == []
        assert AP.tick(sd, cfg, T0 + 122 * MIN + 60 * MIN, source=src)[0]["result"] == "done"
        assert sum(1 for _, n in src.notes if "will apply" in n) == 1
    finally:
        AP.level_fn = None


def test_apply_record_carries_gesture_and_answer(tmp_path):
    sd, cfg, rid = _setup(tmp_path)
    AP.open_ask(sd, 7, "merge_conflict", "docs", ["rebase it"], cfg, T0)
    AP.tick(sd, cfg, T0 + 61 * MIN, source=Src())
    store = DS._store(sd, cfg)
    recs = [json.load(open(os.path.join(store, n))) for n in os.listdir(store) if n.endswith(".json")]
    auto = [r for r in recs if r.get("how") == "autonomous"]
    assert len(auto) == 1 and auto[0]["gesture"] == "unpark" and auto[0]["answer"] == "rebase it"


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path):
    sd, cfg, rid = _setup(tmp_path)
    AP.open_ask(sd, 7, "merge_conflict", "docs", ["rebase it"], cfg, T0)
    json.dump(cfg, open(os.path.join(sd, "config.json"), "w"))
    before = _snapshot(sd)
    out = subprocess.run([sys.executable, str(SCRIPTS / "applier.py"), sd, "--dry-run"],
                         capture_output=True, text=True, env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
    assert out.returncode == 0
    assert rid in out.stdout and "level 3" in out.stdout and "window 60m" in out.stdout
    assert _snapshot(sd) == before


def _snapshot(sd):
    snap = {}
    for d, _, fs in os.walk(sd):
        for f in fs:
            snap[os.path.join(d, f)] = open(os.path.join(d, f)).read()
    return snap


def test_closed_gate_sync_does_nothing(tmp_path):
    sd = _sd(tmp_path)
    cfg = _cfg(enabled=False)
    src = LSrc({7: [("c1", "dependency")]})
    assert AP.sync(sd, cfg, src, now=T0, parked_fn=_fetch([7])) == []
    assert src.reads == 0 and not os.path.exists(sd)
    r = subprocess.run([sys.executable, str(SCRIPTS / "applier.py"), sd, "sync"], capture_output=True, text=True)
    assert (r.stdout, r.stderr) == ("", "") and not os.path.exists(sd)


def test_legacy_asks_without_armed_at_keep_their_window(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    AP.open_ask(sd, 7, "merge_conflict", "docs", ["rebase it"], cfg, T0)
    assert "armed_at" not in AP._asks(sd)[0]
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 61 * MIN, source=Src())] == ["done"]


class _ListSrc(LSrc):
    def _list_issues(self, fields, labels=(), state="open", cap=None, sort="created", direction="desc"):
        if self.parked_label in labels:
            return [{"number": n, "labels": [{"name": self.parked_label}]} for n in self.parks]
        return []


def test_scenario_through_the_applier_verb_on_a_temp_repo(tmp_path, monkeypatch):
    """The applier verb on a real temp repo: the first pass opens the ask, a later tick applies it once,
    and the next passes open no second ask for the same park."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    sd, cfg, rid = _dep_setup(tmp_path)
    json.dump(cfg, open(os.path.join(sd, "config.json"), "w"))
    src = _ListSrc({7: [("c1", "dependency")]})
    real_load = AP._load

    class _Sources:
        GitHubSource = staticmethod(lambda config, run=None: src)

    monkeypatch.setattr(AP, "_load", lambda name: _Sources if name == "sources" else real_load(name))
    assert AP.main(["applier.py", sd]) == 0
    assert [a["state"] for a in AP._asks(sd)] == ["pending"]
    acts = AP.tick(sd, cfg, datetime.datetime.now(datetime.timezone.utc) + 2 * 60 * MIN, source=src)
    assert [a["result"] for a in acts] == ["done"]
    assert AP.main(["applier.py", sd]) == 0
    assert AP.main(["applier.py", sd]) == 0
    assert _states(sd) == ["applied"]
    assert sum(1 for _, n in src.notes if "Applied automatically" in n) == 1


def test_closed_gate_leaves_every_path_byte_identical(tmp_path):
    sd = _sd(tmp_path)
    cfg = _cfg(enabled=False)
    os.makedirs(sd)
    AP.open_ask(sd, 7, "dependency", "docs", ["x"], _cfg(), T0, gesture="keep-parked")
    before = _snapshot(sd)
    assert AP.tick(sd, cfg, T0 + DAY, source=LSrc()) == []
    assert AP.tick(sd, cfg, T0 + DAY, source=LSrc(), dry_run=True) == []
    assert AP.sync(sd, cfg, LSrc(), now=T0, parked_fn=_fetch([7])) == []
    assert AP.open_for_park(sd, cfg, 7, "dependency", source=LSrc(), now=T0) is None
    assert _snapshot(sd) == before
