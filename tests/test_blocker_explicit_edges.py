"""#1467: the two automatic blocker writers (the reconcile-tick repair and the board-pass promotion)
read text edges from explicit markers only ("blocked by", "depends on", "depends upon") plus ledger
hand-offs. The weak words ("after", "needs", "requires", "waiting on") never cause a write on a
third issue; they are reported as advisory on stderr."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_blocker_rank_repair as rep  # noqa: E402
import test_github_project as gp  # noqa: E402

WEAK = ["After #7 lands the claim holds.", "This needs #7 first.", "It requires #7 done.",
        "Currently waiting on #7."]
UNPARK = ("<!-- sigma:unpark-qa:start -->\nblocked by #7\n<!-- sigma:unpark-qa:end -->")


def _board_writes(run):
    return [c for c in run.calls if c[:2] in (["issue", "edit"], ["issue", "comment"])
            and len(c) > 2 and c[2] == "7"]


def _repair(body, capsys):
    """#7 sits OUTSIDE the goal corpus carrying the legacy confirmation label (read by REST view)."""
    run = rep._runner([rep._issue(9, [rep.GOAL, "priority:P0"], body=body)],
                      views={"7": rep._issue(7, ["priority:P3", "sdlc:needs-confirmation"])})
    res = rep._src(run).repair_blocker_ranks()
    return run, res, capsys.readouterr().err


def _board(body, mode="always"):
    issues = [gp._gissue(9, "priority:P0", body=body), gp._gissue(7, "priority:P3")]
    run = gp._promo_world(issues)
    gp._synced(gp._promo_src(run, mode=mode)).mark_in_progress("9")
    return run


@pytest.mark.parametrize("body", WEAK + [UNPARK])
def test_reconcile_repair_writes_nothing_for_weak_or_unpark_qa_text(body, capsys):
    run, res, err = _repair(body, capsys)
    assert _board_writes(run) == [] and res["raised"] == [] and res["stripped"] == []
    if body in WEAK:
        assert "ignored" in err and "Blocked by: #N" in err


@pytest.mark.parametrize("body", WEAK + [UNPARK])
def test_board_pass_writes_nothing_for_weak_or_unpark_qa_text(body):
    assert _board_writes(_board(body)) == []


def test_reconcile_repair_still_strips_the_confirmation_label_for_blocked_by(capsys):
    run, res, _ = _repair("Blocked by: #7", capsys)
    assert res["stripped"] == ["7"]


def test_reconcile_repair_still_raises_for_blocked_by(capsys):
    run = rep._runner(rep._p0_blocked_by_p3())
    assert rep._src(run).repair_blocker_ranks()["raised"] == ["7"]
    assert rep._raised(run, 7, "priority:P0")


def test_board_pass_still_raises_for_blocked_by_and_depends_on():
    for body in ("Blocked by: #7", "depends on #7"):
        run = _board(body)
        assert any(c[:3] == ["issue", "edit", "7"] and "priority:P0" in c for c in run.calls), body


def test_ledger_handoff_channel_survives_in_the_repair(tmp_path):
    ledger = rep._mod("ledger")
    base = tmp_path / ".sdlc"
    ledger.append(str(base), {"ledger": {"enabled": True, "actor": "t"}}, "handoff", "9", issue="7")
    run = rep._runner([rep._issue(9, [rep.GOAL, "priority:P0"], body="after #7 lands"),
                       rep._issue(7, [rep.GOAL, "priority:P3"])])
    res = rep._src(run, sdlc_dir=str(base)).repair_blocker_ranks()
    assert res["raised"] == ["7"]
