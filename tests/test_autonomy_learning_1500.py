"""Goal 1500, part A: levels pool machines, survive retention, recover from vetoes, read one area."""
import datetime
import importlib.util
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
NOW = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
MIN = datetime.timedelta(minutes=1)
KIND = "needs_decision"


def _mod(name):
    p = SCRIPTS / (name + ".py")
    spec = importlib.util.spec_from_file_location(p.stem, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


A = _mod("autonomy")
DS = _mod("decision_store")
DR = _mod("decision_record")


def _cfg(enabled=True, **extra):
    au = {"enabled": enabled, "levels": {"l1_min_answers": 2, "l2_min_answers": 4, "l2_agreement": 0.75,
                                          "l3_min_answers": 8, "l3_agreement": 0.9,
                                          "l4_min_answers": 100, "l4_agreement": 0.99}}
    au.update(extra)
    return {"rulebook": {"records": {"enabled": True}, "autonomy": au}}


def _answers(sd, cfg, n, area="docs"):
    for _ in range(n):
        DS.append(sd, {"how": "human", "qkind": KIND, "area": area, "choice": "a"}, cfg)


def _watch(sd, cfg, agree, disagree=0, machine="m1", at=NOW, area="docs", **kw):
    for _ in range(agree):
        A.record_watch(sd, KIND, area, "a", "a", cfg, now=at, machine=machine, **kw)
    for _ in range(disagree):
        A.record_watch(sd, KIND, area, "a", "b", cfg, now=at, machine=machine, **kw)


def _veto(sd, at):
    A._write_new(sd, "veto-", {"rid": "r", "qkind": KIND, "area": "docs", "machine": "m1", "time": at.isoformat()}, at)


def _lvl(sd, cfg, area="docs"):
    return A.level(sd, KIND, area, cfg)


def test_lagging_machine_does_not_cap(tmp_path):
    cfg = _cfg()
    _answers(tmp_path, cfg, 8)
    _watch(tmp_path, cfg, 8, machine="m1")
    _watch(tmp_path, cfg, 1, machine="m2")
    assert _lvl(tmp_path, cfg) == 3
    row = A.readout(tmp_path, cfg)["kinds"][0]
    assert "machine m2: 1 comparisons, 100% agreement" in A.describe(row)


def test_disagreeing_machine_with_enough_comparisons_still_caps(tmp_path):  # NEGATIVE
    cfg = _cfg()
    _answers(tmp_path, cfg, 8)
    _watch(tmp_path, cfg, 80, machine="m1")
    _watch(tmp_path, cfg, 6, 2, machine="m2")           # 8 own comparisons, 75% agreement
    assert _lvl(tmp_path, cfg) == 2                     # pooled 97%, yet never level 3


def test_veto_wears_off_after_enough_agreeing_answers(tmp_path):
    cfg = _cfg()
    _answers(tmp_path, cfg, 8)
    _watch(tmp_path, cfg, 8)
    _veto(tmp_path, NOW + MIN)
    assert _lvl(tmp_path, cfg) == 2                     # dropped one level
    _watch(tmp_path, cfg, 4, at=NOW + 2 * MIN)
    assert _lvl(tmp_path, cfg) == 2                     # default recover_after is 5
    _watch(tmp_path, cfg, 1, at=NOW + 3 * MIN)
    assert _lvl(tmp_path, cfg) == 3
    assert A.recover_after(cfg) == 5 and A.recover_after(_cfg(veto_recover_after=2)) == 2
    big = _cfg()
    big["rulebook"]["autonomy"]["levels"]["l3_min_answers"] = 60
    assert A.recover_after(big) == 10


def test_three_consecutive_vetoes_still_pause_at_zero(tmp_path):  # NEGATIVE
    cfg = _cfg()
    _answers(tmp_path, cfg, 8)
    _watch(tmp_path, cfg, 8)
    for i in range(3):
        _veto(tmp_path, NOW + (i + 1) * MIN)
    row = A.readout(tmp_path, cfg)["kinds"][0]
    assert row["paused"] and row["level"] == 0


def test_level_does_not_fall_because_comparisons_aged_out(tmp_path):
    cfg = _cfg()                                        # default retention, 30 days
    _answers(tmp_path, cfg, 8)
    _watch(tmp_path, cfg, 8)
    assert _lvl(tmp_path, cfg) == 3
    later = NOW + datetime.timedelta(days=120)
    assert A.prune_watch(tmp_path, later, cfg) == 0
    assert _lvl(tmp_path, cfg) == 3


def test_prune_keeps_newest_n_per_pair_and_never_expires_imported(tmp_path):
    cfg = _cfg()
    cfg["rulebook"]["autonomy"]["levels"].update(l1_min_answers=1, l2_min_answers=2, l3_min_answers=3, l4_min_answers=3)
    old = NOW - datetime.timedelta(days=200)
    _watch(tmp_path, cfg, 2, at=old, imported={"source": "s"})
    for i in range(5):
        _watch(tmp_path, cfg, 1, at=old + (i + 1) * MIN)
    _watch(tmp_path, cfg, 4, at=old, area="web", machine="m9")
    removed = A.prune_watch(tmp_path, NOW, cfg)
    left = A._facts(tmp_path, "watch-")
    assert sum(1 for w in left if w.get("how") == "imported") == 2
    assert sum(1 for w in left if w["area"] == "docs" and w.get("how") != "imported") == 3
    assert sum(1 for w in left if w["area"] == "web") == 3
    assert removed == 2 + 1


def test_ladder_reads_only_newest_n_comparisons(tmp_path):
    cfg = _cfg()
    cfg["rulebook"]["autonomy"]["levels"].update(l4_min_answers=8, l3_min_answers=6)  # the largest threshold is the newest-N
    _answers(tmp_path, cfg, 8)
    _watch(tmp_path, cfg, 8, at=NOW)                    # first, so the write-time sweep stamp is NOW
    _watch(tmp_path, cfg, 0, 20, at=NOW - datetime.timedelta(days=400))
    assert len(A._facts(tmp_path, "watch-")) == 28      # nothing swept: age alone cannot lower the level
    assert _lvl(tmp_path, cfg) == 4


def test_missing_area_reads_repo_and_area_of_is_sorted():
    assert A._area(None) == "repo" and A._area("") == "repo" and A._area("api") == "api"
    assert DR.area_of(["area:web", "area:api", "bug"]) == "api"
    assert DR.area_of([{"name": "area:z"}, {"name": "area:b"}]) == "b"
    assert DR.area_of(["bug"]) == "repo"


def test_old_record_without_area_joins_the_repo_pair(tmp_path):
    cfg = _cfg()
    DS.append(tmp_path, {"how": "human", "qkind": KIND, "area": "", "choice": "a"}, cfg)
    DS.append(tmp_path, {"how": "human", "qkind": KIND, "area": "repo", "choice": "a"}, cfg)
    assert _lvl(tmp_path, cfg, area="repo") == 1
    assert _lvl(tmp_path, cfg, area="") == 1


def test_autonomy_closed_is_inert(tmp_path):
    cfg = _cfg(enabled=False)
    _answers(tmp_path, cfg, 8)
    assert A.record_watch(tmp_path, KIND, "docs", "a", "a", cfg, now=NOW) is None
    assert _lvl(tmp_path, cfg) == 0
    assert A.readout(tmp_path, cfg) == {"enabled": False}
    assert A.prune_watch(tmp_path, NOW, cfg) == 0
    assert not os.path.exists(os.path.join(str(tmp_path), "autonomy"))
