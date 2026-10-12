"""#1177: every human decision record also leaves one watch comparison (what the rulebook or the
earlier answers would have chosen, next to what the person chose), so the autonomy ladder can climb
through real gestures. Reuses `autonomy.record_watch`; no new write site."""
import datetime
import importlib.util
import json
import os
import pathlib

import test_unpark as TU

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
T0 = datetime.datetime(2026, 10, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_t1177", S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


DR, DS, AU, RB, RD = (_mod(n) for n in ("decision_record", "decision_store", "autonomy", "rulebook",
                                        "rule_draft"))


def _cfg(autonomy=True, records=True, visibility=None, l1=3, l2=12, rulebook=True):
    rec = {"enabled": records}
    if visibility:
        rec["repo_visibility"] = visibility
    return {"decision_rubric": {
        "records": rec, "rulebook": {"enabled": rulebook},
        "autonomy": {"enabled": autonomy, "owners": ["boss"],
                     "levels": {"l1_min_answers": l1, "l2_min_answers": l2, "l2_agreement": 0.9}}}}


def _watch_files(sd):
    d = pathlib.Path(sd) / "autonomy"
    return sorted(d.glob("watch-*.json")) if d.is_dir() else []


def _rec(choice="merge", how="human", qkind="needs_decision", area="docs", gesture="unpark"):
    return DR.build(gesture, 5, qkind, choice, "why", how, area=area)


def _seed(sd, cfg, choices, qkind="needs_decision", area="docs"):
    for c in choices:
        DS.append(sd, {"how": "human", "qkind": qkind, "area": area, "choice": c}, cfg)


class _Src:
    def __init__(self): self.posted = []
    def _issue_comment(self, n, body): self.posted.append((n, body))


def _write(sd, rec, cfg):
    return DR.write_all(sd, rec, cfg, _Src())


def _approved_rule(sd, cfg, option="merge", qkind="needs_decision", area="docs"):
    RB.propose(sd, {"qkind": qkind, "area": area, "option": option, "reason": "r"}, cfg, now=T0)
    rid = RB.load(sd, cfg)[0]["id"]
    RB.approve(sd, rid, "boss", cfg, now=T0)


# 1 -- gate closed -----------------------------------------------------------------------------

def test_gate_closed_writes_no_watch_file_and_same_comment_bytes(tmp_path):
    sd = str(tmp_path / ".sdlc")
    for cfg in (_cfg(autonomy=False), _cfg(records=False)):
        _seed(sd, _cfg(), ["merge"] * 5)
        src = _Src()
        DR.write_all(sd, _rec(), cfg, src)
        assert _watch_files(sd) == []


def test_would_for_is_none_when_gate_closed(tmp_path):
    sd = str(tmp_path / ".sdlc")
    _seed(sd, _cfg(), ["merge"] * 5)
    assert DR.would_for(sd, _rec(), _cfg(autonomy=False)) is None
    assert DR.would_for(sd, _rec(how="autonomous"), _cfg()) is None


# 2 -- would -----------------------------------------------------------------------------------

def test_majority_needs_enough_earlier_answers(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=3)
    _seed(sd, cfg, ["merge", "merge"])
    assert DR.would_for(sd, _rec(), cfg) is None
    _seed(sd, cfg, ["merge"])
    assert DR.would_for(sd, _rec(), cfg) == "merge"


def test_tie_and_other_area_give_no_would(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=2)
    _seed(sd, cfg, ["merge", "close"])
    assert DR.would_for(sd, _rec(), cfg) is None
    _seed(sd, cfg, ["merge"] * 5, area="other")
    assert DR.would_for(sd, _rec(), cfg) is None
    assert DR.would_for(sd, _rec(area="other"), cfg) == "merge"


def test_sealed_rule_wins_over_majority(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=3)
    _seed(sd, cfg, ["close"] * 5)
    _approved_rule(sd, cfg, option="merge")
    assert DR.would_for(sd, _rec(), cfg) == "merge"


def test_none_would_writes_nothing_and_comparison_records_agreement(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=3)
    _write(sd, _rec(), cfg)
    assert _watch_files(sd) == []
    _seed(sd, cfg, ["merge"] * 3)
    _write(sd, _rec("close"), cfg)
    (f,) = _watch_files(sd)
    w = json.loads(f.read_text())
    assert (w["would"], w["actual"], w["qkind"], w["area"]) == ("merge", "close", "needs_decision", "docs")


def test_answer_is_not_in_its_own_majority(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=3)
    _seed(sd, cfg, ["merge"] * 2)
    _write(sd, _rec("merge"), cfg)             # only 2 earlier answers: no would, even though 3 now exist
    assert _watch_files(sd) == []


# 3 -- failures --------------------------------------------------------------------------------

def test_store_failure_writes_no_watch_file(tmp_path, monkeypatch):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=3)
    _seed(sd, cfg, ["merge"] * 4)
    monkeypatch.setattr(DR.decision_store, "append",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    out = _write(sd, _rec(), cfg)
    assert out["error"] and _watch_files(sd) == []


def test_watch_failure_does_not_fail_the_gesture(tmp_path, monkeypatch):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=3)
    _seed(sd, cfg, ["merge"] * 4)
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("watch"))
    monkeypatch.setattr(DR, "would_for", boom)
    monkeypatch.setattr(DR, "record_comparison", boom)
    src = _Src()
    out = DR.write_all(sd, _rec(), cfg, src)
    assert out["id"] and out["error"] is None and out["commented"] and len(src.posted) == 1


