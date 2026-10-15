"""#1468: with hard stops on, a hand-typed blocker on a third-party issue is reported, never raised or parked."""
import json
import sys

import pytest

import test_blocker_rank_repair as base

GOAL = "sdlc:goal"
HS_ON = {"rulebook": {"hard_stops": {"enabled": True}}}


def _src(run, hard_stops):
    cfg = base._cfg()
    if hard_stops:
        cfg.update(HS_ON)
    return base._mod("sources").GitHubSource(cfg, run=run)


def _issues(blocker_labels=(), blocker_body=""):
    return [base._issue(9, [GOAL, "priority:P0"], body="blocked by #7"),
            base._issue(7, list(blocker_labels), body=blocker_body)]


def _touching(run, n):
    return [c for c in run.calls if c[:3] in (["issue", "edit", str(n)], ["issue", "comment", str(n)])
            or any("issues/%s" % n in a and "-X" in c for a in c)]


def test_foreign_blocker_is_reported_not_raised_when_hard_stops_on(capsys):
    run = base._runner(_issues())
    res = _src(run, True).repair_blocker_ranks()
    err = capsys.readouterr().err
    assert res["raised"] == []
    assert "#7 not raised: not a Sigma issue" in err
    assert not base._raised(run, 7, "priority:P0")
    assert not any("sdlc:parked" in " ".join(c) for c in run.calls)
    assert not any("comment" in c or "comments" in " ".join(c) for c in run.calls
                   if any("7" in a for a in c) and ("-X" in c or c[:2] == ["issue", "comment"]))


def test_own_labelled_blocker_still_raised_with_one_comment():
    run = base._runner(_issues([GOAL, "priority:P3"]))
    res = _src(run, True).repair_blocker_ranks()
    assert res["raised"] == ["7"]
    assert base._raised(run, 7, "priority:P0")


def test_follow_up_labelled_blocker_counts_as_sigma_own():
    run = base._runner(_issues(["sdlc:followup"]))
    res = _src(run, True).repair_blocker_ranks()
    assert res["raised"] == ["7"]
    assert not any("sdlc:parked" in " ".join(c) for c in run.calls)


def test_hard_stops_off_foreign_blocker_raised_as_before(capsys):
    run = base._runner(_issues(["priority:P3"]))
    res = _src(run, False).repair_blocker_ranks()
    assert res["raised"] == ["7"]
    assert base._raised(run, 7, "priority:P0")
    assert "not a Sigma issue" not in capsys.readouterr().err


def test_note_returns_false_on_guard_refusal_and_none_otherwise():
    run = base._runner(_issues())
    assert _src(run, True).note(7, "x") is False
    run2 = base._runner(_issues([GOAL]))
    assert _src(run2, True).note(7, "x") is None
    run3 = base._runner(_issues())
    assert _src(run3, False).note(7, "x") is None


def test_is_sigma_issue_never_parks_and_is_closed_when_off():
    hsg = base._mod("hard_stop_guard")
    run = base._runner(_issues())
    assert hsg.is_sigma_issue(_src(run, True), 7) is False
    assert hsg.is_sigma_issue(_src(run, False), 7) is True
    assert not any("sdlc:parked" in " ".join(c) for c in run.calls)
