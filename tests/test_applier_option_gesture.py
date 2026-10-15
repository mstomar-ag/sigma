"""#1301: the applier carries out the rule's option (keep parked leaves labels alone)."""
import json
import os

import test_applier as T

AP, AU, DS, RB = T.AP, T.AU, T.DS, T.RB
T0, MIN = T.T0, T.MIN


def _setup(tmp_path, option, qkind="needs_decision"):
    cfg = T._cfg()
    sd = T._sd(tmp_path)
    T._train(sd, cfg, qkind=qkind, area="repo", level=3)
    T._rule(sd, cfg, qkind=qkind, area="repo", option=option)
    for _ in range(8):      # #1496 M-04: the rule needs live comparisons that predicted its own option
        AU.record_watch(sd, qkind, "repo", option, option, cfg, now=T0, machine="m1")
    return sd, cfg


def _records(sd):
    out = []
    for root, _d, files in os.walk(sd):
        for f in files:
            if "decision" in root or "decision" in f:
                out.append(os.path.join(root, f))
    return out


def test_keep_parked_option_leaves_labels_and_records_once(tmp_path):
    sd, cfg = _setup(tmp_path, "leave it parked")
    aid = AP.open_for_park(sd, cfg, 7, "needs_decision", source=T.Src(), now=T0)
    assert aid
    src = T.Src()
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    assert [(a["kind"], a["result"]) for a in acts] == [("keep-parked", "done")]
    assert src.swaps == []
    assert len(src.notes) == 1
    ask = json.load(open(AP._ask_path(sd, aid)))
    assert ask["state"] == "applied" and ask["rid"] is not None
    assert AP.tick(sd, cfg, T0 + 62 * MIN, source=src) == []
    assert src.swaps == [] and len(src.notes) == 1


def test_unpark_option_still_swaps(tmp_path):
    sd, cfg = _setup(tmp_path, "rebase it", qkind="merge_conflict")
    AP.open_for_park(sd, cfg, 7, "merge_conflict", source=T.Src(), now=T0)
    src = T.Src()
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    assert [(a["kind"], a["result"]) for a in acts] == [("unpark", "done")]
    assert len(src.swaps) == 1


def test_helper_reads_options_from_the_bank():
    f = AP.option_gesture
    assert f("needs_decision", "leave it parked") == "keep-parked"
    assert f("handoff", "look into it first") == "keep-parked"
    assert f("merge_conflict", "rebase it") == "unpark"
    assert f("quota", "wait it out") == "unpark"
    assert f("merge_conflict", "start fresh") == "none"
    assert f("merge_conflict", "merge") in (None, "none")
    assert f("needs_decision", "") in (None, "none")


def test_unlisted_option_or_kind_opens_no_ask(tmp_path):
    sd, cfg = _setup(tmp_path, "rebase it", qkind="merge_conflict")
    assert AP.open_for_park(sd, cfg, 7, "scope_hold", source=T.Src(), now=T0) is None


def test_promote_ask_unchanged(tmp_path):
    cfg = T._cfg()
    sd = T._sd(tmp_path)
    assert AP.open_ask(sd, 7, "needs_decision", "repo", ["leave it parked"], cfg, T0, gesture="promote")
    ask = AP._asks(sd)[0]
    assert ask["gesture"] == "promote"


def test_veto_of_keep_parked_does_not_swap(tmp_path):
    sd, cfg = _setup(tmp_path, "leave it parked")
    aid = AP.open_for_park(sd, cfg, 7, "needs_decision", source=T.Src(), now=T0)
    src = T.Src()
    AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    before = AU.level(sd, "needs_decision", "repo", cfg)
    assert AP.answer(sd, aid, "no", cfg, T0 + 70 * MIN) == "veto-pending"
    acts = AP.tick(sd, cfg, T0 + 71 * MIN, source=src)
    assert [(a["kind"], a["result"]) for a in acts] == [("veto", "done")]
    assert src.swaps == []
    assert AU.level(sd, "needs_decision", "repo", cfg) == before - 1


def test_closed_gate_is_byte_identical(tmp_path):
    cfg = T._cfg(enabled=False)
    sd = T._sd(tmp_path)
    os.makedirs(sd)
    snap = sorted(os.walk(str(tmp_path)))
    src = T.Src()
    assert AP.open_for_park(sd, cfg, 7, "needs_decision", source=src, now=T0) is None
    assert AP.tick(sd, cfg, T0 + 61 * MIN, source=src) == []
    assert sorted(os.walk(str(tmp_path))) == snap and src.swaps == [] and src.notes == []
