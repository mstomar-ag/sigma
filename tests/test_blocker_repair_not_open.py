"""#1475: blocker repair never writes to a merged PR, a closed item or a pull request, builds edges only
from explicit phrases, and its dry pass writes nothing (cache included)."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_blocker_rank_repair as base  # noqa: E402

GOAL = base.GOAL


def _fixture():
    issues = [
        base._issue(20, [GOAL, "priority:P3"], body="After #11 the change landed.\nBlocked by: #12"),
        base._issue(21, [GOAL, "priority:P3"], body="Blocked by: #13"),
        base._issue(13, []),
    ]
    views = {"11": base._issue(11, [], state="merged"), "12": base._issue(12, [], state="closed"),
             "13": base._issue(13, [])}
    return issues, views


def _touched(run, n):
    return [c for c in run.calls if c[:3] in (["issue", "edit", str(n)], ["issue", "comment", str(n)])]


def test_merged_pr_and_closed_issue_get_no_write_and_only_the_open_blocker_is_raised(tmp_path):
    issues, views = _fixture()
    run = base._runner(issues, views=views)
    res = base._src(run, sdlc_dir=str(tmp_path)).repair_blocker_ranks()
    assert res["raised"] == ["13"] and res["violations"] == 1
    assert _touched(run, 11) == [] and _touched(run, 12) == []


def test_prose_after_makes_no_edge_but_blocked_by_does(tmp_path):
    src = base._src(base._runner([]), sdlc_dir=str(tmp_path))
    corpus = {"20": base._issue(20, [GOAL], body="After #11 it landed. Needs #14. Blocked by #12")}
    edges, _x, refs = src._repair_edges(corpus, {})
    assert edges == [("12", "20")] and refs == {"12"}


def test_a_pull_request_is_never_a_raise_target():
    issues = [base._issue(20, [GOAL, "priority:P0"], body="blocked by #15")]
    views = {"15": base._issue(15, [])}
    run = base._runner(issues, views=views)
    src = base._src(run)
    orig = src._read_issue
    src._read_issue = lambda ref, fields, **k: dict(orig(ref, fields, **k), isPullRequest=True)
    assert src.repair_blocker_ranks()["raised"] == []
    assert src.raise_blocker_priority("15", "20").startswith("skipped")
    assert _touched(run, 15) == []


def test_raise_blocker_priority_skips_merged_and_strip_leaves_it(tmp_path):
    views = {"11": base._issue(11, [GOAL, "sdlc:needs-confirmation"], state="merged"),
             "20": base._issue(20, [GOAL, "priority:P0"])}
    run = base._runner([], views=views)
    src = base._src(run, sdlc_dir=str(tmp_path))
    assert src.raise_blocker_priority("11", "20").startswith("skipped")
    assert src.strip_blocker_confirmation("11") == "unchanged"
    assert _touched(run, 11) == []


def test_promote_blockers_skips_merged_pr(tmp_path):
    issues = [base._issue(20, [GOAL, "priority:P0"], body="blocked by #11")]
    run = base._runner(issues, views={"11": base._issue(11, [], state="merged")})
    base._src(run, mode="always", sdlc_dir=str(tmp_path))._promote_blockers(issues)
    assert _touched(run, 11) == []


def test_dry_pass_leaves_the_closed_cache_unchanged_and_counts_match(tmp_path):
    issues, views = _fixture()
    cache = tmp_path / "state" / "blocker-closed.json"
    dry = base._src(base._runner(issues, views=views), sdlc_dir=str(tmp_path)).repair_blocker_ranks(apply=False)
    assert not cache.exists()
    real = base._src(base._runner(issues, views=views), sdlc_dir=str(tmp_path)).repair_blocker_ranks()
    assert dry["violations"] == 1 == len(real["raised"])
    assert set(json.loads(cache.read_text())) == {"12"}             # prose "after #11" was never read


def test_a_merged_blocker_is_remembered_in_the_closed_cache(tmp_path):
    issues = [base._issue(20, [GOAL, "priority:P0"], body="blocked by #11")]
    run = base._runner(issues, views={"11": base._issue(11, [], state="merged")})
    base._src(run, sdlc_dir=str(tmp_path)).repair_blocker_ranks()
    assert set(json.loads((tmp_path / "state" / "blocker-closed.json").read_text())) == {"11"}


def test_outside_blockers_beyond_the_cap_are_reported(tmp_path, capsys):
    issues = [base._issue(500, [GOAL, "priority:P0"], body="\n".join("blocked by #%d" % r for r in range(100, 112)))]
    views = {str(r): base._issue(r, [GOAL, "priority:P0"]) for r in range(100, 112)}
    res = base._src(base._runner(issues, views=views), sdlc_dir=str(tmp_path)).repair_blocker_ranks()
    assert res["unread"] == 2
    assert "more than 10 outside blockers" in capsys.readouterr().err
