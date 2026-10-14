"""#1402: the board back-fill stops after a refused `gh project` call. The REST rate-limit counter can read
thousands remaining while the project API refuses (it reports a rate-limited owner lookup as
"unknown owner type"), so the refusal itself is the signal. Fake gh; no network."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_github_project as tgp
import test_board_backfill as tbb

REFUSAL = "gh project item-add failed: unknown owner type"


def _gated(refuse):
    """Wrap the backfill world; project calls raise while `refuse['on']` (or only item-add if 'verb')."""
    inner = tbb._world()
    projects = []

    def run(a):
        if a and a[0] == "project":
            projects.append(list(a))
            if refuse["on"] and (refuse.get("verb") is None or a[1] == refuse["verb"]):
                raise RuntimeError(REFUSAL)
        return inner(a)
    run.calls, run.state, run.rate_reads, run.projects = inner.calls, inner.state, inner.rate_reads, projects
    return run


def _src(run, **project):
    s = tbb._src(run, **project)
    s._RETRY_BASE = 0
    return s


def test_refusal_with_counter_high_skips_backfill():
    refuse = {"on": True}
    run = _gated(refuse)
    s = _src(run)
    try:
        s._run(["project", "list"])        # a refused project call, e.g. during a card write
    except Exception:
        pass
    assert s._project_refusals == 1
    refuse["on"] = False
    before = len(run.projects)
    assert s.backfill_board() == 0         # counter still reads 5000, refusal wins
    assert len(run.projects) == before


def test_no_refusal_is_unchanged():
    run = _gated({"on": False})
    s = _src(run, backfill_cap=3)
    assert s.backfill_board() == 3
    assert s._project_refusals == 0


def test_success_resets_the_count():
    refuse = {"on": True}
    run = _gated(refuse)
    s = _src(run)
    try:
        s._run(["project", "list"])
    except Exception:
        pass
    assert s._project_refusals == 1
    refuse["on"] = False
    s._run(["project", "list"])
    assert s._project_refusals == 0
    assert s.backfill_board() > 0


def test_refusal_inside_sync_backlog_makes_no_further_card_write():
    refuse = {"on": False, "verb": "item-add"}
    run = _gated(refuse)
    s = _src(run)
    refuse["on"] = True
    try:
        s.backfill_board()
    except Exception:
        pass
    adds = [c for c in run.projects if c[1] == "item-add"]
    assert 1 <= len(adds) <= s._PROJECT_RETRIES      # one call's retries, then the tick aborts


def test_invalid_config_falls_back_to_default():
    for bad in (True, 0, -3, "x"):
        refuse = {"on": True}
        run = _gated(refuse)
        s = _src(run, backfill_max_refusals=bad)
        try:
            s._run(["project", "list"])
        except Exception:
            pass
        refuse["on"] = False
        n = len(run.projects)
        assert s.backfill_board() == 0 and len(run.projects) == n, bad


def test_higher_threshold_allows_one_refusal():
    refuse = {"on": True}
    run = _gated(refuse)
    s = _src(run, backfill_max_refusals=2)
    try:
        s._run(["project", "list"])
    except Exception:
        pass
    assert s._project_refusals == 1
    refuse["on"] = False
    assert s.backfill_board() > 0
    s._project_refusals = 2
    assert s.backfill_board() == 0
