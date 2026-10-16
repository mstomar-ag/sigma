"""#1531: a goal claimed by a live session but never started is handed back by the next pick in
any session once held past `session_release.hold_minutes`; next-batch never keeps more claims than
its session's free slots. Fake gh transport; no network."""
import os, pathlib, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_session_unstarted_release as h   # reuse its fixtures (same fake gh transport)

OTHER = os.getppid()          # a second live pid: a different "session" than this process


def _claimed_ago(lp, base, goal, minutes):
    lp.actionlog.append(base, goal, "claimed", "loop", now=time.time() - minutes * 60)


def _setup(d, extra=None):
    lp = h._loop(); base = h._base(d, extra=extra); calls = []; h._wire(lp, base, calls)
    return lp, base, calls


def _held(lp, base, pid, goals, minutes):
    h._register(lp, base, pid, goals)
    for g in goals:
        _claimed_ago(lp, base, g, minutes)


def test_goal_held_past_the_hold_by_a_live_session_is_released_by_another_sessions_pick():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        lp, base, calls = _setup(d)
        _held(lp, base, OTHER, ["41"], 45)
        released = lp._release_held_unstarted(base, lp.state.load_config(base))
        assert released == frozenset({"41"})
        assert "claimed but not started" in " ".join(h._comments(calls))
        assert len(h._released_log(lp, base, "41")) == 1


def test_started_goals_are_never_released_by_the_hold():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        lp, base, calls = _setup(d)
        _held(lp, base, OTHER, ["51", "52", "53"], 600)
        h._mark_started(lp, base, "51")
        assert lp.agent_start(base, "52", OTHER, lp.state.load_config(base))
        released = lp._release_held_unstarted(base, lp.state.load_config(base))
        assert released == frozenset({"53"})
        assert h._released_log(lp, base, "51") == [] and h._released_log(lp, base, "52") == []


def test_a_goal_inside_the_hold_is_kept():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        lp, base, calls = _setup(d)
        _held(lp, base, OTHER, ["61"], 5)
        assert lp._release_held_unstarted(base, lp.state.load_config(base)) == frozenset()
        assert h._comments(calls) == []


def test_hold_disabled_releases_nothing_and_hold_is_configurable():
    import tempfile
    for off in (0, False):
        with tempfile.TemporaryDirectory() as d:
            lp, base, calls = _setup(d, {"session_release": {"hold_minutes": off}})
            _held(lp, base, OTHER, ["71"], 600)
            assert lp._release_held_unstarted(base, lp.state.load_config(base)) == frozenset()
            assert calls == [] and h._released_log(lp, base, "71") == []
    with tempfile.TemporaryDirectory() as d:
        lp, base, calls = _setup(d, {"session_release": {"hold_minutes": 2}})
        _held(lp, base, OTHER, ["72"], 5)
        assert lp._release_held_unstarted(base, lp.state.load_config(base)) == frozenset({"72"})
    assert lp.claim_hold_seconds({}) == 30 * 60
    assert lp.claim_hold_seconds({"session_release": {"hold_minutes": "x"}}) == 30 * 60


def test_a_goal_with_no_claim_time_is_kept():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        lp, base, calls = _setup(d)
        h._register(lp, base, OTHER, ["81"])           # registered, but no claimed entry on record
        assert lp._release_held_unstarted(base, lp.state.load_config(base)) == frozenset()


def test_next_batch_keeps_no_more_claims_than_free_slots():
    import tempfile
    cfg = {"parallel": {"goals": {"enabled": True, "max_concurrent": 3}}, "work": {"enabled": True}}
    with tempfile.TemporaryDirectory() as d:
        lp, base, calls = _setup(d, cfg)
        h._register(lp, base, os.getpid(), ["91", "92"])      # this session already holds 2 of 3
        _claimed_ago(lp, base, "91", 1); _claimed_ago(lp, base, "92", 1)
        picked = []

        def fake_next(sdlc_dir, source, config, **kw):
            g = str(100 + len(picked)); picked.append(g)
            lp._session_claim(base, kw["session_pid"], g)
            return ("goal", g)
        lp._next = fake_next
        out = lp.next_batch(base, lp.sources.get_source(base, {}), lp.state.load_config(base),
                            session_pid=os.getpid())
        assert [k for k, _ in out].count("goal") == 1 and len(picked) == 1


def test_next_batch_fresh_session_shape_is_unchanged():
    import tempfile
    cfg = {"parallel": {"goals": {"enabled": True, "max_concurrent": 3}}, "work": {"enabled": True}}
    with tempfile.TemporaryDirectory() as d:
        lp, base, calls = _setup(d, cfg)
        n = []

        def fake_next(sdlc_dir, source, config, **kw):
            n.append(1); return ("goal", str(200 + len(n)))
        lp._next = fake_next
        out = lp.next_batch(base, lp.sources.get_source(base, {}), lp.state.load_config(base),
                            session_pid=os.getpid())
        assert len(out) == 3


def _batch(extra, held_goals, fake_next_goals, pid=None):
    import tempfile
    pid = pid or os.getpid()
    with tempfile.TemporaryDirectory() as d:
        lp, base, calls = _setup(d, extra)
        h._register(lp, base, pid, held_goals)
        for g in held_goals:
            _claimed_ago(lp, base, g, 1)
        picked = []

        def fake_next(sdlc_dir, source, config, **kw):
            if len(picked) >= len(fake_next_goals):
                return ("DONE", None)
            g = fake_next_goals[len(picked)]; picked.append(g)
            return ("goal", g)
        lp._next = fake_next
        rel = []
        lp._release_unstarted = lambda sd, goals, why: rel.append(list(goals)) or True
        out = lp.next_batch(base, lp.sources.get_source(base, {}), lp.state.load_config(base),
                            session_pid=pid)
        return out, rel


_PAR = {"parallel": {"goals": {"enabled": True, "max_concurrent": 3}}, "work": {"enabled": True}}


def test_hold_off_leaves_next_batch_unchanged_even_with_held_goals():
    for off in (0, False):
        cfg = dict(_PAR, session_release={"hold_minutes": off})
        out, rel = _batch(cfg, ["91", "92"], ["a", "b", "c", "d"])
        assert out == [("goal", "a"), ("goal", "b"), ("goal", "c")] and rel == []


def test_ceilings_case_does_not_subtract_held_goals_twice():
    cfg = {"parallel": {"goals": {"enabled": True, "max_concurrent": 5}}, "work": {"enabled": True},
           "budget": {"max_iterations": 4}}
    # ceiling 4, 2 admitted (in flight) -> 2 left; cap free 5-2=3; picks = min = 2, NOT 2-2 = 0
    out, rel = _batch(cfg, ["91", "92"], ["a", "b", "c", "d"])
    assert out == [("goal", "a"), ("goal", "b")] and rel == []


def test_zero_free_slots_claims_nothing_on_any_source():
    held = ["91", "92", "93"]
    for src in ({}, {"discovery": {"source": "github"}}):
        out, rel = _batch(dict(_PAR, **src), held, ["a", "b"])
        assert out == [("FULL", "no free slots, 3 in flight")] and rel == []   # never calls _next


def test_zero_free_slots_cli_prints_one_terminal_line():
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        import importlib.util as u
        lp, base, _ = _setup(__import__("tempfile").mkdtemp(), _PAR)
        lp._print_pick("FULL", "no free slots, 3 in flight")
    assert buf.getvalue().strip() == "FULL"
