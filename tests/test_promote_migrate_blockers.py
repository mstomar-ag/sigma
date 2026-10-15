"""#1518: the confirmation migration ARMS an item whose only hold is an open blocker.

Before: `plan_for` parked it ("dependency"), so a person had to unpark work the pick-time dependency hold and the
auto-unpark sweep already handle. Now it gets `sdlc:goal` plus the `sdlc:blocked` overlay (docs/label-model.md,
the "Blocked" row) and the reason `armed (held by #N)`. Hard stops (ownership, scope, gate hold, triage non-arm)
still park; an unreadable blocker is a note, never a park. Fake source and fake gh only; no network."""
import json
import subprocess
import tempfile

from test_promote_migrate import FakeSource, PROPOSED, GOAL, PARKED, promote
import test_pick_dependency_gate as pg

BLOCKED = "sdlc:blocked"
BLK = "Blocked by: #50"


class Src(FakeSource):
    goal_blocked_label = BLOCKED


def _src():
    s = Src()
    s.add(50, GOAL, title="the blocker")
    return s


def _run(src, apply, tmp):
    return promote.migrate(src, {}, apply=apply, sdlc_dir=str(tmp))


def _item(rep, n):
    return next(i for i in rep["items"] if int(i["number"]) == n)


def test_dry_run_says_armed_held_by(tmp_path):
    s = _src()
    s.add(51, PROPOSED, "priority:P1", body=BLK)
    rep = _run(s, False, tmp_path)
    it = _item(rep, 51)
    assert it["outcome"] == "would-arm" and "armed (held by #50)" in it["reason"]
    assert s.writes == []


def test_apply_writes_goal_and_blocked_overlay(tmp_path):
    s = _src()
    s.add(51, PROPOSED, "priority:P1", body=BLK)
    s.add(52, PROPOSED, "priority:P1", body="no blocker here")
    rep = _run(s, True, tmp_path)
    assert _item(rep, 51)["outcome"] == "armed"
    assert {GOAL, BLOCKED} <= set(s.store[51]["labels"])
    assert PARKED not in s.store[51]["labels"] and PROPOSED not in s.store[51]["labels"]
    assert BLOCKED not in s.store[52]["labels"] and GOAL in s.store[52]["labels"]    # no blocker: no overlay
    assert "held by #50" in dict(s.comments)[51]


def test_closed_blocker_arms_without_overlay(tmp_path):
    s = _src()
    s.add(53, state="closed")
    s.add(51, PROPOSED, "priority:P1", body="Blocked by: #53")
    _run(s, True, tmp_path)
    assert GOAL in s.store[51]["labels"] and BLOCKED not in s.store[51]["labels"]


def test_hard_stops_still_park_even_with_an_open_blocker(tmp_path):
    s = _src()
    s.add(60, PROPOSED, "priority:P1", body=BLK, comments=[promote._OWNER_MARKER])
    s.add(61, PROPOSED, "priority:P1", body=BLK, comments=[promote._SCOPE_MARKER])
    s.add(62, PROPOSED, "priority:P1", body=BLK)
    orig = promote._feature_hold
    promote._feature_hold = lambda sd, c, n, names, a: "unit holds it" if int(n) == 62 else None
    try:
        rep = _run(s, True, tmp_path)
    finally:
        promote._feature_hold = orig
    for n in (60, 61, 62):
        assert _item(rep, n)["outcome"] == "parked", n
        assert PARKED in s.store[n]["labels"] and GOAL not in s.store[n]["labels"]


def test_triage_non_arm_still_parks(tmp_path):
    s = _src()
    s.add(63, PROPOSED, "sdlc:followup", body=BLK)       # a follow-up with no priority the rulebook accepts
    s.add(64, PROPOSED, "sdlc:followup", body="Blocked by: #50 and more")
    cfg = {"ai_filed": {"mode": "off"}}
    plain = promote.plan_for({"title": "t", "body": BLK, "labels": [PROPOSED], "comments": []}, {}, ["#50 (OPEN)"])
    assert isinstance(plain, promote.Arm)
    ft = promote._feature("file_triage")
    real = ft.decide
    ft.decide = lambda *a, **k: type("D", (), {"kind": "park", "reason": "needs a decision"})()
    try:
        p = promote.plan_for({"title": "t", "body": BLK, "labels": [PROPOSED], "comments": []}, cfg, ["#50 (OPEN)"])
    finally:
        ft.decide = real
    assert isinstance(p, promote.Park)


def test_unreadable_blocker_is_a_note_not_a_park(tmp_path):
    s = _src()
    s.add(51, PROPOSED, "priority:P1", body="Blocked by: #99")
    s.unreadable.add(99)
    rep = _run(s, True, tmp_path)
    it = _item(rep, 51)
    assert it["outcome"] == "armed" and BLOCKED not in s.store[51]["labels"]
    assert any("#99" in n and "unreadable" in n.lower() for n in it.get("notes", []))


def test_picker_skips_while_open_then_picks_after_close(tmp_path, capsys):
    s = _src()
    s.add(51, PROPOSED, "priority:P1", body="x" * 600 + "\n\n**Blocked by:** #50\n")
    _run(s, True, tmp_path)
    assert {GOAL, BLOCKED} <= set(s.store[51]["labels"])
    lp = pg._loop()

    def live():
        return [{"number": n, "title": "t", "body": i["body"], "state": i["state"].upper(),
                 "labels": [{"name": l} for l in i["labels"]], "closedAt": None, "updatedAt": "2026-08-23T00:00:00Z"}
                for n, i in s.store.items()]
    with tempfile.TemporaryDirectory() as d:
        base = pg._base(d, mirror=live())
        board = pg._Board(["51", "50"], live=live())
        assert lp._next(base, board, pg._cfg(lp, base)) == ("goal", "50")     # 51 held, not claimed
        assert board.marked == ["50"]
        s.store[50]["state"] = "closed"
        base2 = pg._base(d + "/b", mirror=live())
        board2 = pg._Board(["51"], live=live())
        assert lp._next(base2, board2, pg._cfg(lp, base2)) == ("goal", "51")


def test_cli_verb_on_a_temp_repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    sd = tmp_path / ".sdlc"
    sd.mkdir()
    (sd / "config.json").write_text(json.dumps({"discovery": {"source": "github", "github": {"repo": "acme/widget"}}}))
    s = _src()
    s.add(51, PROPOSED, "priority:P1", body=BLK)
    assert promote.main(["promote.py", "migrate-confirmation", str(sd), "--apply"], source=s) == 0
    assert {GOAL, BLOCKED} <= set(s.store[51]["labels"])
    assert PARKED not in s.store[51]["labels"]