def test_public_visibility_writes_no_watch_file(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=3, visibility="public")
    _seed(sd, cfg, ["merge"] * 4)
    assert DR.would_for(sd, _rec(), cfg) is None
    _write(sd, _rec(), cfg)
    assert _watch_files(sd) == []


# 4 -- hard stop -------------------------------------------------------------------------------

def test_hard_stop_kind_records_comparison_and_level_is_capped(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=2, l2=3)
    hs = _mod("hard_stop")
    kind = next(k for k in sorted(DR.qkind.QKINDS) if hs.is_hardstop_kind(k, None, cfg))
    for _ in range(12):
        _write(sd, _rec("merge", qkind=kind), cfg)
    assert _watch_files(sd)
    assert AU.level(sd, kind, "docs", cfg) <= AU.HARDSTOP_CAP


# 6 -- ack and promote paths -------------------------------------------------------------------

def test_ack_path_writes_one_comparison(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=3)
    _seed(sd, cfg, ["done"] * 3, qkind="unknown", area="docs")
    _mod("handoff")._record_ack(sd, cfg, 7, None, "done", "why", area="docs")
    (f,) = _watch_files(sd)
    w = json.loads(f.read_text())
    assert (w["would"], w["actual"]) == ("done", "done")


def test_promote_gesture_writes_one_comparison(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=3)
    _seed(sd, cfg, ["promote"] * 3, qkind="needs_decision", area="docs")
    _write(sd, _rec("promote", gesture="promote"), cfg)
    assert len(_watch_files(sd)) == 1


# 5 -- end to end through the real unpark verb -------------------------------------------------

def _e2e_cfg():
    cfg = TU._config()
    cfg.update(_cfg(l1=2, l2=12))
    return cfg


def _unpark_n(sd, cfg, n, u=None):
    u = u or TU._mod("unpark")
    park = TU._park("needs a call\n" + DR.qkind.render_line("needs_decision"))
    for i in range(n):
        run = TU._runner(views={"5": TU._view(labels=("sdlc:parked", "area:docs"), comments=[park])})
        r = u.resolve(sd, cfg, 5, {"decision": "start it again"}, "unpark", run=run)
        assert r["outcome"] == "unparked"


def test_end_to_end_unpark_climbs_to_level_two_and_drafts(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _e2e_cfg()
    _unpark_n(sd, cfg, 14)
    assert AU.level(sd, "needs_decision", "docs", cfg) == 2
    assert ("needs_decision", "docs") in [(p["qkind"], p["area"]) if isinstance(p, dict) else tuple(p[:2])
                                          for p in RD.list_patterns(sd, cfg)]


def test_end_to_end_red_control_without_the_comparison_level_stays_one(tmp_path, monkeypatch):
    sd, cfg = str(tmp_path / ".sdlc"), _e2e_cfg()
    u = TU._mod("unpark")
    monkeypatch.setattr(u.decision_record, "record_comparison", lambda *a, **k: None)
    _unpark_n(sd, cfg, 14, u)
    assert AU.level(sd, "needs_decision", "docs", cfg) == 1


# 7 -- goal 1234: the comparison starter honours the category threshold -------------------------

def _cats(sd, cats):
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "categories.json"), "w") as f:
        json.dump({"schema": "sigma/categories@1", "categories": cats}, f)


def test_would_uses_category_first_level_threshold(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=3)
    _seed(sd, cfg, ["merge"])
    assert DR.would_for(sd, _rec(), cfg) is None
    _cats(sd, [{"id": "fast", "areas": ["docs"], "levels": {"l1_min_answers": 1}}])
    assert DR.would_for(sd, _rec(), cfg) == "merge"
    _cats(sd, [{"id": "slow", "areas": ["docs"], "levels": {"l1_min_answers": 5}}])
    _seed(sd, cfg, ["merge"] * 2)
    assert DR.would_for(sd, _rec(), cfg) is None


def test_would_pair_outside_every_category_is_unchanged(tmp_path):
    sd, cfg = str(tmp_path / ".sdlc"), _cfg(l1=3)
    _cats(sd, [{"id": "fast", "areas": ["src"], "levels": {"l1_min_answers": 1}}])
    _seed(sd, cfg, ["merge"], area="docs")
    assert DR.would_for(sd, _rec(), cfg) is None
    _seed(sd, cfg, ["merge"] * 2, area="docs")
    assert DR.would_for(sd, _rec(), cfg) == "merge"
