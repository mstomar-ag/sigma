"""#1305: a verb about ONE issue touches only that issue's card; the full-board back-fill is a capped,
rate-limit-aware watcher step. Fake gh counts the project calls."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_github_project as tgp

N_GOALS = 12


def _world(remaining=4000, n=N_GOALS):
    issues = [{"number": i, "labels": [{"name": "sdlc:goal"}]} for i in range(1, n + 1)]
    inner = tgp.project_world(projects=[], issues=issues)
    seen = []

    def run(a):
        if a[:2] == ["api", "rate_limit"]:
            seen.append(1)
            if remaining is None:
                raise RuntimeError("no rate limit")
            return json.dumps({"resources": {"graphql": {"remaining": remaining, "limit": 5000}}})
        out = inner(a)
        if a[:2] == ["project", "item-edit"]:      # the real board keeps the Status it was given
            opt = tgp._arg(a, "--single-select-option-id")
            name = {"s_ready": "Ready", "s_backlog": "Backlog"}.get(opt, opt)
            for it in inner.state["items"]:
                if it["id"] == tgp._arg(a, "--id"):
                    it["status"] = name
        return out
    run.calls = inner.calls
    run.state = inner.state
    run.rate_reads = seen
    return run


def _adds(run):
    return [c for c in run.calls if c[:2] == ["project", "item-add"]]


def _src(run, **project):
    return tgp._mod("sources").GitHubSource(tgp._cfg(project={"enabled": True, **project}), run=run)


def test_single_issue_verb_cards_only_its_own_issue():
    run = _world()
    _src(run).mark_in_progress("5")
    assert [tgp._arg(c, "--url").rsplit("/", 1)[1] for c in _adds(run)] == ["5"]


def test_single_issue_verbs_read_no_rate_limit_and_edit_one_card():
    run = _world()
    s = _src(run)
    s.mark_in_progress("5")
    assert {e["item"] for e in tgp._edits(run)} == {"PVTI_5"}


def test_backfill_is_capped_per_call_and_continues():
    run = _world()
    s = _src(run, backfill_cap=5)
    assert s.backfill_board() == 5
    assert len(_adds(run)) == 5
    s2 = _src(run, backfill_cap=5)
    assert s2.backfill_board() == 5
    assert len(_adds(run)) == 10


def test_backfill_default_cap_is_twenty():
    run = _world(n=30)
    assert _src(run).backfill_board() == 20


def test_backfill_cap_zero_is_off():
    run = _world()
    assert _src(run, backfill_cap=0).backfill_board() == 0
    assert not [c for c in run.calls if c and c[0] == "project"]


def test_backfill_skips_when_graphql_is_low_or_unreadable():
    for remaining in (10, None):
        run = _world(remaining=remaining)
        assert _src(run).backfill_board() == 0
        assert not [c for c in run.calls if c and c[0] == "project"]


def test_backfill_with_board_off_makes_no_calls():
    run = _world()
    s = tgp._mod("sources").GitHubSource(tgp._cfg(project={"enabled": False}), run=run)
    assert s.backfill_board() == 0
    assert run.calls == [] and run.rate_reads == []
