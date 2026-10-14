"""release on a never-claimed goal (#1394): when the state probe SUCCEEDED and the claim
(in-progress) label is not on the issue, there is nothing to release, so release() returns False
and writes nothing (no label call, no comment, no board move). A failed probe stays fail-open."""
import json

from test_sources import _mod, _recording_runner, _gh, _rest_429_then_view, _views


def _src(labels, state="OPEN"):
    run = _recording_runner({"rest": {"state": state, "labels": [{"name": n} for n in labels]}})
    gh = _mod("sources").GitHubSource({"discovery": {"source": "github"}}, run=run)
    return gh, run


def _writes(run):
    return [c for c in run.calls
            if c[0:2] == ["issue", "edit"] or (len(c) > 1 and c[1] in ("comment", "close"))
            or "project" in c[:1]]


def test_unclaimed_goal_release_is_a_silent_noop():
    gh, run = _src(["sdlc:goal"])
    assert gh.release("42", "never claimed") is False
    assert _writes(run) == []
    assert not any(c[0] == "api" and ("PATCH" in c or "POST" in c or "DELETE" in c) for c in run.calls)
    assert gh.release_warnings() == []


def test_unclaimed_goal_release_with_stale_resume_note_is_also_a_noop():
    gh, run = _src(["sdlc:goal"])
    assert gh.release("42", "stale", note="Released - stale resume") is False
    assert _writes(run) == []


def test_unclaimed_goal_makes_no_board_move():
    gh, run = _src(["sdlc:goal"])
    moves = []
    gh._set_board_status = lambda goal, col: moves.append((goal, col))
    assert gh.release("42", "x") is False
    assert moves == []


def test_claimed_goal_release_is_unchanged():
    gh, run = _src(["sdlc:goal", "sdlc:in-progress"])
    moves = []
    gh._set_board_status = lambda goal, col: moves.append((goal, col))
    assert gh.release("42", "dispatch never happened") is True
    assert any("--remove-label" in c and "sdlc:in-progress" in c for c in run.calls)
    assert any(len(c) > 1 and c[1] == "comment" for c in run.calls)
    assert len(moves) == 1


def test_failed_probe_stays_fail_open():
    def run(a):
        if a[0:2] == ["issue", "view"] or (a and a[0] == "api" and "GET" in a):
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return ""
    gh = _mod("sources").GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._set_board_status = lambda goal, col: None
    assert gh.release("42", "probe failed") is True
