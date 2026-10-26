import hashlib, json, os, pathlib, importlib.util, tempfile, subprocess, sys, time, shlex, re

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gqlfake as _gqlfake

import pytest
from journal_events import journal_events

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


@pytest.fixture(autouse=True)
def _legacy_loop_host_unless_test_sets_codex_thread(monkeypatch):
    # Most existing loop tests exercise the original PID-only Claude/CLI registry. Codex desktop
    # injects its thread markers into pytest too, so isolate that ambient host identity here.
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)


def _loop():
    spec = importlib.util.spec_from_file_location("loop", S / "loop.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _work():
    """#2527: `work.py`'s own `_blocked_by_a_live_foreign_agent`, for the one test that chains
    `agent_start`'s fix into work.py's independent downstream guard. Mirrors `_loop()`'s own
    loading shape; `tests/test_work.py` has its own `_load("work")` for the same module, kept
    separate rather than importing across test files."""
    spec = importlib.util.spec_from_file_location("work", S / "work.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _backlog(d, n, max_iter=10):
    base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True); (base / "state").mkdir()
    (base / "config.json").write_text(json.dumps({"budget": {"max_iterations": max_iter}}))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    (base / "state" / "review-queue.md").write_text("# Q\n")
    for i in range(1, n + 1):
        (base / "goals" / f"{i:04d}.md").write_text(f"---\nid: {i:04d}\nstatus: pending\n---\nx\n")
    return str(base)


def _with_action_log(base):
    """Patch an existing .sdlc's config.json to also turn `action_log` on, preserving everything
    else already there — the action-log regression tests below reuse this file's existing
    backlog/journal fixtures rather than building a parallel set (#463)."""
    p = pathlib.Path(base) / "config.json"
    cfg = json.loads(p.read_text())
    cfg["action_log"] = {"enabled": True}
    p.write_text(json.dumps(cfg))
    return cfg


def test_drains_backlog_all_done():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 3)
        res = _loop().run_loop(base, lambda g: ("done", ""))
        assert res["done"] == 3 and res["parked"] == 0 and res["stopped"] == "backlog-empty"


def test_parks_blocked_goal_and_continues():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 3)
        rg = lambda g: ("parked", "deploy gate") if g.endswith("0002.md") else ("done", "")
        res = _loop().run_loop(base, rg)
        assert res["done"] == 2 and res["parked"] == 1
        assert "0002.md" in (pathlib.Path(base) / "state" / "review-queue.md").read_text()


def test_halts_on_iteration_budget():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 10, max_iter=2)
        res = _loop().run_loop(base, lambda g: ("done", ""))
        assert res["done"] == 2 and res["stopped"] == "budget"


def test_resume_after_budget_processes_remaining():   # the I1 regression guard
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 5, max_iter=2); lp = _loop()
        r1 = lp.run_loop(base, lambda g: ("done", "")); assert r1["done"] == 2 and r1["stopped"] == "budget"
        r2 = lp.run_loop(base, lambda g: ("done", "")); assert r2["done"] == 2 and r2["stopped"] == "budget"
        r3 = lp.run_loop(base, lambda g: ("done", "")); assert r3["done"] == 1 and r3["stopped"] == "backlog-empty"


def test_drained_backlog_reports_empty_not_budget():   # M1 boundary
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2, max_iter=2)
        res = _loop().run_loop(base, lambda g: ("done", ""))
        assert res["done"] == 2 and res["stopped"] == "backlog-empty"


def _backlog_with_telemetry(d, n, max_iter=10):
    """#547: a `_backlog` whose config also turns the ledger + journal (events stream) on, so
    `run_loop`'s `run_stop` emission is actually written -- events are gated on BOTH being true."""
    base = _backlog(d, n, max_iter=max_iter)
    p = pathlib.Path(base) / "config.json"
    cfg = json.loads(p.read_text())
    cfg["ledger"] = {"enabled": True, "actor": "me"}
    cfg["journal"] = {"enabled": True}
    p.write_text(json.dumps(cfg))
    return base


def _run_stops(lp, base):
    return [e for e in journal_events(lp.ledger, base) if e["kind"] == "run_stop"]


def test_run_loop_emits_one_backlog_empty_run_stop_at_done(monkeypatch):   # #547 / #905
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 2)
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")   # #905: emission is gated on attribution
        lp = _loop()
        res = lp.run_loop(base, lambda g: ("done", ""))
        assert res["stopped"] == "backlog-empty"
        stops = _run_stops(lp, base)
        assert len(stops) == 1                          # exactly one run-level stop, not per-goal
        assert stops[0]["reason_class"] == "backlog-empty"
        assert stops[0]["goal"] == ""                   # empty goal -> a null goal downstream


def test_run_loop_emits_one_budget_run_stop_carrying_the_ceiling_reason(monkeypatch):   # #547 / #905
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 5, max_iter=2)
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        lp = _loop()
        res = lp.run_loop(base, lambda g: ("done", ""))
        assert res["stopped"] == "budget"
        stops = _run_stops(lp, base)
        assert len(stops) == 1
        assert stops[0]["reason_class"] == "budget"
        assert stops[0].get("why")                      # the tripped-ceiling reason, human context


def test_run_loop_writes_no_run_stop_when_telemetry_is_off(monkeypatch):   # #547: gated like every event
    """PR-review correction: without `monkeypatch.setenv(SIGMA_RUN_ID, ...)`, this test now
    short-circuits at #905's own NEW attribution gate before ever reaching the ORIGINAL #547
    journal gate it exists to prove -- passing vacuously for the wrong reason (an unattributed
    ambient test environment, not the journal being off). Setting the run id here isolates the two
    gates: attribution present, journal absent, so this genuinely re-proves journal-gating
    still holds on its own, independent of #905's new gate."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2)                            # no ledger/journal block at all
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        lp = _loop()
        lp.run_loop(base, lambda g: ("done", ""))
        assert _run_stops(lp, base) == []


def test_run_loop_writes_no_run_stop_when_unattributed(monkeypatch):   # #905: the scope decision
    """No SIGMA_RUN_ID (any interactive/direct session not wrapped in supervise.sh) -> no
    run_stop at all, on purpose. NOT recorded under a shared `None` key: a durable "at most one
    per identity" guard keyed on `None` would let the FIRST unattributed run ever claim that key
    and silently suppress every later unattributed run's real stop, forever."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 2)
        monkeypatch.delenv("SIGMA_RUN_ID", raising=False)
        lp = _loop()
        lp.run_loop(base, lambda g: ("done", ""))
        assert _run_stops(lp, base) == []


def test_run_stop_records_every_distinct_terminal_event_across_relaunches_under_one_run_id(monkeypatch):
    """#905 plan-review Defect 1, regression-tested directly: mirrors
    test_resume_after_budget_processes_remaining's own 3-call shape (budget, budget, backlog-empty
    from one 5-goal backlog with max_iter=2), but under ONE shared SIGMA_RUN_ID throughout --
    exactly supervise.sh's real behaviour, where a budget stop triggers a relaunch (a fresh
    `start`/`start-run`, hence a fresh `run_started_at`) under the SAME worker id. A dedupe keyed
    on the bare run id alone would record only the FIRST of these three terminal events; the fix
    keys on (run_id, run_started_at) so all three -- each a genuinely distinct stop -- land."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 5, max_iter=2)
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        lp = _loop()
        r1 = lp.run_loop(base, lambda g: ("done", "")); assert r1["stopped"] == "budget"
        r2 = lp.run_loop(base, lambda g: ("done", "")); assert r2["stopped"] == "budget"
        r3 = lp.run_loop(base, lambda g: ("done", "")); assert r3["stopped"] == "backlog-empty"
        stops = _run_stops(lp, base)
        assert [s["reason_class"] for s in stops] == ["budget", "budget", "backlog-empty"]


def test_cli_next_writes_one_run_stop_on_a_drained_backlog(monkeypatch):
    """#905: the ACTUAL production chokepoint -- the CLI `next` verb, which emitted nothing at all
    before this fix (the issue's own premise: run_loop's emitter had no production caller)."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 0)               # already drained
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        lp = _loop()
        lp.state.start_run(base)                           # models the skill's own `loop.py start`
        assert lp.main(["loop.py", "next", base]) == 0
        stops = _run_stops(lp, base)
        assert len(stops) == 1 and stops[0]["reason_class"] == "backlog-empty"


def test_cli_next_dedupes_repeated_polling_within_one_unchanged_drain(monkeypatch):
    """The ORIGINAL concern in #905's own issue body: "the polled 'next' verb... would emit a
    duplicate backlog-empty stop on every idle poll". Two `next` calls with no intervening
    `start`/`start-run` (i.e. the SAME run_started_at) must write only one row."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 0)
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        lp = _loop()
        lp.state.start_run(base)
        assert lp.main(["loop.py", "next", base]) == 0
        assert lp.main(["loop.py", "next", base]) == 0     # idle re-poll, same drain
        assert len(_run_stops(lp, base)) == 1


def test_cli_next_records_a_new_stop_after_a_simulated_relaunch(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 0)
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        lp = _loop()
        lp.state.start_run(base)
        assert lp.main(["loop.py", "next", base]) == 0
        lp.state.start_run(base)                            # a relaunch: fresh run_started_at, same id
        assert lp.main(["loop.py", "next", base]) == 0
        assert len(_run_stops(lp, base)) == 2


def test_cli_next_records_independently_under_a_different_run_id(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 0)
        lp = _loop()
        lp.state.start_run(base)
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        assert lp.main(["loop.py", "next", base]) == 0
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-B")
        assert lp.main(["loop.py", "next", base]) == 0      # same run_started_at, different worker
        assert len(_run_stops(lp, base)) == 2


def test_cli_next_skips_the_emit_when_the_source_reports_a_degraded_read(monkeypatch):
    """#905 plan-review Defect 2: a masked read failure (GithubSource.read_degraded() True) must
    not write a false reason_class='backlog-empty' row -- an honest absence instead."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 0)
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        lp = _loop()
        lp.state.start_run(base)

        class DegradedSource:
            def next_pending(self, skip=()): return None
            def read_degraded(self): return True

        lp.sources.get_source = lambda sdlc_dir, config: DegradedSource()
        assert lp.main(["loop.py", "next", base]) == 0
        assert _run_stops(lp, base) == []


def test_cli_next_emits_normally_when_the_source_has_no_read_degraded_method(monkeypatch):
    """LocalSource (and any fake source in these tests) has no `read_degraded` at all -- `_next()`
    must default that to "not degraded" via getattr, not crash or silently suppress emission for
    every source that predates #905."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 0)
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        lp = _loop()
        lp.state.start_run(base)
        assert lp.main(["loop.py", "next", base]) == 0
        stops = _run_stops(lp, base)
        assert len(stops) == 1 and stops[0]["reason_class"] == "backlog-empty"


def test_next_returns_done_none_when_backlog_genuinely_empty():
    """#1084: the explicit, first-class version of the genuinely-empty contract -- the byte-
    identical-to-today shape when nothing degraded the read."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0)
        lp = _loop()
        lp.state.start_run(base)
        src = lp.sources.get_source(base, lp.state.load_config(base))
        kind, payload = lp._next(base, src, lp.state.load_config(base))
        assert (kind, payload) == ("DONE", None)


def test_next_returns_done_with_reason_when_the_read_is_degraded():
    """#1084: a `read_degraded()`-True source must widen `_next()`'s DONE payload to a non-None
    reason string, rather than reporting the same `("DONE", None)` a genuinely-empty backlog
    would -- this is the tri-state contract's central claim."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0)
        lp = _loop()
        lp.state.start_run(base)

        class DegradedSource:
            def next_pending(self, skip=()): return None
            def read_degraded(self): return True

        kind, payload = lp._next(base, DegradedSource(), lp.state.load_config(base))
        assert kind == "DONE"
        assert isinstance(payload, str)
        assert "degraded" in payload


def test_run_loop_drives_any_injected_source():    # loop.py is source-agnostic (local OR github)
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0, max_iter=10)         # no goal files; the fake source supplies the backlog
        lp = _loop()

        class Fake:
            def __init__(s): s.q = ["a", "b", "c"]; s.done = []
            def next_pending(s, skip=()): return next((g for g in s.q if g not in skip), None)
            def mark_in_progress(s, g): pass
            def complete(s, g): s.done.append(g); s.q.pop(0)
            def park(s, g, r): s.q.pop(0)

        fake = Fake()
        lp.sources.get_source = lambda sdlc_dir, config: fake     # inject via the factory seam
        res = lp.run_loop(base, lambda g: ("done", ""))
        assert res["done"] == 3 and res["stopped"] == "backlog-empty" and fake.done == ["a", "b", "c"]


def test_run_loop_builds_source_once():    # one source per run, not per _next/_record (labels ensured once)
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0, max_iter=10)
        lp = _loop()
        calls = {"n": 0}

        class Fake:
            def __init__(s): s.q = ["a", "b", "c"]
            def next_pending(s, skip=()): return next((g for g in s.q if g not in skip), None)
            def mark_in_progress(s, g): pass
            def complete(s, g): s.q.pop(0)
            def park(s, g, r): s.q.pop(0)

        fake = Fake()
        def gs(sdlc_dir, config): calls["n"] += 1; return fake
        lp.sources.get_source = gs
        lp.run_loop(base, lambda g: ("done", ""))
        assert calls["n"] == 1                 # built once for the whole run


def test_note_verb_local_appends_journey():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()
        rc = lp.main(["loop.py", "note", base, base + "/goals/0001.md", "research: 3 files"])
        jlog = pathlib.Path(base) / "journey" / "0001.md"
        assert rc == 0 and jlog.exists() and "research: 3 files" in jlog.read_text()


def test_note_verb_prints_ok_on_a_landed_write(capsys):
    """#1986: the successful half of the same OK/FAILED convention `mark-designed` already uses --
    a landed note now says so on stdout too, not just via a silent 0 that reads identically to a
    swallowed failure."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()
        rc = lp.main(["loop.py", "note", base, base + "/goals/0001.md", "research: 3 files"])
        assert rc == 0 and capsys.readouterr().out.strip() == "OK"


def test_note_verb_is_fail_open_but_reports_the_failure_loudly(capsys):
    """#1986: a failed `note` must never crash the run (that half of "fail-open" is unchanged --
    the CLI still catches the exception itself, it does not propagate), but it must no longer
    report success. The OLD shape here returned 0 unconditionally and printed one easy-to-miss
    stderr line; for `sigma-goal-review`'s REJECT verdict this note is its ENTIRE output (§7g), so a
    silent 0 left a rejection with zero trace anywhere. Now: `FAILED` on stdout (the same word
    `mark-designed` already prints on ITS failure) plus a non-zero exit, so a caller checking either
    channel sees it."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0, max_iter=5); lp = _loop()

        class Boom:
            def note(self, g, t): raise RuntimeError("no gh")

        lp.sources.get_source = lambda sdlc_dir, config: Boom()
        rc = lp.main(["loop.py", "note", base, "5", "hello"])
        out = capsys.readouterr()
        assert rc != 0, "a failed note must no longer report success via the exit code"
        assert "FAILED" in out.out
        assert "no gh" in out.err     # the underlying reason is still on stderr, for a human reading the log


def test_note_verb_failure_wording_names_what_is_retried(capsys):
    """#895 slice 8: the retry covers only a server 5xx / ambiguous transport failure, so the stderr line must
    not claim "after retries" for a 404 that was never retried."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0, max_iter=5); lp = _loop()

        class Boom:
            def note(self, g, t): raise RuntimeError("HTTP 404")

        lp.sources.get_source = lambda sdlc_dir, config: Boom()
        assert lp.main(["loop.py", "note", base, "5", "hello"]) == 1
        err = capsys.readouterr().err
        assert "retries cover only a server 5xx or ambiguous transport failure" in err
        assert "after retries" not in err


def test_cli_qc_verb_is_a_safe_noop_for_local():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5)
        g = base + "/goals/0001.md"
        r = subprocess.run([sys.executable, str(S / "loop.py"), "qc", base, g], capture_output=True, text=True)
        assert r.returncode == 0 and "status: pending" in open(g).read()   # board-only op; local untouched


def test_cli_mark_designed_routes_to_the_source_and_prints_ok(capsys):
    """#1826: goal-review's own write-back verb. Routes straight to source.mark_designed and
    reports its landed bool as OK/FAILED, mirroring qc's plain source-delegation shape above."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()
        calls = []

        class Stub:
            def mark_designed(self, g):
                calls.append(g); return True

        lp.sources.get_source = lambda sdlc_dir, config: Stub()
        rc = lp.main(["loop.py", "mark-designed", base, "1826"])
        assert rc == 0
        assert calls == ["1826"]
        assert capsys.readouterr().out.strip() == "OK"


def test_cli_mark_designed_prints_failed_when_the_swap_does_not_land(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()

        class Stub:
            def mark_designed(self, g):
                return False

        lp.sources.get_source = lambda sdlc_dir, config: Stub()
        rc = lp.main(["loop.py", "mark-designed", base, "1826"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "FAILED"


def test_cli_mark_designed_is_unsupported_on_a_source_with_no_story_epic_tickets(capsys):
    """LocalSource (and any source without the concept) must never crash on this verb -- it has no
    story/epic tickets to overlay, and the CLI says so plainly instead of raising AttributeError."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()

        class NoDesignSupport:
            pass

        lp.sources.get_source = lambda sdlc_dir, config: NoDesignSupport()
        rc = lp.main(["loop.py", "mark-designed", base, "1826"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "UNSUPPORTED"


def test_cli_release_calls_source_release_with_reason():
    """#841: the CLI wiring for the new `release` verb — the 4th positional arg (when present and
    not flag-shaped) is threaded through to `source.release(goal, reason)` unchanged, mirroring
    `record`'s own optional-trailing-reason parsing."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()
        calls = []

        class Stub:
            def release(self, g, reason):
                calls.append((g, reason))

        lp.sources.get_source = lambda sdlc_dir, config: Stub()
        rc = lp.main(["loop.py", "release", base, "0001.md", "claimed but not started"])
        assert rc == 0
        assert calls == [("0001.md", "claimed but not started")]


def test_cli_release_reason_defaults_to_empty_string_when_omitted():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()
        calls = []

        class Stub:
            def release(self, g, reason):
                calls.append((g, reason))

        lp.sources.get_source = lambda sdlc_dir, config: Stub()
        rc = lp.main(["loop.py", "release", base, "0001.md"])
        assert rc == 0
        assert calls == [("0001.md", "")]


def test_cli_release_local_end_to_end_sets_status_back_to_pending():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()
        g = base + "/goals/0001.md"
        claimed = lp.main(["loop.py", "next", base])          # claims it -> status: in_progress
        assert claimed == 0 and "status: in_progress" in pathlib.Path(g).read_text()
        rc = lp.main(["loop.py", "release", base, g, "abandoned this run"])
        assert rc == 0
        assert "status: pending" in pathlib.Path(g).read_text()


def test_cli_release_on_an_already_done_goal_prints_a_clear_message_and_leaves_it_untouched(capsys):
    """PR #1107 review, Finding 3: a no-op release (goal already done/parked/failed) is not an
    ERROR -- exit 0 either way (unlike `record done`'s REFUSED/exit-4 convention), so a future
    automated staleness sweep over many goals is never aborted by one already-finished goal. But it
    must not be silently invisible either: a clear, non-fatal stderr message names what happened."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()
        g = base + "/goals/0001.md"
        assert lp.main(["loop.py", "next", base]) == 0
        assert lp.main(["loop.py", "record", base, g, "done"]) == 0
        assert "status: done" in pathlib.Path(g).read_text()

        rc = lp.main(["loop.py", "release", base, g, "stale cleanup sweep"])

        assert rc == 0                                          # a no-op is not a fatal error
        assert "status: done" in pathlib.Path(g).read_text()     # goal itself untouched
        err = capsys.readouterr().err
        assert "already done/parked/failed" in err and g in err
        assert "never claimed" in err                           # #1413: the message also covers an unclaimed goal


def test_cli_release_does_not_consume_the_iteration_budget():
    """Mirrors test_cli_start_next_record_and_budget's own end-to-end shape. A goal that was
    claimed and then released must not count against budget.max_iterations — it was never
    actually worked, so a fresh `next` right after the release must still return a real goal
    (not BUDGET), and the run's one real iteration is still available for whichever goal
    actually gets recorded done.

    POST-REVIEW FIX (PR #1107, Finding 4): `_backlog()`'s config carries no `ledger` key, so
    `ledger.enabled(config)` was False and the claim-lease gate in `_next()` never engaged — the
    released goal was "pickable again" here purely because LocalSource's file-status-based
    discovery re-offers any `status: pending` goal regardless of the ledger, which release's
    status flip alone already guarantees. Turning the ledger on (explicit `actor`, so this never
    shells out to the real `gh`) is necessary but NOT sufficient on its own: every `next`/`release`
    call here is a SEPARATE subprocess, so `claim_belongs_to_me`'s dead-writer-of-my-own-actor
    fallback (#374) would reclaim the goal for the second `next` regardless of whether release ever
    touched the ledger at all (confirmed: this assertion alone still passed with `_held()`
    temporarily reverted to not recognize the `release` kind). The line that actually closes the
    gap is the direct `open_claims` check below, right after `release` and before the second
    `next` — it reads the shared ledger view the same way `_next()` itself does, with no pid-
    liveness fallback to mask a broken `_held()`."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2, max_iter=1)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        cfg["ledger"] = {"enabled": True, "actor": "release-budget-test"}
        (pathlib.Path(base) / "config.json").write_text(json.dumps(cfg))
        lp = _loop()
        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True, text=True)
        run("start", base)
        g = run("next", base).stdout.strip(); assert g.endswith("0001.md")
        assert lp.ledger.open_claims(lp.ledger.read_all(base)) == {g: "release-budget-test"}

        run("release", base, g, "claimed but never started")
        assert lp.ledger.open_claims(lp.ledger.read_all(base)) == {}, \
            "release must actually end the ledger lease, not just flip the goal's file status"

        g2 = run("next", base).stdout.strip()
        assert g2 == g, "the released goal must be pickable again, not skipped or budget-blocked"
        run("record", base, g2, "done")
        assert run("next", base).stdout.strip() == "BUDGET"   # the one real iteration is now spent


def test_cli_start_next_record_and_budget():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2, max_iter=1)
        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True, text=True)
        run("start", base)
        g = run("next", base).stdout.strip(); assert g.endswith("0001.md")
        run("record", base, g, "done")
        assert run("next", base).stdout.strip() == "BUDGET"            # per-run budget=1 spent


def test_record_done_prunes_only_the_direct_review_copy_and_keeps_evidence_text():
    """The documented ``loop.py record <dir> <goal> done`` gesture must release copied review
    worktrees without treating the human evidence beside them as disposable.  Removing the terminal
    cleanup call, or widening its direct-child selection to a recursive/glob delete, makes one of
    these observable filesystem assertions fail."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5)
        evidence = pathlib.Path(base) / "evidence" / "0001"
        copied = evidence / "rv9" / "wt"
        copied.mkdir(parents=True)
        (copied / "checkout.py").write_text("copied review tree\n")
        (evidence / "rv9" / "notes.md").write_text("human review evidence\n")
        (evidence / "rv9" / "wt-extra").mkdir()
        nested = evidence / "rv9" / "nested" / "wt"
        nested.mkdir(parents=True)

        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a],
                                        capture_output=True, text=True)
        run("start", base)
        goal = run("next", base).stdout.strip()
        result = run("record", base, goal, "done")

        assert result.returncode == 0, result.stderr
        assert not copied.exists()
        assert (evidence / "rv9" / "notes.md").read_text() == "human review evidence\n"
        assert (evidence / "rv9" / "wt-extra").is_dir()
        assert nested.is_dir()


def test_record_failed_prunes_review_copy_without_releasing_the_failed_worktree():
    """A failed terminal record reaches the same cleanup, but does not route through
    ``work.finish``: losing a failed goal's checkout would destroy the work needed to repair it."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5)
        config_path = pathlib.Path(base) / "config.json"
        config = json.loads(config_path.read_text())
        config["work"] = {"enabled": True}
        config_path.write_text(json.dumps(config))
        evidence = pathlib.Path(base) / "evidence" / "0001"
        copied = evidence / "rv2" / "wt"
        copied.mkdir(parents=True)
        (copied / "snapshot.txt").write_text("copy\n")
        failed_worktree = pathlib.Path(base) / "work" / "0001"
        failed_worktree.mkdir(parents=True)
        (failed_worktree / "uncommitted.txt").write_text("keep me\n")

        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a],
                                        capture_output=True, text=True)
        run("start", base)
        goal = run("next", base).stdout.strip()
        record = pathlib.Path(base) / "state" / "work" / "0001.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"worktree": str(failed_worktree), "branch": "sdlc/0001",
                                      "base": "main", "remote": "origin", "pr": ""}))
        result = run("record", base, goal, "failed", "needs repair")

        assert result.returncode == 0, result.stderr
        assert not copied.exists()
        assert (failed_worktree / "uncommitted.txt").read_text() == "keep me\n"
        assert record.exists(), "failed terminal records must not call work.finish"


def test_two_real_picker_processes_the_second_is_refused_a_goal_a_live_worker_still_holds():
    """#1197's own acceptance bar, driven end-to-end: two REAL, separate `loop.py next` picker
    subprocesses against ONE ledger-enabled `.sdlc`, exactly as two `/sigma-loop` sessions minutes
    apart, same account, same machine would produce -- not a same-process unit check of
    `claim_belongs_to_me`. The first subprocess wins goal 0001 and exits normally (no crash,
    business as usual); by the time this test asserts, its own pid is already provably dead --
    which IS the bug's premise, not a contrived setup. This test process then registers a genuine
    long-lived worker for that goal -- `agent_start`, exactly as SKILL.md step 3a's
    `agent-start --pid $PPID` does after a real dispatch, with an ordinary config carrying no
    `agent_watch` key at all (#1197's own acceptance criterion: registered by default, no
    hand-passed flag) -- before the second picker subprocess runs. The second picker must be
    refused 0001 and take 0002 instead."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2, max_iter=5)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        cfg["ledger"] = {"enabled": True, "actor": "two-picker-test"}
        (pathlib.Path(base) / "config.json").write_text(json.dumps(cfg))
        lp = _loop()
        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True, text=True)
        run("start", base)

        g1 = run("next", base).stdout.strip()              # picker subprocess #1 -- claims, exits
        assert g1.endswith("0001.md"), g1

        # picker #1's own pid is now provably dead -- exactly the bug's premise. Register the real,
        # long-lived worker for the goal it claimed, with no `agent_watch` key at all.
        lp.agent_start(base, g1, os.getpid(), {})

        g2 = run("next", base).stdout.strip()               # picker subprocess #2, "minutes later"
        assert g2.endswith("0002.md"), (
            f"expected the second picker to skip {g1!r} (a live worker still holds it) and take "
            f"0002 instead -- got {g2!r}")
        assert lp.ledger.open_claims(lp.ledger.read_all(base))[g1] == "two-picker-test"  # untouched


def test_a_stale_release_from_one_run_of_an_actor_does_not_wipe_a_live_reclaim_by_another_run():
    """#1121 (follow-up from #841's own review), driven end to end as real `loop.py` subprocesses
    -- not a unit-level `_held()` check. `ledger._held()`'s stale-terminal-entry guard used to
    scope purely by `actor`, so it could not tell apart two DIFFERENT, concurrent PROCESSES
    authenticated as the exact SAME nominal actor (the realistic shape: two independent sigma
    sessions both running as one person's own `gh` login). Reproduced with the SAME
    dead-picker-pid-no-live-corroboration mechanism `test_two_real_picker_processes_the_second_is_
    refused_a_goal_a_live_worker_still_holds` (right above) uses to force a RECLAIM instead of a
    skip: picker subprocess #1 (`SIGMA_RUN_ID=worker-A`) claims 0001.md and exits -- its pid is
    now provably dead, and (no `agent_watch`/`agent-start` marker registered here, deliberately)
    picker subprocess #2 of the SAME actor (`SIGMA_RUN_ID=worker-B`) legitimately reclaims the
    SAME goal via the existing #1197 dead-writer-pid path, landing a SECOND `claimed` ledger entry
    for the same actor under a different run_id. Worker A's own delayed `release` (simulating a
    stale/queued write physically landing after B's reclaim, exactly #1107's own repro shape) must
    not wipe B's live claim -- pre-#1121, actor-only scoping could not tell the two `A`s apart and
    did exactly that.

    PR #1269 REVIEW FIX (blocking finding 2): #1270 (landed on main after this branch's own tip)
    composes #1199's session-in-flight registry INTO `_next()`, upstream of and independent from
    the ledger-level reclaim this test exercises -- `_next()` now ALSO skips any goal registered
    in-flight by a still-LIVE session, via `_session_in_flight_goals()`. Without an explicit
    `--session-pid` on every call here, BOTH subprocesses below default to `os.getppid()` -- THIS
    pytest process's own pid, shared by both since it spawns them directly -- so worker-A's
    session never reads as dead even though its own short-lived `next` invocation already has, and
    `_next()` skips goal 0001 for worker-B instead of ever reaching the ledger-level reclaim this
    test means to prove. Passing DISTINCT `--session-pid` values (matching the shipped `/sigma-loop`
    skill's own real pattern: every session captures and threads its OWN stable id, see SKILL.md's
    `--session-pid "$PPID"` on every `start`/`next`/`next-batch` call) is what makes two
    subprocesses actually MODEL two separate sessions -- worker-A's under a provably-dead pid (its
    whole session is gone, not merely its own transient picker process), worker-B's under this test
    process's own genuinely-alive one. This is the same "definitively dead pid" convention
    `test_session_active_is_false_for_a_definitively_dead_pid` already establishes elsewhere in
    this file."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2, max_iter=5)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        cfg["ledger"] = {"enabled": True, "actor": "same-actor-run-race-test"}
        # This test isolates claim-ledger attribution using deliberately dead fake
        # session PIDs, so the session admission ceiling is outside its scope.
        cfg["budget"] = {}
        cfg["handoff"] = {"enabled": False}
        (pathlib.Path(base) / "config.json").write_text(json.dumps(cfg))
        lp = _loop()
        run = lambda env, *a: subprocess.run([sys.executable, str(S / "loop.py"), *a],
                                              capture_output=True, text=True, env={**os.environ, **env})
        run({}, "start", base)

        dead_session_pid = 2 ** 30   # not a real pid on any sane system -- worker-A's WHOLE session
        g1 = run({"SIGMA_RUN_ID": "worker-A"}, "next", base,
                  "--session-pid", str(dead_session_pid)).stdout.strip()   # process #1 claims, exits
        assert g1.endswith("0001.md"), g1

        # process #2, its own genuinely separate session (this test process's own real, alive pid)
        # -- SAME actor, DIFFERENT run_id, "minutes later". #1's own session is provably dead (not
        # merely its short-lived picker pid), so nothing corroborates it as still live via EITHER
        # the #1199 session registry or the #1197 dead-writer-pid path -- this legitimately
        # reclaims g1 (not 0002.md).
        g2 = run({"SIGMA_RUN_ID": "worker-B"}, "next", base,
                  "--session-pid", str(os.getpid())).stdout.strip()
        assert g2 == g1, (
            f"expected process #2 to reclaim {g1!r} under its own run_id (no live worker "
            f"corroborates process #1) -- got {g2!r}")
        assert lp.ledger.open_claims(lp.ledger.read_all(base))[g1] == "same-actor-run-race-test"

        # process #1's own delayed release lands last -- stale relative to process #2's live reclaim.
        rel = run({"SIGMA_RUN_ID": "worker-A"}, "release", base, g1, "stale, already reclaimed")
        assert rel.returncode == 0, rel.stderr

        assert lp.ledger.open_claims(lp.ledger.read_all(base)) == {g1: "same-actor-run-race-test"}, (
            "process #1's stale release (same actor, different run_id) must not wipe process #2's "
            "live reclaim of the same goal"
        )


def test_two_real_picker_processes_under_the_real_shipped_template_the_second_is_still_refused():
    """Independent review of PR #1237 (closing #1197): every test above -- including the one right
    above this one -- registers the corroborating worker marker via a direct `lp.agent_start(...)`
    Python call with a hand-built config dict. None goes through `lp.main([...])` (the real
    `agent-start` CLI verb -- `agent_start()`'s ONLY production caller, per SKILL.md step 3a's
    `agent-start .sdlc "$goal" --pid $PPID`) with a config loaded the way the real CLI loads it
    (`state.load_config`, inside `main()`/`_dispatch`), shaped like the literal shipped
    `/sigma-init` template (`config.json.tmpl`) rather than a stripped-down test config that just
    happens to omit the `agent_watch` key entirely. That gap matters: the template sets
    `agent_watch.enabled` to `false` EXPLICITLY, so if a future edit re-gated the marker write
    behind that flag (reverting #1197's own decoupling fix), every hand-built-dict test above
    would keep passing right alongside it -- none of them round-trip the actual shipped default.

    This test closes that gap at the real process boundary: the SAME real-subprocess two-picker
    shape as the test above, but (1) `config.json` is the literal template file, untouched apart
    from turning `ledger` on so the picks are cross-process-visible, and (2) the corroborating
    worker is registered via a REAL `loop.py agent-start` subprocess -- the exact command SKILL.md
    step 3a instructs -- not a direct Python call."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2)
        tmpl_path = (pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-init" /
                     "templates" / "config.json.tmpl")
        cfg = json.loads(tmpl_path.read_text(encoding="utf-8"))
        assert cfg["agent_watch"]["enabled"] is False        # the exact shipped default, unchanged
        # #2741: work.enabled ships `true` directly from /sigma-init as of 1.0.0 (was `null` --
        # "not yet decided", per #2255 -- until /sigma-setup applied its own adoption default on a
        # fresh scaffold). This assertion documents the real shipped template shape; the rest of
        # this test is unaffected either way, since agent-start's marker write is gated on
        # `agent_watch.enabled` (still explicitly false, asserted above), never on `work.enabled`.
        assert cfg["work"]["enabled"] is True
        cfg["ledger"] = {"enabled": True, "actor": "template-cfg-test"}
        (pathlib.Path(base) / "config.json").write_text(json.dumps(cfg))
        lp = _loop()
        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True, text=True)
        run("start", base)

        g1 = run("next", base).stdout.strip()               # picker subprocess #1 -- claims, exits
        assert g1.endswith("0001.md"), g1

        # the real CLI verb, real config.load, real subprocess -- exactly SKILL.md step 3a's own
        # line, against a config where agent_watch.enabled is explicitly false.
        reg = run("agent-start", base, g1, "--pid", str(os.getpid()))
        assert reg.returncode == 0, reg.stderr
        assert lp.agent_alive(base, g1, cfg) == ("alive", os.getpid())   # sanity: the marker landed

        g2 = run("next", base).stdout.strip()               # picker subprocess #2, "minutes later"
        assert g2.endswith("0002.md"), (
            f"under the real shipped default template config, expected the second picker to skip "
            f"{g1!r} (a live worker still holds it, registered via the real agent-start CLI verb) "
            f"and take 0002 instead -- got {g2!r}")


def test_skill_md_step_3a_registers_the_agent_start_marker_unconditionally_not_behind_work_enabled():
    """Independent review of PR #1237: SKILL.md step 3a's `agent-start --pid $PPID` line used to
    sit INSIDE the very same sentence as `With config.work.enabled on: ... work.py start` -- so on
    a genuinely fresh `/sigma-init` install (`work.enabled: false`, the shipped default -- see
    `config.json.tmpl`), the DOCUMENTED per-goal flow never called `agent-start` at all. That is
    #1197's own acceptance criterion 6 ("registered by default on a normal /sigma-loop run") failing
    for exactly the config `/sigma-init` ships, even though `agent_start()` itself was already fixed
    to write unconditionally (see the CLI-verb test above). This is a structural pin, not a
    behavioral one -- SKILL.md is prose an agent reads, not code this suite can execute -- but it
    catches the registration call being silently re-nested under a gate the way it was before this
    fix, or the (now factually wrong) 'a no-op unless agent_watch.enabled' claim creeping back in."""
    skill = (pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" /
             "SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(skill.split())        # collapse hand-wrapped newlines before substring checks
    idx_agent_start = skill.index('agent-start .sdlc "$goal" --pid $PPID')
    idx_work_gate = skill.index("With `config.work.enabled` on")
    assert idx_agent_start < idx_work_gate, (
        "step 3a's agent-start registration must be issued before/outside the "
        "config.work.enabled-gated worktree clause, not nested inside it")
    assert "Regardless of `config.work.enabled`" in normalized
    assert "a no-op unless `agent_watch.enabled`" not in normalized, (
        "stale: agent_start() writes unconditionally as of #1197 -- this claim is no longer true")


def test_cli_next_budget_stop_prints_diagnostic_to_stderr_not_stdout():
    """#411: stdout stays EXACTLY the bare `BUDGET` token -- both `supervise_classify.py`'s own
    `^\\s*BUDGET\\s*$` pattern (a real, live consumer -- see test_supervise.py) and the test right
    above this one depend on that exact line. The diagnostic naming which ceiling tripped and the
    observed-vs-configured numbers goes to stderr instead, the same channel `_surface_inbox`'s own
    LEDGER INBOX block already uses for informational text that must never contaminate stdout."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2, max_iter=1)
        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True, text=True)
        run("start", base)
        g = run("next", base).stdout.strip(); assert g.endswith("0001.md")
        run("record", base, g, "done")
        result = run("next", base)
        assert result.stdout.strip() == "BUDGET"
        assert "BUDGET (" in result.stderr
        assert "max_iterations" in result.stderr


def test_cli_next_batch_budget_stop_prints_diagnostic_to_stderr_not_stdout():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2, max_iter=1)
        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True, text=True)
        run("start", base)
        g = run("next", base).stdout.strip(); assert g.endswith("0001.md")
        run("record", base, g, "done")
        result = run("next-batch", base)
        assert result.stdout.strip() == "BUDGET"
        assert "BUDGET (" in result.stderr
        assert "max_iterations" in result.stderr


def test_cli_next_handoff_stop_prints_diagnostic_to_stderr_not_stdout(capsys):
    """Mirrors test_cli_next_batch_budget_stop_prints_diagnostic_to_stderr_not_stdout above, for the
    new kind -- in-process via capsys (loop.main() still writes to real stdout/stderr, which capsys
    captures the same way) rather than a subprocess, matching this file's own `lp.main([...])`
    convention used throughout (e.g. the cluster starting at line 169)."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=999)     # max_iter far above 1 so BUDGET cannot interfere
        cfg_path = pathlib.Path(base) / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["handoff"] = {"after_goals": 1}
        cfg_path.write_text(json.dumps(cfg))
        lp = _loop()
        lp.state.advance_cursor(base, "goal")
        lp.main(["loop.py", "next", base, "--session-pid", "12345"])
        out = capsys.readouterr()
        assert out.out.strip() == "HANDOFF"
        assert "HANDOFF (" in out.err



def test_print_pick_done_with_reason_prints_stderr_diagnostic_stdout_unchanged(capsys):
    """#1084: `_print_pick` now gives `"DONE"` the same stderr-diagnostic treatment as
    `"BUDGET"` -- stdout stays the bare `DONE` token, the reason rides stderr only."""
    lp = _loop()
    lp._print_pick("DONE", "degraded read — backlog state unknown")
    out, err = capsys.readouterr()
    assert out.strip() == "DONE"
    assert "DONE (degraded read — backlog state unknown)" in err


def test_print_pick_done_without_reason_prints_nothing_to_stderr(capsys):
    """#1084: the non-degraded path (`payload is None`) is byte-identical to before this change --
    bare `DONE` on stdout, nothing at all on stderr."""
    lp = _loop()
    lp._print_pick("DONE", None)
    out, err = capsys.readouterr()
    assert out.strip() == "DONE"
    assert err == ""


def test_record_done_warns_loudly_when_work_is_off(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()             # _backlog config has no `work` -> off
        lp.main(["loop.py", "record", base, base + "/goals/0001.md", "done"])
        err = capsys.readouterr().err
        assert "work.enabled is off" in err and "no branch/commit/PR" in err   # the silent no-PR is now loud


def test_record_done_refuses_when_work_is_on_and_the_pr_is_neither_merged_nor_armed(capsys):
    """#254 task 5's regression test: PR open + checks pending + no auto-merge armed -> `record
    done` refuses. Monkeypatches `work.done_refusal` to isolate the CLI DISPATCH's wiring from the
    predicate's own internals (already exhaustively proven in test_work.py) -- the same split
    `state.done_refusal` already gets across test_state.py vs this file's own verify.enforce test
    above. `_loop()` builds a FRESH module object per call (see its own definition), so this
    monkeypatch cannot leak into any other test."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()
        (pathlib.Path(base) / "config.json").write_text(json.dumps({"work": {"enabled": True}}))
        lp.work.done_refusal = lambda *a, **kw: "PR #7 is open (state=OPEN), not merged, and auto-merge is not armed"
        goal = base + "/goals/0001.md"
        rc = lp.main(["loop.py", "record", base, goal, "done"])
        err = capsys.readouterr().err
        assert rc == 4
        assert "REFUSED" in err and "PR #7" in err and "not merged" in err
        # The mutation-proof half: `_record`/`source.complete` must never have been reached -- the
        # goal file is untouched, still pending, not silently marked done despite the refusal.
        assert "status: pending" in pathlib.Path(goal).read_text()


def test_record_done_still_succeeds_when_done_refusal_returns_none(capsys):
    """The inverse of the test above: guards against the new gate accidentally becoming
    unconditional (always refusing) rather than driven by `done_refusal`'s own verdict."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5); lp = _loop()
        (pathlib.Path(base) / "config.json").write_text(json.dumps({"work": {"enabled": True}}))
        lp.work.done_refusal = lambda *a, **kw: None
        goal = base + "/goals/0001.md"
        rc = lp.main(["loop.py", "record", base, goal, "done"])
        assert rc == 0
        assert "status: done" in pathlib.Path(goal).read_text()


def test_record_done_with_verify_enforce_refuses_an_unsafe_goal_cleanly_instead_of_a_raw_traceback():
    """#487 independent review (B1): `record ... done` under `verify.enforce` reaches
    `_done_refusal` -> `_evidence_path`, which raises ValueError for an unsafe goal (see
    state.unsafe_goal_reason) -- but `main`'s dispatch only ever caught `state.ConfigMissing`
    (#403's docstring), so a traversal goal on this specific path fell through as an uncaught
    exception: `sys.exit(main(...))` on an uncaught ValueError prints a full traceback and exits 1,
    unlike every other unsafe-goal site in this same PR, which refuses cleanly with exit 2."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0)
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"verify": {"enforce": True, "command": ""}}))
        r = subprocess.run([sys.executable, str(S / "loop.py"), "record", base,
                             "../../../evil-goal", "done"], capture_output=True, text=True)
        assert r.returncode == 2
        assert "Traceback" not in r.stderr
        assert "unsafe goal" in r.stderr


def test_record_done_advances_iteration_by_exactly_k_under_k_parallel_real_processes():
    """The end-to-end proof `_record`'s old cross-call `load_cursor` + `save_cursor` RMW is really
    gone (#531): K REAL OS processes, each recording a DIFFERENT goal `done` via the actual CLI at
    genuinely overlapping instants, must advance `iteration` by exactly K -- no lost increments,
    matching the issue's own concurrent-recorders probe. `_backlog`'s config has no `work` key, so
    each call prints the `work.enabled is off` warning to stderr -- expected, not asserted empty
    (LocalSource's default `record ... done` path works fine against this bare fixture)."""
    k = 8
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, k)
        procs = [subprocess.Popen(
                     [sys.executable, str(S / "loop.py"), "record", base,
                      f"{base}/goals/{i:04d}.md", "done"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(1, k + 1)]
        for p in procs:
            _, err = p.communicate(timeout=60)
            assert p.returncode == 0, err
        lp = _loop()
        assert lp.state.load_cursor(base)["iteration"] == k


def test_start_surfaces_the_work_off_and_verify_traps(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); lp = _loop()
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"verify": {"enforce": True, "command": ""}}))            # work off + the verify trap
        lp.main(["loop.py", "start", base])
        err = capsys.readouterr().err
        assert "work.enabled is off" in err and "EVERY `done` will be refused" in err


def _with_autowatch(base, autowatch_overrides=None, ledger_overrides=None):
    """Patch an existing .sdlc's config.json to turn `ledger` + `ledger.autowatch` on, mirroring
    `_with_action_log`'s shape (#1323/#1322)."""
    p = pathlib.Path(base) / "config.json"
    cfg = json.loads(p.read_text())
    ledger_cfg = {"enabled": True, "actor": "watcher"}
    if ledger_overrides:
        ledger_cfg.update(ledger_overrides)
    aw = {"enabled": True}
    if autowatch_overrides:
        aw.update(autowatch_overrides)
    ledger_cfg["autowatch"] = aw
    cfg["ledger"] = ledger_cfg
    p.write_text(json.dumps(cfg))
    return cfg


def test_start_warns_about_autowatch_desktop_setup_when_enabled_and_unwired(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config-empty"))
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); lp = _loop()
        _with_autowatch(base)                              # surface defaults to "desktop"
        lp.main(["loop.py", "start", base])
        err = capsys.readouterr().err
        assert "surface=desktop" in err and "create a scheduled task" in err


def test_start_warns_about_autowatch_cli_setup_when_surface_is_cli(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config-empty"))
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); lp = _loop()
        _with_autowatch(base, {"surface": "cli"})
        lp.main(["loop.py", "start", base])
        err = capsys.readouterr().err
        assert "surface=cli" in err and "channel_webhook_url" in err


def test_start_does_not_warn_about_autowatch_when_adapter_already_wired(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config-empty"))
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); lp = _loop()
        _with_autowatch(base, {"channel_webhook_url": "http://127.0.0.1:8788"})
        lp.main(["loop.py", "start", base])
        err = capsys.readouterr().err
        assert "ledger.autowatch is enabled" not in err


def test_start_does_not_warn_about_autowatch_when_ledger_is_off(capsys):
    """Ledger off must gate the autowatch nudge too, same as it gates the feature itself — a
    reminder to set up an adapter for a feature that can't run yet would be actively confusing."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); lp = _loop()
        p = pathlib.Path(base) / "config.json"
        cfg = json.loads(p.read_text())
        cfg["ledger"] = {"enabled": False, "autowatch": {"enabled": True}}
        p.write_text(json.dumps(cfg))
        lp.main(["loop.py", "start", base])
        err = capsys.readouterr().err
        assert "ledger.autowatch is enabled" not in err


def test_start_does_not_warn_about_autowatch_when_the_block_is_absent(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); lp = _loop()
        p = pathlib.Path(base) / "config.json"
        cfg = json.loads(p.read_text())
        cfg["ledger"] = {"enabled": True, "actor": "watcher"}    # no autowatch key at all
        p.write_text(json.dumps(cfg))
        lp.main(["loop.py", "start", base])
        err = capsys.readouterr().err
        assert "ledger.autowatch is enabled" not in err


def test_start_surfaces_the_verify_trap_for_a_truthy_non_bool_enforce(capsys):
    # F17/#342: `enforce: 1` is an easy JSON typo for `true` — the empty-command warning must still
    # fire, not go silent just because 1 fails a strict `is True` check.
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); lp = _loop()
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"verify": {"enforce": 1, "command": ""}}))
        lp.main(["loop.py", "start", base])
        err = capsys.readouterr().err
        assert "EVERY `done` will be refused" in err


def test_enforce_enabled_reads_truthy_non_bool_values_generously():
    """F17/#342: `enforce: 1` / `"true"` are easy JSON mistakes for the literal bool `true` — the
    strict `is True` idiom this file uses for ledger.enabled et al is the WRONG failure direction
    here, because `enforce` gates `record done` itself; failing to recognise these must not
    silently leave the gate off (contrast ledger.enabled, where failing safe means off)."""
    lp = _loop()
    assert lp._enforce_enabled({"enforce": True}) is True
    assert lp._enforce_enabled({"enforce": 1}) is True
    assert lp._enforce_enabled({"enforce": "true"}) is True
    assert lp._enforce_enabled({"enforce": "True"}) is True
    assert lp._enforce_enabled({"enforce": "1"}) is True
    assert lp._enforce_enabled({"enforce": "yes"}) is True
    assert lp._enforce_enabled({"enforce": False}) is False
    assert lp._enforce_enabled({"enforce": 0}) is False
    assert lp._enforce_enabled({"enforce": "false"}) is False
    assert lp._enforce_enabled({"enforce": "0"}) is False
    assert lp._enforce_enabled({"enforce": "no"}) is False
    assert lp._enforce_enabled({"enforce": "off"}) is False
    assert lp._enforce_enabled({"enforce": ""}) is False
    assert lp._enforce_enabled({}) is False                     # absent key: still off, same as before


# --- real budgets (0.6): max_minutes / max_tokens enforce when configured ---

def _write_cfg(base, budget):
    (pathlib.Path(base) / "config.json").write_text(json.dumps({"budget": budget}))


def test_wall_clock_budget_halts_via_next():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 3)
        _write_cfg(base, {"max_iterations": 10, "max_minutes": 1})
        lp = _loop()
        # a run that started 2 minutes ago — the STATE.md cursor is authoritative
        (pathlib.Path(base) / "state" / "STATE.md").write_text(
            "iteration: 0\nrun_iteration: 0\n"
            f"run_started_at: {int(lp.time.time()) - 120}\nrun_tokens: 0\nlast_run: none\n")
        src = lp.sources.get_source(base, lp.state.load_config(base))
        # #411: the second element is now the diagnostic naming which ceiling tripped, never None
        # once budget genuinely trips.
        assert lp._next(base, src, lp.state.load_config(base)) == \
            ("BUDGET", "elapsed 2min >= max_minutes 1")


def test_token_budget_halts_when_signal_reported():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 3)
        _write_cfg(base, {"max_iterations": 10, "max_tokens": 1000})
        lp = _loop()
        lp.state.start_run(base)
        lp.state.add_tokens(base, 600)
        lp.state.add_tokens(base, 500)          # cumulative 1100 >= 1000
        src = lp.sources.get_source(base, lp.state.load_config(base))
        assert lp._next(base, src, lp.state.load_config(base)) == \
            ("BUDGET", "1100 tokens >= max_tokens 1000")


def _budget_control_assistant_line(input_tokens, output_tokens, ts):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": "msg_budget_control",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            },
        },
    }


def test_a_real_phase_end_call_pushes_a_tiny_budget_over_and_loop_next_stops(tmp_path):
    # #2515, Decision 3 -- the issue's own "Done when" bar: the FULL chain, no layer mocked.
    # 1. a tiny synthetic .sdlc with budget.max_tokens = 100.
    base = _backlog(str(tmp_path), 1)
    _write_cfg(base, {"max_iterations": 10, "max_tokens": 100})
    lp = _loop()

    # 2. start_run resets run_tokens to 0, matching what /sigma-loop does at the start of a real run.
    lp.state.start_run(base)

    # 3. pre-seed near-exhaustion -- standing in for prior phases already real-run and already
    #    recorded earlier in the SAME run. A direct state call, deliberately, so this test isolates
    #    what it's actually proving: that the NEXT real `end` call, and only that call, tips it over.
    lp.state.add_tokens(base, 95)

    # 4. the live half: a real, minimal Claude-Code-shaped transcript fixture, and a real subprocess
    #    `phase_report.py end` call -- the exact gesture SKILL.md/running.md documents, not a
    #    stronger one (AGENTS.md: "run the control on the gesture the docs give").
    home = tmp_path / "home"
    session_id = "sess-budget-control"
    subagents = home / ".claude" / "projects" / "-slug" / session_id / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-abc.jsonl").write_text(
        json.dumps(_budget_control_assistant_line(1_000_000, 0, "2026-08-24T00:00:00Z")) + "\n"
    )
    env = {**os.environ, "HOME": str(home), "CLAUDE_CODE_SESSION_ID": session_id}
    subprocess.run([sys.executable, str(S / "phase_report.py"), "start", base, "0001",
                     "research", "--model", "sonnet"], capture_output=True, text=True, env=env)
    end_result = subprocess.run(
        [sys.executable, str(S / "phase_report.py"), "end", base, "0001", "research",
         "--agent-id", "abc"],
        capture_output=True, text=True, env=env,
    )
    assert end_result.returncode == 0
    assert "P2 RESEARCH → P3 PLAN" in end_result.stdout

    # 5. STATE.md's run_tokens now exceeds 100 -- 1 Mtok input @ intro $2.00/Mtok = $2.00 ->
    #    2.0 / (2.00 / 1e6) = 1_000_000 cost-equivalent tokens, plus the pre-seeded 95, far over 100.
    cursor = lp.state.load_cursor(base)
    assert cursor["run_tokens"] > 100

    # 6. the real loop.py _next() call, observed to actually return BUDGET.
    src = lp.sources.get_source(base, lp.state.load_config(base))
    kind, reason = lp._next(base, src, lp.state.load_config(base))
    assert kind == "BUDGET"
    assert "max_tokens 100" in reason


def test_token_budget_without_reports_never_enforces():
    # max_tokens set but the host never reported spend → run_tokens stays 0 → no stop
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        _write_cfg(base, {"max_iterations": 10, "max_tokens": 1000})
        lp = _loop()
        lp.state.start_run(base)
        lp.session_start(base, os.getppid())
        src = lp.sources.get_source(base, lp.state.load_config(base))
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert kind == "goal" and goal.endswith("0001.md")


def test_absent_optional_budget_keys_enforce_nothing():
    # pre-0.6 config shape (iterations only) behaves exactly as before, whatever the counters say
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        _write_cfg(base, {"max_iterations": 10})
        lp = _loop()
        (pathlib.Path(base) / "state" / "STATE.md").write_text(
            "iteration: 0\nrun_iteration: 0\nrun_started_at: 1\nrun_tokens: 999999\nlast_run: none\n")
        lp.session_start(base, os.getppid())
        src = lp.sources.get_source(base, lp.state.load_config(base))
        kind, _ = lp._next(base, src, lp.state.load_config(base))
        assert kind == "goal"


def test_budget_spent_max_iterations_absent_or_zero_enforces_nothing():
    """F18/#349: the header promises "an absent/zero key enforces nothing" for every budget, but
    _budget_spent special-cased max_iterations with a `20` default and a plain `>=` -- absent
    silently capped a run at 20 goals, and an explicit 0 halted it immediately. Both contradicted
    the docstring one line above the code. max_iterations now follows the same falsy-guard already
    used here for max_minutes/max_tokens (and already used by next_batch's own remaining-budget cap
    a few functions down)."""
    lp = _loop()
    cursor = {"run_iteration": 37, "run_started_at": 0, "run_tokens": 0}   # 37 > the old 20 default
    assert lp._budget_spent(cursor, {}) is False                     # absent -- no 20-goal ceiling
    assert lp._budget_spent(cursor, {"max_iterations": 0}) is False   # explicit zero -- not "spent"
    assert lp._budget_spent(cursor, {"max_iterations": 37}) is True   # a real cap still enforces


# --- #411: BUDGET names WHICH ceiling tripped and the observed-vs-configured numbers ------------


def test_budget_reason_is_none_when_nothing_is_spent():
    lp = _loop()
    cursor = {"run_iteration": 0, "run_started_at": 0, "run_tokens": 0}
    assert lp._budget_reason(cursor, {"max_iterations": 10, "max_minutes": 60, "max_tokens": 1000}) is None


def test_budget_reason_names_the_tripped_ceiling_for_iterations():
    lp = _loop()
    cursor = {"run_iteration": 15, "run_started_at": 0, "run_tokens": 0}
    assert lp._budget_reason(cursor, {"max_iterations": 15}) == "15 iterations >= max_iterations 15"


def test_budget_reason_names_the_tripped_ceiling_for_minutes():
    lp = _loop()
    started = lp.time.time() - 720 * 60   # a run that started 720 minutes ago
    cursor = {"run_iteration": 0, "run_started_at": started, "run_tokens": 0}
    assert lp._budget_reason(cursor, {"max_minutes": 480}) == "elapsed 720min >= max_minutes 480"


def test_budget_reason_names_the_tripped_ceiling_for_tokens():
    lp = _loop()
    cursor = {"run_iteration": 0, "run_started_at": 0, "run_tokens": 1100}
    assert lp._budget_reason(cursor, {"max_tokens": 1000}) == "1100 tokens >= max_tokens 1000"


def test_codex_raw_token_ceiling_is_separate_and_precedes_handoff():
    lp = _loop()
    cursor = {"run_iteration": 20, "run_started_at": 0, "run_tokens": 0,
              "run_codex_raw_tokens": 1100}
    budget = {"max_iterations": 20, "max_tokens": 1, "max_codex_raw_tokens": 1000}
    assert lp._budget_reason(cursor, budget) == "20 iterations >= max_iterations 20"
    assert lp._budget_resource_reason(cursor, budget) == (
        "1100 Codex raw tokens >= max_codex_raw_tokens 1000")


def test_codex_raw_token_credit_stops_the_documented_next_pick():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        _write_cfg(base, {"max_iterations": 20, "max_codex_raw_tokens": 100})
        lp = _loop()
        lp.state.start_run(base)
        started = lp.state.load_cursor(base)["run_started_at"]
        lp.state.record_phase_end(base, "goal/research/attempt", started + 1,
                                  codex_raw_tokens=120)
        lp.session_start(base, os.getppid())
        src = lp.sources.get_source(base, lp.state.load_config(base))
        assert lp._next(base, src, lp.state.load_config(base)) == (
            "BUDGET", "120 Codex raw tokens >= max_codex_raw_tokens 100")


def test_budget_reason_and_budget_spent_agree_on_every_case():
    """`_budget_spent` is now a thin wrapper over `_budget_reason` (#411) -- proves the two can
    never independently drift: for every case in the existing near/at/absent matrix, `is not None`
    on the reason must equal the bool."""
    lp = _loop()
    cases = [
        ({"run_iteration": 0, "run_started_at": 0, "run_tokens": 0}, {}),
        ({"run_iteration": 5, "run_started_at": 0, "run_tokens": 0}, {"max_iterations": 5}),
        ({"run_iteration": 4, "run_started_at": 0, "run_tokens": 0}, {"max_iterations": 5}),
        ({"run_iteration": 0, "run_started_at": lp.time.time() - 3600, "run_tokens": 0},
         {"max_minutes": 30}),
        ({"run_iteration": 0, "run_started_at": 0, "run_tokens": 2000}, {"max_tokens": 1000}),
    ]
    for cursor, budget in cases:
        assert (lp._budget_reason(cursor, budget) is not None) == lp._budget_spent(cursor, budget)


# --- #2521: HANDOFF names the goal-count ceiling that bounds the ORCHESTRATING session itself ---


def test_handoff_reason_defaults_to_20_goals_when_the_key_is_entirely_absent():
    """#2521: unlike every other opt-in block in this file, an ABSENT `handoff` key is not "off" —
    the defect this closes is present on an existing install's own already-on-disk config.json,
    which predates this key by construction (templates are applied once, never re-synced), so
    "absent means off" would leave every install that already exists exhibiting the exact
    unbounded-growth bug forever."""
    lp = _loop()
    cursor = {"run_iteration": 20, "run_started_at": None, "run_tokens": 0}
    assert lp._handoff_reason(cursor, {}) == "20 goals >= handoff.after_goals 20"
    assert lp._handoff_reason(cursor, None) == "20 goals >= handoff.after_goals 20"


def test_handoff_reason_is_none_below_the_default_threshold():
    lp = _loop()
    cursor = {"run_iteration": 19, "run_started_at": None, "run_tokens": 0}
    assert lp._handoff_reason(cursor, {}) is None


def test_handoff_reason_honors_a_configured_after_goals():
    lp = _loop()
    cursor = {"run_iteration": 5, "run_started_at": None, "run_tokens": 0}
    assert lp._handoff_reason(cursor, {"after_goals": 5}) == "5 goals >= handoff.after_goals 5"
    assert lp._handoff_reason({**cursor, "run_iteration": 4}, {"after_goals": 5}) is None


def test_handoff_reason_is_none_when_explicitly_disabled():
    """The documented escape hatch: an operator who has measured their own repo and wants the old
    unbounded-single-session behaviour back sets `enabled: false`."""
    lp = _loop()
    cursor = {"run_iteration": 50, "run_started_at": None, "run_tokens": 0}
    assert lp._handoff_reason(cursor, {"enabled": False}) is None
    assert lp._handoff_reason(cursor, {"enabled": False, "after_goals": 5}) is None


def test_handoff_reason_explicit_zero_after_goals_enforces_nothing():
    """Same convention `_budget_reason` already uses for max_iterations: explicit 0 means
    unlimited, distinct from "absent" (which means the default, not unlimited)."""
    lp = _loop()
    cursor = {"run_iteration": 50, "run_started_at": None, "run_tokens": 0}
    assert lp._handoff_reason(cursor, {"after_goals": 0}) is None


# --- #2521 x #2515 merge fix: a REAL resource budget stop (max_minutes/max_tokens, now genuinely
# fed by phase_report.py cmd_end) must win over HANDOFF, unlike the max_iterations-vs-handoff tie
# (Top Risk 5), which stays HANDOFF's -- see _budget_resource_reason's own docstring for why.


def test_budget_resource_reason_names_tokens_independently_of_a_tripped_max_iterations():
    """The scenario `_budget_reason`'s own iterations-first short-circuit cannot answer: on the
    SHIPPED DEFAULT config, max_iterations and handoff.after_goals watch the identical counter at
    the identical number (20), so an iterations trip coinciding with a genuine max_tokens breach is
    the COMMON case, not a rare one. `_budget_resource_reason` must still name the tokens breach
    here, not return None just because iterations alone would already explain a `_budget_reason`
    call."""
    lp = _loop()
    cursor = {"run_iteration": 20, "run_started_at": None, "run_tokens": 5000}
    budget = {"max_iterations": 20, "max_tokens": 1000}
    assert lp._budget_reason(cursor, budget) == "20 iterations >= max_iterations 20", (
        "sanity check: _budget_reason's own short-circuit names iterations first, as documented")
    assert lp._budget_resource_reason(cursor, budget) == "5000 tokens >= max_tokens 1000"


def test_budget_resource_reason_is_none_when_only_iterations_is_configured():
    lp = _loop()
    cursor = {"run_iteration": 20, "run_started_at": None, "run_tokens": 0}
    assert lp._budget_resource_reason(cursor, {"max_iterations": 20}) is None


def test_budget_resource_reason_is_none_when_nothing_is_spent():
    lp = _loop()
    cursor = {"run_iteration": 0, "run_started_at": None, "run_tokens": 0}
    assert lp._budget_resource_reason(cursor, {"max_minutes": 60, "max_tokens": 1000}) is None


def test_budget_resource_reason_message_shape_matches_budget_reasons_own():
    """Drift guard for the intentional duplication `_budget_resource_reason`'s own docstring names:
    for any case where max_iterations does NOT independently trip, the two functions must agree on
    the exact message text, byte for byte -- if `_budget_reason`'s own minutes/tokens branches ever
    change, this goes red before the two silently diverge."""
    lp = _loop()
    cases = [
        ({"run_iteration": 0, "run_started_at": lp.time.time() - 3600, "run_tokens": 0},
         {"max_minutes": 30}),
        ({"run_iteration": 0, "run_started_at": None, "run_tokens": 2000}, {"max_tokens": 1000}),
        ({"run_iteration": 5, "run_started_at": None, "run_tokens": 2000},
         {"max_iterations": 50, "max_tokens": 1000}),   # iterations configured but NOT tripped
    ]
    for cursor, budget in cases:
        assert lp._budget_resource_reason(cursor, budget) == lp._budget_reason(cursor, budget)


def test_next_resource_budget_wins_over_handoff_on_a_genuine_simultaneous_trip():
    """The core precedence fix: construct a tie where handoff.after_goals (20, the shipped default)
    AND a genuine max_tokens breach are BOTH live at the same _next() call -- the realistic shape
    #2515 introduced (a `large`-lane drain can plausibly burn 500k tokens well within 20 goals,
    given a fresh subagent's own ~70-77k-token starting cost alone). Unlike the max_iterations tie
    (Top Risk 5, HANDOFF wins on purpose), a resource ceiling must win: a HANDOFF-triggered relaunch
    calls state.start_run(), which clears run_tokens -- reporting HANDOFF here would silently let an
    unattended, supervised drain spend past the operator's own configured hard cap forever, one
    hand-off at a time."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "budget": {"max_tokens": 1000}, "handoff": {"after_goals": 20}}))
        lp = _loop()
        for _ in range(20):
            lp.state.advance_cursor(base, "goal")   # trips handoff.after_goals=20
        lp.state.add_tokens(base, 5000)              # ALSO genuinely exceeds max_tokens=1000
        src = _Queue(["a"])
        kind, reason = lp._next(base, src, lp.state.load_config(base), session_pid=12345)
        assert kind == "BUDGET", (
            f"expected a genuine max_tokens breach to win over a simultaneous HANDOFF trip, got "
            f"{kind!r} -- a resource ceiling must never be silently defeated by a hand-off relaunch "
            f"resetting run_tokens")
        assert "tokens" in reason and "max_tokens 1000" in reason


def test_next_handoff_still_wins_the_max_iterations_only_tie_after_the_merge():
    """Re-affirms Top Risk 5 (already reviewed, pinned by test_next_handoff_wins_over_budget_on_
    every_default_configured_install above) is UNCHANGED by the #2515 merge: with no max_tokens/
    max_minutes configured at all, an iterations-vs-handoff tie still resolves to HANDOFF, exactly
    as before -- the new resource pre-check must be a no-op when neither resource key is set."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "budget": {"max_iterations": 20}, "handoff": {"after_goals": 20}}))
        lp = _loop()
        for _ in range(20):
            lp.state.advance_cursor(base, "goal")
        src = _Queue(["a"])
        kind, reason = lp._next(base, src, lp.state.load_config(base), session_pid=12345)
        assert kind == "HANDOFF"


def test_the_github_mutating_sweeps_skip_on_a_handoff_return_not_only_budget(monkeypatch):
    """Code-review cycle 1, finding 2 (should-fix): the pre-pick sweep gate (loop.py ~2117) —
    `_feature_needs_label_sweep`/`_feature_needs_unit_sweep`, GitHub-mutating, gated since #1468 on
    "only when this call could actually do work" — checked `_budget_reason` alone, never
    `_handoff_reason`. Failing case named in the review: {"budget": {"max_iterations": 50},
    "handoff": {"after_goals": 10}} -- a tuning the template's own `_handoff` comment explicitly
    invites ("tune after_goals down..."). At goal 10, `_budget_reason` is None so (before this fix)
    the sweeps ran, then the SAME call returned ("HANDOFF", ...) with no goal claimed -- invisible
    only on the shipped default, where both ceilings share one number. Spies on both sweep
    functions prove they are never even called once handoff has tripped, not merely that a return
    value happens to look right."""
    label_calls = []
    unit_calls = []
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "budget": {"max_iterations": 50}, "handoff": {"after_goals": 10}}))
        lp = _loop()
        monkeypatch.setattr(lp, "_feature_needs_label_sweep",
                             lambda *a, **kw: label_calls.append(1))
        monkeypatch.setattr(lp, "_feature_needs_unit_sweep",
                             lambda *a, **kw: unit_calls.append(1))
        for _ in range(10):
            lp.state.advance_cursor(base, "goal")   # trips handoff.after_goals=10; max_iterations=50 untouched
        src = _Queue(["a"])
        kind, reason = lp._next(base, src, lp.state.load_config(base), session_pid=12345)
        assert kind == "HANDOFF", (
            f"test setup is wrong if this isn't HANDOFF -- got {kind!r} {reason!r}")
        assert label_calls == [], (
            "_feature_needs_label_sweep ran on a call that returned HANDOFF with no goal claimed")
        assert unit_calls == [], (
            "_feature_needs_unit_sweep ran on a call that returned HANDOFF with no goal claimed")


def test_start_run_resets_all_run_counters():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        lp.state.add_tokens(base, 500)
        lp.state.start_run(base)
        cur = lp.state.load_cursor(base)
        assert cur["run_tokens"] == 0 and cur["run_iteration"] == 0
        assert cur["run_started_at"] > 0     # the wall-clock anchor is stamped


def test_spend_cli_verb_accumulates():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        for n in ("120", "80"):
            proc = subprocess.run([sys.executable, str(S / "loop.py"), "spend", base, n],
                                  capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr
        lp = _loop()
        assert lp.state.load_cursor(base)["run_tokens"] == 200


# --- F8: a non-integer spend token count must refuse cleanly, never traceback ------------------


def test_spend_rejects_a_float_token_count_with_a_usable_message_not_a_traceback():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        proc = subprocess.run([sys.executable, str(S / "loop.py"), "spend", base, "1.5"],
                              capture_output=True, text=True)
        assert proc.returncode == 2
        assert "Traceback" not in proc.stderr
        assert "1.5" in proc.stderr and "not an integer" in proc.stderr


def test_spend_rejects_a_non_numeric_token_count():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        proc = subprocess.run([sys.executable, str(S / "loop.py"), "spend", base, "abc"],
                              capture_output=True, text=True)
        assert proc.returncode == 2
        assert "Traceback" not in proc.stderr


def test_spend_rejects_a_bad_token_count_without_corrupting_the_run_tokens_counter():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        subprocess.run([sys.executable, str(S / "loop.py"), "spend", base, "120"],
                       capture_output=True, text=True)
        proc = subprocess.run([sys.executable, str(S / "loop.py"), "spend", base, "1.5"],
                              capture_output=True, text=True)
        assert proc.returncode == 2
        lp = _loop()
        assert lp.state.load_cursor(base)["run_tokens"] == 120   # unchanged by the rejected call


def test_spend_still_accepts_a_clean_integer_after_the_fix():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        proc = subprocess.run([sys.executable, str(S / "loop.py"), "spend", base, "50"],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        lp = _loop()
        assert lp.state.load_cursor(base)["run_tokens"] == 50


# --- failed != parked (0.6): a fix-needed lane distinct from decide-needed ---

def test_failed_result_gets_failed_status_and_own_queue_tag():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2)
        rg = lambda g: ("failed", "tests will not pass") if g.endswith("0001.md") else ("done", "")
        res = _loop().run_loop(base, rg)
        assert res["done"] == 1 and res["failed"] == 1 and res["parked"] == 0
        goal_text = (pathlib.Path(base) / "goals" / "0001.md").read_text()
        assert "status: failed" in goal_text
        queue = (pathlib.Path(base) / "state" / "review-queue.md").read_text()
        assert "needs: a fix" in queue and "tests will not pass" in queue


def test_parked_and_failed_are_counted_separately():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 3)
        results = {"0001.md": ("parked", "deploy gate"), "0002.md": ("failed", "red suite")}
        rg = lambda g: results.get(pathlib.Path(g).name, ("done", ""))
        res = _loop().run_loop(base, rg)
        assert res == {**res, "done": 1, "parked": 1, "failed": 1}


def test_discovery_skips_failed_goals():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2)
        (pathlib.Path(base) / "goals" / "0001.md").write_text(
            "---\nid: 0001\nstatus: failed\n---\nx\n")
        lp = _loop()
        src = lp.sources.get_source(base, lp.state.load_config(base))
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert kind == "goal" and goal.endswith("0002.md")


# ---------------------------------------------------------------- _ensure_watcher
# A loop trigger starts the ledger watcher itself, so entries actually get published without a
# separate manual step. `is_worktree` reads `.sdlc/ledger/.git`, so a stub file is enough to fake
# an initialised worktree — no git needed. `spawn` is injected to observe the launch.


def _ledger_sdlc(d, enabled=True, worktree=False):
    base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps({"ledger": {"enabled": enabled, "actor": "rae"}}))
    if worktree:
        (base / "ledger").mkdir(); (base / "ledger" / ".git").write_text("gitdir: elsewhere\n")
    return str(base)


def test_ensure_watcher_starts_the_watcher_when_ledger_on_and_initialised():
    with tempfile.TemporaryDirectory() as d:
        base = _ledger_sdlc(d, enabled=True, worktree=True)
        lp = _loop(); calls = []
        lp._ensure_watcher(base, lp.state.load_config(base), spawn=lambda: calls.append(1))
        assert calls == [1]


def test_ensure_watcher_is_a_noop_when_the_ledger_is_off():
    with tempfile.TemporaryDirectory() as d:
        base = _ledger_sdlc(d, enabled=False, worktree=True)
        lp = _loop(); calls = []
        lp._ensure_watcher(base, lp.state.load_config(base), spawn=lambda: calls.append(1))
        assert calls == []                              # nothing to publish, so don't spawn


def test_ensure_watcher_waits_until_sync_init_has_made_the_worktree():
    with tempfile.TemporaryDirectory() as d:
        base = _ledger_sdlc(d, enabled=True, worktree=False)     # ledger on, but `sync.py init` not run
        lp = _loop(); calls = []
        lp._ensure_watcher(base, lp.state.load_config(base), spawn=lambda: calls.append(1))
        assert calls == []                              # no worktree = nothing to publish yet


def test_ensure_watcher_is_fail_open_when_the_spawn_raises():
    with tempfile.TemporaryDirectory() as d:
        base = _ledger_sdlc(d, enabled=True, worktree=True)
        lp = _loop()
        def boom(): raise RuntimeError("no bash on this box")
        lp._ensure_watcher(base, lp.state.load_config(base), spawn=boom)   # must not propagate


def test_ensure_watcher_spawns_the_python_daemon_with_the_expected_argv(monkeypatch):
    """#2488 D-4: all four tests above inject a zero-argument `spawn`, so the default lambda at
    loop.py:59-61 -- the ONE automatic spawn site in the whole tree -- is never executed by any of
    them. Nothing in the suite proved the argv, which means swapping "bash" for sys.executable would
    have broken nothing here BY CONSTRUCTION: exactly the "spawn guard that passed against the very
    bug it targeted" shape AGENTS.md names. This test reaches the real lambda by passing spawn=None
    and monkeypatching subprocess.Popen.

    `_ensure_watcher` swallows every exception (loop.py:63-64), so the assertions MUST be after the
    call, on the recorded argv -- an assertion raised inside a fake spawn would be silently absorbed
    and this test would pass vacuously. The injected spawn's arity is deliberately left alone
    (the four tests above pass a zero-argument callable)."""
    with tempfile.TemporaryDirectory() as d:
        base = _ledger_sdlc(d, enabled=True, worktree=True)
        lp = _loop(); seen = {}

        class _P:                                  # a stand-in Popen; never starts a process
            def __init__(self, argv, **kw):
                seen["argv"] = argv; seen["kw"] = kw

        monkeypatch.setattr(lp.subprocess, "Popen", _P)
        lp._ensure_watcher(base, lp.state.load_config(base))        # spawn=None -> the REAL lambda

        assert seen["argv"] == [sys.executable, str(lp._HERE / "watch_daemon.py"), str(base)]
        assert seen["kw"]["start_new_session"] is True
        assert seen["kw"]["stdout"] is lp.subprocess.DEVNULL
        assert seen["kw"]["stderr"] is lp.subprocess.DEVNULL


# ---------------------------------------------------------------- _ensure_ledger_delivery (#2393)
# A loop trigger keeps the ledger flowing on its own via `_ensure_watcher`; this notices when that
# flow has STALLED -- no watcher to rely on, or a live one whose publishes keep failing -- and
# tries once (bounded) to clear a real backlog. Real git throughout, like
# tests/test_ledger_publish.py's own `clone` fixture: `sync.pending_entry_count` IS real git
# state, and a stub would only ever confirm the shape this file already assumes.


def _real_ledger_clone(d, actor="dana"):
    d = pathlib.Path(d)
    origin = d / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = d / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / ".gitignore").write_text(".sdlc/\n", encoding="utf-8")
    _git(repo, "add", "--", ".gitignore")
    _git(repo, "commit", "-q", "-m", "base")
    base = repo / ".sdlc"; (base / "state").mkdir(parents=True)
    config = {"ledger": {"enabled": True, "actor": actor}}
    (base / "config.json").write_text(json.dumps(config))
    lp = _loop()
    sync = lp._load("sync")
    out = sync.bootstrap(base, config)
    assert "published" in out, out
    return lp, sync, base, config


def test_ensure_ledger_delivery_does_nothing_below_threshold(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        lp.ledger.append(base, config, "note", "g.md", why="one")
        calls = []
        lp._ensure_ledger_delivery(base, config, threshold=5,
                                   publish_fn=lambda *a, **k: calls.append(1))
        assert calls == []
        assert capsys.readouterr().err == ""


def test_ensure_ledger_delivery_self_publishes_above_threshold_with_no_live_watcher(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        lp.ledger.append(base, config, "note", "g.md", why="one")
        lp.ledger.append(base, config, "note", "g.md", why="two")
        calls = []
        def fake_publish(*a, **k):
            calls.append(1)
            return "published"
        lp._ensure_ledger_delivery(base, config, threshold=1, publish_fn=fake_publish)
        assert calls == [1]
        assert capsys.readouterr().err == ""          # silent once it actually clears the backlog


def test_ensure_ledger_delivery_warns_on_a_deferred_publish(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        lp.ledger.append(base, config, "note", "g.md", why="one")
        lp.ledger.append(base, config, "note", "g.md", why="two")
        lp._ensure_ledger_delivery(
            base, config, threshold=1,
            publish_fn=lambda *a, **k: "publish deferred (will retry next tick): no route")
        err = capsys.readouterr().err
        assert "ledger backlog is 2 entries" in err, err
        assert "/sigma-doctor" in err, err


def test_ensure_ledger_delivery_warns_when_nothing_to_publish_still_leaves_a_real_backlog(capsys):
    """Plan-review finding 1: `publish()`'s own "nothing to publish" is not "nothing outstanding",
    so this re-measures the real backlog rather than trusting the string. The real `publish()`,
    no stub: two entries written under an older actor name are pending lines this actor's
    publish never stages, so it truthfully has nothing to publish while the backlog stays."""
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        renamed = {"ledger": {"enabled": True, "actor": "oldname"}}
        lp.ledger.append(base, renamed, "note", "g.md", why="one")
        lp.ledger.append(base, renamed, "note", "g.md", why="two")
        lp._ensure_ledger_delivery(base, config, threshold=1)
        err = capsys.readouterr().err
        assert "ledger backlog is 2 entries" in err, err
        assert "another actor name" in err, err
        assert "/sigma-doctor" in err, err


def test_ensure_ledger_delivery_delivers_a_stranded_commit_without_a_word(capsys):
    """#1210 at the loop's level: a commit an earlier failed push left behind is pushed by the
    real `publish()` even though nothing new is staged. Before #1210 publish said "nothing to
    publish" here, so this warned once per cooldown window while the commit stayed local."""
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        lp.ledger.append(base, config, "note", "g.md", why="one")
        lp.ledger.append(base, config, "note", "g.md", why="two")
        wt = sync.worktree(base)
        sync._write_team(base, wt)
        _git(wt, "add", "-A")
        _git(wt, "commit", "-q", "-m", "ledger: local only")     # committed, never pushed
        lp._ensure_ledger_delivery(base, config, threshold=1)
        assert capsys.readouterr().err == ""
        assert sync.pending_entry_count(base, config) == 0


def test_ensure_ledger_delivery_is_silent_when_nothing_to_publish_and_the_backlog_is_actually_clear(monkeypatch, capsys):
    """The mirror of the finding above: "nothing to publish" IS benign once the real, re-measured
    backlog is actually at or below threshold (e.g. someone else's push landed in the interim) --
    this must not warn just because the string itself carries no verdict either way. `sync` is
    re-`_load`ed (same technique `test_work.py`'s `_spy_on_ensure_calls` uses for `loop`) so the
    SAME call's two internal `pending_entry_count` reads can answer differently -- a real git
    repo cannot be made to report two different backlogs for the same state within one call."""
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        lp.ledger.append(base, config, "note", "g.md", why="one")
        real_load = lp._load
        counts = iter([2, 0])          # first check: above threshold; re-measure: cleared
        def spying_load(name):
            mod = real_load(name)
            if name == "sync":
                mod.pending_entry_count = lambda *a, **k: next(counts)
            return mod
        monkeypatch.setattr(lp, "_load", spying_load)
        lp._ensure_ledger_delivery(base, config, threshold=1,
                                   publish_fn=lambda *a, **k: "nothing to publish")
        assert capsys.readouterr().err == ""


def test_ensure_ledger_delivery_defers_to_a_live_watcher_but_still_warns_if_it_is_not_keeping_up(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        lp.ledger.append(base, config, "note", "g.md", why="one")
        lp.ledger.append(base, config, "note", "g.md", why="two")
        hb = sync.heartbeat_path(base); hb.parent.mkdir(parents=True, exist_ok=True); hb.write_text("")
        calls = []
        lp._ensure_ledger_delivery(base, config, threshold=1,
                                   publish_fn=lambda *a, **k: calls.append(1))
        assert calls == [], "must not race a live watcher for the ledger worktree's index lock"
        err = capsys.readouterr().err
        assert "ledger backlog is 2 entries" in err, err
        assert "watcher is live" in err, err


def test_ensure_ledger_delivery_cooldown_bounds_repeated_attempts(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        lp.ledger.append(base, config, "note", "g.md", why="one")
        lp.ledger.append(base, config, "note", "g.md", why="two")
        calls = []
        def fake_publish(*a, **k):
            calls.append(1)
            return "publish deferred (will retry next tick): no route"
        lp._ensure_ledger_delivery(base, config, threshold=1, cooldown_s=300, publish_fn=fake_publish)
        assert len(calls) == 1
        capsys.readouterr()
        lp._ensure_ledger_delivery(base, config, threshold=1, cooldown_s=300, publish_fn=fake_publish)
        assert len(calls) == 1, "a second attempt within the cooldown window must not re-publish"
        assert capsys.readouterr().err == ""


def test_ensure_ledger_delivery_retries_once_the_cooldown_elapses():
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        lp.ledger.append(base, config, "note", "g.md", why="one")
        lp.ledger.append(base, config, "note", "g.md", why="two")
        calls = []
        def fake_publish(*a, **k):
            calls.append(1)
            return "publish deferred (will retry next tick): no route"
        lp._ensure_ledger_delivery(base, config, threshold=1, cooldown_s=300, publish_fn=fake_publish)
        assert len(calls) == 1
        marker = lp._ledger_delivery_cooldown_path(base)
        old = time.time() - 400
        os.utime(marker, (old, old))
        lp._ensure_ledger_delivery(base, config, threshold=1, cooldown_s=300, publish_fn=fake_publish)
        assert len(calls) == 2


# ------------------------------------------------------- _ensure_unit_tracking (#2435)
# The side job: on every goal-scoped trigger, confirm the goal being worked has a proper unit BOTH
# on its issue and in that unit's registry entry, repairing the registry side when they drift --
# modelled directly on `_ensure_watcher`/`_ensure_ledger_delivery`'s own shape. The real
# check-and-repair logic (`feature_labels.ensure_unit_tracking`) is unit-tested in isolation in
# tests/test_feature_labels.py; these tests pin the WRAPPER -- the config gate, the cooldown, and
# fail-open behaviour -- via a spy, the same technique `_ensure_ledger_delivery`'s own tests at
# line ~6244 use for a sibling `_ensure_*`.

def _unit_tracking_base(d, enabled=True):
    base = _gh_base(d)
    p = pathlib.Path(base) / "config.json"
    cfg = json.loads(p.read_text())
    if enabled:
        cfg["discovery"]["no_dangling_goal"] = {"enabled": True, "core": "core"}
    p.write_text(json.dumps(cfg))
    return base


def test_ensure_unit_tracking_off_by_default_never_calls_the_real_check(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        base = _unit_tracking_base(d, enabled=False)
        lp = _loop(); calls = []
        monkeypatch.setattr(lp.feature_labels, "ensure_unit_tracking",
                            lambda *a, **k: calls.append(1))
        lp._ensure_unit_tracking(base, "2017")
        assert calls == []
        assert not (pathlib.Path(base) / "state" / "unit-tracking" / "2017.attempt").exists()


def test_ensure_unit_tracking_calls_the_real_check_when_enabled(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        base = _unit_tracking_base(d)
        lp = _loop(); calls = []
        monkeypatch.setattr(lp.feature_labels, "ensure_unit_tracking",
                            lambda *a, **k: calls.append(a))
        lp._ensure_unit_tracking(base, "2017")
        assert len(calls) == 1
        sdlc_dir, source, goal, config = calls[0]
        assert sdlc_dir == base and goal == "2017" and config.get("discovery")
        assert (pathlib.Path(base) / "state" / "unit-tracking" / "2017.attempt").exists()


def test_ensure_unit_tracking_cooldown_bounds_repeated_calls(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        base = _unit_tracking_base(d)
        lp = _loop(); calls = []
        monkeypatch.setattr(lp.feature_labels, "ensure_unit_tracking",
                            lambda *a, **k: calls.append(1))
        lp._ensure_unit_tracking(base, "2017", cooldown_s=300)
        lp._ensure_unit_tracking(base, "2017", cooldown_s=300)
        assert len(calls) == 1, "a second call within the cooldown window must not re-check"


def test_ensure_unit_tracking_retries_once_the_cooldown_elapses(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        base = _unit_tracking_base(d)
        lp = _loop(); calls = []
        monkeypatch.setattr(lp.feature_labels, "ensure_unit_tracking",
                            lambda *a, **k: calls.append(1))
        lp._ensure_unit_tracking(base, "2017", cooldown_s=300)
        assert len(calls) == 1
        marker = pathlib.Path(base) / "state" / "unit-tracking" / "2017.attempt"
        old = time.time() - 400
        os.utime(marker, (old, old))
        lp._ensure_unit_tracking(base, "2017", cooldown_s=300)
        assert len(calls) == 2


def test_ensure_unit_tracking_cooldown_is_per_goal():
    """Two different goals on the same machine must not share one cooldown window -- the marker
    path is keyed on the goal stem, mirroring `_claim_marker_path`'s own per-goal shape."""
    with tempfile.TemporaryDirectory() as d:
        base = _unit_tracking_base(d)
        lp = _loop(); calls = []
        lp.feature_labels.ensure_unit_tracking = lambda *a, **k: calls.append(a[2])
        lp._ensure_unit_tracking(base, "2017")
        lp._ensure_unit_tracking(base, "2018")
        assert calls == ["2017", "2018"]


def test_ensure_unit_tracking_is_fail_open_when_the_real_check_raises(capsys, monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        base = _unit_tracking_base(d)
        lp = _loop()
        def boom(*a, **k):
            raise RuntimeError("simulated failure")
        monkeypatch.setattr(lp.feature_labels, "ensure_unit_tracking", boom)
        lp._ensure_unit_tracking(base, "2017")               # must not propagate
        assert "non-fatal" in capsys.readouterr().err


def test_note_arms_both_the_claim_and_the_unit_tracking_check(monkeypatch):
    """The real wiring: `_ARMS_CLAIM`'s trigger set now reaches BOTH side jobs from one dispatch,
    sharing the same argv shape `_ensure_claimed` already established."""
    with tempfile.TemporaryDirectory() as d:
        base = _unit_tracking_base(d)
        lp = _loop(); src = _GhLike(labels=["sdlc:goal"]); calls = []
        lp.sources.get_source = lambda *a, **k: src
        monkeypatch.setattr(lp.feature_labels, "ensure_unit_tracking",
                            lambda *a, **k: calls.append(1))
        lp.main(["loop.py", "note", base, "2017", "x"])
        assert calls == [1]
        assert src.marked == ["2017"], "the claim guard's own wiring must be unaffected"


def test_a_non_arms_claim_verb_does_not_trigger_unit_tracking_either(monkeypatch):
    """Sharing `_ARMS_CLAIM` means sharing its exclusions too -- `record` is deliberately excluded
    from that tuple (#1962: it is evidence work has ENDED, not begun), and this side job rides the
    identical trigger set for the identical reason: a verb that never arms the claim must not arm
    this side check either."""
    with tempfile.TemporaryDirectory() as d:
        base = _unit_tracking_base(d)
        lp = _loop(); calls = []
        monkeypatch.setattr(lp.feature_labels, "ensure_unit_tracking",
                            lambda *a, **k: calls.append(1))
        lp.main(["loop.py", "record", base, "2017", "pass"])
        assert calls == []


def test_ensure_unit_tracking_is_a_noop_on_a_local_goal_with_an_unsafe_stem():
    """`_unit_tracking_cooldown_path` refuses the same unsafe-goal shapes `_claim_marker_path`
    already refuses, and the fail-open outer guard turns that refusal into a quiet no-op rather
    than a crash."""
    with tempfile.TemporaryDirectory() as d:
        base = _unit_tracking_base(d)
        lp = _loop()
        lp._ensure_unit_tracking(base, "../../etc/passwd")   # must not raise


def test_ensure_ledger_delivery_is_a_noop_when_the_ledger_is_off():
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        lp.ledger.append(base, config, "note", "g.md", why="one")
        off = {"ledger": {"enabled": False, "actor": "dana"}}
        calls = []
        lp._ensure_ledger_delivery(base, off, threshold=0, publish_fn=lambda *a, **k: calls.append(1))
        assert calls == []


def test_ensure_ledger_delivery_is_a_noop_when_not_a_worktree():
    with tempfile.TemporaryDirectory() as d:
        base = _ledger_sdlc(d, enabled=True, worktree=False)      # ledger on, `sync.py init` never run
        lp = _loop(); calls = []
        lp._ensure_ledger_delivery(base, lp.state.load_config(base), threshold=0,
                                   publish_fn=lambda *a, **k: calls.append(1))
        assert calls == []


def test_ensure_ledger_delivery_reports_a_returned_failure_string_without_any_new_code(capsys):
    """Item 3's empirical question: once publish() honours its own contract (fix 1) and returns
    'publish deferred: ...' instead of raising, does _ensure_ledger_delivery's EXISTING branching
    (loop.py:140-151) already surface it? Uses a fake publish_fn that RETURNS the shape fix 1
    produces -- this does not depend on sync.py's fix being present to run."""
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        lp.ledger.append(base, config, "note", "g.md", why="one")
        lp._ensure_ledger_delivery(
            base, config, threshold=0,
            publish_fn=lambda *a, **k: "publish deferred: forced failure for #2449")
        err = capsys.readouterr().err
        assert "could not be published" in err
        assert "forced failure for #2449" in err


def test_ensure_ledger_delivery_is_fail_open_when_publish_fn_raises(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, sync, base, config = _real_ledger_clone(d)
        lp.ledger.append(base, config, "note", "g.md", why="one")
        def boom(*a, **k): raise RuntimeError("origin unreachable")
        lp._ensure_ledger_delivery(base, config, threshold=0, publish_fn=boom)   # must not propagate
        err = capsys.readouterr().err
        assert "origin unreachable" in err


# ---------------------------------------------------------------- claim lease in _next
# Two loops on one board must not start the same goal. _next reads the ledger claim lease: a goal
# another actor holds an open claim on is skipped; a goal I hold is still mine to resume. ttl_hours=0
# disables expiry so these tests are independent of wall-clock (TTL itself is unit-tested in ledger).


class _Queue:
    """Minimal source: hands out the first queued goal not in `skip`, records what it marked.

    PR #1235 review, Finding 6: `release()` is a real, recording method -- not simply absent --
    so a test asserting "no release happened" (e.g. the non-GitHub-source auto-reclaim guard) can
    positively prove the isinstance check short-circuited BEFORE calling it, rather than relying
    on an ACCIDENTAL `AttributeError` (from `release` not existing at all) that `loop.py`'s own
    outer `except Exception` fail-open handler happens to swallow either way. The real production
    non-GitHub source, `sources.LocalSource`, DOES implement `release()` -- a missing-attribute
    double was unrepresentative of the class it stood in for."""
    def __init__(self, items): self.items = list(items); self.marked = []; self.released = []
    def next_pending(self, skip=()):
        s = {str(x) for x in skip}
        return next((g for g in self.items if g not in s), None)
    def mark_in_progress(self, g): self.marked.append(g)
    def release(self, goal, reason): self.released.append((goal, reason))


def _lease_base(d, actor="me", claims=(), enabled=True, ttl_hours=0):
    """`claims` entries are `(who, goal, kind)` for a legacy, pre-#337 claim (one writer file per
    actor, 2-part id) or `(who, goal, kind, pid)` for a specific WRITER's claim (F10/#337 shape:
    its own `<who>-<pid>.jsonl` file, 3-part id) — used by #374's own tests below to simulate a
    second, distinct process of the same actor."""
    base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps(
        {"ledger": {"enabled": enabled, "actor": actor, "lease": {"ttl_hours": ttl_hours}},
         "budget": {"max_iterations": 10}}))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    ent = base / "ledger" / "entries"; ent.mkdir(parents=True)
    seqs = {}
    for claim in claims:
        who, goal, kind = claim[0], claim[1], claim[2]
        pid = claim[3] if len(claim) > 3 else None
        key = (who, pid)
        seqs[key] = seqs.get(key, 0) + 1
        fname = f"{who}-{pid}.jsonl" if pid is not None else f"{who}.jsonl"
        ident = f"{who}:{pid}:{seqs[key]}" if pid is not None else f"{who}:{seqs[key]}"
        with (ent / fname).open("a") as f:
            f.write(json.dumps({"id": ident, "ts": "2026-07-27T09:00:00Z",
                                "actor": who, "kind": kind, "goal": str(goal)}) + "\n")
    return str(base)


# --------------------------------------------------------------------- auto-unpark sweep (#1129)
# `_auto_unpark_sweep` itself: gated FIRST on `discovery.auto_unpark.mode` (default 'off', zero
# cost -- doesn't even import auto_unpark.py), fail-open on any error, and wired into `_next()`'s
# own chokepoint so every driver (CLI `next`, `next_batch`, `run_loop`) gets it for free. The real
# sweep MECHANICS (which parked issues are eligible, what gets mutated) live in
# test_auto_unpark.py -- these tests are only about the GATING and WIRING, using a fake
# `auto_unpark` module so no real `gh` call is ever at risk here.


def _with_auto_unpark(base, mode="on"):
    p = pathlib.Path(base) / "config.json"
    cfg = json.loads(p.read_text())
    cfg.setdefault("discovery", {})["auto_unpark"] = {"mode": mode}
    p.write_text(json.dumps(cfg))
    return cfg


def test_auto_unpark_sweep_is_a_no_op_and_imports_nothing_when_unconfigured(monkeypatch):
    lp = _loop()
    def boom(name):
        raise AssertionError(f"must not _load({name!r}) when discovery.auto_unpark.mode is 'off'")
    monkeypatch.setattr(lp, "_load", boom)
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])   # no discovery.auto_unpark key at all -> off
        lp._auto_unpark_sweep(base, lp.state.load_config(base))   # must not raise


def test_auto_unpark_sweep_stays_off_on_an_unrecognised_mode(monkeypatch):
    lp = _loop()
    monkeypatch.setattr(lp, "_load", lambda name: (_ for _ in ()).throw(
        AssertionError(f"must not _load({name!r})")))
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_auto_unpark(base, mode="always")   # a blocker_promotion value, not a valid one here
        lp._auto_unpark_sweep(base, lp.state.load_config(base))   # must not raise


def test_auto_unpark_sweep_calls_sweep_unpark_when_enabled(monkeypatch):
    lp = _loop()
    calls = []

    class FakeAutoUnpark:
        def sweep_unpark(self, sdlc_dir, config, apply=False, run=None, **kw):
            calls.append((sdlc_dir, apply))

    real_load = lp._load
    monkeypatch.setattr(lp, "_load",
                        lambda name: FakeAutoUnpark() if name == "auto_unpark" else real_load(name))
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_auto_unpark(base)
        lp._auto_unpark_sweep(base, lp.state.load_config(base))
    assert calls == [(base, True)]         # apply=True -- an unattended sweep has no dry-run to show


def test_auto_unpark_sweep_fails_open_when_sweep_unpark_raises(monkeypatch, capsys):
    lp = _loop()

    class FakeAutoUnpark:
        def sweep_unpark(self, *a, **k):
            raise RuntimeError("simulated transient gh failure")

    real_load = lp._load
    monkeypatch.setattr(lp, "_load",
                        lambda name: FakeAutoUnpark() if name == "auto_unpark" else real_load(name))
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_auto_unpark(base)
        lp._auto_unpark_sweep(base, lp.state.load_config(base))   # must not raise
    assert "auto-unpark sweep failed non-fatally" in capsys.readouterr().err


def test_next_runs_the_sweep_first_but_still_returns_its_own_pick(monkeypatch):
    """The GENERAL "unstick a stale park" case: a sweep that reports nothing eligible (the
    overwhelming common case — nothing parked, or nothing newly closed this pass) must leave
    picking byte-identical to no sweep ever having run; the sweep call itself never blocks or
    interferes with the pick that follows it. The #1129-followup COOLDOWN case — a sweep that DOES
    report a just-unparked ref, and that ref specifically being withheld from this SAME call's own
    pick — is `test_next_holds_a_just_unparked_goal_back_from_its_own_pick` below."""
    lp = _loop()
    order = []

    class FakeAutoUnpark:
        def sweep_unpark(self, sdlc_dir, config, apply=False, run=None, **kw):
            order.append("swept")

    real_load = lp._load
    monkeypatch.setattr(lp, "_load",
                        lambda name: FakeAutoUnpark() if name == "auto_unpark" else real_load(name))
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_auto_unpark(base)
        src = _Queue(["a", "b"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
    assert order == ["swept"]
    assert (kind, goal) == ("goal", "a") and src.marked == ["a"]


def test_next_holds_a_just_unparked_goal_back_from_its_own_pick(monkeypatch):
    """The exact scenario the #1129 review finding described: a goal was parked with a
    `blocked by #N` reference; #N has since closed, so THIS call's sweep is eligible to unpark it
    and does — `sdlc:parked` -> `sdlc:goal`, real mutation, real audit comment (proven for real
    against a fake `gh` transport in test_auto_unpark.py; here the fake `auto_unpark` module reports
    the identical `result["unparked"]` contract the real one does, so this test can assert on the
    label-flip signal without re-driving actual `gh` calls — this file's job is the WIRING, not the
    mechanics, matching the section comment above). The finding: the docstring for `_next()`
    explicitly said the unpark-then-pick happens in the SAME call, with no cooldown, so the very
    goal just re-added to the candidate pool could ALSO be the one autonomous work starts on this
    same call, before a human has any real chance to read the audit comment and react. This asserts
    BOTH halves of the fix at once: the label genuinely flips back (the sweep still runs and still
    applies, exactly as before) but this SAME `_next()` call must not also be the one that picks it
    back up — the goal becomes eligible again only from the NEXT `_next()` call onward."""
    lp = _loop()
    flipped = {}                        # stand-in for the goal's real GitHub label state post-sweep

    class FakeAutoUnpark:
        def sweep_unpark(self, sdlc_dir, config, apply=False, run=None, **kw):
            assert apply is True        # #1129's sweep is always a live apply, never a dry-run, from _next()
            flipped["42"] = {"sdlc:goal"}                  # the mutation: sdlc:parked -> sdlc:goal, for real
            return {"apply": True, "checked": 1, "eligible": 1,
                   "actions": [{"action": "add-label", "issue": "42", "detail": "sdlc:goal",
                                "result": "done", "error": None},
                               {"action": "remove-label", "issue": "42", "detail": "sdlc:parked",
                                "result": "done", "error": None}],
                   "unparked": ["42"]}   # -- the real sweep_unpark's own contract (see auto_unpark.py)

    real_load = lp._load
    monkeypatch.setattr(lp, "_load",
                        lambda name: FakeAutoUnpark() if name == "auto_unpark" else real_load(name))
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_auto_unpark(base)
        src = _Queue(["42", "other"])   # "42" is the just-unparked goal; "other" is free to pick
        kind, goal = lp._next(base, src, lp.state.load_config(base))
    assert flipped == {"42": {"sdlc:goal"}}                 # the sweep DID run and DID flip the label
    assert (kind, goal) == ("goal", "other")                # ...but this call did NOT pick "42"
    assert "42" not in src.marked                           # never even offered to mark_in_progress


def test_a_just_unparked_goal_is_pickable_on_the_very_next_next_call(monkeypatch):
    """The cooldown is scoped to exactly ONE `_next()` call, not a standing exclusion:
    `just_unparked` (see `_next()`'s docstring) is a plain local, never persisted anywhere -- so a
    second, separate `_next()` call must be free to pick the very goal the previous call's sweep
    just unparked, the instant nothing THIS pass needs holding back (the realistic case: a real
    sweep no longer even surfaces the issue as a candidate once it no longer carries `sdlc:parked`
    -- see test_auto_unpark.py's `test_sweep_unpark_apply_is_idempotent_on_a_second_pass`). Proves
    the "next `run_loop` iteration / next `next_batch` slot / separate future invocation" half of
    the docstring's claim, not just the same-call withholding the test above already covers."""
    lp = _loop()

    class FirstCallUnparks:
        def sweep_unpark(self, sdlc_dir, config, apply=False, run=None, **kw):
            return {"apply": True, "checked": 1, "eligible": 1, "actions": [], "unparked": ["42"]}

    class SecondCallFindsNothingLeftToUnpark:
        def sweep_unpark(self, sdlc_dir, config, apply=False, run=None, **kw):
            return {"apply": True, "checked": 0, "eligible": 0, "actions": [], "unparked": []}

    real_load = lp._load
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_auto_unpark(base)
        src = _Queue(["42", "other"])
        cfg = lp.state.load_config(base)

        monkeypatch.setattr(lp, "_load",
                            lambda name: FirstCallUnparks() if name == "auto_unpark" else real_load(name))
        kind1, goal1 = lp._next(base, src, cfg)
        assert (kind1, goal1) == ("goal", "other")      # cooldown holds "42" back this call

        monkeypatch.setattr(lp, "_load", lambda name: (
            SecondCallFindsNothingLeftToUnpark() if name == "auto_unpark" else real_load(name)))
        kind2, goal2 = lp._next(base, src, cfg)
        assert (kind2, goal2) == ("goal", "42")         # free again on the very next call


def test_next_picks_normally_with_auto_unpark_left_at_its_default():
    """No discovery.auto_unpark key at all (the overwhelming-majority config shape) -- _next() must
    still pick exactly as before, proving the new gate is a genuine no-op end-to-end through the
    real chokepoint, not just non-crashing in isolation."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        src = _Queue(["a"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
    assert (kind, goal) == ("goal", "a")


# ------------------------------------------------- pick-time model-tier resolution (#1627) --------
# _next() is the one chokepoint every picker (next/next_batch/run_loop) passes through, so this is
# where predict.py gets invoked from CODE for the first time -- previously its only caller anywhere
# in the repo was SKILL.md prose telling an agent to run it by hand. Gated FIRST on
# `model_selection == "auto"` (cheap, in-memory) so a repo that hasn't opted in pays nothing extra
# -- no `fetch_title_body` call (a real `gh` API call in github mode) and no subprocess spawn.


class _QueueWithTitleBody(_Queue):
    """`_Queue` plus a `fetch_title_body` a real source would have -- `title_body` is the fixed
    `{"title", "body"}` pair to return, and `calls` (if given) records every goal id it was asked
    about, so a test can assert it was (or was never) called at all."""
    def __init__(self, items, title_body, calls=None):
        super().__init__(items)
        self._title_body = title_body
        self._calls = calls

    def fetch_title_body(self, goal):
        if self._calls is not None:
            self._calls.append(goal)
        return dict(self._title_body)


class _QueueWithTitleBodyByGoal(_Queue):
    """`_QueueWithTitleBody`, but a DIFFERENT title/body per goal id -- needed to prove two goals
    in the same batch resolve to two DIFFERENT tiers, correctly paired, not just "a line appears
    per goal." `title_body_by_goal` maps goal id -> {"title", "body"}."""
    def __init__(self, items, title_body_by_goal):
        super().__init__(items)
        self._by_goal = title_body_by_goal

    def fetch_title_body(self, goal):
        return dict(self._by_goal[goal])


def _model_choice_base(d, model_selection="auto", journal=True):
    """A `.sdlc` wired for the pick-time model-resolution hook: `ledger.enabled` (realistic —
    a real repo running this feature has the ledger on too) and, separately, `journal.enabled`
    -- which is the ACTUAL gate `ledger.append`'s own EVENTS-stream branch checks, decoupled from
    `ledger.enabled` by design (see `ledger.append`'s own docstring, #244). Omitting `model_
    selection` entirely (pass `model_selection=None`) reproduces the overwhelming-majority
    unconfigured shape."""
    base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
    cfg = {"ledger": {"enabled": True, "actor": "me"}, "budget": {"max_iterations": 10}}
    if model_selection is not None:
        cfg["model_selection"] = model_selection
    if journal:
        cfg["journal"] = {"enabled": True}
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return str(base)


def _model_choice_events(base):
    """Every real `model_choice` event actually persisted under `<base>` -- the genuine, unmocked
    read-back the issue's own Definition of Done asks for ("verified by reading the ledger back"),
    not an assertion on a captured subprocess argv list.

    #2574/S1-G3: the destination moved from `<base>/ledger/events/` to `<base>/events/`, so this
    reads BOTH through the shared helper rather than naming a directory. Naming one is what made
    this helper wrong the moment the writer moved -- and it would have gone quiet, not red, on the
    `== []` assertions several of its callers make."""
    return [e for e in journal_events(_loop().ledger, base) if e.get("kind") == "model_choice"]


def test_codex_only_model_override_reaches_pick_time_without_changing_claude(tmp_path, monkeypatch):
    lp = _loop()
    base = _model_choice_base(tmp_path, model_selection="off")
    cfg_path = pathlib.Path(base) / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["model_selection_host_overrides"] = {"codex": "auto"}
    cfg_path.write_text(json.dumps(cfg))
    title = {"title": "Migrate the schema", "body": "for a new tenant"}
    monkeypatch.setenv("CODEX_THREAD_ID", "00000000-0000-4000-8000-000000000823")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("SIGMA_HOST", raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    assert lp._next(base, _QueueWithTitleBody(["a"], title), lp.state.load_config(base)) == ("goal", "a")
    assert len(_model_choice_events(base)) == 1
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert lp._next(base, _QueueWithTitleBody(["b"], title), lp.state.load_config(base)) == ("goal", "b")
    assert len(_model_choice_events(base)) == 1


def test_next_writes_a_real_model_choice_ledger_event_at_pick_time(capsys):
    """THE Definition-of-Done proof (issue #1627): a goal picked through the real `_next()`
    chokepoint writes a genuine `model_choice` ledger event with no agent told to run any command
    -- verified by reading the real ledger file back, with NO subprocess mock anywhere in this
    test. (A plan-review of an earlier draft of this fix BLOCKED it precisely because its own
    version of this test mocked `subprocess.run` -- proving only that an argv list was built, never
    that an event was actually written. This is the corrected, genuinely end-to-end version.)

    Also asserts the SUCCESS-path stderr line (a code-review finding on an earlier draft): SKILL.md
    still tells an agent to separately learn the tier for phase dispatch, and in github mode a
    manual recomputation from the bare "$goal" shares the exact bug this call exists to fix -- the
    resolved tier must be printed so an agent can read it instead of recomputing a wrong one."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        title_body = {"title": "Migrate the schema", "body": "for the new tenant model. " * 15}
        src = _QueueWithTitleBody(["a"], title_body)
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")
        events = _model_choice_events(base)
    assert len(events) == 1, events
    assert events[0]["goal"] == "a"
    assert events[0]["model"] == "opus"
    assert events[0]["signal"] == "migrat"
    err = capsys.readouterr().err
    assert "model tier for a resolved to opus" in err
    assert "signal=migrat" in err


def test_next_pick_ignores_a_haiku_stem_that_is_only_in_the_body(capsys):
    """#2827: a body-only `comment` must not route a goal to haiku at pick time. The title here
    alone routes sonnet; the body word alone used to win. Also pins the `why` line's new
    `in=<where>` field being PARSED, not swallowed into the recorded signal."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        title_body = {"title": "Fence the shared write sites",
                      "body": "The S2 comment in the writer is wrong."}
        kind, goal = lp._next(base, _QueueWithTitleBody(["a"], title_body),
                              lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")
        events = _model_choice_events(base)
    assert [(e["model"], e.get("signal")) for e in events] == [("sonnet", None)]
    assert "model tier for a resolved to sonnet" in capsys.readouterr().err


def test_next_pick_with_an_empty_title_never_promotes_the_body_first_line(capsys):
    """#2827: the combined text used to be `.strip()`ped, so an empty title made the body's first
    line the title and a body `fix typo` routed haiku."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        kind, goal = lp._next(base, _QueueWithTitleBody(["a"], {"title": "", "body": "fix typo"}),
                              lp.state.load_config(base))
        events = _model_choice_events(base)
    assert [e["model"] for e in events] == ["sonnet"]


def test_why_line_parser_keeps_a_spaced_signal_and_drops_the_location():
    """#390: `predict.py why` prints `hits=<n>` between `in=<where>` and `signal=`, always together
    with `in=` (both or neither) -- this fixture is the exact shape a 2+-hit body match prints."""
    lp = _loop()
    m = lp._WHY_LINE.match("model=opus in=body hits=2 signal=race condition")
    assert m and m.group("model") == "opus" and m.group("signal") == "race condition"
    assert m.group("where") == "body" and m.group("hits") == "2"
    m = lp._WHY_LINE.match("model=sonnet signal=")
    assert m and m.group("model") == "sonnet" and m.group("signal") == ""


def test_next_pick_skips_prediction_entirely_when_model_selection_is_not_auto():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d, model_selection=None)     # the overwhelming-majority shape
        calls = []
        src = _QueueWithTitleBody(["a"], {"title": "Migrate the schema", "body": "x"}, calls=calls)
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")
        events = _model_choice_events(base)
    assert calls == []                 # fetch_title_body never even called -- gate is first
    assert events == []


def test_next_pick_skips_prediction_entirely_when_telemetry_is_off():
    """The EVENTS stream `ledger.append` writes to is gated on `telemetry_enabled(config)` ALONE
    (decoupled from `ledger.enabled` -- see `ledger.append`'s own docstring, #244), so a repo with
    `model_selection: "auto"` but the journal off would have the write silently dropped anyway. The
    hook must check this BEFORE `fetch_title_body` (a real `gh` API call in github mode) and the
    subprocess spawn, not pay for both only to discard the result."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d, journal=False)
        calls = []
        src = _QueueWithTitleBody(["a"], {"title": "Migrate the schema", "body": "x"}, calls=calls)
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")
        events = _model_choice_events(base)
    assert calls == []
    assert events == []


def test_next_pick_refuses_an_invalid_codex_mapping_before_recording_model_choice(capsys):
    """The pure `why` classifier cannot bypass the strict host resolver at the pick-time writer."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        cfg_path = pathlib.Path(base) / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["model_host_overrides"] = {"codex": {"sonnet": {
            "model": "claude-opus-4-5", "effort": "high"}}}
        cfg_path.write_text(json.dumps(cfg))
        src = _QueueWithTitleBody(["a"], {
            "title": "Add a status line to the dashboard", "body": "x"})
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        events = _model_choice_events(base)
    assert (kind, goal) == ("goal", "a")
    assert events == []
    err = capsys.readouterr().err
    assert "Codex host mapping refused" in err
    assert "approved Codex model ID" in err


def test_next_pick_refuses_malformed_successful_codex_output_before_recording_model_choice(
        capsys, monkeypatch):
    """A zero exit alone cannot turn an unsupported host-model response into an advisory event."""
    lp = _loop()

    class Result:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def run(argv, **_kwargs):
        return Result("model=sonnet signal=\n" if argv[2] == "why"
                      else "model=unapproved-future-model effort=medium\n")

    monkeypatch.setattr(lp.subprocess, "run", run)
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        src = _QueueWithTitleBody(["a"], {"title": "Add a status line", "body": "x"})
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        events = _model_choice_events(base)
    assert (kind, goal) == ("goal", "a")
    assert events == []
    assert "Codex host mapping refused" in capsys.readouterr().err


def test_emit_refuses_an_invalid_model_choice_tier_before_writing_an_event(capsys):
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        assert lp.main(["loop.py", "emit", base, "a", "model_choice", "--model",
                        "unapproved-future-model"]) == 2
        events = _model_choice_events(base)
    assert events == []
    assert "portable model tier" in capsys.readouterr().err


def test_pick_time_codex_catalog_is_exactly_the_approved_models():
    """Both call boundaries must change with the versioned host catalog, never independently."""
    assert _loop()._CODEX_HOST_MODELS == {
        "gpt-5.5",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-6-astra",
    }


def test_next_pick_records_nothing_when_fetched_text_is_empty(monkeypatch):
    """The north-star's own non-goal ("never a fabricated reading"): a degraded/empty
    `fetch_title_body` result must not classify empty text into a confident-looking, entirely
    fabricated `sonnet` entry. Proven two ways: no subprocess spawned at all, and no ledger entry
    written. A RECORDING double, not a raising one, checked OUTSIDE any exception handler --
    `_predict_model_choice_at_pick`'s own fail-open `except Exception` would swallow a raise from
    inside it exactly as it swallows a real failure (the same reasoning test_model_predict.py's own
    #1030 section documents for `_emit_model_choice`), so a raising double would prove nothing
    about whether the call was skipped versus attempted-and-swallowed."""
    lp = _loop()
    calls = []
    monkeypatch.setattr(lp.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        src = _QueueWithTitleBody(["a"], {"title": "", "body": ""})
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")
        events = _model_choice_events(base)
    assert calls == []
    assert events == []


def test_next_pick_survives_a_source_with_no_fetch_title_body(capsys):
    """`_Queue` (the fake most `_next()` tests use) has no `fetch_title_body` at all. With
    `model_selection: "auto"` set (otherwise the gate above returns before ever reaching the
    missing method, and this test would prove nothing), the resulting AttributeError must be
    swallowed exactly like `_check_cross_repo_at_pick`'s own established fail-open shape -- the
    pick still succeeds, with one non-fatal stderr line, never a lost goal."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        src = _Queue(["a"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
    assert (kind, goal) == ("goal", "a")
    assert "model-tier prediction" in capsys.readouterr().err


def test_next_pick_survives_the_predict_subprocess_returning_nonzero(monkeypatch, capsys):
    """A non-zero exit must not just be logged as an opaque exit code -- a code-review finding on
    an earlier draft was that `capture_output=True` plus no returncode check swallowed the only
    diagnostic a real crash produces (this is exactly the shape the plan-review's own blocking
    finding 1, the `_read()` crash, hit before it was fixed). The child's own stderr tail must
    reach the operator."""
    lp = _loop()

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "Traceback (most recent call last):\nOSError: boom\n"
    monkeypatch.setattr(lp.subprocess, "run", lambda *a, **k: _Result())
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        src = _QueueWithTitleBody(["a"], {"title": "Migrate the schema", "body": "x" * 300})
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")
        events = _model_choice_events(base)
    assert "OSError: boom" in capsys.readouterr().err
    assert events == []


def test_next_pick_survives_an_unparseable_why_output(monkeypatch):
    lp = _loop()

    class _Result:
        returncode = 0
        stdout = "not the expected shape at all"
        stderr = ""
    monkeypatch.setattr(lp.subprocess, "run", lambda *a, **k: _Result())
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        src = _QueueWithTitleBody(["a"], {"title": "Migrate the schema", "body": "x" * 300})
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")
        events = _model_choice_events(base)
    assert events == []


def test_next_batch_stderr_lines_stay_attributed_to_their_own_goal(capsys):
    """BR-8/BR-13 (#2544 research): next_batch() calls _next() once per goal, and each call
    independently resolves+prints THAT goal's own tier -- but nothing before this test proved the
    N stderr lines a multi-goal batch produces stay correctly PAIRED with their own goal, rather
    than colliding. This is the property picking.md's goal-slot capture instruction depends on:
    match a stderr line to a goal by the goal id the line names, not by position. Two goals with
    two DIFFERENTLY-signalled texts must produce two distinctly-texted lines and two correctly
    paired ledger events. Fixture text for goal "b" verified empirically against the real
    predict.py (`python3 skills/sigma-model/scripts/predict.py why "Add a status line to the
    dashboard\n\nx" .sdlc` -> `model=sonnet signal=`) -- an earlier draft used "Fix a typo in the
    README", which actually resolves to `haiku` ("typo" is a listed haiku signal,
    skills/sigma-model/scripts/predict.py:128) and would have made this test's own assertions
    wrong from the start; caught in plan-review, not left for Implement to discover."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        src = _QueueWithTitleBodyByGoal(["a", "b"], {
            "a": {"title": "Migrate the schema", "body": "for the new tenant model. " * 15},
            "b": {"title": "Add a status line to the dashboard", "body": "x"},
        })
        picks = lp.next_batch(base, src, lp.state.load_config(base), max_concurrent=2)
        events = _model_choice_events(base)
    assert [p for p in picks] == [("goal", "a"), ("goal", "b")]
    by_goal = {e["goal"]: e["model"] for e in events}
    assert by_goal["a"] == "opus"      # "migrat" signal
    assert by_goal["b"] == "sonnet"    # unsignalled default -- verified empirically, see docstring
    err = capsys.readouterr().err
    assert "model tier for a resolved to opus" in err
    assert "model tier for b resolved to sonnet" in err


# ---------------------------------------------------------- auto-reclaim stale claims (#1198)
# `_fetch_pending` (sources.py) now excludes any GitHub issue carrying `in_progress_label`
# unconditionally -- closing the gap where `mark_in_progress` wrote the label on every pick and
# the picker never read it back. That exclusion alone would leave a genuinely CRASHED run's goal
# permanently unpickable (claimed, never completed/parked/failed/released), so `_next()` also
# runs a reconciliation sweep, gated on the EXISTING `ledger.enabled` switch (default off, no new
# config key): a claim the ledger itself already treats as expired past `ledger.lease.ttl_hours`
# is released via the sanctioned `_release()` path -- label stripped, board card back in Ready,
# one audit-trail comment -- putting the goal back in the ordinary candidate pool. Mirrors
# `_auto_unpark_sweep`'s own cooldown: the goal a call just reclaimed is withheld from that SAME
# call's own pick, free again from the very next call.


def _stale_ts(hours_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours_ago * 3600))


def _github_ledger_base(d, claims=(), ttl_hours=None, enabled=True, actor="me"):
    """A `.sdlc` rigged for a GitHub-backed, ledger-enabled install. `claims` are `(actor, goal,
    kind, ts)` tuples written straight into the ledger's own entries stream -- like `_lease_base`
    above, but with an explicit, injectable `ts` since these tests need to control claim AGE
    precisely, not just presence. `actor` (PR #1235 review, Finding 1/3) is the config's OWN
    `ledger.actor` -- the identity the running sweep resolves to -- kept independent of each
    claim's own recorded actor so a test can rig a genuinely CROSS-actor scenario (a claim
    written by 'alice', reclaimed by a sweep configured as 'bob'). Defaults to 'me' so every
    existing same-actor caller is unaffected."""
    base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
    cfg = {"discovery": {"source": "github", "github": {"repo": "acme/widget"}},
          "ledger": {"enabled": enabled, "actor": actor},
          "budget": {"max_iterations": 10}}
    if ttl_hours is not None:
        cfg["ledger"]["lease"] = {"ttl_hours": ttl_hours}
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    ent = base / "ledger" / "entries"; ent.mkdir(parents=True)
    with (ent / "me.jsonl").open("a") as f:
        for i, (actor, goal, kind, ts) in enumerate(claims):
            f.write(json.dumps({"id": f"{actor}:{i}", "ts": ts, "actor": actor, "kind": kind,
                                "goal": str(goal)}) + "\n")
    return str(base)


def _in_progress_gh_run(issues, calls=None):
    """Fake `gh` transport: `issue list` returns `issues` verbatim, `issue view` reports the
    first issue OPEN with its current labels (`release()`'s own terminal-state probe), everything
    else a no-op success -- records every call so a test can assert on the label removal / audit
    comment `release()` performs.

    PR #1235 review, Finding 5: `issue edit ... --remove-label <label>` now genuinely MUTATES the
    matching entry in `issues` (matched by number), in place -- a real `gh issue edit` call has
    exactly this effect on the real issue's live label set. Before this fix the mock was static:
    a `release()` call logged the mutation attempt in `calls` but the `issues` list itself never
    changed, so `source.next_pending()`'s own label filter kept excluding the goal independently
    of whatever the caller's `skip` set (the `just_reclaimed` cooldown) said -- a cooldown-wiring
    test built on this fixture could pass even with the cooldown deleted entirely, since the
    STATIC label alone already excluded the goal. Making the mutation real is what lets a
    same-call re-offer actually happen absent the cooldown, so the cooldown assertion depends on
    the cooldown."""
    calls = calls if calls is not None else []
    def run(args):
        calls.append(list(args))
        # #1829: `_fetch_pending` now reads through `gh api repos/{owner}/{repo}/issues`
        # (REST) rather than `gh issue list` (graphql-search-billed) -- recognize either shape.
        is_issues_list = ((len(args) > 1 and args[0] == "issue" and args[1] == "list") or
                          (len(args) > 1 and args[0] == "api" and str(args[1]).startswith("repos/")
                           and str(args[1]).endswith("/issues")))
        verb = "list" if is_issues_list else (args[1] if len(args) > 1 else args[0])
        if verb == "list":
            return json.dumps(issues)
        if verb == "view":
            return json.dumps({"state": "OPEN", "labels": issues[0]["labels"]})
        legacy = _gqlfake.legacy_of(args)       # #895 slice 3a: release()'s REST writes -> legacy records
        if legacy:
            calls.pop()
            calls.extend(legacy)
            args = legacy[0]
            verb = args[1]
        if verb == "edit" and "--remove-label" in args:
            number = args[2]
            label = args[args.index("--remove-label") + 1]
            for issue in issues:
                if str(issue.get("number")) == str(number):
                    issue["labels"] = [l for l in issue["labels"] if l.get("name") != label]
        return "[]" if legacy else ""
    run.calls = calls
    return run


def test_auto_reclaim_is_a_noop_when_the_ledger_is_off():
    """#1198 AC5: gated on the EXISTING `ledger.enabled` switch (default off) -- with no ledger
    there is no durable claim timestamp to age a label against, so nothing here can tell "crashed"
    from "still being worked". No ledger claim can even be expired; no gh mutation is attempted.

    PR #1235 review, Finding 4: the original fixture called `_github_ledger_base(d, enabled=False)`
    with the DEFAULT `claims=()` -- no ledger entry existed at all, so `ledger.expired_claims()`
    returned `{}` regardless of whether the `ledger.enabled(config)` gate was even checked; the
    assertion passed just as well with that gate deleted from the implementation entirely. A
    genuinely EXPIRED claim (the same shape the sibling on-tests below use) is now present on
    disk -- raw ledger files are written directly by this fixture, independent of `enabled`, so
    this is a real claim the gate must actively suppress, not an empty read the gate is vacuously
    consistent with."""
    lp = _loop()
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "42", "claimed", _stale_ts(13))], ttl_hours=12, enabled=False)
        cfg = lp.state.load_config(base)
        calls = []
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues, calls), sdlc_dir=base)
        reclaimed = lp._auto_reclaim_stale_claims(base, src, cfg)
    assert reclaimed == frozenset()
    assert not any(c[0:2] == ["issue", "edit"] for c in calls)


def test_auto_reclaim_is_a_noop_for_a_non_github_source():
    """`LocalSource`/`_Queue`-style sources have no `in_progress_label` concept at all
    (`LocalSource.mark_in_progress` writes local `state.json`, not a GitHub label) -- nothing here
    to reclaim, even with the ledger on and a genuinely expired claim.

    PR #1235 review, Finding 6: asserts `release()` was never called at all (positive proof the
    `isinstance(source, sources.GitHubSource)` guard short-circuited and returned early), not just
    that the overall call happened not to raise. Before this, `_Queue` had no `release` method,
    so with the isinstance guard removed the call would hit `source.release(...)` -> `AttributeError`
    -> `_auto_reclaim_stale_claims`'s own outer fail-open `except Exception` -> `frozenset()`
    anyway -- the SAME return value, for the wrong reason, masking a missing guard rather than
    proving one fired."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "42", "claimed", _stale_ts(13))], ttl_hours=12)
        cfg = lp.state.load_config(base)
        src = _Queue(["42"])
        reclaimed = lp._auto_reclaim_stale_claims(base, src, cfg)
    assert reclaimed == frozenset()
    assert src.released == []


def test_auto_reclaim_releases_a_claim_aged_past_the_ttl_with_an_audit_comment():
    lp = _loop()
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "42", "claimed", _stale_ts(13))], ttl_hours=12)
        cfg = lp.state.load_config(base)
        calls = []
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues, calls), sdlc_dir=base)
        reclaimed = lp._auto_reclaim_stale_claims(base, src, cfg)
        # PR #1235 review, test-quality fix (discovered verifying Finding 1/3's own repro): this
        # read MUST happen before `TemporaryDirectory` tears down `base` -- `read_all()` on an
        # already-deleted directory silently returns `[]`, which made this assertion pass
        # UNCONDITIONALLY regardless of whether the release actually cleared the ledger's own
        # claim view (confirmed: it still passed against the pre-#1235-review-fix code, where the
        # claim in fact stayed open). Capturing it here, inside the block, is what makes it real.
        still_open = lp.ledger.open_claims(lp.ledger.read_all(base))

    assert reclaimed == frozenset({"42"})
    remove_calls = [c for c in calls if c[0:2] == ["issue", "edit"] and "--remove-label" in c]
    assert any("sdlc:in-progress" in c for c in remove_calls)
    comments = [c for c in calls if len(c) > 1 and c[1] == "comment"]
    assert len(comments) == 1
    body = comments[0][comments[0].index("--body") + 1]
    assert "Released by Sigma" in body and "lease TTL" in body
    # the SANCTIONED release path (loop.py's own `_release`), not a bare `source.release()` --
    # also lands a `release` ledger entry, so the reclaim is indistinguishable in the ledger's own
    # audit trail from one a human triggered by hand.
    assert still_open == {}


def test_auto_reclaim_leaves_a_claim_still_within_the_ttl_untouched():
    lp = _loop()
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "42", "claimed", _stale_ts(1))], ttl_hours=12)   # 1h old, well within
        cfg = lp.state.load_config(base)
        calls = []
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues, calls), sdlc_dir=base)
        reclaimed = lp._auto_reclaim_stale_claims(base, src, cfg)
    assert reclaimed == frozenset()
    assert not any(c[0:2] == ["issue", "edit"] for c in calls)


def test_auto_reclaim_fails_open_when_the_ledger_read_raises(monkeypatch, capsys):
    lp = _loop()
    def boom(sdlc_dir):
        raise RuntimeError("simulated disk error")
    monkeypatch.setattr(lp.ledger, "read_all", boom)
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "42", "claimed", _stale_ts(13))], ttl_hours=12)
        cfg = lp.state.load_config(base)
        issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues), sdlc_dir=base)
        reclaimed = lp._auto_reclaim_stale_claims(base, src, cfg)   # must not raise
    assert reclaimed == frozenset()
    assert "reclaim failed non-fatally" in capsys.readouterr().err


def test_auto_reclaim_durably_clears_a_different_actors_stale_claim():
    """PR #1235 review, Finding 1/3: `_release()` self-attributed its `release` ledger entry to
    the RUNNING process's own actor (`ledger.safe_append`'s default `who = actor(config, run)`),
    never to the actor whose claim was actually being reclaimed. `ledger._held()`'s own
    actor-scoped pop (#1107) only ends a lease when the terminal entry's OWN actor matches
    whoever currently holds it -- so a cross-actor reclaim (the norm for any real multi-person
    ledger; README.md's own "one file per person" design) never actually cleared
    `open_claims()`'s view: the claim stayed held under its ORIGINAL actor forever. Reproduces
    the exact scenario: a claim originally written by 'alice', reclaimed by a sweep whose OWN
    configured `ledger.actor` is a different actor, 'bob'."""
    lp = _loop()
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("alice", "42", "claimed", _stale_ts(13))], ttl_hours=12, actor="bob")
        cfg = lp.state.load_config(base)
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues), sdlc_dir=base)
        reclaimed = lp._auto_reclaim_stale_claims(base, src, cfg)
        # captured INSIDE the block -- `read_all()` on a directory `TemporaryDirectory` has
        # already torn down silently returns `[]`, which would make this pass vacuously.
        still_open = lp.ledger.open_claims(lp.ledger.read_all(base))
    assert reclaimed == frozenset({"42"})
    # the critical assertion: the ledger's own open-claims view is DURABLY clear, not merely
    # reported as reclaimed this one call -- this stayed {"42": "alice"} pre-fix, since the
    # release entry landed self-attributed to "bob", not "alice".
    assert still_open == {}


def test_auto_reclaim_does_not_repost_a_duplicate_comment_on_a_second_sweep():
    """The direct, user-visible consequence of Finding 1/3: because the ledger's own view was
    never actually cleared pre-fix, a second, independent sweep call (exactly what the very next
    `_next()`/`next_batch` invocation runs) re-discovered the SAME 'expired' claim and posted a
    SECOND duplicate 'Released by Sigma' audit comment on the same open GitHub issue -- and
    would keep doing so on every future call, forever, for the whole lifetime of a genuinely
    cross-actor stale claim."""
    lp = _loop()
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("alice", "42", "claimed", _stale_ts(13))], ttl_hours=12, actor="bob")
        cfg = lp.state.load_config(base)
        calls = []
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues, calls), sdlc_dir=base)
        first = lp._auto_reclaim_stale_claims(base, src, cfg)
        second = lp._auto_reclaim_stale_claims(base, src, cfg)
    assert first == frozenset({"42"})
    assert second == frozenset()                      # nothing left to reclaim on sweep 2
    comments = [c for c in calls if len(c) > 1 and c[1] == "comment"]
    assert len(comments) == 1                          # exactly one audit comment, not one per sweep


def test_auto_reclaim_does_not_wipe_the_same_actors_live_reclaim_mid_sweep(monkeypatch):
    """PR #1269 review, blocking finding 1, end to end through the real sweep (not just at the
    `ledger._held()` unit level -- see `test_a_stale_reclaimed_actor_release_does_not_wipe_the_
    same_actors_live_reclaim_by_another_run` in tests/test_ledger.py for that half). This sweep
    (`_auto_reclaim_stale_claims`) snapshots `ledger.expired_claims()`, THEN calls
    `_goal_has_registered_worker` (#1284: the per-goal check the sweep now uses, same as
    `_claimed_goal_has_live_worker` before it -- a real `gh`-touching check, non-instant) before it finally
    writes its `release(reclaimed_actor=...)` entry -- a genuine window. This reproduces amy's own
    OTHER live process legitimately re-claiming the goal, under a NEW `run_id`, inside that exact
    window: the live-worker check is monkeypatched to append a fresh `claimed` entry (run 'run-2',
    actor 'amy' -- a SEPARATE physical process's own config, deliberately NOT the sweep's own
    `cfg`, exactly as a genuinely concurrent process would use its own) as a side effect before
    returning `False` ('no live-worker MARKER, proceed to release') -- deterministic stand-in for
    the timing-dependent race the review names as 'not hypothetical', since two concurrent
    sessions sharing one login is this repo's own concurrent-drain shape. The sweep itself runs as
    a DIFFERENT nominal actor ('sweep-actor', matching the established alice/bob convention
    `test_auto_reclaim_durably_clears_a_different_actors_stale_claim` above already uses) so this
    isolates the `reclaimed_actor`/`reclaimed_run` branch specifically, not the primary
    (actor, run)-scoped branch `test_two_concurrent_processes_of_the_same_actor_do_not_race_via_a_
    stale_terminal_entry` (tests/test_ledger.py) already covers. The sweep must still release the
    STALE lease (run-1) -- it has no way to know a race happened -- but the release it writes must
    name that stale run as `reclaimed_run`, so `ledger._held()` leaves run-2's live reclaim
    untouched."""
    lp = _loop()
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("amy", "42", "claimed", _stale_ts(13))], ttl_hours=12, actor="sweep-actor")
        # rig the stale claim's own run_id (the helper's `claims` tuples carry no run_id slot).
        entries_path = pathlib.Path(base) / "ledger" / "entries" / "me.jsonl"
        rigged = [json.loads(line) for line in entries_path.read_text().splitlines() if line.strip()]
        rigged[0]["run_id"] = "run-1"
        entries_path.write_text("\n".join(json.dumps(e) for e in rigged) + "\n")
        cfg = lp.state.load_config(base)

        def race_then_no_marker(sdlc_dir, goal, config):
            amys_own_cfg = {"ledger": {"enabled": True, "actor": "amy"}}   # amy's OTHER process
            lp.ledger.safe_append(sdlc_dir, "claimed", goal, amys_own_cfg, run_id="run-2")
            return False
        monkeypatch.setattr(lp, "_goal_has_registered_worker", race_then_no_marker)

        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues), sdlc_dir=base)
        reclaimed = lp._auto_reclaim_stale_claims(base, src, cfg)
        still_open = lp.ledger.open_claims(lp.ledger.read_all(base))
    assert reclaimed == frozenset({"42"})       # the sweep still acts -- it releases the stale lease
    assert still_open == {"42": "amy"}           # but the live reclaim under run-2 survives intact


def test_auto_reclaim_does_not_reclaim_a_stale_lease_with_a_live_worker_present():
    """PR #1235 review, round 2: elapsed lease time alone was being treated as proof of
    abandonment, with zero cross-check against whether a genuine long-lived worker was still on
    the goal -- the same double-work collision class #1197 documents for the picker's own claim
    check (a since-exited picker-process pid mistaken for the worker's own liveness), reached here
    via wall-clock age instead of a dead-pid misread: a lease past the TTL that a real subagent is
    still actively working would get reclaimed out from under it -- label stripped, board card
    back in Ready, goal handed to a second `_next()` call while the first is still mid-flight.
    Reproduces the corroborated case: the SAME claim shape every sibling test above ages past the
    TTL (13h old against a 12h TTL), but this time with a genuine `agent_start` marker registered
    for the goal, using THIS test process's own (unambiguously alive) pid -- exactly what SKILL.md
    step 3a's real `agent-start --pid $PPID` does after a real dispatch. Must NOT reclaim."""
    lp = _loop()
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "42", "claimed", _stale_ts(13))], ttl_hours=12)
        cfg = lp.state.load_config(base)
        cfg["agent_watch"] = {"enabled": True}
        lp.agent_start(base, "42", os.getpid(), cfg)   # this test process: genuinely alive
        calls = []
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues, calls), sdlc_dir=base)
        reclaimed = lp._auto_reclaim_stale_claims(base, src, cfg)
        still_open = lp.ledger.open_claims(lp.ledger.read_all(base))
    assert reclaimed == frozenset()
    assert not any(c[0:2] == ["issue", "edit"] for c in calls)      # label left alone
    assert still_open == {"42": "me"}                                # claim left alone in the ledger


def test_auto_reclaim_still_reclaims_a_stale_lease_with_no_live_worker():
    """The existing case, pinned so the fix above cannot silently widen into "never reclaim
    anything": the identical stale claim, `agent_watch` left off (the default -- no live-worker
    marker registered at all, so `_claimed_goal_has_live_worker` reports False and the TTL-only
    diagnosis stands), must still be reclaimed exactly as it was before this round's fix."""
    lp = _loop()
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "42", "claimed", _stale_ts(13))], ttl_hours=12)
        cfg = lp.state.load_config(base)
        calls = []
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues, calls), sdlc_dir=base)
        reclaimed = lp._auto_reclaim_stale_claims(base, src, cfg)
        still_open = lp.ledger.open_claims(lp.ledger.read_all(base))
    assert reclaimed == frozenset({"42"})
    assert any(c[0:2] == ["issue", "edit"] and "--remove-label" in c for c in calls)
    assert still_open == {}


def test_next_holds_a_just_reclaimed_goal_back_from_its_own_pick():
    """The SAME cooldown reasoning `_auto_unpark_sweep` established: a goal this call's own
    reclaim just made pickable again must not ALSO be the one this SAME call's pick returns."""
    lp = _loop()
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]},
              {"number": 43, "labels": [{"name": "sdlc:goal"}]}]
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "42", "claimed", _stale_ts(13))], ttl_hours=12)
        cfg = lp.state.load_config(base)
        calls = []
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues, calls), sdlc_dir=base)
        src._BACKLOG_READ_RETRY_BASE = 0
        kind, goal = lp._next(base, src, cfg)
    assert (kind, goal) == ("goal", "43")    # 42 reclaimed this call, but withheld from its own pick
    assert any(c[0:2] == ["issue", "edit"] and "--remove-label" in c for c in calls)   # yet it DID fire


def test_a_just_reclaimed_goal_is_pickable_on_the_very_next_next_call():
    """PR #1235 review, Finding 1/2 (test-quality): the original version of this test hand-set
    `issues[0]["labels"] = [{"name": "sdlc:goal"}]` between the two `_next()` calls instead of
    letting the first call's OWN reclaim sweep produce that state -- which made it pass just as
    well with `_auto_reclaim_stale_claims` (and its `_next()` wiring) deleted from loop.py
    entirely (empirically confirmed: reverting loop.py to origin/main and re-running this test
    still passes 'live', pre-fix). Fixed by asserting on `calls` -- proof the first `_next()`'s
    own reclaim actually invoked the sanctioned `_release()` -> `source.release()` path, which is
    what mutates the shared `issues` list in place (`_in_progress_gh_run`'s own real
    `--remove-label` mutation, PR #1235 review Finding 5) -- and by reusing that SAME
    now-mutated `issues` list for the second `GitHubSource`/`_next()` call instead of faking it.
    With the mechanism deleted, `calls` never gets a `--remove-label` entry for 42, `issues[0]`
    keeps its `sdlc:in-progress` label, and the second call reports DONE (both issues excluded/
    already claimed) instead of picking 42 back up -- so both assertions below go red without
    the real fix, not just the final one."""
    lp = _loop()
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]},
              {"number": 43, "labels": [{"name": "sdlc:goal"}]}]
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "42", "claimed", _stale_ts(13))], ttl_hours=12)
        cfg = lp.state.load_config(base)
        calls = []
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues, calls), sdlc_dir=base)
        src._BACKLOG_READ_RETRY_BASE = 0
        kind1, goal1 = lp._next(base, src, cfg)
        assert (kind1, goal1) == ("goal", "43")

        # Proof the first call's OWN reclaim sweep actually fired the real release mechanism on
        # goal 42 -- not assumed, not hand-faked.
        remove_calls = [c for c in calls if c[0:2] == ["issue", "edit"] and "--remove-label" in c]
        assert any("42" in c and "sdlc:in-progress" in c for c in remove_calls)
        # That real call is what left `issues[0]` in this state -- the mock mutates in place.
        assert issues[0]["labels"] == [{"name": "sdlc:goal"}]

        # A genuinely fresh GitHubSource, matching how a separate later `next`/`next-batch`
        # invocation would actually run -- reading the SAME (now genuinely mutated) `issues`.
        src2 = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues), sdlc_dir=base)
        src2._BACKLOG_READ_RETRY_BASE = 0
        kind2, goal2 = lp._next(base, src2, cfg)
    assert (kind2, goal2) == ("goal", "42")   # free again -- not permanently unpickable


def test_next_skips_a_goal_another_loop_holds_and_takes_the_next():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[("other", "a", "claimed")])
        lp = _loop(); src = _Queue(["a", "b", "c"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "b") and src.marked == ["b"]     # "a" is other's; took "b"
        assert lp.ledger.open_claims(lp.ledger.read_all(base))["b"] == "me"   # and claimed it myself


def test_next_resumes_a_goal_this_actor_already_holds():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[("me", "a", "claimed")])
        lp = _loop(); src = _Queue(["a", "b"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")             # my own claim is not a lock against me


def test_a_released_claim_no_longer_blocks_selection():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[("other", "a", "claimed"), ("other", "a", "done")])
        lp = _loop(); src = _Queue(["a"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")             # other finished it → free again


def test_next_reports_done_when_every_goal_is_held_elsewhere():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[("o1", "a", "claimed"), ("o2", "b", "claimed")])
        lp = _loop(); src = _Queue(["a", "b"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("DONE", None) and src.marked == []       # nothing free for me


# --------------------------------------------------------------------- goal-level batch (#375)
# One managing session fills up to parallel.goals.max_concurrent slots per pass, each destined for
# its own worktree subagent -- mirrors slices.py's wave-dispatch SHAPE (compute in Python, dispatch
# in the skill), but goals need no file-conflict detection: each gets its own worktree+branch+PR.


def test_goals_parallel_is_off_by_default():
    assert _loop().goals_parallel({}) == (False, 3)


def test_goals_parallel_reads_enabled_and_max_concurrent():
    cfg = {"parallel": {"goals": {"enabled": True, "max_concurrent": 5}}}
    assert _loop().goals_parallel(cfg) == (True, 5)


def test_goals_parallel_requires_strict_true_not_truthy():
    lp = _loop()
    for value in ("yes", 1, "true"):
        cfg = {"parallel": {"goals": {"enabled": value}}}
        assert lp.goals_parallel(cfg)[0] is False, value


def test_goals_parallel_falls_back_to_the_default_on_a_bad_cap():
    cfg = {"parallel": {"goals": {"enabled": True, "max_concurrent": "oops"}}}
    assert _loop().goals_parallel(cfg) == (True, 3)


def test_goals_parallel_is_independent_of_the_slices_parallel_block():
    """A repo with slice-level parallel.enabled on must not silently also enable goal-level
    parallelism -- they are sibling, independently-opted-in blocks, not one shared switch."""
    cfg = {"parallel": {"enabled": True, "max_concurrent": 4}}   # slices.py's own block, untouched
    assert _loop().goals_parallel(cfg) == (False, 3)


def test_next_batch_fills_up_to_the_cap_with_distinct_goals():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        lp = _loop(); src = _Queue(["a", "b", "c", "d"])
        picks = lp.next_batch(base, src, lp.state.load_config(base), max_concurrent=3)
        assert picks == [("goal", "a"), ("goal", "b"), ("goal", "c")]
        assert src.marked == ["a", "b", "c"]              # each one genuinely, durably claimed


def test_next_batch_stops_early_and_reports_done_when_the_backlog_runs_out():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        lp = _loop(); src = _Queue(["a", "b"])              # only 2 available, cap asks for 5
        picks = lp.next_batch(base, src, lp.state.load_config(base), max_concurrent=5)
        assert picks == [("goal", "a"), ("goal", "b"), ("DONE", None)]


def test_next_batch_stops_at_the_remaining_iteration_budget_even_with_room_in_max_concurrent():
    """A single pass must not dispatch more goals than max_iterations allows for the WHOLE run,
    even though _budget_spent's cursor only advances on completion (not on a mere pick) -- without
    an explicit remaining-budget cap, nothing would stop a burst past the configured ceiling within
    one batch (found while testing an earlier, wrong assumption about this — see the commit)."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "budget": {"max_iterations": 2}}))
        lp = _loop(); src = _Queue(["a", "b", "c", "d"])
        picks = lp.next_batch(base, src, lp.state.load_config(base), max_concurrent=5)
        assert picks == [("goal", "a"), ("goal", "b")]     # capped at 2, not the full max_concurrent=5


def test_next_batch_reports_budget_immediately_when_it_is_already_fully_spent():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "budget": {"max_iterations": 2}}))
        (pathlib.Path(base) / "state" / "STATE.md").write_text(
            "iteration: 0\nrun_iteration: 2\nlast_run: none\n")
        # cursor already at the 2-goal cap -- genuinely spent (F18/#349: 0 now means "unlimited",
        # not "already spent", so this fixture can no longer use 0 to fake exhaustion)
        lp = _loop(); src = _Queue(["a", "b", "c", "d"])
        picks = lp.next_batch(base, src, lp.state.load_config(base), max_concurrent=5)
        # #411: the terminal tuple's second element now names the tripped ceiling.
        assert picks == [("BUDGET", "2 admitted iterations >= max_iterations 2")]   # not even the first slot fills


def test_next_batch_defaults_to_a_single_item_when_goal_parallelism_is_off():
    """OFF (the default -- a repo that hasn't opted in) must be byte-identical to calling _next()
    once, not a behavior change for every existing repo that never touched parallel.goals."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        lp = _loop(); src = _Queue(["a", "b", "c"])
        picks = lp.next_batch(base, src, lp.state.load_config(base))   # no explicit cap
        assert picks == [("goal", "a")]


# --- #2521: HANDOFF is a third terminal kind `_next()`/`next_batch()` can return, checked before
# BUDGET -- a deliberate, healthy context-bound stop for the ORCHESTRATING session, never a problem.


def test_next_returns_handoff_at_the_default_goal_count_with_no_config():
    """Mirrors test_next_batch_reports_budget_immediately_when_it_is_already_fully_spent's own
    shape, for the new default-on ceiling. No goal is claimed once the ceiling has tripped -- the
    same "nothing mutates before a gate whose whole job is to stop the run" property BUDGET already
    has (loop.py's own BUDGET-gate comment, immediately above the new HANDOFF gate). No `budget`
    key at all, so this isolates HANDOFF's own default-on behavior from BUDGET entirely -- see the
    dedicated precedence test below for the case where both are configured and genuinely tie."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}}}))
        lp = _loop()
        for _ in range(20):
            lp.state.advance_cursor(base, "goal")
        src = _Queue(["a", "b"])
        kind, reason = lp._next(base, src, lp.state.load_config(base), session_pid=12345)
        assert kind == "HANDOFF"
        assert reason == "20 admitted goals >= handoff.after_goals 20"
        assert src.marked == []          # nothing claimed once the ceiling tripped


def test_next_handoff_wins_over_budget_on_every_default_configured_install():
    """Plan-review round 1, finding 4, made explicit and pinned rather than left implicit:
    `handoff.after_goals` (default 20) and `budget.max_iterations` (also 20 in the SHIPPED
    template, config.json.tmpl:60) are the SAME number by construction -- see Top Risk 5 -- so on
    every default-configured install, from this change on, `_next()` reports HANDOFF, never
    BUDGET, the moment the 20th goal is claimed; a genuine `max_iterations` overrun (>20 goals,
    HANDOFF disabled) is the only way BUDGET's iteration ceiling is still observed directly. This
    is INTENTIONAL: `_BUDGET_PAUSE`/`_HANDOFF_PAUSE` are both 60s (supervise_classify.py) so the
    supervisor behaves identically either way, and "handoff" is the more specific, more accurate
    reason for why a healthy run stopped at 20 goals.

    Plan-review round 2, finding 6 (should-fix): an EARLIER draft of this test passed an entirely
    EMPTY config, relying on the (wrong) assumption that an absent `budget` block still reads as
    max_iterations=20 -- it does not (`_budget_reason` has no fallback for an absent key, unlike
    `_handoff_reason`'s own default), so that version could never exercise a genuine tie. Fixed by
    constructing the tie EXPLICITLY, mirroring what the shipped `config.json.tmpl` actually writes
    to a real installed config.json for both keys."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "budget": {"max_iterations": 20}, "handoff": {"after_goals": 20}}))
        lp = _loop()
        for _ in range(20):
            lp.state.advance_cursor(base, "goal")
        src = _Queue(["a"])
        kind, reason = lp._next(base, src, lp.state.load_config(base), session_pid=12345)
        assert kind == "HANDOFF", (
            f"expected the default-configured tie to resolve to HANDOFF (the intended precedence), "
            f"got {kind!r} -- if this is failing, either the precedence order in _next() changed or "
            f"the two defaults have drifted apart; either way, update this test's docstring and Top "
            f"Risk 5 to match, do not just make it pass")
        assert "handoff" in reason


def test_next_batch_stops_the_batch_on_a_handoff_tuple():
    """Characterization test proving `next_batch`'s existing `if kind != "goal":` dispatch is
    already generic enough to need NO code change for a third terminal kind -- this is the actual
    regression guard for that claim, not merely an assertion in a docstring."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "handoff": {"after_goals": 3}}))
        lp = _loop()
        for _ in range(3):
            lp.state.advance_cursor(base, "goal")
        src = _Queue(["a", "b", "c"])
        picks = lp.next_batch(base, src, lp.state.load_config(base), max_concurrent=3, session_pid=12345)
        assert picks[-1][0] == "HANDOFF"


def test_next_batch_stops_at_the_remaining_handoff_budget_even_with_room_in_max_concurrent():
    """#2521, plan-review round 1 finding 3: mirrors test_next_batch_stops_at_the_remaining_
    iteration_budget_even_with_room_in_max_concurrent exactly, for the NEW ceiling -- proves
    next_batch's own pre-loop shrink, not just _next()'s per-call check, respects
    handoff.after_goals. Without this fix, 3 already-recorded goals + max_concurrent=5 against
    after_goals=5 would claim 5 MORE goals in one call (8 total) before ever reporting HANDOFF,
    instead of claiming only the 2 actually remaining."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "handoff": {"after_goals": 5},
             "work": {"enabled": True}, "parallel": {"goals": {"enabled": True, "max_concurrent": 5}}}))
        lp = _loop()
        for _ in range(3):
            lp.state.advance_cursor(base, "goal")   # 3 goals already recorded this run; 2 remain before HANDOFF
        src = _Queue(["a", "b", "c", "d", "e"])
        picks = lp.next_batch(base, src, lp.state.load_config(base), session_pid=12345)
        goal_picks = [p for p in picks if p[0] == "goal"]
        assert len(goal_picks) <= 2, (
            f"expected next_batch to shrink max_concurrent to the 2 goals actually remaining before "
            f"handoff.after_goals=5, got {len(goal_picks)} goal picks -- the pre-loop shrink is not "
            f"consulting _handoff_ceiling the way it already consults budget.max_iterations")


def test_handoff_refill_counts_claimed_goals_before_admitting_another():
    """The documented next-batch -> record -> next refill must not exceed after_goals.

    A completed slot can be refilled while sibling slots remain active. Counting only
    run_iteration admits a sixth goal against a five-goal ceiling in this sequence.
    """
    class CompletingQueue(_Queue):
        def complete(self, goal):
            self.items.remove(goal)

    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "handoff": {"after_goals": 5},
             "work": {"enabled": True}, "parallel": {"goals": {"enabled": True, "max_concurrent": 5}}}))
        lp = _loop()
        for _ in range(3):
            lp.state.advance_cursor(base, "goal")
        source = CompletingQueue(["a", "b", "c", "d"])
        config = lp.state.load_config(base)
        session_pid = os.getpid()
        picks = lp.next_batch(base, source, config, session_pid=session_pid)
        assert [goal for kind, goal in picks if kind == "goal"] == ["a", "b"]
        lp._record(base, source, "a", "done")
        kind, reason = lp._next(base, source, config, extra_skip={"b"}, session_pid=session_pid)
        assert kind == "HANDOFF", (kind, reason, source.marked)
        assert source.marked == ["a", "b"]


def test_handoff_refuses_refill_after_own_live_session_marker_expires():
    """A long goal can outlive the registry TTL; losing that file cannot reset admission."""
    class CompletingQueue(_Queue):
        def complete(self, goal):
            self.items.remove(goal)

    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 12}},
             "handoff": {"after_goals": 2},
             "work": {"enabled": True}, "parallel": {"goals": {"enabled": True, "max_concurrent": 2}}}))
        lp = _loop()
        pid = os.getpid()
        lp.main(["loop.py", "start", base, "--session-pid", str(pid)])
        source = CompletingQueue(["a", "b", "c"])
        config = lp.state.load_config(base)
        assert [g for kind, g in lp.next_batch(base, source, config, session_pid=pid)
                if kind == "goal"] == ["a", "b"]
        marker = lp._session_marker_path(base, pid)
        old = time.time() - 13 * 3600
        os.utime(marker, (old, old))
        assert lp._session_in_flight_goals(base, config) == set()  # existing TTL prune
        lp._record(base, source, "a", "done")
        kind, _ = lp._next(base, source, config, extra_skip={"b"}, session_pid=pid)
        assert kind == "HANDOFF"
        assert source.marked == ["a", "b"]


def test_handoff_count_is_not_reset_by_a_second_codex_task_start(monkeypatch):
    """Global STATE.md resets cannot replenish another live task's goal allowance."""
    class CompletingQueue(_Queue):
        def complete(self, goal):
            self.items.remove(goal)

    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "handoff": {"after_goals": 2}}))
        lp = _loop()
        pid = os.getpid()
        thread_a = "01a03637-7800-7000-8000-000000000001"
        thread_b = "01a03637-7800-7000-8000-000000000002"
        monkeypatch.setenv("CODEX_THREAD_ID", thread_a)
        lp.main(["loop.py", "start", base, "--session-pid", str(pid)])
        source = CompletingQueue(["a", "b", "c"])
        config = lp.state.load_config(base)
        assert lp._next(base, source, config, session_pid=pid)[0] == "goal"
        lp._record(base, source, "a", "done")
        monkeypatch.setenv("CODEX_THREAD_ID", thread_b)
        lp.main(["loop.py", "start", base, "--session-pid", str(pid)])
        monkeypatch.setenv("CODEX_THREAD_ID", thread_a)
        assert lp._next(base, source, config, session_pid=pid)[0] == "goal"
        lp._record(base, source, "b", "done")
        kind, _ = lp._next(base, source, config, session_pid=pid)
        assert kind == "HANDOFF"
        assert source.marked == ["a", "b"]


def test_handoff_count_survives_repeated_start_in_the_same_session():
    """A repeated start resets shared budgets but cannot buy the same context more goals."""
    class CompletingQueue(_Queue):
        def complete(self, goal):
            self.items.remove(goal)

    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "handoff": {"after_goals": 1}}))
        lp = _loop()
        pid = os.getpid()
        lp.main(["loop.py", "start", base, "--session-pid", str(pid)])
        source = CompletingQueue(["a", "b"])
        config = lp.state.load_config(base)
        assert lp._next(base, source, config, session_pid=pid) == ("goal", "a")
        lp._record(base, source, "a", "done")
        lp.main(["loop.py", "start", base, "--session-pid", str(pid)])
        assert lp._next(base, source, config, session_pid=pid)[0] == "HANDOFF"
        assert source.marked == ["a"]


def test_handoff_refuses_a_corrupt_session_admission_marker():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        lp = _loop()
        pid = os.getpid()
        lp.main(["loop.py", "start", base, "--session-pid", str(pid)])
        lp._session_marker_path(base, pid).write_text("{partial")
        source = _Queue(["a"])
        kind, reason = lp._next(base, source, lp.state.load_config(base), session_pid=pid)
        assert kind == "HANDOFF" and "marker" in reason
        assert source.marked == []


def test_handoff_refuses_invalid_goal_members_in_session_marker():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "handoff": {"after_goals": 1}}))
        lp = _loop()
        pid = os.getpid()
        lp.main(["loop.py", "start", base, "--session-pid", str(pid)])
        source = _Queue(["a", "b"])
        config = lp.state.load_config(base)
        assert lp._next(base, source, config, session_pid=pid) == ("goal", "a")
        lp._session_marker_path(base, pid).write_text(json.dumps(
            {"in_flight": [None], "settled_admissions": 0}))
        kind, reason = lp._next(base, source, config, extra_skip={"a"}, session_pid=pid)
        assert kind == "HANDOFF" and "malformed" in reason
        assert source.marked == ["a"]


def test_repeated_start_refuses_missing_admission_count_in_existing_marker():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        lp = _loop()
        pid = os.getpid()
        lp.main(["loop.py", "start", base, "--session-pid", str(pid)])
        lp._session_marker_path(base, pid).write_text(json.dumps({"in_flight": []}))
        with pytest.raises(RuntimeError, match="admission count"):
            lp.main(["loop.py", "start", base, "--session-pid", str(pid)])


def test_handoff_preclaim_refusal_refunds_its_slot(monkeypatch):
    """A label gate can decline one candidate before claim; the next remains admissible."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "handoff": {"after_goals": 1}}))
        lp = _loop()
        pid = os.getpid()
        lp.main(["loop.py", "start", base, "--session-pid", str(pid)])
        source = _Queue(["a", "b"])
        original = lp.feature_labels.attach_at_pick

        def refuse_first(sdlc_dir, src, goal, config):
            if goal == "a":
                return lp.feature_labels.Decision(False, "refused", None)
            return original(sdlc_dir, src, goal, config)

        monkeypatch.setattr(lp.feature_labels, "attach_at_pick", refuse_first)
        assert lp._next(base, source, lp.state.load_config(base), session_pid=pid) == ("goal", "b")
        marker = lp._session_read(lp._session_marker_path(base, pid), strict=True)
        assert marker["settled_admissions"] == 0 and marker["in_flight"] == ["b"]


def test_next_batch_defaults_to_the_configured_cap_when_goal_parallelism_is_on():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "work": {"enabled": True},
             "parallel": {"goals": {"enabled": True, "max_concurrent": 2}}}))
        lp = _loop(); src = _Queue(["a", "b", "c"])
        picks = lp.next_batch(base, src, lp.state.load_config(base))   # no explicit cap
        assert picks == [("goal", "a"), ("goal", "b")]


def test_next_batch_clamps_to_one_when_work_is_off_even_with_goal_parallelism_on(capsys):
    """D2 round 2 F06: concurrent goals are only safe because each gets its own worktree
    (`work.py start`). With `work.enabled` off every goal edits the one checkout, so the cap is 1
    and the operator is told why (stderr), politely: nothing is refused, nothing stalls."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "parallel": {"goals": {"enabled": True, "max_concurrent": 3}}}))
        lp = _loop(); src = _Queue(["a", "b", "c"])
        picks = lp.next_batch(base, src, lp.state.load_config(base))
        assert picks == [("goal", "a")]
        err = capsys.readouterr().err
        assert "parallel.goals" in err and "work.enabled" in err


def test_next_batch_never_reclaims_a_goal_it_already_picked_this_batch():
    """The accumulating extra_skip is what makes this session's own multiple slots safe from each
    other WITHOUT touching the ledger a second time per pick -- verified directly by checking the
    ledger only ever recorded each goal claimed exactly once."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        lp = _loop(); src = _Queue(["a", "b", "c"])
        lp.next_batch(base, src, lp.state.load_config(base), max_concurrent=3)
        claimed_ids = [e["goal"] for e in lp.ledger.read_all(base) if e["kind"] == "claimed"]
        assert sorted(claimed_ids) == ["a", "b", "c"]      # each claimed exactly once, never twice


# ------------------------------------------------- cross-call skip for slot refills (#375 follow-up)
# #374's writer-liveness check can only tell whether the SHORT-LIVED `loop.py` invocation that wrote
# a claim is still literally running -- it has already exited by the time this process's own call
# returns, regardless of whether the goal is still being actively worked by a long-running subagent.
# Refilling ONE freed slot, some time after the batch that filled the others returned, needs the
# caller to say "these other goals are still active" explicitly -- `next`/`next-batch`'s `--skip`.


def test_cli_skip_parses_a_comma_separated_list_and_trims_whitespace():
    lp = _loop()
    assert lp._cli_skip(["--skip", "a, b ,c"]) == {"a", "b", "c"}


def test_cli_skip_drops_empty_entries_from_stray_commas():
    lp = _loop()
    assert lp._cli_skip(["--skip", "a,,b,"]) == {"a", "b"}


def test_cli_skip_is_empty_when_the_flag_is_absent():
    lp = _loop()
    assert lp._cli_skip(["--other", "x"]) == set()


def test_cli_skip_is_empty_for_a_bare_flag_with_no_value():
    """A bare `--skip` (nothing follows) hits `_flags`'s own "true" sentinel for a valueless flag --
    must not be read as a literal goal named "true"."""
    lp = _loop()
    assert lp._cli_skip(["--skip"]) == set()


def test_flags_consumes_a_known_event_field_value_that_starts_with_a_double_dash():
    """#541: loop.py's own `_flags` copy gets the same known-value-flags fix as ledger.py's --
    `why`/`model` (free-text EVENT_FIELDS, e.g. gate/park/spend) unconditionally consume the next
    token even when it starts with '--', instead of silently landing on the "true" sentinel."""
    lp = _loop()
    assert lp._flags(["--why", "--needs a --fix before this can land"]) == {
        "why": "--needs a --fix before this can land"}
    assert lp._flags(["--model=--claude-ish"]) == {"model": "--claude-ish"}


def test_flags_never_keeps_a_whitespace_bearing_leaked_key():
    lp = _loop()
    assert lp._flags(["--this looks like leaked prose, not a flag"]) == {}


def test_flags_drops_a_whitespace_bearing_key_in_the_eq_form_too():
    """#541 cycle 2: the `--name=value` branch bypassed the never-a-real-flag rule its
    space-separated sibling applies, so leaked prose that happened to contain '=' still landed
    as a whitespace-bearing key. Same shape in all four `_flags` copies, pinned in each."""
    lp = _loop()
    assert lp._flags(["--zzunknown", "--a b=c d"]) == {"zzunknown": "true"}


def test_next_batch_extra_skip_excludes_a_goal_this_call_never_picked_itself():
    """The whole point: a goal skipped via `extra_skip` here was NOT claimed by this call at all --
    unlike the internal accumulation test above, which skips goals THIS batch just picked. This is
    the caller (the orchestrating skill) saying "leave this one alone, a sibling from an earlier
    call is still working it," and the ledger never sees a second claim attempt for it."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        lp = _loop(); src = _Queue(["a", "b", "c"])
        picks = lp.next_batch(base, src, lp.state.load_config(base), max_concurrent=3,
                               extra_skip={"a"})
        # 2 real picks (a is excluded from the start) + a 3rd _next() call correctly finding the
        # backlog now exhausted -- same shape as test_next_batch_stops_early_and_reports_done...
        assert picks == [("goal", "b"), ("goal", "c"), ("DONE", None)]
        claimed_ids = [e["goal"] for e in lp.ledger.read_all(base) if e["kind"] == "claimed"]
        assert "a" not in claimed_ids


def test_next_without_skip_would_redispatch_a_goal_still_marked_in_progress():
    """Proves the gap `--skip` exists to close, not just the fix: with the ledger off (or a goal a
    sibling slot claimed long enough ago that its short-lived writer process has already exited --
    the normal case, see `next_batch`'s docstring), a source-level `in_progress` status alone does
    NOT stop a later `next()` from re-returning the same goal. Without this proof the `--skip`
    tests above could be read as defending against a risk that was never real."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        goal = base + "/goals/0001.md"
        lp.sources.get_source(base, lp.state.load_config(base)).mark_in_progress(goal)
        kind, got = lp._next(base, lp.sources.get_source(base, lp.state.load_config(base)),
                              lp.state.load_config(base))
        assert (kind, got) == ("goal", goal)      # re-dispatched -- exactly the bug --skip prevents


def test_cli_next_skip_flag_prevents_redispatching_a_goal_a_sibling_slot_still_holds(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2)
        lp = _loop()
        goal_a, goal_b = base + "/goals/0001.md", base + "/goals/0002.md"
        lp.sources.get_source(base, lp.state.load_config(base)).mark_in_progress(goal_a)
        rc = lp.main(["loop.py", "next", base, "--skip", goal_a])
        assert rc == 0
        assert capsys.readouterr().out.strip() == goal_b


def test_cli_next_batch_skip_flag_is_wired_through(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2)
        lp = _loop()
        goal_a, goal_b = base + "/goals/0001.md", base + "/goals/0002.md"
        lp.sources.get_source(base, lp.state.load_config(base)).mark_in_progress(goal_a)
        rc = lp.main(["loop.py", "next-batch", base, "--skip", goal_a])
        assert rc == 0
        assert capsys.readouterr().out.strip() == goal_b


# ------------------------------------------------------------------ session-active marker (#377)
# A routine/cron firing needs to tell "a managing session is still genuinely running" from "safe to
# start one" -- built on the SAME two independent signals ledger._held() already combines for claim
# leases (F10.5/#374): ledger.pid_alive() as the primary, unconditional-TTL age as the fallback.


def test_session_active_is_false_when_no_marker_exists():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        assert lp.session_active(d + "/.sdlc", {}) is False


def test_session_start_then_session_active_reads_true_for_a_live_pid():
    """The core guarantee, first half: a genuinely live session's marker blocks a second launch."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.session_start(sdlc, os.getpid())            # this test process is unambiguously alive
        assert lp.session_active(sdlc, {}) is True


def test_codex_threads_with_same_live_pid_keep_separate_session_claims(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    thread_a = "01a03637-7800-7000-8000-000000000001"
    thread_b = "01a03637-7800-7000-8000-000000000002"
    with tempfile.TemporaryDirectory() as d:
        sdlc = _backlog(d, 2)
        lp = _loop()
        pid = os.getpid()
        monkeypatch.setenv("CODEX_THREAD_ID", thread_a)
        generation_a = lp.session_start(sdlc, pid)
        lp._session_claim(sdlc, pid, "goal-a")
        path_a = lp._session_marker_path(sdlc, pid)

        monkeypatch.setenv("CODEX_THREAD_ID", thread_b)
        generation_b = lp.session_start(sdlc, pid)
        lp._session_claim(sdlc, pid, "goal-b")
        path_b = lp._session_marker_path(sdlc, pid)

        assert path_a != path_b
        assert {path.name for _, path, _ in lp._session_entries(sdlc)} == {
            f"{pid}-{thread_a}.active", f"{pid}-{thread_b}.active"}
        assert lp._session_read(path_a)["in_flight"] == ["goal-a"]
        assert lp._session_read(path_b)["in_flight"] == ["goal-b"]
        assert lp._session_in_flight_goals(sdlc, {}) == {"goal-a", "goal-b"}

        lp._session_release(sdlc, "goal-a")
        assert lp._session_read(path_a)["in_flight"] == []
        assert lp._session_read(path_b)["in_flight"] == ["goal-b"]
        lp.session_end(sdlc, pid, generation=generation_b)
        assert path_a.exists() and not path_b.exists()


def test_loop_start_refuses_invalid_codex_thread_before_changing_run_state(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with tempfile.TemporaryDirectory() as d:
        sdlc = _backlog(d, 1)
        lp = _loop()
        before = (pathlib.Path(sdlc) / "state" / "STATE.md").read_text()
        monkeypatch.setenv("CODEX_THREAD_ID", "../unsafe")
        assert lp.main(["loop.py", "start", sdlc, "--session-pid", str(os.getpid())]) == 2
        assert "CODEX_THREAD_ID" in capsys.readouterr().err
        assert (pathlib.Path(sdlc) / "state" / "STATE.md").read_text() == before
        assert not list((pathlib.Path(sdlc) / "state" / "sessions").glob("*.active"))


def test_codex_prune_and_release_lock_the_enumerated_entry(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    thread_a = "01a03637-7800-7000-8000-000000000001"
    thread_b = "01a03637-7800-7000-8000-000000000002"
    with tempfile.TemporaryDirectory() as d:
        sdlc = _backlog(d, 1)
        lp = _loop()
        pid = os.getpid()
        monkeypatch.setenv("CODEX_THREAD_ID", thread_a)
        lp._session_claim(sdlc, pid, "goal-a")
        path_a = lp._session_marker_path(sdlc, pid)
        monkeypatch.setenv("CODEX_THREAD_ID", thread_b)
        lp._session_claim(sdlc, pid, "goal-b")
        path_b = lp._session_marker_path(sdlc, pid)
        original_lock = lp._session_locked
        locked_paths = []

        def observe_lock(sdlc_dir, session_pid, fn, path=None, require_lock=False):
            locked_paths.append(path)
            return original_lock(sdlc_dir, session_pid, fn, path=path,
                                 require_lock=require_lock)

        monkeypatch.setattr(lp, "_session_locked", observe_lock)
        lp._session_release(sdlc, "goal-a")
        assert set(locked_paths) == {path_a, path_b}
        assert lp._session_read(path_b)["in_flight"] == ["goal-b"]

        stale = time.time() - (lp.ledger.DEFAULT_LEASE_TTL_HOURS * 3600 + 60)
        os.utime(path_a, (stale, stale))
        locked_paths.clear()
        lp._prune_dead_session_entries(sdlc, {})
        assert locked_paths == [path_a]
        assert not path_a.exists()
        assert path_b.exists()
        assert lp._session_lock_path(sdlc, path_a).exists()
        assert lp._session_lock_path(sdlc, path_b).exists()


def test_session_lock_stripe_is_stable_when_host_cpu_count_changes(monkeypatch):
    """The stripe is an on-disk synchronization identity, not a process-local tuning knob."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        entry = pathlib.Path(sdlc) / "state" / "sessions" / "12345.active"
        monkeypatch.setattr(lp.os, "cpu_count", lambda: 4)
        before = lp._session_lock_path(sdlc, entry)
        monkeypatch.setattr(lp.os, "cpu_count", lambda: 8)
        assert lp._session_lock_path(sdlc, entry) == before


def test_session_prune_removes_stale_atomic_write_temps():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        directory = pathlib.Path(sdlc) / "state" / "sessions"
        directory.mkdir(parents=True)
        orphan = directory / "12345.active.999999999.tmp"
        orphan.write_text("partial")
        stale = time.time() - 2 * 24 * 3600
        os.utime(orphan, (stale, stale))
        lp._prune_dead_session_entries(sdlc, {})
        assert not orphan.exists()


def test_session_active_is_false_for_a_definitively_dead_pid():
    """The core guarantee, second half: a dead session's stale marker does NOT block a launch --
    no timeout needed to detect a crash, the same reasoning _try_acquire_claim_lock's flock-based
    lock relies on (F10.5-2/#387)."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        dead_pid = 2**30                                # not a real pid on any sane system
        lp.session_start(sdlc, dead_pid)
        assert lp.session_active(sdlc, {}) is False


def test_session_end_clears_a_live_marker():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        generation = lp.session_start(sdlc, os.getpid())
        assert lp.session_active(sdlc, {}) is True
        lp.session_end(sdlc, os.getpid(), generation=generation)
        assert lp.session_active(sdlc, {}) is False


def test_session_end_is_a_safe_noop_when_nothing_was_ever_started():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        lp.session_end(d + "/.sdlc")                    # must not raise


def test_session_start_is_a_safe_noop_on_an_unparseable_pid():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.session_start(sdlc, "not-a-pid")             # must not raise
        assert lp.session_active(sdlc, {}) is False


def test_session_active_expires_a_stale_marker_past_the_ttl_even_for_a_resolvable_pid():
    """Mirrors ledger._held()'s own TTL cutoff, applied UNCONDITIONALLY -- not only as a fallback
    for when pid_alive can't resolve. The realistic risk this guards is pid REUSE over long spans
    (a marker orphaned by a crash, later coinciding with an unrelated process getting that same pid
    number), not a legitimately-still-running session outliving routine-firing frequency."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.session_start(sdlc, os.getpid())             # alive pid throughout -- only age changes
        path = lp._session_marker_path(sdlc, os.getpid())
        stale = time.time() - (lp.ledger.DEFAULT_LEASE_TTL_HOURS * 3600 + 60)
        os.utime(path, (stale, stale))
        assert lp.session_active(sdlc, {}) is False


def test_session_active_ttl_zero_never_expires():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.session_start(sdlc, os.getpid())
        path = lp._session_marker_path(sdlc, os.getpid())
        ancient = time.time() - (1000 * 3600)
        os.utime(path, (ancient, ancient))
        assert lp.session_active(sdlc, {"ledger": {"lease": {"ttl_hours": 0}}}) is True


def test_cli_start_with_session_pid_writes_a_marker_session_active_sees(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        assert lp.main(["loop.py", "start", base, "--session-pid", str(os.getpid())]) == 0
        capsys.readouterr()
        assert lp.main(["loop.py", "session-active", base]) == 0
        assert capsys.readouterr().out.strip() == "ACTIVE"


def test_cli_start_without_session_pid_flag_still_registers_via_ppid():
    """#1199 AC5: `start` now registers a session BY DEFAULT, no `--session-pid` needed -- the
    shipped `/sigma-loop` skill invocation (SKILL.md's own bare `loop.py start .sdlc`) is exactly
    this call. Falls back to THIS PROCESS's own PPID, the same long-lived-parent identity
    `--session-pid` was always meant to capture (see session_start's own docstring) -- here that's
    the pytest-runner process, genuinely alive throughout the test."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        assert lp.main(["loop.py", "start", base]) == 0
        assert lp.session_active(base, {}) is True


def test_cli_session_end_clears_what_cli_start_wrote(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        lp.main(["loop.py", "start", base])           # registers via this process's own PPID
        assert lp.main(["loop.py", "session-end", base]) == 0   # clears the SAME PPID by default
        capsys.readouterr()
        lp.main(["loop.py", "session-active", base])
        assert capsys.readouterr().out.strip() == "FREE"


# ------------------------------------------------------------------ session REGISTRY (#1199) -- the single
# fixed-slot marker above is a single-session mental model that silently breaks the moment a SECOND
# session exists: a second session_start clobbers the first session's own pid (last-writer-wins),
# and a single session_end from EITHER session deletes the shared file, hiding a still-running
# session's liveness entirely. Below: the marker is a directory, one file per session pid, so two
# live sessions never collide -- plus the in-flight registration `_next()` reads to skip a goal a
# SIBLING live session already claimed, with no caller ever passing `--skip`.


def test_two_concurrent_session_starts_both_remain_visible():
    """The ORIGINAL bug, reproduced: with the pre-fix single fixed-path marker, session_start(sdlc,
    11111) followed by session_start(sdlc, 22222) left the marker containing only '22222\\n' --
    session A's pid gone entirely, last-writer-wins. Fixed: a registry (one file per pid) means
    both entries persist independently, regardless of write order."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        pid_a, pid_b = os.getpid(), os.getppid()      # two genuinely distinct, genuinely alive pids
        lp.session_start(sdlc, pid_a)
        lp.session_start(sdlc, pid_b)
        registered = {pid for pid, _, _ in lp._session_entries(sdlc)}
        assert registered == {pid_a, pid_b}
        assert lp.session_active(sdlc, {}) is True


def test_session_end_from_one_session_leaves_the_other_registered():
    """The ORIGINAL bug's other half: pre-fix, a single session_end from EITHER session unlinked
    the one shared file, so the SURVIVING session read as inactive. Fixed: session_end now takes
    the pid to end, and only that pid's own entry is removed."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        pid_a, pid_b = os.getpid(), os.getppid()
        generation_a = lp.session_start(sdlc, pid_a)
        lp.session_start(sdlc, pid_b)
        lp.session_end(sdlc, pid_a, generation=generation_a)
        registered = {pid for pid, _, _ in lp._session_entries(sdlc)}
        assert registered == {pid_b}
        assert lp.session_active(sdlc, {}) is True     # B is still live and registered


def test_claimed_goal_has_live_worker_stays_true_after_one_of_two_sessions_ends():
    """Direct regression test for the worst half of the original bug: a second session's
    session_end must never blind `_claimed_goal_has_live_worker` to a DIFFERENT, still-live
    session's claim."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        pid_a, pid_b = os.getpid(), os.getppid()
        lp.session_start(sdlc, pid_a)
        lp.session_start(sdlc, pid_b)
        lp.session_end(sdlc, pid_a)
        assert lp._claimed_goal_has_live_worker(sdlc, "g.md", {}) is True


def test_claim_lock_alive_reports_alive_when_either_of_two_registered_sessions_is_live():
    """#1199 AC7: claim_lock_alive reads _claimed_goal_has_live_worker -> session_active, which now
    scans the registry -- a stale FIRST registration must not hide a live SECOND one, AND (the
    order this test originally skipped) a live FIRST registration must not be hidden by a stale
    SECOND one either.

    #1239 review (finding 4): the ORIGINAL version of this test only checked dead-then-live order,
    which made it vacuous -- it passed unchanged against a real pre-#1199 `loop.py` (`origin/main`,
    the single fixed-path `state/session.active`, last-writer-wins) too, for the WRONG reason: with
    dead registered first and live second, last-writer-wins happens to leave the live pid as the
    file's own final content, so the old, buggy single-slot model reports "alive" by coincidence,
    not because it correctly scans a registry (which it doesn't have). Reversing the order alone
    (live first, dead second) is exactly what exposes the difference: against that same old
    single-slot code it reports `('unknown', <pid>)` -- FAILS, correctly, since last-writer-wins
    would leave the DEAD pid as the file's final content. Both orderings are asserted here so this
    test can never again pass by matching a coincidental order-dependent artifact of whichever
    model happens to be running underneath it."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        _win_claim_lock_in_a_real_subprocess(sdlc, "g.md")
        lp.session_start(sdlc, 2**30)                 # a dead/stale session, registered first
        lp.session_start(sdlc, os.getpid())           # a second, genuinely live session
        assert lp.claim_lock_alive(sdlc, "g.md", {})[0] == "alive"

    with tempfile.TemporaryDirectory() as d:           # reversed order -- the case that was skipped
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        _win_claim_lock_in_a_real_subprocess(sdlc, "g.md")
        lp.session_start(sdlc, os.getpid())           # a genuinely live session, registered FIRST
        lp.session_start(sdlc, 2**30)                 # a dead/stale session, registered SECOND
        assert lp.claim_lock_alive(sdlc, "g.md", {})[0] == "alive", (
            "a live session registered FIRST must not be hidden by a stale session registered "
            "SECOND -- if this fails, the registry has regressed toward last-writer-wins"
        )


def test_session_in_flight_goals_ignores_a_dead_sessions_claim():
    """#1199 AC3: a crashed session's own in-flight claim is aged out by the SAME pid_alive() +
    lease-TTL combination session_active already applies -- so a goal a crashed session claimed is
    NOT held hostage forever."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        dead_pid = 2**30                              # not a real pid on any sane system
        lp.session_start(sdlc, dead_pid)
        lp._session_claim(sdlc, dead_pid, "0001.md")
        assert lp._session_in_flight_goals(sdlc, {}) == set()


# ------------------------------------------------------------ registry pruning (#1239 review round 3,
# finding B): entries under state/sessions/ were created on every _next()/next_batch()/session_start()
# call but NEVER pruned -- only session_end() for the EXACT matching pid ever deleted an .active file,
# and nothing ever deleted a .lock file or opportunistically swept a dead/stale entry. Below: pruning
# is wired into the two real chokepoints -- the registry read _next() already performs on every call
# (_session_in_flight_goals), and the CLI `start` verb alongside session_start -- both using the SAME
# pid_alive()+lease-TTL liveness check every other registry reader already applies.


def test_session_in_flight_goals_prunes_dead_active_but_keeps_bounded_lock_stripe():
    """A dead marker is pruned; its reusable lock stripe stays to avoid inode races."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        dead_pid = 2**30                              # not a real pid on any sane system
        lp.session_start(sdlc, dead_pid)
        lp._session_claim(sdlc, dead_pid, "0001.md")
        active_path = lp._session_marker_path(sdlc, dead_pid)
        lock_path = lp._session_lock_path(sdlc, active_path)
        assert active_path.exists() and lock_path.exists()

        lp._session_in_flight_goals(sdlc, {})

        assert not active_path.exists(), "a dead session's .active marker was never pruned"
        assert lock_path.exists(), "a lock stripe must not be unlinked while another process might hold it"


def test_session_in_flight_goals_never_prunes_a_live_sessions_entry():
    """The liveness check pruning reuses must never mistake a genuinely-live session for a dead
    one -- pruning is additive maintenance, not a second, looser liveness gate than every other
    registry reader in this file already applies."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.session_start(sdlc, os.getpid())            # this test process -- genuinely alive
        active_path = lp._session_marker_path(sdlc, os.getpid())

        lp._session_in_flight_goals(sdlc, {})

        assert active_path.exists(), "a live session's own registry entry must never be pruned"


def test_cli_start_prunes_a_dead_sibling_sessions_registry_entry():
    """The OTHER real chokepoint finding B names: a session announcing itself via the CLI `start`
    verb -- the production entry point `session_start` is actually reached through -- also sweeps
    a dead sibling's leftover entry, not only the read path in `_session_in_flight_goals` above."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        dead_pid = 2**30
        lp.session_start(base, dead_pid)
        active_path = lp._session_marker_path(base, dead_pid)
        assert active_path.exists()

        assert lp.main(["loop.py", "start", base, "--session-pid", str(os.getpid())]) == 0

        assert not active_path.exists(), "CLI `start` did not prune a dead sibling session's entry"
        assert lp.session_active(base, {}) is True     # the NEW session itself is still registered


def test_next_without_skip_no_longer_redispatches_a_goal_a_live_sessions_own_next_call_claimed():
    """#1199 AC4: the exact gap `test_next_without_skip_would_redispatch_a_goal_still_marked_in_
    progress` (above) documents is now closed automatically, with no caller-supplied --skip: the
    FIRST _next() call registers the goal in-flight under its OWN session_pid, and a SECOND live
    session's _next() call (a different session_pid) reads the registry and skips it."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        src = lp.sources.get_source(base, lp.state.load_config(base))
        goal = base + "/goals/0001.md"
        pid_a, pid_b = os.getpid(), os.getppid()
        kind_a, got_a = lp._next(base, src, lp.state.load_config(base), session_pid=pid_a)
        assert (kind_a, got_a) == ("goal", goal)
        kind_b, got_b = lp._next(base, src, lp.state.load_config(base), session_pid=pid_b)
        assert kind_b == "DONE"          # NOT re-offered -- pid_a's own live claim skips it


def test_next_composes_the_session_skip_set_with_the_auto_reclaim_cooldown_in_one_call(monkeypatch):
    """#1199 rebase (PR #1239 vs #1198/#1197 on `origin/main`): exercises the actual `skip = (...)`
    union `_next()` now builds -- `set(extra_skip) | just_unparked | just_reclaimed |
    _session_in_flight_goals(...)` -- with BOTH #1199's session-registry skip and #1198's
    auto-reclaim cooldown skip populated in the SAME call, proving they compose rather than one
    silently shadowing the other.

    Deliberately does NOT drive this through a real GitHub-backed `_auto_reclaim_stale_claims` --
    that function's own live-worker corroboration (per-goal vs. goal-agnostic) is separately
    tested end to end by `test_auto_reclaim_sweep_reclaims_past_an_unrelated_live_session` and
    `test_auto_reclaim_sweep_still_blocked_by_a_live_worker_on_the_same_goal` (below; #1284 --
    as of that fix, a goal-agnostic live session like the one registered here no longer blocks
    reclaim of an unrelated goal at all, so this test's cooldown/skip-set union would no longer
    even need the stub to pass; it stays stubbed anyway so this test keeps isolating exactly what
    THIS rebase wrote -- the union itself -- from the separately-tested internals of what feeds
    it, rather than depending on `_auto_reclaim_stale_claims`'s own behavior).

    Three goals, three fates in the SAME `_next()` call: `goal_reclaimed` is withheld by the
    (stubbed) reclaim cooldown, `goal_in_flight` is withheld because a live SIBLING session already
    has it registered, and `goal_free` -- encumbered by neither -- is the one actually returned."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 3)
        lp = _loop()
        src = lp.sources.get_source(base, lp.state.load_config(base))
        cfg = lp.state.load_config(base)
        goal_reclaimed = base + "/goals/0001.md"
        goal_in_flight = base + "/goals/0002.md"
        goal_free = base + "/goals/0003.md"

        sibling_pid = os.getppid()                          # a DIFFERENT, genuinely live session
        lp.session_start(base, sibling_pid)
        lp._session_claim(base, sibling_pid, goal_in_flight)   # ... already working goal_in_flight

        monkeypatch.setattr(lp, "_auto_reclaim_stale_claims",
                             lambda *a, **k: frozenset({goal_reclaimed}))

        kind, got = lp._next(base, src, cfg, session_pid=os.getpid())

        assert (kind, got) == ("goal", goal_free), (
            f"expected the reclaim cooldown and the session skip-set to compose, leaving only "
            f"{goal_free!r} pickable this call; got {(kind, got)!r}")
        # both withheld goals are still genuinely available to a LATER call, not permanently lost --
        # the reclaim cooldown clears the moment this call returns, and goal_in_flight is only ever
        # withheld from OTHER sessions, never actually removed from the backlog.
        assert goal_in_flight in lp._session_in_flight_goals(base, cfg)


def test_record_clears_the_goal_from_every_sessions_in_flight_list():
    """A goal that finishes must stop being in-flight -- otherwise a completed goal would be
    skipped forever, a worse bug than the one this fixes."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        src = lp.sources.get_source(base, lp.state.load_config(base))
        goal = base + "/goals/0001.md"
        lp._next(base, src, lp.state.load_config(base), session_pid=os.getpid())
        assert goal in lp._session_in_flight_goals(base, {})
        lp._record(base, src, goal, "done")
        assert lp._session_in_flight_goals(base, {}) == set()


def test_release_clears_the_goal_from_every_sessions_in_flight_list():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        src = lp.sources.get_source(base, lp.state.load_config(base))
        goal = base + "/goals/0001.md"
        lp._next(base, src, lp.state.load_config(base), session_pid=os.getpid())
        lp._release(base, src, goal)
        assert lp._session_in_flight_goals(base, {}) == set()


def test_shipped_skill_start_then_next_registers_a_session_as_actually_invoked():
    """#1199 AC5, corrected (#1239 review round 3, findings A/D): the ORIGINAL version of this test
    claimed to run "SKILL.md's own literal, shipped invocation -- `loop.py start .sdlc` bare, then
    `loop.py next .sdlc` bare, NEVER --session-pid" -- that was accurate when written, but this
    PR's own later commit (see `test_os_getppid_is_unstable_across_genuinely_separate_forked_
    dispatch` below) found `os.getppid()` read inside a freshly-forked `loop.py` subprocess is NOT
    a stable cross-call identity when `loop.py` is dispatched the way the real `/sigma-loop` skill
    actually dispatches it (one Bash-tool call per command, each genuinely forking a fresh shell).
    The design pivoted in response: SKILL.md/README/sigma-triage's SKILL.md were all rewritten to
    pass `--session-pid "$PPID"` on EVERY `start`/`next`/`next-batch` call, unconditionally -- so a
    test asserting the bare, flagless form is "the shipped invocation" was left contradicting the
    doc it claimed to describe. `test_every_start_next_and_next_batch_invocation_in_shipped_skill_
    passes_session_pid` (below) reads SKILL.md fresh, on every run, to prove that claim rather than
    hardcoding it here a second time -- this test's own job is narrower: prove the MECHANICS
    genuinely work when invoked exactly as SKILL.md now instructs, through `main()`'s real CLI
    dispatch, not the Python-level helpers directly.

    AC5's "by default" now means from the SKILL's perspective, not the bare-CLI-flag perspective:
    an operator following SKILL.md never hand-adds anything beyond what the skill's own prompt text
    already gives them ($PPID, read fresh from their own shell each call) -- there is no path
    through the shipped skill where registration is skipped. That is still "no hand-added flag
    required" in the sense the original acceptance criterion meant, even though it is not
    flag-free at the raw `loop.py` CLI level (see `test_cli_start_without_session_pid_flag_still_
    registers_via_ppid` above for that separate, still-true fallback capability)."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        pid = str(os.getpid())        # this test process -- genuinely alive throughout, like $PPID
        assert lp.main(["loop.py", "start", base, "--session-pid", pid]) == 0
        assert lp.session_active(base, {}) is True
        assert lp.main(["loop.py", "next", base, "--session-pid", pid]) == 0
        assert lp._session_in_flight_goals(base, {}) == {base + "/goals/0001.md"}


def test_every_start_next_and_next_batch_invocation_in_shipped_skill_passes_session_pid():
    """#1199 AC5 / #1239 review round 3, finding A: "a normal /sigma-loop run registers itself by
    default, no hand-added flag required" is a claim about the REAL, CURRENT shipped SKILL.md --
    checked here by reading that file fresh on every run, not by hardcoding what an earlier commit
    intended (the exact way the test this replaces went stale: its docstring asserted a bare,
    flagless invocation that SKILL.md had already stopped issuing by the time this PR shipped).
    Mirrors tests/test_triage_skill_docs.py's own established pattern for locking sigma-triage's
    SKILL.md the same way, applied here to /sigma-loop's own doc -- the doc this PR's design pivot
    actually rewrote."""
    text = (S.parent / "SKILL.md").read_text()
    # Whitespace-collapsed to one line before matching: this doc hand-wraps a long invocation like
    # `loop.py ... next .sdlc --session-pid "$PPID"` across physical lines -- a literal
    # per-physical-line check would false-fail on correctly-wrapped, correctly-flagged prose.
    flat = re.sub(r"\s+", " ", text)
    invocation = re.compile(r"loop\.py[\"']?\s+(?:start|next|next-batch)\s+\.sdlc\b")
    flagged = re.compile(
        r"loop\.py[\"']?\s+(?:start|next|next-batch)\s+\.sdlc\s+--session-pid\s+\"\$PPID\""
    )
    total = len(invocation.findall(flat))
    ok = len(flagged.findall(flat))
    assert total >= 2, (
        "expected to find loop.py start/next/next-batch .sdlc invocations in sigma-loop's own "
        "SKILL.md -- if the doc no longer issues them this way, this test is stale, not passing"
    )
    assert ok == total, (
        f"found {total} loop.py start/next/next-batch .sdlc invocation(s) in sigma-loop's own "
        f"SKILL.md but only {ok} pass --session-pid \"$PPID\" immediately after .sdlc -- a bare "
        f"invocation is exactly the unstable-cross-call-identity gap #1239 review found, and AC5's "
        f"'registers by default' claim only holds because the skill ALWAYS supplies this flag"
    )


def test_os_getppid_is_unstable_across_genuinely_separate_forked_dispatch():
    """Grounds the whole premise #1239 review finding 1 turns on, with NOTHING from loop.py
    involved -- pure OS-level fact, checked directly. A LONE `bash -c "python3 ..."` (nothing after
    it) lets bash tail-exec straight into python3 without forking, which would make `os.getppid()`
    equal the CALLING test process every time -- accidentally stable, and the reason the ORIGINAL
    (deleted) version of this test file's subprocess-based redispatch test was vacuous: it used
    `subprocess.run([sys.executable, ...])` with no shell at all, so python3's immediate parent was
    always this one stable pytest process, whatever loop.py itself did with `--session-pid`. A
    COMPOUND command (`; true` after it) forces bash to genuinely fork a fresh subshell per call --
    the same topology a real Bash-tool call actually has (confirmed against the live harness: two
    separate Bash-tool invocations of `python3 -c "import os; print(os.getppid())"` returned
    different pids) -- and `os.getppid()` read from inside that forked child is a fresh, different
    value each call, never the stable calling process. `next`/`test_shipped_skill_start_then_next_
    via_real_separate_bash_dispatch_never_redispatches` (below) relies on exactly this forced-fork
    construction to be a real test at all."""
    runs = set()
    for _ in range(3):
        r = subprocess.run(["bash", "-c", 'python3 -c "import os; print(os.getppid())" ; true'],
                            capture_output=True, text=True)
        runs.add(r.stdout.strip())
    assert len(runs) == 3, (
        f"expected 3 DIFFERENT os.getppid() values across 3 genuinely-forked subprocess calls, got "
        f"{runs!r} -- if this environment's bash does not fork for a compound `-c` command the way "
        f"assumed, the redispatch test below cannot be trusted to exercise the real bug either"
    )


def test_shipped_skill_start_then_next_via_real_separate_bash_dispatch_never_redispatches():
    """#1239 review finding 1, corrected: the PREVIOUS version of this test (see git history)
    called `subprocess.run([sys.executable, str(S / "loop.py"), *a], ...)` with no shell -- which
    parents `loop.py` DIRECTLY under this one stable pytest process every time, so `os.getppid()`
    inside `loop.py` was accidentally stable across all three calls REGARDLESS of whether
    `--session-pid` was threaded through anywhere. Proof it was vacuous: run unchanged against the
    pre-#1239-review-fix `loop.py` (origin/main / the original PR #1239 head, where `next`'s CLI
    dispatch drops `--session-pid` on the floor and always falls back to its own internal
    `os.getppid()`), it PASSED -- it could never have caught the bug it claimed to guard against.

    This version forces the real topology instead: each command runs as its own COMPOUND `bash -c`
    call (`; true` after the payload prevents tail-exec, so bash genuinely forks a fresh subshell
    per call -- see `test_os_getppid_is_unstable_across_genuinely_separate_forked_dispatch` above,
    which proves that alone makes `os.getppid()` unstable in exactly this setup), and each call
    reads `$PPID` FRESH FROM WITHIN ITS OWN bash -c AND PASSES IT EXPLICITLY as `--session-pid`,
    exactly as SKILL.md/README now document (`--session-pid "$PPID"` on every `start`/`next`/
    `next-batch` call) -- `$PPID` inside a freshly-forked bash -c is that subshell's own parent
    (this pytest process), stable across calls, unlike `os.getppid()` read one level deeper from
    inside the spawned `loop.py` process.

    Verified both ways directly against the two `loop.py` states this repo actually has: run
    unchanged against the pre-fix `next`/`next-batch` CLI dispatch (drops `--session-pid` on the
    floor, `_next` falls back to ITS OWN unstable `os.getppid()`), the second `next` call
    RE-DISPATCHES the same goal -- red, the exact bug #1199 exists to close. Against the fixed
    dispatch (this diff -- `next`/`next-batch` now read `--session-pid` from argv and thread it
    into `_next`/`next_batch`), the second call correctly reports `DONE` -- green."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)

        def run(verb):
            cmd = f'{shlex.quote(sys.executable)} {shlex.quote(str(S / "loop.py"))} {verb} ' \
                  f'{shlex.quote(base)} --session-pid "$PPID" ; true'
            return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)

        assert run("start").returncode == 0
        first = run("next").stdout.strip()
        assert first.endswith("0001.md")           # the only goal, correctly claimed once
        second = run("next").stdout.strip()
        assert second == "DONE", (
            f"a THIRD separate, genuinely-forked `loop.py` dispatch re-dispatched the SAME goal "
            f"(got {second!r} instead of DONE) even with `--session-pid \"$PPID\"` passed explicitly "
            f"on every call -- the CLI is not threading the flag through to `_next`"
        )


def _spawn_sleeper():
    """A real, separate, genuinely-alive-for-a-while python process -- so a "three concurrent
    sessions" test has three real, distinct pids to register, the same way
    `_win_claim_lock_in_a_real_subprocess` (below) stands in for a real sibling process elsewhere
    in this file. Killed by the caller once no longer needed."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_session_claim_race_loses_a_goal_without_a_lock_guard():
    """#1239 review finding 3: `_session_claim`'s read-modify-write (read the marker file's current
    `in_flight` list, append the new goal, write the whole list back) had no atomicity guard of its
    own -- two genuinely CONCURRENT writers to the SAME session's marker file (e.g. two `next_batch`
    slots under one session, each claiming a different goal) can both read the same pre-write state
    and the second write silently clobbers the first's addition, a lost update. Real
    `multiprocessing.Process` pair, not threads -- the GIL can mask or narrow a race threads alone
    would show only intermittently -- barrier-synchronized so both writers genuinely overlap, the
    same reproduction shape the review finding itself used (10/10 runs lost a goal there).

    Pre-fix (`_session_claim` writing directly, no `_session_locked` guard), this reliably drops
    one of the two goals. Post-fix (`_session_claim` wrapped in `_session_locked`, mirroring
    `session_start`'s own guard), both survive every run."""
    import multiprocessing
    ctx = multiprocessing.get_context("fork")

    def _claim(sdlc, pid, goal, barrier):
        lp = _loop()
        barrier.wait()                     # both processes unblock at the same instant
        lp._session_claim(sdlc, pid, goal)

    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        pathlib.Path(sdlc).mkdir()
        pid = os.getpid()                  # both writers target THIS ONE session's own entry
        barrier = ctx.Barrier(2)
        p1 = ctx.Process(target=_claim, args=(sdlc, pid, "goalA", barrier))
        p2 = ctx.Process(target=_claim, args=(sdlc, pid, "goalB", barrier))
        p1.start(); p2.start(); p1.join(timeout=60); p2.join(timeout=60)   # 60 s: a loaded runner took >10 s (CI 4857377)
        assert not p1.is_alive() and not p2.is_alive()   # both writers actually finished
        lp = _loop()
        path = lp._session_marker_path(sdlc, pid)
        in_flight = set(lp._session_read(path)["in_flight"])
        assert in_flight == {"goalA", "goalB"}, (
            f"lost a concurrent claim under the same session -- expected both goals registered, "
            f"got {in_flight!r}"
        )


def test_session_claim_and_release_race_do_not_lose_updates():
    """#1239 review finding 3's own named "trigger scenario in production": one slot's goal
    finishing (`_session_release`) while a SIBLING slot under the SAME session claims its refill
    (`_session_claim`) -- exactly a `next_batch` refill racing a sibling slot's `record`/`release`,
    both against the one session's marker file. Pre-existing `goalX` must be cleanly removed AND
    the new `goalY` cleanly added, with neither writer's update lost to the other.

    This caught a SECOND, subtler bug beyond the missing lock itself: `_session_release`'s outer
    `_session_entries()` scan reads every session's file WITHOUT holding that session's lock, purely
    to decide whether it's worth acquiring the lock at all -- and `Path.write_text` truncates the
    file to 0 bytes on open, before writing the new content. If this unlocked scan lands inside a
    CONCURRENT locked writer's truncate-then-write window, it reads invalid/empty JSON,
    `_session_read`'s own by-design fail-open (`except (OSError, ValueError): return
    {"in_flight": []}`) reports an empty list, and the stale `if goal in in_flight` pre-check then
    skips the goal's session ENTIRELY -- `_session_release` returns having done nothing, no
    exception, no lock ever attempted, and the goal silently never gets released. Confirmed directly
    (temporary instrumentation): a real run hit exactly this -- the release process's own
    `_session_entries()` scan read `in_flight=[]` for a file `_session_claim` had already durably
    written `["goalX"]` to moments earlier. Fix: `_session_release` no longer trusts the outer
    scan's `in_flight` snapshot to decide whether to lock -- it always takes the lock and re-reads
    before deciding, for every session entry found, so the only read that can ever decide the
    outcome is the one taken while holding that session's own lock."""
    import multiprocessing
    ctx = multiprocessing.get_context("fork")

    def _claim(sdlc, pid, goal, barrier):
        lp = _loop()
        barrier.wait()
        lp._session_claim(sdlc, pid, goal)

    def _release(sdlc, goal, barrier):
        lp = _loop()
        barrier.wait()
        lp._session_release(sdlc, goal)

    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        pathlib.Path(sdlc).mkdir()
        pid = os.getpid()
        lp = _loop()
        lp._session_claim(sdlc, pid, "goalX")   # pre-seed, sequentially -- not part of the race
        assert set(lp._session_read(lp._session_marker_path(sdlc, pid))["in_flight"]) == {"goalX"}

        barrier = ctx.Barrier(2)
        p1 = ctx.Process(target=_claim, args=(sdlc, pid, "goalY", barrier))
        p2 = ctx.Process(target=_release, args=(sdlc, "goalX", barrier))
        p1.start(); p2.start(); p1.join(timeout=60); p2.join(timeout=60)   # 60 s: a loaded runner took >10 s (CI 4857377)
        assert not p1.is_alive() and not p2.is_alive()

        in_flight = set(lp._session_read(lp._session_marker_path(sdlc, pid))["in_flight"])
        assert in_flight == {"goalY"}, (
            f"claim/release race under one session lost an update -- expected only goalY left "
            f"(goalX released, goalY claimed), got {in_flight!r}"
        )


def test_three_concurrent_sessions_drain_backlog_with_zero_double_picks_and_no_skip():
    """#1199 AC6: three live sessions, each capped at `parallel.goals.max_concurrent: 2` (mirrored
    here as `next_batch(..., max_concurrent=2, ...)` per session), draining ONE shared backlog to
    empty -- every goal picked exactly once, across all three sessions combined, with no caller
    ever passing `--skip`. Two of the three "sessions" are real subprocesses (genuinely alive,
    genuinely distinct pids); the third is this test process itself.

    #1239 review round 3, finding C: this test had NO iteration cap of its own -- a regression in
    the exact skip-set/registry mechanism it guards (e.g. `next_batch` never reporting DONE/BUDGET
    for a live session, or a double-pick that never lets the backlog drain) would hang the WHOLE
    test run forever instead of failing cleanly. `MAX_ROUNDS` below is a hard ceiling, generous
    against the ~3 rounds a correct drain of 6 goals across 3 sessions (max_concurrent=2 each)
    actually needs, so a regression fails fast with a clear message instead of hanging."""
    procs = [_spawn_sleeper(), _spawn_sleeper()]
    try:
        session_pids = [os.getpid(), procs[0].pid, procs[1].pid]
        with tempfile.TemporaryDirectory() as d:
            base = _backlog(d, 6, max_iter=50)
            lp = _loop()
            src = lp.sources.get_source(base, lp.state.load_config(base))
            config = lp.state.load_config(base)
            picked = []
            sessions_done = [False, False, False]
            MAX_ROUNDS = 50
            for _round in range(MAX_ROUNDS):
                if all(sessions_done):
                    break
                for i, pid in enumerate(session_pids):
                    if sessions_done[i]:
                        continue
                    batch = lp.next_batch(base, src, config, max_concurrent=2, session_pid=pid)
                    for kind, payload in batch:
                        if kind == "goal":
                            picked.append(payload)
                        else:                          # DONE or BUDGET terminates this session's own drain
                            sessions_done[i] = True
            else:
                raise AssertionError(
                    f"drain did not finish within {MAX_ROUNDS} rounds -- a regression in the "
                    f"skip-set/registry mechanism this test guards is the likely cause (this "
                    f"test failing fast here, instead of hanging, IS the fix for #1239 review "
                    f"round 3 finding C); picked so far: {picked!r}, sessions_done: {sessions_done!r}"
                )
            assert len(picked) == len(set(picked)) == 6      # every goal picked exactly once
    finally:
        for p in procs:
            p.kill(); p.wait()


# ------------------------------------------------------------------ start-run: standalone state.start_run — #712
# `state.start_run` was already reachable via `run_loop()` (every drain) and via the full `start`
# verb (every session bootstrap) — but neither is a way to say, mid-session, "begin a fresh run"
# on its own: `run_loop()` isn't CLI-callable by itself, and `start` bundles the reset together with
# config-warning prints and an opt-in session-marker write. `start-run` exposes JUST the reset.


def test_cli_start_run_resets_the_budget_cursor():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        lp.state.add_tokens(base, 500)
        lp.state.advance_cursor(base, "prior iteration")     # bumps run_iteration off zero too
        before = lp.state.load_cursor(base)
        assert before["run_tokens"] == 500 and before["run_iteration"] == 1
        assert lp.main(["loop.py", "start-run", base]) == 0
        after = lp.state.load_cursor(base)
        assert after["run_tokens"] == 0 and after["run_iteration"] == 0
        assert after["run_started_at"] > 0     # the wall-clock anchor is stamped, same as `start`


def test_cli_start_run_never_touches_the_session_marker():
    """Unlike `start`, `start-run` never registers a session -- proving the two verbs stay
    decoupled (a fresh mid-session budget window must never silently create a session-liveness
    marker `session-active` would then report) rather than `start-run` quietly duplicating `start`.
    Since #955 `--session-pid` IS read: it names the session whose run block to reset, and only
    when that session's marker already exists, so with no marker it still creates none."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        assert lp.main(["loop.py", "start-run", base, "--session-pid", str(os.getpid())]) == 0
        assert lp.session_active(base, {}) is False


# ------------------------------------------------------------------ agent marker: per (goal, thread) — #465
# Generalizes the session-active marker above from one whole-.sdlc_dir pid to one pid per
# (goal, thread), so a background/subagent's death is detectable goal-by-goal instead of only
# session-wide. Same two-signal liveness check (pid_alive() + lease TTL) as session_active.

AGENT_WATCH_ON = {"agent_watch": {"enabled": True}}


def test_agent_start_writes_and_agent_alive_reads_a_live_pid():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON)
        assert lp.agent_alive(sdlc, "g.md", AGENT_WATCH_ON) == ("alive", os.getpid())


def test_agent_alive_reports_dead_for_a_definitively_dead_pid():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        dead_pid = 2**30                                # not a real pid on any sane system
        lp.agent_start(sdlc, "g.md", dead_pid, AGENT_WATCH_ON)
        assert lp.agent_alive(sdlc, "g.md", AGENT_WATCH_ON) == ("dead", dead_pid)


def test_agent_alive_reports_unknown_with_no_marker_at_all():
    """"unknown" (nobody registered) must never collapse into "dead" -- a claim held by a
    different actor, a different machine, or a pre-#465 session all leave no marker at all."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        assert lp.agent_alive(sdlc, "g.md", AGENT_WATCH_ON) == ("unknown", None)


def test_agent_alive_expires_a_stale_marker_past_the_ttl_even_for_a_resolvable_pid():
    """Mirrors test_session_active_expires_a_stale_marker_past_the_ttl_even_for_a_resolvable_pid."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON)   # alive pid throughout
        path = lp._agent_marker_path(sdlc, "g.md")
        stale = time.time() - (lp.ledger.DEFAULT_LEASE_TTL_HOURS * 3600 + 60)
        os.utime(path, (stale, stale))
        assert lp.agent_alive(sdlc, "g.md", AGENT_WATCH_ON) == ("dead", os.getpid())


def test_agent_start_writes_the_marker_regardless_of_agent_watch_config():
    """#1197: this used to be a no-op unless `agent_watch.enabled is True`. But the SHIPPED config
    template sets `agent_watch: {"enabled": false, ...}` EXPLICITLY, not merely absent — so gating
    the marker WRITE on that flag made it inert for every fresh `/sigma-init` scaffold, exactly the
    gap `ledger.claim_belongs_to_me`'s new `live_worker_check` now depends on this marker to close.
    Registering it is unconditional now (cheap, fail-open, local-only); the SEPARATE dead-agent
    NOTIFY tick (`agent_watch.py`'s own `enabled()`) is untouched and still gated."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.agent_start(sdlc, "g.md", os.getpid(), {})              # no agent_watch block at all
        assert lp.agent_alive(sdlc, "g.md", {}) == ("alive", os.getpid())
        lp.agent_end(sdlc, "g.md")
        lp.agent_start(sdlc, "g.md", os.getpid(), {"agent_watch": {"enabled": False}})
        assert lp.agent_alive(sdlc, "g.md", {}) == ("alive", os.getpid())


def test_agent_start_is_a_safe_noop_on_an_unparseable_pid():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.agent_start(sdlc, "g.md", "not-a-pid", AGENT_WATCH_ON)  # must not raise
        assert lp.agent_alive(sdlc, "g.md", AGENT_WATCH_ON) == ("unknown", None)


def test_agent_start_returns_false_not_none_for_an_unparseable_pid_against_a_live_marker():
    """Code review, #2527: `_write()`'s own new PID comparison (`marker_pid != pid_int`) parses
    `agent_pid` before it can ever reach the pre-existing owner check -- an unparseable value must
    still return the SAME clean `False` every other branch in this function already gives, not
    raise past the comparison into the outer `except`, which answers `None` (not `False`) for a
    plain Claude caller -- ambiguous where `loop.py`'s own CLI dispatch branches on `is False`.
    Unreachable via the shipped CLI (`--pid` is validated before `agent_start` is ever called) or
    any in-repo caller today, but the invariant costs nothing to keep and is otherwise untested."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        proc = _spawn_sleeper()
        try:
            assert lp.agent_start(sdlc, "g.md", proc.pid, AGENT_WATCH_ON) is True
            assert lp.agent_start(sdlc, "g.md", "not-a-pid", AGENT_WATCH_ON) is False
            assert lp.agent_alive(sdlc, "g.md", AGENT_WATCH_ON) == ("alive", proc.pid)
        finally:
            proc.kill(); proc.wait()


def test_agent_threads_lists_every_registered_thread_for_a_goal():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        assert lp.agent_threads(sdlc, "g.md") == []
        lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON)
        lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON, thread="slice-a1")
        assert lp.agent_threads(sdlc, "g.md") == ["main", "slice-a1"]


def test_agent_end_clears_every_thread_for_a_goal():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON)
        lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON, thread="slice-a1")
        assert lp.agent_threads(sdlc, "g.md") == ["main", "slice-a1"]
        lp.agent_end(sdlc, "g.md")
        assert lp.agent_threads(sdlc, "g.md") == []


def test_agent_end_is_a_safe_noop_when_nothing_was_ever_started():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        lp.agent_end(d + "/.sdlc", "g.md")              # must not raise


def test_agent_end_clears_a_marker_even_when_agent_watch_was_since_disabled():
    """Cleanup carries no gate of its own — same as `agent_start`'s write, as of #1197 — a marker
    written earlier must not linger forever just because config later "turned it off" (a value
    neither call has consulted since #1197 in any case)."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON)
        assert lp.agent_threads(sdlc, "g.md") == ["main"]
        lp.agent_end(sdlc, "g.md")                       # called with agent_watch off/absent, still clears
        assert lp.agent_threads(sdlc, "g.md") == []


def test_record_calls_agent_end_regardless_of_outcome():
    """One regression test per terminal outcome, matching this file's own hardened-sibling-
    divergence discipline: the background-agent-death watcher (#465) must not silently stop
    firing on a future edit to _record()."""
    lp = _loop()
    for verb in ("done", "parked", "failed"):
        with tempfile.TemporaryDirectory() as d:
            base = _backlog(d, 1)
            goal = base + "/goals/0001.md"
            config = lp.state.load_config(base)
            lp.agent_start(base, goal, os.getpid(), AGENT_WATCH_ON)
            assert lp.agent_threads(base, goal) == ["main"]
            source = lp.sources.get_source(base, config)
            lp._record(base, source, goal, verb, "reason" if verb != "done" else "")
            assert lp.agent_threads(base, goal) == []


def _write_phase_marker(base, goal, phase="retro"):
    """Stamp a `phase_report.py`-shaped marker the way `phase_report.py start` does, without
    importing that module -- this file's subject is `loop.py`'s own cleanup, not the stamper."""
    stem = pathlib.Path(goal).name
    if stem.endswith(".md"):
        stem = stem[:-3]
    path = pathlib.Path(base) / "state" / "phase" / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"phase": phase, "goal": goal, "model": "opus", "title": "t",
                                "ts_start": "2026-08-25T02:09:00Z", "ts_start_epoch": 1_756_087_740.0},
                               sort_keys=True))
    return path


def test_record_prunes_the_goals_own_phase_marker_regardless_of_outcome():
    """#2658: nothing ever unlinked `.sdlc/state/phase/<goal>.json`. Observed 2026-09-24: 63 had
    accumulated, the oldest 30 days old, 57 of them belonging to CLOSED issues. `_record()` is the
    one chokepoint every terminal outcome passes through -- the same reasoning, and the same call
    site, as the `agent_end()` cleanup one line above it.

    One case per terminal verb, matching `test_record_calls_agent_end_regardless_of_outcome`'s own
    hardened-sibling discipline: a future edit to `_record()` must not silently stop pruning."""
    lp = _loop()
    for verb in ("done", "parked", "failed"):
        with tempfile.TemporaryDirectory() as d:
            base = _backlog(d, 1)
            goal = base + "/goals/0001.md"
            marker = _write_phase_marker(base, goal)
            assert marker.exists()
            source = lp.sources.get_source(base, lp.state.load_config(base))
            lp._record(base, source, goal, verb, "reason" if verb != "done" else "")
            assert not marker.exists(), f"{verb} left {marker} behind"


def test_record_prunes_the_marker_the_real_phase_report_start_wrote():
    """#2667 (Task 6): the writer and the pruner are two different scripts (`phase_report.py` and
    `loop.py`), so a hand-shaped fixture like `_write_phase_marker` above proves nothing about
    whether the REAL writer's marker still lands somewhere `_record()`'s prune actually reaches.
    Runs the real CLI and selects the new marker BY CONTENT (`phase == "retro"` and a `ts_start`),
    never by re-deriving its path: `start` under a Claude Code session ALSO writes
    `.sdlc/state/time/_sessions/<session>/<pid>.jsonl` (measured), so "the one new file under
    state/" is the wrong predicate and would make the non-vacuity assertion below pass for the
    wrong reason."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        goal = base + "/goals/0001.md"
        before = {p for p in pathlib.Path(base).rglob("*") if p.is_file()}
        result = subprocess.run(
            [sys.executable, str(S / "phase_report.py"), "start", base, goal, "retro",
             "--model", "opus", "--pid", str(os.getpid())],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        after = {p for p in pathlib.Path(base).rglob("*") if p.is_file()}
        new_files = after - before

        candidates = []
        for path in new_files:
            try:
                obj = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(obj, dict) and obj.get("phase") == "retro" and obj.get("ts_start"):
                candidates.append(path)
        assert len(candidates) == 1, (
            f"expected exactly one new retro phase marker among {sorted(new_files)}, "
            f"found {candidates}")
        marker = candidates[0]
        assert marker.exists()

        source = lp.sources.get_source(base, lp.state.load_config(base))
        lp._record(base, source, goal, "done")
        assert not marker.exists(), f"done left the real writer's {marker} behind"


def test_record_never_prunes_another_goals_phase_marker():
    """The load-bearing half. A phase marker for a goal still RUNNING -- in this session or another
    -- is what `end` measures its cost and elapsed from, so recording goal A must touch nothing but
    A's own file. `_record` is per-goal and terminal by definition, which is why this is safe at
    all; pruning on `_release` would NOT be, and deliberately is not done (see #1391 in
    `_release`'s own docstring: clearing a live goal's marker on a reclaim was a data-loss chain)."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2)
        finished, running = base + "/goals/0001.md", base + "/goals/0002.md"
        gone = _write_phase_marker(base, finished, phase="retro")
        live = _write_phase_marker(base, running, phase="implement")
        source = lp.sources.get_source(base, lp.state.load_config(base))
        lp._record(base, source, finished, "done")
        assert not gone.exists()
        assert live.exists(), "recording one goal must never disturb another goal's live phase"
        assert json.loads(live.read_text())["phase"] == "implement"


# ------------------------------------------------------------------ agent_reclaim: identity-checked
# escape hatch (#2015) -- `agent_end()`'s ONE production caller, `_record()`, is legitimate: the
# goal is genuinely OVER, so clearing every thread's marker is correct. But the bare `agent-end`
# CLI verb removed below was `work.py`'s own SUGGESTED workaround for a blocked resume ("If the
# process is genuinely gone, clear it with `loop.py agent-end ...`") and called the SAME
# unconditional whole-directory `shutil.rmtree` -- so any agent that followed that suggestion, or
# was merely wrong about liveness, silently evicted every OTHER thread's live registration too,
# with no verification and no trace. `agent_reclaim()` is the identity-checked replacement: it
# checks each target thread's OWN recorded pid via `agent_alive()` before removing anything, and
# removes only that one thread's marker FILE -- never the parent directory.

def test_agent_reclaim_removes_only_a_confirmed_dead_threads_marker():
    """THE CONTROL for #2015 (see the sibling test below for the reproduced bug this replaces).
    Two threads on one goal: "main" is genuinely alive (this test process's own pid), "slice-a1"
    is genuinely dead (an unreachable pid). A sweep (`thread=None`) must remove ONLY slice-a1's
    marker file and record the takeover, leaving main's marker -- file and content -- untouched."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON)                  # "main": alive
        dead_pid = 2**30                                    # not a real pid on any sane system
        lp.agent_start(sdlc, "g.md", dead_pid, AGENT_WATCH_ON, thread="slice-a1")  # dead
        main_path = lp._agent_marker_path(sdlc, "g.md", thread="main")
        before = main_path.read_text()

        result = lp.agent_reclaim(sdlc, "g.md", AGENT_WATCH_ON)

        assert result["reclaimed"] == [("slice-a1", dead_pid)]
        assert result["blocked"] == [("main", os.getpid())]
        assert lp.agent_threads(sdlc, "g.md") == ["main"]                  # slice-a1's file is gone...
        assert main_path.exists() and main_path.read_text() == before     # ...main survives byte-identical


def test_agent_end_is_the_bug_agent_reclaim_replaces():
    """Non-vacuous control (AGENTS.md: 'run the control, or the check is decoration'). NOT a call
    to change `agent_end()` -- it stays correct for its one real caller, `_record()`, where the
    goal really is over. Reproduced here only to prove the identity check above is closing a real,
    live defect: the exact call `work.py`'s old refusal message used to hand a blocked agent
    (`loop.py agent-end <dir> <goal>`) silently deletes a live sibling's registration too."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON)                  # alive
        lp.agent_start(sdlc, "g.md", 2**30, AGENT_WATCH_ON, thread="slice-a1")     # dead
        lp.agent_end(sdlc, "g.md")
        assert lp.agent_threads(sdlc, "g.md") == []          # main's LIVE registration is gone too


def test_agent_reclaim_refuses_and_reports_when_the_thread_is_genuinely_alive():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON)
        result = lp.agent_reclaim(sdlc, "g.md", AGENT_WATCH_ON, thread="main")
        assert result == {"reclaimed": [], "blocked": [("main", os.getpid())]}
        assert lp.agent_threads(sdlc, "g.md") == ["main"]


def test_agent_reclaim_is_a_safe_noop_for_an_unregistered_thread():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        result = lp.agent_reclaim(sdlc, "g.md", AGENT_WATCH_ON, thread="never-started")
        assert result == {"reclaimed": [], "blocked": []}


def test_agent_reclaim_narrows_to_one_thread_and_leaves_the_rest_alone():
    """`thread=<name>` must not sweep -- a caller who already knows exactly which thread blocked
    it (work.py's own refusal message names one) must not touch any other thread as a side effect,
    even one that is ALSO genuinely dead."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        lp.agent_start(sdlc, "g.md", 2**30, AGENT_WATCH_ON, thread="slice-a1")   # dead
        lp.agent_start(sdlc, "g.md", 2**30 - 1, AGENT_WATCH_ON, thread="slice-b1")  # also dead
        result = lp.agent_reclaim(sdlc, "g.md", AGENT_WATCH_ON, thread="slice-a1")
        assert result == {"reclaimed": [("slice-a1", 2**30)], "blocked": []}
        assert lp.agent_threads(sdlc, "g.md") == ["slice-b1"]      # untouched, even though also dead


def test_agent_reclaim_writes_an_action_log_entry_for_every_takeover():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        goal = base + "/goals/0001.md"
        _with_action_log(base)
        lp.agent_start(base, goal, 2**30, AGENT_WATCH_ON, thread="slice-a1")
        lp.agent_reclaim(base, goal, AGENT_WATCH_ON, thread="slice-a1")
        entries = lp.actionlog.read_goal(base, goal)
        hits = [e for e in entries if e["kind"] == "agent_reclaimed"]
        assert len(hits) == 1
        assert hits[0]["thread"] == "slice-a1" and hits[0]["pid"] == str(2**30)


def test_agent_reclaim_never_raises_on_an_unsafe_thread_name():
    """Defense in depth: even called directly (not through the CLI's own pre-check), an unsafe
    `thread` degrades to nothing-to-reclaim rather than raising -- the same fail-open posture
    every other `_agent_marker_path`-adjacent function in this file already takes."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        result = lp.agent_reclaim(sdlc, "g.md", AGENT_WATCH_ON, thread="../../evil")
        assert result == {"reclaimed": [], "blocked": []}


def test_cli_agent_start_requires_pid(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        rc = lp.main(["loop.py", "agent-start", base, base + "/goals/0001.md"])
        assert rc == 2
        assert "--pid is required" in capsys.readouterr().err


def test_cli_agent_start_rejects_a_non_integer_pid(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        lp = _loop()
        rc = lp.main(["loop.py", "agent-start", base, base + "/goals/0001.md", "--pid", "nope"])
        assert rc == 2
        assert "not an integer" in capsys.readouterr().err


def test_cli_agent_start_and_agent_reclaim_round_trip():
    """#2015: the CLI escape hatch is `agent-reclaim` now, not the old whole-directory `agent-end`
    -- and it must clear only a genuinely dead thread, leaving a live one (registered with THIS
    test process's own real, live pid) untouched."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        goal = base + "/goals/0001.md"
        lp = _loop()
        cfg_path = pathlib.Path(base) / "config.json"
        cfg_path.write_text(json.dumps({"budget": {"max_iterations": 10},
                                        "agent_watch": {"enabled": True}}))
        assert lp.main(["loop.py", "agent-start", base, goal, "--pid", str(os.getpid())]) == 0
        assert lp.agent_threads(base, goal) == ["main"]
        assert lp.main(["loop.py", "agent-start", base, goal, "--pid", "1073741824",
                        "--thread", "slice-a1"]) == 0                      # dead pid
        assert lp.agent_threads(base, goal) == ["main", "slice-a1"]
        assert lp.main(["loop.py", "agent-reclaim", base, goal, "--thread", "slice-a1"]) == 0
        assert lp.agent_threads(base, goal) == ["main"]                   # only slice-a1 was cleared


def test_cli_agent_reclaim_reports_failure_and_a_nonzero_exit_when_still_blocked():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        goal = base + "/goals/0001.md"
        lp = _loop()
        cfg_path = pathlib.Path(base) / "config.json"
        cfg_path.write_text(json.dumps({"budget": {"max_iterations": 10},
                                        "agent_watch": {"enabled": True}}))
        assert lp.main(["loop.py", "agent-start", base, goal, "--pid", str(os.getpid())]) == 0
        rc = lp.main(["loop.py", "agent-reclaim", base, goal])
        assert rc == 1
        assert lp.agent_threads(base, goal) == ["main"]                   # the live thread survives


def test_cli_agent_reclaim_rejects_a_thread_with_path_traversal(capsys):
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        goal = base + "/goals/0001.md"
        rc = lp.main(["loop.py", "agent-reclaim", base, goal, "--thread", "../../evil"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "thread" in err and "invalid" in err.lower()


def test_cli_agent_end_verb_is_retired_and_falls_through_to_usage(capsys):
    """#2015: the blind whole-directory escape hatch is gone from the CLI surface -- the Python
    `agent_end()` function it used to dispatch to is untouched (still `_record()`'s own legitimate
    cleanup); only the unsafe manual entry point is removed, in favour of `agent-reclaim` above."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        goal = base + "/goals/0001.md"
        lp = _loop()
        rc = lp.main(["loop.py", "agent-end", base, goal])
        assert rc == 2
        assert "usage" in capsys.readouterr().err.lower()


# ------------------------------------------------------------------ thread path-traversal (PR #467 review)
# Independent review of #467 reproduced a real write-outside-sandbox primitive: `thread` (an
# LLM-authored slice id from .sdlc/plans/<goal>.slices.json, validated by slices.py with only
# .strip() -- no character/format check) was spliced directly into _agent_marker_path's join with
# no validation. `f"{thread}.active"` is a single Python string, but pathlib's own `/` join
# operator RE-PARSES it for separator characters -- so a `/` inside `thread` becomes additional
# path segments, some of which can be a literal `..`, escaping .sdlc/state/agents/<goal>/ entirely
# once the OS resolves the path during mkdir/write. These tests are NON-VACUOUS: written and run
# BEFORE the fix below, they failed with the exact symptom (a real file created outside the
# sandbox, e.g. at tmp_path/evil.active); after the fix, they pass.

TRAVERSAL_THREAD = "../../../../evil"    # 4 levels: g's dir -> agents -> state -> sdlc_dir -> escapes it


def test_agent_marker_path_rejects_a_thread_containing_a_path_separator():
    lp = _loop()
    for bad in (TRAVERSAL_THREAD, "..", "a/b", "a\\b", "C:evil", "/etc/passwd"):
        try:
            lp._agent_marker_path("/tmp/.sdlc", "g.md", thread=bad)
            assert False, f"expected _agent_marker_path to refuse thread={bad!r}"
        except ValueError:
            pass


def test_agent_marker_path_accepts_ordinary_thread_ids():
    """No false positives: real slice ids (main, slice-a1, dotted versions) still work."""
    lp = _loop()
    for ok in ("main", "slice-a1", "v1.2"):
        path = lp._agent_marker_path("/tmp/.sdlc", "g.md", thread=ok)
        assert path.name == f"{ok}.active"


def test_agent_start_never_writes_a_marker_outside_the_sandbox_for_a_traversal_thread(tmp_path):
    """The actual reviewer-reproduced exploit, proven functionally against the real filesystem --
    not just that a ValueError is raised somewhere. Before the fix this created tmp_path/evil.active
    (four '../' unwind exactly out of .sdlc/state/agents/<goal>/ back past .sdlc/ itself)."""
    lp = _loop()
    sdlc = str(tmp_path / ".sdlc")
    lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON, thread=TRAVERSAL_THREAD)
    assert not (tmp_path / "evil.active").exists()                     # did not escape .sdlc/ entirely
    assert not (tmp_path / ".sdlc" / "evil.active").exists()           # nor even to the .sdlc root
    for p in tmp_path.rglob("*.active"):
        assert str(p.parent).startswith(str(tmp_path / ".sdlc" / "state" / "agents")), \
            f"a marker file landed outside the sandbox: {p}"


def test_agent_alive_returns_unknown_for_a_traversal_thread_rather_than_raising():
    """agent_alive must stay fail-open (never raise) even for a malicious thread -- "unknown" is
    the semantically correct answer: nobody validly registered this (goal, thread)."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        assert lp.agent_alive(sdlc, "g.md", AGENT_WATCH_ON, thread=TRAVERSAL_THREAD) == ("unknown", None)


def test_cli_agent_start_rejects_a_thread_with_path_traversal(capsys):
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        goal = base + "/goals/0001.md"
        rc = lp.main(["loop.py", "agent-start", base, goal, "--pid", str(os.getpid()),
                     "--thread", TRAVERSAL_THREAD])
        assert rc == 2
        err = capsys.readouterr().err
        assert "thread" in err and ("invalid" in err.lower() or "not valid" in err.lower())
        # nothing escaped onto disk outside base's own .sdlc tree
        assert not (pathlib.Path(base).parent / "evil.active").exists()
        assert not (pathlib.Path(base) / "evil.active").exists()


# ------------------------------------------------------------------ goal path-traversal (PR #486/#487 review)
# Independent review of PR #487 (which fixed actionlog.py's own instance of this bug) found the
# IDENTICAL unguarded pattern repeated across loop.py: `goal` (untrusted exactly like `thread`
# above) was spliced into _agent_marker_path/_claim_lock_path/verify_goal's evidence path with NO
# validation, unlike thread. agent_end()'s case is the most severe: an unconditional, ungated
# shutil.rmtree() on the resulting path, reachable from the everyday `record` verb (not just the
# `agent-end` escape hatch). These tests are NON-VACUOUS -- run against pre-fix code they fail with
# the exact symptom (a real directory deleted / file written outside the sandbox); after the fix
# they pass.

TRAVERSAL_GOAL = "../../../evil-goal"    # 3 levels: <subdir> -> state -> sdlc_dir -> escapes to tmp_path


def test_agent_marker_path_rejects_a_goal_containing_a_path_separator():
    lp = _loop()
    for bad in (TRAVERSAL_GOAL, "..", "a/b", "a\\b", "C:evil"):
        try:
            lp._agent_marker_path("/tmp/.sdlc", bad)
            assert False, f"expected _agent_marker_path to refuse goal={bad!r}"
        except ValueError:
            pass


def test_agent_end_never_deletes_a_real_directory_outside_the_sandbox_for_a_traversal_goal(tmp_path):
    """The reviewer's own live reproduction, proven functionally against the real filesystem: a
    real directory + file planted OUTSIDE .sdlc, at exactly the location the pre-fix code's
    shutil.rmtree() would resolve to for TRAVERSAL_GOAL, must survive agent_end() untouched.
    `state/agents/` must exist first -- POSIX path resolution needs every intermediate component
    of a `..`-bearing path to actually exist before the `..` segments can resolve at all; a real
    agent_watch-enabled repo already has this directory from an earlier legitimate agent_start()
    by the time agent_end() ever runs, so creating it here matches the real precondition, not just
    a convenient shortcut."""
    lp = _loop()
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state" / "agents").mkdir(parents=True)
    victim_dir = tmp_path / "evil-goal"          # 3 levels of '../' from .sdlc/state/agents/<goal>/
    victim_dir.mkdir()
    victim_file = victim_dir / "important-data.txt"
    victim_file.write_text("do not delete me")

    lp.agent_end(str(sdlc), TRAVERSAL_GOAL)      # must not raise (fail-open), must not delete

    assert victim_dir.is_dir()
    assert victim_file.read_text() == "do not delete me"


def test_agent_threads_returns_empty_for_a_traversal_goal_rather_than_raising():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        assert lp.agent_threads(d + "/.sdlc", TRAVERSAL_GOAL) == []


def test_agent_reclaim_never_deletes_a_real_directory_outside_the_sandbox_for_a_traversal_goal(tmp_path):
    """Same live reproduction as `test_agent_end_never_deletes...` above, aimed at the new
    function: `agent_reclaim`'s own `agent_threads`/`agent_alive` calls degrade a traversal goal to
    "no threads registered" before any deletion is ever attempted, so this must be a pure no-op."""
    lp = _loop()
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state" / "agents").mkdir(parents=True)
    victim_dir = tmp_path / "evil-goal"
    victim_dir.mkdir()
    victim_file = victim_dir / "important-data.txt"
    victim_file.write_text("do not delete me")

    result = lp.agent_reclaim(str(sdlc), TRAVERSAL_GOAL, AGENT_WATCH_ON)   # must not raise

    assert result == {"reclaimed": [], "blocked": []}
    assert victim_dir.is_dir()
    assert victim_file.read_text() == "do not delete me"


def test_agent_reclaim_returns_a_safe_noop_for_a_traversal_thread_rather_than_raising():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        result = lp.agent_reclaim(sdlc, "g.md", AGENT_WATCH_ON, thread=TRAVERSAL_THREAD)
        assert result == {"reclaimed": [], "blocked": []}


def test_claim_lock_path_rejects_a_traversal_goal():
    lp = _loop()
    try:
        lp._claim_lock_path("/tmp/.sdlc", TRAVERSAL_GOAL)
        assert False, "expected _claim_lock_path to refuse a traversal goal"
    except ValueError:
        pass


def test_try_acquire_claim_lock_fails_open_for_a_traversal_goal(tmp_path):
    """Same fail-open posture as no-fcntl / cannot-create-directory (existing tests above) --
    an unsafe goal degrades to _LOCK_UNAVAILABLE, never a raw crash, never a lock written outside
    the sandbox."""
    lp = _loop()
    sdlc = str(tmp_path / ".sdlc")
    assert lp._try_acquire_claim_lock(sdlc, TRAVERSAL_GOAL) == lp._LOCK_UNAVAILABLE
    assert not (tmp_path / "evil-goal.lock").exists()


def test_cli_verify_refuses_a_traversal_goal_and_writes_no_evidence_outside_the_sandbox(tmp_path, capsys):
    """The reviewer's own live reproduction: `loop.py verify <dir> "<traversal-goal>"` used to
    write a file outside .sdlc and report VERIFIED (exit 0). Checked before the proving command
    even runs (see verify_goal's own docstring) -- exit 2, not 0 or 1."""
    lp = _loop()
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text(json.dumps({"verify": {"command": "true"}}))
    rc = lp.main(["loop.py", "verify", str(sdlc), TRAVERSAL_GOAL])
    assert rc == 2
    assert "unsafe goal" in capsys.readouterr().err
    assert not (tmp_path / "evil-goal.json").exists()


def test_cli_verbs_handle_a_never_init_d_sdlc_dir_gracefully(capsys):
    """#403: `next`/`next-batch`/`start`/`session-active` all call `state.load_config` before any
    of their own logic runs. Pointed at a `.sdlc` that was never `/sigma-init`'d (no config.json at
    all), each used to crash with a raw, unhandled FileNotFoundError traceback instead of a usable
    message. Non-vacuous: reverting just the fix (`state.ConfigMissing` + `loop.py` main()'s catch)
    makes this fail — `rc` comes back `None` (the process would have raised instead of returning)
    and/or "Traceback" appears on stderr."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = str(pathlib.Path(d) / ".sdlc")             # deliberately never created at all
        for argv in (["loop.py", "next", base],
                     ["loop.py", "next-batch", base],
                     ["loop.py", "start", base],
                     ["loop.py", "session-active", base]):
            rc = lp.main(argv)
            err = capsys.readouterr().err
            assert rc == 2, f"{argv[1]}: expected a clean exit 2, got {rc!r}"
            assert "Traceback" not in err, f"{argv[1]}: a raw traceback leaked to stderr: {err!r}"
            assert "config.json" in err and "/sigma-init" in err, (
                f"{argv[1]}: stderr isn't an actionable one-liner: {err!r}")


# --------------------------------------------------------------------- writer-aware lease (#374)
# A claim held by MY OWN actor is not always mine to resume: two concurrent processes sharing one
# gh login (a routine firing again before an earlier run finished) must not read each other's
# claims as "mine" just because the actor matches. See ledger.claim_belongs_to_me.


def test_next_skips_a_goal_a_live_sibling_process_of_my_own_actor_holds(monkeypatch):
    """THE regression this issue exists to close: reproduces exactly what a stakeholder hit live
    -- a routine's fresh invocation must not resume a goal a DIFFERENT, still-running process of
    the SAME actor already claimed. It must skip to the next eligible goal instead, exactly like
    it already does for a genuinely different actor."""
    real_pid = os.getpid()                            # this test process's REAL pid -- verifiably
    with tempfile.TemporaryDirectory() as d:           # alive right now, a true positive for pid_alive
        base = _lease_base(d, actor="me", claims=[("me", "a", "claimed", real_pid)])
        monkeypatch.setattr(os, "getpid", lambda: real_pid + 1)  # simulate a DIFFERENT process of "me"
        lp = _loop(); src = _Queue(["a", "b"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "b")           # "a" belongs to a live sibling -- passed over
        assert lp.ledger.open_claims(lp.ledger.read_all(base))["a"] == "me"   # still "me" -- untouched


def test_next_reclaims_a_goal_a_dead_sibling_process_of_my_own_actor_held(monkeypatch):
    """A crashed sibling's claim is still safely reclaimable -- liveness-checking must not become
    a NEW way to wedge a goal forever (the existing TTL already covers this case too; this proves
    the FASTER, liveness-based path also works, not just the slow TTL fallback)."""
    with tempfile.TemporaryDirectory() as d:
        dead_pid = 2**30                               # not a real pid on any sane system
        base = _lease_base(d, actor="me", claims=[("me", "a", "claimed", dead_pid)])
        monkeypatch.setattr(os, "getpid", lambda: dead_pid + 1)
        lp = _loop(); src = _Queue(["a"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")           # dead sibling's claim -- safe to reclaim


# --------------------------------------------------------------------- #1197: dead-PICKER pid is
# not the same question as dead-WORKER -- the claim's writer pid is always the short-lived `next`
# invocation, dead within moments of every acquisition whether the goal is abandoned or being
# actively worked. The two tests above only ever set up a claim with NO worker marker registered
# at all; the two below are the actual #1197 regression: a REGISTERED worker corroborates (or, once
# it too has died, no longer corroborates) the dead picker pid.


def test_next_skips_a_dead_pickers_claim_when_a_live_worker_marker_is_registered(monkeypatch):
    """THE #1197 regression, at the picker level: a confirmed-dead writer pid must no longer be
    decisive on its own once a genuine long-lived worker for THIS goal is registered (`agent_start`,
    exactly as SKILL.md step 3a's `agent-start --pid $PPID` does after a real dispatch) -- `_next()`
    must skip past it to the next eligible goal, not re-pick a goal a live subagent is still
    actively driving. Reproduces the report's own scenario: two `/sigma-loop` sessions minutes
    apart, same account, same machine."""
    real_worker_pid = os.getpid()                     # THIS test process -- verifiably alive
    with tempfile.TemporaryDirectory() as d:
        dead_picker_pid = 2**30                        # not a real pid on any sane system
        base = _lease_base(d, actor="me", claims=[("me", "a", "claimed", dead_picker_pid)])
        lp = _loop()
        lp.agent_start(base, "a", real_worker_pid, {})    # the real worker for "a" -- still alive
        monkeypatch.setattr(os, "getpid", lambda: dead_picker_pid + 1)   # a second picker's own pid
        src = _Queue(["a", "b"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "b"), (
            f"expected 'a' to be skipped in favor of 'b' -- its dead picker pid is corroborated by "
            f"a live agent_alive marker, so it must not be re-picked out from under the real "
            f"worker; got {(kind, goal)!r}")
        assert lp.ledger.open_claims(lp.ledger.read_all(base))["a"] == "me"   # "a" left untouched


def test_next_reclaims_a_dead_pickers_claim_once_its_registered_worker_has_also_died(monkeypatch):
    """The other half of #1197's own acceptance bar: killing session A mid-goal must not leave the
    goal permanently unpickable. A worker WAS registered for this goal (unlike the plain
    dead-sibling test above, which never registers one at all) but it has since died too -- a
    genuine crash, not merely its picker exiting normally. Once BOTH signals read dead, `_next()`
    reclaims exactly as it always has for a plain dead sibling.

    The reclaim OUTCOME alone cannot tell this from the pre-#1197 code, which reclaimed on a dead
    picker pid unconditionally and never consulted a worker signal at all -- for this exact
    both-dead scenario the two implementations are output-identical, so a stub `live_worker_check`
    that is never even called would pass just as well. `calls` below is the actual regression
    guard: it proves `_goal_has_registered_worker` -- the PER-GOAL check, not the OR'd
    `_claimed_goal_has_live_worker` (see that function's own docstring for why the reclaim-refusal
    path must never consult the wider, goal-agnostic signal) -- was genuinely consulted (and
    returned False) on the way to this reclaim, the same way the ledger-level `..._never_calls_
    ..._for_a_live_sibling_pid` tests in test_ledger.py prove the NEGATIVE with their own `boom()`
    sentinel."""
    with tempfile.TemporaryDirectory() as d:
        dead_picker_pid = 2**30
        dead_worker_pid = 2**30 - 1                    # also not a real pid on any sane system
        base = _lease_base(d, actor="me", claims=[("me", "a", "claimed", dead_picker_pid)])
        lp = _loop()
        lp.agent_start(base, "a", dead_worker_pid, {})    # session A's own worker -- since died
        monkeypatch.setattr(os, "getpid", lambda: dead_picker_pid + 1)

        calls = []
        real_check = lp._goal_has_registered_worker
        def spying_check(sdlc_dir, goal, config):
            calls.append(goal)
            return real_check(sdlc_dir, goal, config)
        monkeypatch.setattr(lp, "_goal_has_registered_worker", spying_check)

        src = _Queue(["a"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")   # both picker and worker confirmed dead -- reclaim
        assert calls == ["a"], (
            f"expected _goal_has_registered_worker to actually be consulted for the dead-picker "
            f"claim before reclaiming it -- got {calls!r}; without this the test cannot tell "
            f"#1197's real fix from a stub that never checks the worker at all and just reclaims "
            f"on a dead picker pid the way the pre-#1197 code always did")


def test_next_reclaims_a_crashed_goals_claim_even_while_an_unrelated_session_is_active(monkeypatch):
    """Independent review of PR #1237: the exact bug the goal-agnostic/per-goal correction above
    exists for, reproduced end-to-end. `_claimed_goal_has_live_worker` -- reused, pre-correction, as
    `_next()`'s own `live_worker_check` -- ORs a GOAL-AGNOSTIC signal (`session_active`: ONE marker
    per `.sdlc`, true if ANY managing session is registered, regardless of which goal it is working)
    into a decision that #1197 itself scopes to ONE SPECIFIC goal. Reproduced scenario: `session_
    start` registers a genuinely live, long-lived managing session (the project's own documented
    overnight/routine shape, `--session-pid`) -- for the whole `.sdlc`, not for goal "a" specifically
    -- while "a" itself has a dead picker pid AND has never registered its own `agent_alive` marker
    at all (a genuine crash, not merely a picker exiting normally; contrast the test above, which
    DOES register and then kill a worker for "a" -- this one never registers one). Pre-correction,
    `_next()` judged "a"'s claim as "still has a live worker" purely because the UNRELATED session
    was active, permanently blocking "a"'s reclaim for as long as that session ran anything at all
    -- directly violating #1197's own acceptance criterion 4 ("a crash must not leave a goal
    permanently unpickable"). Post-correction, `_next()` must still reclaim "a": the per-goal
    `_goal_has_registered_worker` check correctly reports nothing registered for "a", and
    `session_active` is no longer consulted on this reclaim-refusal path at all."""
    with tempfile.TemporaryDirectory() as d:
        dead_picker_pid = 2**30                        # not a real pid on any sane system
        base = _lease_base(d, actor="me", claims=[("me", "a", "claimed", dead_picker_pid)])
        lp = _loop()
        lp.session_start(base, os.getpid())            # a genuinely live managing session -- for the
                                                         # WHOLE .sdlc, not specifically for goal "a"
        assert lp.session_active(base, {}) is True      # sanity: the goal-agnostic marker reads live
        assert lp.agent_threads(base, "a") == [], (      # sanity: "a" registered no worker of its own
            "test setup bug: this scenario requires 'a' to have NO agent_alive marker at all")
        monkeypatch.setattr(os, "getpid", lambda: dead_picker_pid + 1)   # a second picker's own pid
        src = _Queue(["a"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a"), (
            f"expected 'a' to be reclaimed despite the UNRELATED session being active -- its dead "
            f"picker pid has no corroborating agent_alive marker of its own, so a goal-agnostic "
            f"session_active signal must never be what blocks its reclaim; got {(kind, goal)!r}")
        assert lp.ledger.open_claims(lp.ledger.read_all(base))["a"] == "me"   # reclaimed by "me", not stuck


# ------------------------------------------------------- #1199 x #1198 x #1197 composition (rebase
# of #1239 onto origin/main after both #1198 and #1197 merged): three independent skip/reclaim
# mechanisms now run inside the SAME `_next()` call -- #1199's session-registry skip-set, #1197's
# per-goal reclaim-refusal corroboration, and #1198's TTL-based auto-reclaim sweep. The two tests
# below prove they compose correctly where they should, and PIN the one place they provably do not
# (a pre-existing gap inherited from #1198+#1197, not introduced or fixed by this rebase).


def test_next_composes_session_skip_set_with_per_goal_reclaim_corroboration(monkeypatch):
    """All three mechanisms exercised together in ONE `_next()` call. Session S (this test
    process's own, genuinely alive pid) is registered as a live managing session (`session_start`)
    and has ALSO claimed goal "held" via the session registry (`_session_claim` -- what a prior
    `_next()` call from that SAME session would have done): #1199's skip-set
    (`_session_in_flight_goals`) must exclude "held" with no `--skip` from this second caller. A
    wholly separate goal "crashed" carries a ledger claim whose WRITER pid is now dead, with no
    worker ever registered for it: #1197's `claim_belongs_to_me(..., live_worker_check=
    _goal_has_registered_worker)` must still reclaim it -- and, the point of this test, must do so
    despite session S's `session_active` reading True the WHOLE time (the exact goal-agnostic
    signal #1197's own correction moved this call site OFF of; see `_claimed_goal_has_live_worker`'s
    docstring). A THIRD, untouched goal proves the pick lands specifically on "crashed", not merely
    that nothing raised."""
    real_pid = os.getpid()
    dead_picker_pid = 2**30                            # not a real pid on any sane system
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[("me", "crashed", "claimed", dead_picker_pid)])
        lp = _loop()
        lp.session_start(base, real_pid)                # session S: genuinely alive
        lp._session_claim(base, real_pid, "held")        # ... already holds an UNRELATED goal
        assert lp.session_active(base, {}) is True        # sanity: goal-agnostic marker reads live
        assert lp.agent_threads(base, "crashed") == [], (
            "test setup bug: 'crashed' must have no agent_alive marker of its own")
        monkeypatch.setattr(os, "getpid", lambda: dead_picker_pid + 1)   # a second picker's own pid
        src = _Queue(["held", "crashed", "untouched"])
        kind, goal = lp._next(base, src, lp.state.load_config(base), session_pid=real_pid + 1)
        assert (kind, goal) == ("goal", "crashed"), (
            f"expected 'held' skipped via the session registry and 'crashed' reclaimed via the "
            f"per-goal corroboration despite session S's goal-agnostic session_active reading "
            f"True throughout; got {(kind, goal)!r}")
        assert lp.ledger.open_claims(lp.ledger.read_all(base))["crashed"] == "me"   # durably reclaimed
        assert "held" in lp._session_in_flight_goals(base, {}), (
            "session S's own claim on 'held' must still stand -- this call never touched it")


def test_auto_reclaim_sweep_reclaims_past_an_unrelated_live_session():
    """#1284: `_auto_reclaim_stale_claims` (#1198) used to check `_claimed_goal_has_live_worker`
    -- the OR'd, GOAL-AGNOSTIC signal #1197's own correction had already moved `_next()`'s and
    `work.py`'s reclaim-refusal call sites OFF of, specifically because it lets one unrelated live
    session block a DIFFERENT goal's reclaim forever (see that correction's own commit message and
    `_claimed_goal_has_live_worker`'s docstring). #1198 merged before #1197's correction existed
    and was never updated to match it, so the sweep carried the SAME hole on its own release side,
    just reached via wall-clock TTL age instead of a dead-picker-pid misread.

    This mattered MORE after #1199 than before it: #1199's SKILL.md has every ordinary
    `/sigma-loop` call pass `--session-pid "$PPID"`, so a live, correctly-registered session is the
    NORMAL running state, not an edge case -- meaning the sweep was routinely inert for the whole
    DURATION of any properly-run loop, not just in a rare misconfigured-overlap scenario. Worse,
    `_fetch_pending` (sources.py) unconditionally excludes any issue still carrying
    `in_progress_label`, and this sweep is the ONLY thing that clears that label after a crash --
    so the practical effect was a crashed goal going invisible to the picker for as long as the
    unrelated session ran anything at all.

    #1284 closes the gap by switching `_auto_reclaim_stale_claims` to the narrower
    `_goal_has_registered_worker`, mirroring the fix #1197 already applied to `_next()` and
    `work.py`. This test used to PIN the gap-preserving behavior (`reclaimed == frozenset()`);
    it now asserts the corrected behavior -- a goal-agnostic live session must NOT block reclaim
    of an unrelated, genuinely crashed goal. See
    `test_auto_reclaim_sweep_still_blocked_by_a_live_worker_on_the_same_goal` (below) for the
    thing that must NOT regress: a worker actually registered for THIS goal still blocks it."""
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "crashed", "claimed", _stale_ts(13))], ttl_hours=12)
        lp = _loop()
        cfg = lp.state.load_config(base)
        lp.session_start(base, os.getpid())              # a genuinely live, UNRELATED session
        assert lp.session_active(base, cfg) is True
        assert lp.agent_threads(base, "crashed") == [], (
            "test setup bug: 'crashed' must have no agent_alive marker of its own")
        issues = [{"number": "crashed", "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues), sdlc_dir=base)

        reclaimed = lp._auto_reclaim_stale_claims(base, src, cfg)

        assert reclaimed == frozenset({"crashed"}), (
            "#1284: an unrelated live session must no longer block the auto-reclaim sweep from "
            "reclaiming a DIFFERENT, genuinely crashed goal")
        assert lp.ledger.open_claims(lp.ledger.read_all(base)) == {}   # durably reclaimed


def test_auto_reclaim_sweep_still_blocked_by_a_live_worker_on_the_same_goal():
    """#1284 acceptance criterion 2, the companion to the test above: switching
    `_auto_reclaim_stale_claims` to the narrower `_goal_has_registered_worker` must not widen
    reclaim into ignoring a genuine worker -- a worker actually registered for THIS SPECIFIC goal
    (a real `agent_start` marker, this test process's own alive pid, the same shape SKILL.md's
    step 3a real `agent-start --pid $PPID` produces) must still block reclaim, regardless of how
    far past the TTL the lease has aged. No unrelated session is registered here at all -- this
    isolates the per-goal signal from the goal-agnostic one the test above exercises."""
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "crashed", "claimed", _stale_ts(13))], ttl_hours=12)
        lp = _loop()
        cfg = lp.state.load_config(base)
        cfg["agent_watch"] = {"enabled": True}
        lp.agent_start(base, "crashed", os.getpid(), cfg)   # this test process: genuinely alive
        issues = [{"number": "crashed", "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
        calls = []
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues, calls), sdlc_dir=base)

        reclaimed = lp._auto_reclaim_stale_claims(base, src, cfg)

        assert reclaimed == frozenset(), (
            "a worker genuinely registered for THIS goal must still block reclaim past the TTL")
        assert not any(c[0:2] == ["issue", "edit"] for c in calls)      # label left alone
        assert lp.ledger.open_claims(lp.ledger.read_all(base)) == {"crashed": "me"}   # left alone


# --------------------------------------------------------------------- local claim lock (#387)
# #374 (above) closes correctly INTERPRETING a claim that already exists. It has no answer for "two
# readers look at the same instant, nothing claimed yet" -- that needs something atomic. Built on
# `fcntl.flock` (kernel-mediated, POSIX-only), not ordinary file operations -- two schemes built the
# latter way (unlink-then-recreate, then a rename-then-verify-then-restore refinement) were each
# independently broken across two review cycles of PR #392; see loop.py's own
# `_try_acquire_claim_lock` docstring for why flock has no equivalent TOCTOU gap to begin with.


def test_claim_lock_is_exclusive_when_a_second_open_file_description_races_the_first():
    """The core guarantee, at the level flock actually enforces it: two INDEPENDENT open file
    descriptions on the same path -- what two separate CLI invocations would each get -- the first
    wins, the second is denied outright. Deterministic by construction, no timing tolerance needed:
    unlike the file-rename schemes this replaced, flock's exclusivity does not depend on WHEN the
    second caller shows up, only on whether the first still holds it."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        fd1 = lp._try_acquire_claim_lock(sdlc, "g.md")
        assert isinstance(fd1, int) and fd1 >= 0
        assert lp._try_acquire_claim_lock(sdlc, "g.md") is None   # denied outright -- still held
        lp._release_claim_lock(fd1)


def test_claim_lock_is_exclusive_under_genuine_thread_concurrency():
    """The same guarantee under a REAL race, not two sequential calls: a thread barrier forces both
    `_try_acquire_claim_lock` calls to genuinely overlap. Unlike the schemes this replaced, flock
    needs no forced-ordering trick to prove this deterministically -- the kernel itself serializes
    the two `flock()` calls, whichever order they land in, with no window for both to succeed."""
    import threading
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        barrier = threading.Barrier(2)
        results = []
        results_lock = threading.Lock()

        def attempt():
            barrier.wait()                     # both threads unblock at the same instant
            got = lp._try_acquire_claim_lock(sdlc, "g.md")
            with results_lock:
                results.append(got)

        t1, t2 = threading.Thread(target=attempt), threading.Thread(target=attempt)
        t1.start(); t2.start(); t1.join(); t2.join()
        winners = [r for r in results if r is not None]
        losers = [r for r in results if r is None]
        assert len(winners) == 1 and len(losers) == 1   # exactly one winner, never both, never neither
        lp._release_claim_lock(winners[0])


def test_claim_lock_release_lets_a_later_caller_win():
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        fd1 = lp._try_acquire_claim_lock(sdlc, "g.md")
        assert lp._try_acquire_claim_lock(sdlc, "g.md") is None   # still held
        lp._release_claim_lock(fd1)
        fd2 = lp._try_acquire_claim_lock(sdlc, "g.md")
        assert isinstance(fd2, int) and fd2 >= 0                  # released -- free again
        lp._release_claim_lock(fd2)


def test_claim_lock_release_is_a_safe_noop_for_denied_or_unavailable():
    lp = _loop()
    lp._release_claim_lock(None)                  # denied -- nothing was ever acquired
    lp._release_claim_lock(lp._LOCK_UNAVAILABLE)   # fail-open -- no real fd to close


def test_claim_lock_needs_no_staleness_window_to_recover_a_dead_holders_lock():
    """The whole point of moving to flock: closing the fd -- exactly what the kernel does when a
    holding process dies for ANY reason, crash included -- makes the lock available again
    IMMEDIATELY. No age to wait out, unlike the file-mtime scheme this replaced, which needed a 120s
    staleness window and an eviction dance just to recover a crashed holder's lock at all."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        fd1 = lp._try_acquire_claim_lock(sdlc, "g.md")
        os.close(fd1)                              # simulates the holder dying, not a clean release call
        fd2 = lp._try_acquire_claim_lock(sdlc, "g.md")
        assert isinstance(fd2, int) and fd2 >= 0    # immediately available -- no wait, no mtime trick
        lp._release_claim_lock(fd2)


def test_claim_lock_fails_open_without_fcntl(monkeypatch):
    """Windows (no fcntl module): fails open unconditionally, exactly as if this whole file didn't
    exist -- #374's ledger claim check remains the primary defense there."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        monkeypatch.setattr(lp, "fcntl", None)
        assert lp._try_acquire_claim_lock(sdlc, "g.md") == lp._LOCK_UNAVAILABLE


def test_claim_lock_fails_open_when_the_lock_directory_cannot_be_created():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        blocker = pathlib.Path(d) / "blocker"
        blocker.write_text("x")                    # a FILE, not a directory
        sdlc = str(blocker / "nested" / ".sdlc")    # mkdir(parents=True) under a file always raises
        assert lp._try_acquire_claim_lock(sdlc, "g.md") == lp._LOCK_UNAVAILABLE


def test_next_skips_a_goal_whose_claim_lock_is_already_held():
    """Deterministic proof that _next() actually WIRES IN the lock (the lock's own exclusivity is
    already proven in isolation above) -- holds a REAL flock directly, simulating "a sibling process
    on this machine won this exact instant" without needing a genuine race to reproduce. A
    touched-but-unlocked file is no longer meaningful under this scheme, unlike the old file-presence
    check, so the setup must actually acquire the lock, not just create the file."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        lp = _loop()
        held_fd = lp._try_acquire_claim_lock(base, "a")   # simulate: someone else's lock, held now
        assert isinstance(held_fd, int) and held_fd >= 0
        src = _Queue(["a", "b"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "b")            # "a"'s lock is held -- skipped straight to "b"
        lp._release_claim_lock(held_fd)


def test_next_releases_the_lock_after_a_successful_claim():
    """The lock's whole job is bridging the gap until a durable ledger claim exists -- it must not
    outlive that. The lock FILE is deliberately left on disk under this scheme (see
    `_release_claim_lock`'s docstring), so "released" now means the FLOCK is gone, not that the file
    is -- proven here by successfully acquiring it again right after."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        lp = _loop()
        src = _Queue(["a"])
        lp._next(base, src, lp.state.load_config(base))
        fd = lp._try_acquire_claim_lock(base, "a")
        assert isinstance(fd, int) and fd >= 0          # released, not leaked -- re-acquirable
        lp._release_claim_lock(fd)


def test_next_releases_the_lock_even_when_budget_is_already_spent():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"ledger": {"enabled": True, "actor": "me", "lease": {"ttl_hours": 0}},
             "budget": {"max_iterations": 1}}))
        (pathlib.Path(base) / "state" / "STATE.md").write_text(
            "iteration: 0\nrun_iteration: 1\nlast_run: none\n")   # already at the cap
        lp = _loop()
        src = _Queue(["a"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert kind == "BUDGET"
        fd = lp._try_acquire_claim_lock(base, "a")
        assert isinstance(fd, int) and fd >= 0        # BUDGET halt still released it, never leaked
        lp._release_claim_lock(fd)


# --------------------------------------------------------------- claim lock liveness (#494)
# #387 (above) makes the LOCK correct -- the flock itself needs no staleness window, ever. But the
# FILE it leaves behind on disk carried nothing (0 bytes forever), so a reader who cannot re-attempt
# the flock themselves (a human, a different tool) had no way to tell "claimed and abandoned" from
# "claimed and working" from the file alone -- reported live 2026-08-07: a session stood down rather
# than guess, and clearing the orphaned locks by hand was the only way forward.
#
# PR #1108 review: the first cut of this fix stamped the WINNING PROCESS's own os.getpid() and read
# it back with the same session_active/agent_alive two-signal combination (ledger.pid_alive() OR
# the file's own mtime past the TTL). That looked right against every test below -- because every
# one of them checked liveness from the SAME still-running process that had just won the lock. In
# production that process is always the short-lived `next`/`next-batch` CLI invocation, which exits
# within moments of returning (see session_start's own docstring) -- long before a dispatched
# subagent's real, multi-hour work even starts. So ledger.pid_alive() on the stamped pid reads
# "dead" within a fraction of a second of EVERY acquisition, worked or abandoned alike; empirically
# reproduced below with a real subprocess that wins the lock and exits normally, checked from a
# separate, still-running process. Fixed by cross-checking agent_alive/session_active -- the two
# markers that already correctly track a genuine long-lived pid -- before ever trusting the claim
# lock's own stamp; see claim_lock_alive's docstring for the full account.


def _win_claim_lock_in_a_real_subprocess(sdlc, goal):
    """Spawns a REAL, separate python process that plays the role of `loop.py next` in production:
    it wins the claim lock via `_try_acquire_claim_lock` (stamping ITS OWN pid, exactly as
    `_write_claim_lock_liveness` does today) and exits normally -- no crash, no abandonment, just
    an ordinary return. Callers then check `claim_lock_alive`/`reclaim_stale_claim_lock` from THIS
    (the caller's, still-running) process afterward -- the one thing the original 13 same-process
    tests never did, and exactly the gap the PR #1108 review found."""
    code = (
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('loop', %r)\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "fd = m._try_acquire_claim_lock(%r, %r)\n"
        "assert isinstance(fd, int) and fd >= 0, fd\n"
        "m._release_claim_lock(fd)\n"
    ) % (str(S / "loop.py"), sdlc, goal)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, f"claim-winning subprocess failed: {result.stderr}"


def test_try_acquire_claim_lock_stamps_the_acquiring_pid():
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        fd = lp._try_acquire_claim_lock(sdlc, "g.md")
        lp._release_claim_lock(fd)
        assert lp._claim_lock_path(sdlc, "g.md").read_text().strip() == str(os.getpid())


def test_try_acquire_claim_lock_overwrites_a_stale_pid_from_a_prior_acquisition(monkeypatch):
    """The fd can be reused from an EARLIER acquisition's now-stale content -- a longer old pid
    fully replaced by a shorter new one must leave no trailing garbage behind."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        monkeypatch.setattr(os, "getpid", lambda: 123456789)
        fd1 = lp._try_acquire_claim_lock(sdlc, "g.md")
        lp._release_claim_lock(fd1)
        monkeypatch.setattr(os, "getpid", lambda: 42)
        fd2 = lp._try_acquire_claim_lock(sdlc, "g.md")
        lp._release_claim_lock(fd2)
        assert lp._claim_lock_path(sdlc, "g.md").read_text().strip() == "42"


def test_try_acquire_claim_lock_still_succeeds_when_stamping_the_pid_fails(monkeypatch):
    """Fail-open: the flock is the real prize. A write() failure while stamping liveness data must
    never turn a genuine acquisition into a lost one."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        monkeypatch.setattr(lp.os, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        fd = lp._try_acquire_claim_lock(sdlc, "g.md")
        assert isinstance(fd, int) and fd >= 0
        lp._release_claim_lock(fd)


def test_claim_lock_alive_reports_unknown_for_a_resolvable_stamped_pid_with_no_corroboration():
    """#1108: a resolvable stamped pid ALONE is no longer enough for "alive" -- in production that
    pid is always the short-lived CLI invocation, never a genuine worker (see claim_lock_alive's
    docstring). With no agent_alive/session_active marker to corroborate it, and the file still
    fresh (within the TTL), the honest answer is "unknown" -- there is simply no evidence of
    ongoing work either way yet. (Renamed from ..._reports_alive_for_a_live_pid, which encoded
    the very same-process assumption the #1108 review found broken.)"""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        fd = lp._try_acquire_claim_lock(sdlc, "g.md")
        lp._release_claim_lock(fd)
        assert lp.claim_lock_alive(sdlc, "g.md", {}) == ("unknown", os.getpid())


def test_claim_lock_alive_reports_unknown_not_dead_for_a_dead_stamped_pid_within_the_ttl(monkeypatch):
    """#1108: THE bug, isolated. Pre-fix, a stamped pid that doesn't resolve
    (`ledger.pid_alive()` False) read as "dead" immediately -- no TTL wait at all -- which is
    exactly what made the short-lived CLI's own pid such a bad liveness oracle: it is ALWAYS
    "dead" by that test, moments after every acquisition, whether the goal was abandoned or is
    being actively worked (see the real cross-process tests below for the empirical version of
    this exact scenario). Post-fix, with no corroborating agent_alive/session_active trace and
    the file still within the TTL, this correctly reads "unknown" -- not enough evidence to call
    it dead."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        dead_pid = 2**30                                # not a real pid on any sane system
        monkeypatch.setattr(os, "getpid", lambda: dead_pid)
        fd = lp._try_acquire_claim_lock(sdlc, "g.md")
        lp._release_claim_lock(fd)
        assert lp.claim_lock_alive(sdlc, "g.md", {}) == ("unknown", dead_pid)


def test_claim_lock_alive_reports_unknown_with_no_lock_file_at_all():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        assert lp.claim_lock_alive(sdlc, "g.md", {}) == ("unknown", None)


def test_claim_lock_alive_reports_unknown_for_a_pre_494_empty_lock_file():
    """Backward compatible with a lock file this fix never wrote -- pre-#494 code always left it
    at 0 bytes, and a live repo upgrading in place will have exactly these on disk already."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        path = lp._claim_lock_path(sdlc, "g.md")
        path.parent.mkdir(parents=True)
        path.write_text("")
        assert lp.claim_lock_alive(sdlc, "g.md", {}) == ("unknown", None)


def test_claim_lock_alive_expires_a_stale_marker_past_the_ttl_with_no_corroboration():
    """#1108: this is now the ONLY way `claim_lock_alive` reaches "dead" -- aged past the TTL AND
    no agent_alive/session_active trace corroborates a live worker. Whether the stamped pid itself
    resolves is irrelevant post-fix (it never was a meaningful signal in production -- see
    claim_lock_alive's docstring); age plus silence is the whole story now. (Renamed from
    ..._even_for_a_resolvable_pid, which is still true -- this test's own pid IS resolvable
    throughout -- but no longer the point being tested.)"""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        fd = lp._try_acquire_claim_lock(sdlc, "g.md")     # a genuinely live (this test's own) pid
        lp._release_claim_lock(fd)
        path = lp._claim_lock_path(sdlc, "g.md")
        stale = time.time() - (lp.ledger.DEFAULT_LEASE_TTL_HOURS * 3600 + 60)
        os.utime(path, (stale, stale))
        assert lp.claim_lock_alive(sdlc, "g.md", {}) == ("dead", os.getpid())


def test_claim_lock_path_traversal_goal_reads_as_unknown_not_a_raise():
    lp = _loop()
    assert lp.claim_lock_alive("/tmp/.sdlc", TRAVERSAL_GOAL, {}) == ("unknown", None)


# ---- #1108: the REAL cross-process scenario -- a genuinely separate subprocess wins the lock and
# exits, checked from a DIFFERENT, still-running process. This is the shape production always has
# (`next`/`next-batch` is a fresh subprocess per call) and the one the original 13 same-process
# tests never exercised -- exactly why they missed this bug despite looking thorough.

def test_claim_lock_alive_does_not_misreport_dead_moments_after_a_real_subprocess_exits():
    """The empirical proof. A REAL subprocess wins the claim lock the way `next` does in
    production and exits normally (no crash) -- then, from THIS separate, still-running process,
    `claim_lock_alive` is checked immediately after. Pre-fix this reported "dead" within a
    fraction of a second (reproduced by hand: ~30ms after the subprocess exited). Post-fix, with
    no corroborating worker marker and the file still fresh, it must report "unknown" -- never a
    confident, wrong "dead" for a goal that, for all this function actually knows, could still be
    getting worked by a subagent elsewhere."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        _win_claim_lock_in_a_real_subprocess(sdlc, "g.md")
        state, pid = lp.claim_lock_alive(sdlc, "g.md", {})
        assert state == "unknown", (
            f"expected 'unknown' -- got {state!r}. A subprocess that already exited normally "
            "must never read as a confident 'dead' this soon; see claim_lock_alive's docstring.")
        assert pid is not None and not lp.ledger.pid_alive(pid)   # the stamped pid IS dead -- and
        # that is exactly why its liveness must not be the signal (see claim_lock_alive's docstring).


def test_claim_lock_alive_reports_alive_via_agent_alive_despite_a_real_dead_stamped_pid():
    """The other half of the real scenario: the SAME real-subprocess claim (its own stamped pid
    is provably dead, exactly like the test above) -- but this time a genuine long-lived worker
    IS registered for this goal via `agent_start`, exactly as SKILL.md step 3a's
    `agent-start --pid $PPID` does after a real dispatch. `claim_lock_alive` must report "alive",
    proving it now answers the right question (is anyone genuinely working this?) instead of the
    wrong one (does the claiming CLI invocation still happen to be running?)."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        config = {"agent_watch": {"enabled": True}}
        _win_claim_lock_in_a_real_subprocess(sdlc, "g.md")
        lp.agent_start(sdlc, "g.md", os.getpid(), config)   # this test process: genuinely alive,
        # genuinely long-lived relative to the subprocess above -- the real worker's own pid.
        assert lp.claim_lock_alive(sdlc, "g.md", config)[0] == "alive"
        # And the consequence that actually matters -- the CHANGELOG's own motivating incident --
        # is fixed too: a goal still being worked must never be auto-reclaimed out from under it.
        assert lp.reclaim_stale_claim_lock(sdlc, "g.md", config) is False
        assert lp._claim_lock_path(sdlc, "g.md").exists()


def test_claim_lock_alive_reports_alive_via_session_active_with_no_goal_specific_marker():
    """`session_active` alone (no `agent_watch` marker for this specific goal at all) is also
    sufficient corroboration -- a live managing session is reason enough not to declare a claim
    it might resume dead, even without per-goal tracking on. Same real-subprocess claim as above."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        _win_claim_lock_in_a_real_subprocess(sdlc, "g.md")
        lp.session_start(sdlc, os.getpid())
        assert lp.claim_lock_alive(sdlc, "g.md", {})[0] == "alive"


def test_reclaim_stale_claim_lock_removes_a_confirmed_dead_lock():
    """#1108: "confirmed dead" now means past the TTL with no corroborating worker marker -- not
    merely an unresolvable stamped pid (see claim_lock_alive). Age the lock file past the TTL to
    genuinely earn the "dead" diagnosis this call requires before it will act."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        fd = lp._try_acquire_claim_lock(sdlc, "g.md")
        lp._release_claim_lock(fd)
        path = lp._claim_lock_path(sdlc, "g.md")
        stale = time.time() - (lp.ledger.DEFAULT_LEASE_TTL_HOURS * 3600 + 60)
        os.utime(path, (stale, stale))
        assert path.exists()
        assert lp.reclaim_stale_claim_lock(sdlc, "g.md", {}) is True
        assert not path.exists()


def test_reclaim_stale_claim_lock_leaves_a_fresh_unresolved_lock_untouched():
    """#1108: a freshly-won lock with no corroborating worker marker now reads "unknown", not
    "alive" -- but `reclaim_stale_claim_lock` only ever acts on "dead", so the outcome is
    identical either way: nothing is touched. (Renamed from ..._leaves_a_live_lock_untouched:
    pre-fix this scenario read as "alive"; post-fix it reads "unknown" -- both are non-"dead",
    both correctly refuse to act.)"""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        fd = lp._try_acquire_claim_lock(sdlc, "g.md")     # a fresh acquisition, no age, no marker
        lp._release_claim_lock(fd)
        path = lp._claim_lock_path(sdlc, "g.md")
        before = path.read_text()
        assert lp.reclaim_stale_claim_lock(sdlc, "g.md", {}) is False
        assert path.exists() and path.read_text() == before


def test_reclaim_stale_claim_lock_reports_false_with_no_lock_file_at_all():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        assert lp.reclaim_stale_claim_lock(sdlc, "g.md", {}) is False


def test_reclaim_stale_claim_lock_never_removes_a_lock_someone_else_genuinely_still_holds():
    """The diagnosis alone is never enough to delete -- even a genuine "dead" diagnosis (now:
    aged past the TTL with no corroborating worker marker) must yield to a flock some OTHER open
    file description still actually holds, right now. The deletion is gated on genuinely WINNING
    the flock first (#387's own kernel-mediated exclusivity), so a lock this call cannot actually
    acquire is never removed out from under its real holder, regardless of what the diagnosis
    said."""
    with tempfile.TemporaryDirectory() as d:
        sdlc = str(pathlib.Path(d) / ".sdlc")
        lp = _loop()
        held_fd = lp._try_acquire_claim_lock(sdlc, "g.md")    # STILL HELD -- never released below
        path = lp._claim_lock_path(sdlc, "g.md")
        stale = time.time() - (lp.ledger.DEFAULT_LEASE_TTL_HOURS * 3600 + 60)
        os.utime(path, (stale, stale))                        # ages into a genuine "dead" diagnosis...
        assert lp.claim_lock_alive(sdlc, "g.md", {}) == ("dead", os.getpid())   # ...looks reclaimable...
        assert lp.reclaim_stale_claim_lock(sdlc, "g.md", {}) is False           # ...but is refused
        assert lp._claim_lock_path(sdlc, "g.md").exists()
        lp._release_claim_lock(held_fd)


def test_next_resumes_a_goal_my_own_current_process_already_holds(monkeypatch):
    """Not just the legacy (no-pid) resume case above (test_next_resumes_a_goal_this_actor_
    already_holds) -- my own CURRENT pid's post-#337 claim must resume too, matching pre-#374
    single-process behavior exactly."""
    with tempfile.TemporaryDirectory() as d:
        my_pid = 424242
        base = _lease_base(d, actor="me", claims=[("me", "a", "claimed", my_pid)])
        monkeypatch.setattr(os, "getpid", lambda: my_pid)   # the SAME pid as the claim -- my own
        lp = _loop(); src = _Queue(["a", "b"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")


def test_next_ignores_the_lease_when_the_ledger_is_off():
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[("other", "a", "claimed")], enabled=False)
        lp = _loop(); src = _Queue(["a", "b"])
        kind, goal = lp._next(base, src, lp.state.load_config(base))
        assert (kind, goal) == ("goal", "a")             # no ledger, no lock — byte-identical to before


# ---------------------------------------------------------------- #139 Slice 1: site a (verify) + site b (park)
# Both need ledger.enabled AND journal.enabled (the Slice 0 AND-gate) for an events-stream write
# to actually land — see test_ledger.py's gate tests for the gate itself; these prove the two call
# sites use it correctly.


def _telemetry_backlog(d, verify_command=None):
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "--local",
                    "sigma.allowRepositoryShellCommands", "true"], check=True)
    base = pathlib.Path(d) / ".sdlc"
    (base / "goals").mkdir(parents=True); (base / "state").mkdir()
    cfg = {"budget": {"max_iterations": 10}, "verify": {"command": ""},
           "ledger": {"enabled": True, "actor": "rae"}, "journal": {"enabled": True}}
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    (base / "state" / "review-queue.md").write_text("# Q\n")
    fm = "---\nid: 0001\nstatus: pending\n"
    if verify_command:
        fm += f"verify_command: {verify_command}\n"
    (base / "goals" / "0001.md").write_text(fm + "---\nx\n")
    return str(base), str(base / "goals" / "0001.md")


def _telemetry_base(d):
    """A bare .sdlc with ledger+journal on but no goals backlog — for tests that drive `_record`
    directly instead of through `run_loop`."""
    base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
    cfg = {"ledger": {"enabled": True, "actor": "rae"}, "journal": {"enabled": True}}
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return str(base)


class _Sink:
    """A source stub with just enough surface for `_record` to drive (complete/fail/park)."""
    def complete(self, g): pass
    def fail(self, g, r): pass
    def park(self, g, r): pass


def test_verify_goal_emits_a_verify_event_with_timing_and_command_hash():
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command="true")
        lp = _loop()
        assert lp.verify_goal(base, goal) == 0
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "verify"]
        assert len(events) == 1
        e = events[0]
        assert e["ok"] is True and e["exit"] == 0
        assert isinstance(e["ms"], int) and e["ms"] >= 0
        assert e["command_sha256"] == hashlib.sha256(b"true").hexdigest()


def test_verify_goal_emits_absent_when_no_command_is_declared():
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command=None)
        lp = _loop()
        assert lp.verify_goal(base, goal) == 3
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "verify"]
        assert len(events) == 1
        e = events[0]
        assert e["absent"] is True and e["exit"] == 3 and "command_sha256" not in e


def test_verify_goal_survives_a_raising_command_hash(monkeypatch):
    """Python evaluates call ARGUMENTS in the caller's frame, so `hashlib.sha256(cmd.encode(...))`
    runs in `verify_goal`'s own frame, not inside `safe_append`'s try/except — a raise there would
    take down `verify_goal` itself unless the field computation is guarded too, not just the
    append() call. This is the fail-open hole an independent plan review flagged for site a."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command="true")
        lp = _loop()
        def boom(*a, **k):
            raise RuntimeError("a weird cmd broke the hash")
        monkeypatch.setattr(hashlib, "sha256", boom)
        assert lp.verify_goal(base, goal) == 0     # the real verify outcome, unaffected by the crash


def test_verify_goal_proceeds_when_ensure_fresh_finds_nothing_stale(monkeypatch):
    """Contract test for the #1890 wiring: a `None` from `work.ensure_fresh` (nothing to check,
    already fresh, or a clean auto-rebase) must never change `verify_goal`'s existing behavior."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command="true")
        lp = _loop()
        monkeypatch.setattr(lp.work, "ensure_fresh", lambda *a, **k: None)
        assert lp.verify_goal(base, goal) == 0


def test_verify_goal_refuses_with_exit_4_and_writes_no_evidence_when_the_worktree_is_stale(monkeypatch, capsys):
    """#1890: a non-`None` refusal from `work.ensure_fresh` (the worktree is behind its base and
    the auto-rebase could not apply cleanly) must stop `verify_goal` BEFORE the expensive proving
    command runs — never conflated with a real test failure (exit 1), and critically must never
    write verify evidence, since a fresh PASS here is exactly what would let `state.done_refusal`
    wrongly accept a goal that was never actually verified against current code.

    #1899: unlike exit=2 (unsafe goal), exit=4 now DOES write a ledger event, matching exit=3's own
    convention of logging a refused-before-running verify for overnight/unattended visibility — see
    the ledger-write's own comment in `verify_goal` for why the event carries `exit=4` alone,
    never `absent=True` (that would falsely read as "no command configured" downstream, in
    a downstream goal timeline's verify synthesis). The *evidence file* guarantee this
    test originally existed to pin is unchanged and still asserted below."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command="true")
        lp = _loop()
        monkeypatch.setattr(lp.work, "ensure_fresh",
                             lambda *a, **k: "worktree for '0001' is 3 commit(s) behind origin/main "
                                             "and the automatic rebase could not apply cleanly")
        assert lp.verify_goal(base, goal) == 4
        ev = lp.state.evidence_path(base, goal)
        assert not ev.exists()
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "verify"]
        assert len(events) == 1
        e = events[0]
        assert e["exit"] == 4
        assert "ok" not in e and "absent" not in e   # never fabricate a pass/fail/no-command verdict
        err = capsys.readouterr().err
        assert "STALE" in err and "exit=4" in err and "commit(s) behind" in err


def test_verify_goal_exit_4_emits_verify_run_to_the_action_log(monkeypatch):
    """#1899: the actionlog half of the same fix — `/sigma-log` reads ONLY the local actionlog
    (never `ledger.EVENTS`, see `skills/sigma-log/scripts/log.py`'s own module docstring), so the
    ledger write alone (test above) does not actually close the overnight-visibility gap the issue
    names; this is the write that does. Mirrors `test_verify_goal_emits_verify_run_to_the_action_log`'s
    own shape for the exit=0 case, applied to exit=4 — `exit=4` alone, no `ok` key (there is no
    honest pass/fail value for a verify that never ran)."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command="true")
        _with_action_log(base)
        lp = _loop()
        monkeypatch.setattr(lp.work, "ensure_fresh",
                             lambda *a, **k: "worktree for '0001' is 3 commit(s) behind origin/main "
                                             "and the automatic rebase could not apply cleanly")
        assert lp.verify_goal(base, goal) == 4
        entries = lp.actionlog.read_goal(base, goal)
        hits = [e for e in entries if e["kind"] == "verify_run"]
        assert len(hits) == 1
        assert hits[0]["exit"] == "4"
        assert "ok" not in hits[0]


def test_verify_event_never_contains_the_raw_command():
    """#141 regression pin: the raw verify command string must never appear anywhere in the
    written event — only its sha256. Uses a DISTINCTIVE command (not `"true"`) so the substring
    check is meaningful."""
    with tempfile.TemporaryDirectory() as d:
        marker_cmd = "echo super-secret-marker-xyz123"
        base, goal = _telemetry_backlog(d, verify_command=marker_cmd)
        lp = _loop()
        assert lp.verify_goal(base, goal) == 0
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "verify"]
        assert len(events) == 1
        e = events[0]
        assert json.dumps(e).find("super-secret-marker-xyz123") == -1
        assert e["command_sha256"] == hashlib.sha256(marker_cmd.encode("utf-8")).hexdigest()


def test_record_park_why_is_capped_and_scrubbed():
    """#141: `_record`'s park path is one of the three deterministic sites — it must NEVER reject
    (an in-flight autonomous loop cannot 'refuse' the explanation for a goal it already parked),
    only sanitize. A >200-char detail with an embedded newline and a planted AWS-key shape must
    still land, flattened, capped, and scrubbed."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        secret = "AK" "IAIOSFODNN7EXAMPLE"
        detail = f"blocked on a decision\nsee key {secret}\n" + ("x" * 300)
        lp._record(base, _Sink(), "g.md", "parked", detail)
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert len(events) == 1
        why = events[0]["why"]
        assert "\n" not in why
        assert len(why) <= 200
        assert secret not in why


def test_record_emits_a_park_event_with_reason_class():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        detail = "PARK: no fresh verify evidence for this run (no verify evidence for this goal)"
        lp._record(base, _Sink(), "g.md", "parked", detail)
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert len(events) == 1
        assert events[0]["reason_class"] == "no_evidence"
        assert events[0]["why"] == detail


def test_record_unmatched_park_reason_is_unknown():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        lp._record(base, _Sink(), "g.md", "parked", "some free text nobody wrote a rule for")
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert len(events) == 1 and events[0]["reason_class"] == "unknown"


def test_record_emits_no_park_event_for_done_or_failed():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        lp._record(base, _Sink(), "g.md", "done", "")
        lp._record(base, _Sink(), "g2.md", "failed", "boom")
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert events == []


# --------------------------------------------------------------------------- issue #1201: a raising
# `source.complete()` (e.g. `gh issue close` failing on exhausted GraphQL quota) must never lose the
# terminal record on the CLI `record` path -- `_record()` did the remote write FIRST with no guard,
# so any exception there propagated straight out, skipping every local write below it entirely
# (cursor advance, ledger `done` entry, action-log `recorded` line). Mirrors the downgrade-to-park
# semantics `run_loop` already had for its OWN path since #335, but `_record()` itself had no guard
# at all -- only run_loop's outer try/except did.


class _RaisingCompleteSink:
    """A source stub whose `complete()` always raises -- the exhausted-quota `gh issue close`
    shape from #1201 -- with `park()` left working normally so the downgrade path can succeed."""
    def __init__(self):
        self.parked = []

    def complete(self, g):
        raise RuntimeError("gh issue close 42 failed: API rate limit exceeded for installation")

    def fail(self, g, r):
        raise AssertionError("fail() must never be called for a 'done' record")

    def park(self, g, r):
        self.parked.append((g, r))


def test_record_done_survives_a_raising_source_complete_and_still_persists_the_local_record():
    """The core #1201 fix at the `_record()` layer: `source.complete()` raising must not propagate
    -- the local terminal record (cursor advance, ledger entry, action-log line) must still land."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        _with_action_log(base)
        lp = _loop()
        before = lp.state.load_cursor(base)["iteration"]

        lp._record(base, _RaisingCompleteSink(), "g.md", "done")   # must not raise

        after = lp.state.load_cursor(base)["iteration"]
        assert after == before + 1                                 # cursor still advanced

        entries = lp.ledger.read_all(base)
        outcomes = [e for e in entries if e.get("goal") == "g.md" and e["kind"] in ("done", "parked")]
        assert len(outcomes) == 1
        assert outcomes[0]["kind"] == "parked"                      # downgraded, not silently "done"

        action_entries = lp.actionlog.read_goal(base, "g.md")
        hits = [e for e in action_entries if e["kind"] == "recorded"]
        assert len(hits) == 1 and hits[0]["result"] == "parked"


def test_record_done_source_complete_raising_prints_a_named_warning(capsys):
    """A remote failure during `complete()` must be surfaced as a warning naming the remote
    operation that failed, never as an uncaught traceback (#1201 acceptance criterion)."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        lp._record(base, _RaisingCompleteSink(), "g.md", "done")
        err = capsys.readouterr().err
        assert "Traceback" not in err
        assert "complete" in err                    # names the failed remote operation
        assert "g.md" in err


def test_record_done_source_complete_raising_releases_the_ledger_claim():
    """#1201 acceptance criterion: the ledger claim held on this goal must be released on the
    local-record (downgrade-to-parked) path, exactly as any other terminal outcome releases it."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        lp.ledger.safe_append(base, "claimed", "g.md")
        assert lp.ledger.open_claims(lp.ledger.read_all(base)) == {"g.md": "rae"}

        lp._record(base, _RaisingCompleteSink(), "g.md", "done")

        assert lp.ledger.open_claims(lp.ledger.read_all(base)) == {}


def test_record_done_source_complete_raising_still_calls_park_on_the_source():
    """The downgrade must actually reach the source's own `park()` -- not just fudge the local
    bookkeeping while leaving the remote side with no trace at all of the park."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        sink = _RaisingCompleteSink()
        lp._record(base, sink, "g.md", "done")
        assert len(sink.parked) == 1
        assert sink.parked[0][0] == "g.md"


def test_cli_record_done_survives_a_raising_source_complete():
    """#1201 acceptance criterion, exactly as stated: with the backlog source mocked to raise on
    `issue close`, `loop.py record <dir> <goal> done` exits non-fatally and the local record is
    present on disk afterwards -- driven through the real CLI dispatch (`main`), not `_record`
    directly, so the fix is proven on the actual unguarded call site (`loop.py record`'s own
    dispatch had no try/except around `_record` at all)."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1, max_iter=5)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        cfg["ledger"] = {"enabled": True, "actor": "cli-1201-test"}
        (pathlib.Path(base) / "config.json").write_text(json.dumps(cfg))
        lp = _loop()
        lp.sources.get_source = lambda sdlc_dir, config: _RaisingCompleteSink()
        goal = base + "/goals/0001.md"

        rc = lp.main(["loop.py", "record", base, goal, "done"])

        assert rc == 0                                              # non-fatal exit, no traceback
        entries = lp.ledger.read_all(base)
        outcomes = [e for e in entries if e.get("goal") == goal and e["kind"] in ("done", "parked")]
        assert len(outcomes) == 1 and outcomes[0]["kind"] == "parked"


def test_run_loop_downgrade_to_park_on_a_raising_complete_is_not_reimplemented_separately():
    """#1201: `_record()` now owns the "source.complete() raised -> downgrade to a recorded park"
    behaviour itself (it's the single chokepoint both the CLI `record` verb and `run_loop` share).
    `run_loop`'s own pre-existing #335 outer try/except stays as a backstop for anything `_record()`
    itself cannot recover from, but for the plain single-failure case it must not ALSO re-invoke
    `source.complete()` a second time -- proving the two mechanisms were unified, not duplicated."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0, max_iter=10)
        lp = _loop()

        class Fake:
            def __init__(s):
                s.q = ["a"]; s.done = []; s.parked = []; s.complete_calls = 0
            def next_pending(s, skip=()): return next((g for g in s.q if g not in skip), None)
            def mark_in_progress(s, g): pass
            def complete(s, g):
                s.complete_calls += 1
                raise RuntimeError("gh: API rate limit exceeded")
            def park(s, g, r):
                s.q.remove(g); s.parked.append(g)

        fake = Fake()
        lp.sources.get_source = lambda sdlc_dir, config: fake
        res = lp.run_loop(base, lambda g: ("done", ""))
        assert res["stopped"] == "backlog-empty"
        assert fake.parked == ["a"]
        assert res["done"] == 0 and res["parked"] == 1
        assert fake.complete_calls == 1                              # never retried a second time


def test_reason_class_quota_detail_classifies_as_quota():
    """#1242 supersedes #1201 acceptance criterion 7's explicit-scope branch (this test's own former
    name and body, `test_reason_class_quota_detail_is_unknown_by_design`, pinned the OLD by-design
    "stays unknown" decision -- see loop.py's comment right above `_REASON_CLASS_RULES` for why that
    scope was widened): a quota/rate-limit park detail -- the exact `source.complete()`
    downgrade-to-park text #1201 itself produces on an exhausted GitHub rate limit -- now gets its
    own `REASON_CLASSES` member, `quota`, instead of falling into `unknown` indistinguishably from a
    genuinely unclassified park.

    This was already `unknown`, never a crash, so nothing about the underlying #1201 fix's
    reliability depends on this test; what changes is that the recurrence signal downstream
    (a downstream park-recurrence gap rule) can now group quota parks on their own class instead
    of mixing them into every other unclassified park."""
    detail = "source error recording 'done' (RuntimeError: gh issue close 42 failed: " \
             "API rate limit exceeded for installation)"
    lp = _loop()
    assert lp._reason_class(detail) == "quota"
    # the secondary-rate-limit shape shares the "rate limit" substring with the primary one above --
    # same needle, same class, both real GitHub API error bodies (gh_session.py's own header comment).
    assert lp._reason_class("gh api graphql failed: You have exceeded a secondary rate limit — "
                             "please wait a few minutes") == "quota"


# --------------------------------------------------------------------------- issue #1013: retro_grade
# `_record()` gains an optional `retro_grade` kwarg; the CLI `record` verb gains `--retro-grade`,
# threaded through to it. Absence stays the honest default (no fabricated grade) -- see the two
# byte-identical-to-today tests below, at both the `_record` layer and the CLI layer.


def test_record_emits_a_retro_event_when_grade_given():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        lp._record(base, _Sink(), "g.md", "done", "", retro_grade="achieved")
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "retro"]
        assert len(events) == 1
        assert events[0]["grade"] == "achieved"


def test_record_emits_no_retro_event_when_grade_omitted():
    """Backward-compat proof at the `_record` layer: no `retro_grade` arg at all (today's exact
    call shape) must never fabricate a retro event."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        lp._record(base, _Sink(), "g.md", "done", "")
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "retro"]
        assert events == []


def test_record_emits_retro_event_alongside_a_parked_outcome():
    """Proves the grade isn't gated on `outcome == "done"` -- Retrospective can run before a goal
    that ultimately parks (e.g. a merge conflict discovered after retro already ran)."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        lp._record(base, _Sink(), "g.md", "parked", "merge conflict", retro_grade="partial")
        evs = journal_events(lp.ledger, base)
        assert len([e for e in evs if e["kind"] == "park"]) == 1
        retro_events = [e for e in evs if e["kind"] == "retro"]
        assert len(retro_events) == 1
        assert retro_events[0]["grade"] == "partial"


def test_record_emits_retro_event_alongside_a_failed_outcome():
    """The third outcome combination (review-phase code-review finding, #1013): `done` and `parked`
    are covered above, `failed` was not. The retro-emission line is unconditional on `outcome` (only
    gated on `retro_grade is not None`), so this proves it by construction rather than leaving the
    `failed` path as an untested assumption -- a goal can complete Retrospective and still end up
    recorded `failed` (e.g. a review cycle that never converges), and that grade must still land."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        lp._record(base, _Sink(), "g.md", "failed", "review cycles exhausted", retro_grade="diverged")
        evs = journal_events(lp.ledger, base)
        assert [e for e in evs if e["kind"] == "park"] == []   # `failed` never writes a `park` event
        retro_events = [e for e in evs if e["kind"] == "retro"]
        assert len(retro_events) == 1
        assert retro_events[0]["grade"] == "diverged"


def test_cli_record_writes_a_retro_event_with_retro_grade_flag():
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d)
        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True, text=True)
        r = run("record", base, goal, "done", "--retro-grade", "achieved")
        assert r.returncode == 0
        evs = _events(base, "retro")
        assert len(evs) == 1
        assert evs[0]["grade"] == "achieved"


def test_cli_record_rejects_unknown_retro_grade():
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d)
        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True, text=True)
        r = run("record", base, goal, "done", "--retro-grade", "meh")
        assert r.returncode == 2
        assert "achieved" in r.stderr and "partial" in r.stderr and "diverged" in r.stderr
        assert _events(base) == []


def test_cli_record_reason_and_retro_grade_together():
    """Proves the positional-vs-flag disambiguation: a freeform `reason` and a trailing
    `--retro-grade` flag both land correctly from the same call."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d)
        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True, text=True)
        r = run("record", base, goal, "parked", "merge conflict", "--retro-grade", "diverged")
        assert r.returncode == 0
        park_evs = _events(base, "park")
        retro_evs = _events(base, "retro")
        assert len(park_evs) == 1 and park_evs[0]["why"] == "merge conflict"
        assert len(retro_evs) == 1 and retro_evs[0]["grade"] == "diverged"


def test_cli_record_with_no_retro_grade_flag_is_byte_identical_to_today():
    """Backward-compat proof at the CLI layer: today's exact call shape (no flag at all) must
    behave exactly as it did before this flag existed."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d)
        run = lambda *a: subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True, text=True)
        r = run("record", base, goal, "done")
        assert r.returncode == 0
        assert _events(base, "retro") == []


# ------------------------------------------------------------------- #953: decision_tier wiring
# #952 shipped a real, tested, standalone decision_tier.classify()/resolve() -- nothing called it.
# #953 wired it into `_record()`'s single park chokepoint, but gated it on reason_class ==
# `needs_decision` ALONE -- and #1185 found that gate structurally unreachable for the free text the
# classifier's own vocabulary was written for (every agent-typed `loop.py record ... parked "<free
# text>"` maps to `unknown`, never `needs_decision`), plus a dead `irreversible` collision (that
# reason_class's own rule fires first and always intercepts an `irreversible`-flavored detail before
# decision_tier ever saw it, even though `irreversible` is itself one of decision_tier's own
# escalate_l0 signal words). #1185 widens the gate to `_DECISION_TIER_REASON_CLASSES` =
# (`needs_decision`, `irreversible`, `unknown`) -- every reason_class whose text can carry a genuine
# human judgment call. The five remaining reason_classes (dependency/no_evidence/merge_conflict/
# failing_check/review_cap) are fixed, mechanical park reasons with no judgment call embedded in the
# text, so they stay excluded: `_record` must never call decision_tier.resolve() for those, and must
# drive `source.park()` with the exact same 2-positional-arg call it always has.


def _telemetry_base_auto_tier(d):
    """Same shape as `_telemetry_base`, plus the new, non-colliding `decision_tier: "auto"` top-level
    config key #952 added -- the opt-in gate `decision_tier.resolve()` checks before ever returning a
    real tier. Kept as its own helper (not a param on `_telemetry_base`) so the ~40 existing callers
    of that helper are untouched."""
    base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
    cfg = {"ledger": {"enabled": True, "actor": "rae"}, "journal": {"enabled": True},
           "decision_tier": "auto"}
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return str(base)


class _TierSink:
    """A source stub whose `park()` mirrors the REAL LocalSource/GitHubSource signature after #953
    (`tier=None`, optional and trailing) -- records every call's full args/kwargs so a test can
    assert exactly what `_record()` handed it, including whether `tier` was passed at all."""
    def __init__(self):
        self.parked = []           # [(goal, reason, tier_or_MISSING)]

    def complete(self, g):
        pass

    def fail(self, g, r):
        pass

    def park(self, g, r, tier=None):
        # `_record()` only ever passes `tier=` as a kwarg when it has a real value (see its own
        # comment) -- record a sentinel, not just `tier`, so a test can tell "tier=None was passed"
        # apart from "tier was never passed at all" if that ever mattered.
        self.parked.append((g, r, tier))


def test_record_computes_a_decision_tier_for_a_needs_decision_park_when_auto():
    """The core wiring: a `needs_decision` park, with `decision_tier: "auto"` configured, carries a
    real tier end to end -- the ledger event AND the source.park() call both get it."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base_auto_tier(d)
        lp = _loop()
        sink = _TierSink()
        detail = "PR #1 changes requested — this touches the pricing budget, address them"
        lp._record(base, sink, "g.md", "parked", detail)
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert len(events) == 1
        assert events[0]["reason_class"] == "needs_decision"
        assert events[0]["decision_tier"] == "escalate_l0"          # "budget"/"pricing" -> l0
        assert sink.parked == [("g.md", detail, "escalate_l0")]


def test_record_needs_decision_defaults_to_escalate_l1_with_no_extra_signal():
    """No decision_tier-specific vocabulary in the detail text -> the safe middle default
    (escalate_l1), per decision_tier.py's own documented default -- still end to end."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base_auto_tier(d)
        lp = _loop()
        sink = _TierSink()
        detail = "changes requested by someone on PR #1 — address them"
        lp._record(base, sink, "g.md", "parked", detail)
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert events[0]["decision_tier"] == "escalate_l1"
        assert sink.parked == [("g.md", detail, "escalate_l1")]


def test_record_computes_a_decision_tier_for_an_irreversible_park_when_auto():
    """#1185: `irreversible` used to be dead code from decision_tier's point of view --
    `_REASON_CLASS_RULES`'s own `irreversible` rule fires first and reclassifies any such detail
    to reason_class `irreversible`, which the pre-#1185 gate (`== "needs_decision"` only) then
    silently excluded from ever reaching `decision_tier.resolve()`. Now it does, and (unsurprisingly,
    since `irreversible` is also one of decision_tier's own escalate_l0 signal words) resolves to
    the top tier end to end."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base_auto_tier(d)
        lp = _loop()
        sink = _TierSink()
        detail = "this action is irreversible, parking for a human"
        lp._record(base, sink, "g.md", "parked", detail)
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert len(events) == 1
        assert events[0]["reason_class"] == "irreversible"
        assert events[0]["decision_tier"] == "escalate_l0"
        assert sink.parked == [("g.md", detail, "escalate_l0")]


def test_record_computes_a_decision_tier_for_genuine_free_text_reaching_unknown_when_auto():
    """#1185's central fix: real, agent-typed decision prose -- exactly the kind #818/#952's own
    worked examples were written against, and exactly the kind this issue's own evidence names as
    permanently unreachable pre-fix ('not sure which approach to take') -- classifies into
    reason_class `unknown` (it matches none of loop.py's ~20 fixed machine-generated substrings),
    which the widened gate now also offers to decision_tier.resolve()."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base_auto_tier(d)
        lp = _loop()
        sink = _TierSink()
        detail = "not sure which approach to take on the retry backoff, could go either way"
        assert lp._reason_class(detail) == "unknown"        # sanity: this really is free text
        lp._record(base, sink, "g.md", "parked", detail)
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert len(events) == 1
        assert events[0]["reason_class"] == "unknown"
        assert events[0]["decision_tier"] == "escalate_l1"          # "which approach"/"not sure which"
        assert sink.parked == [("g.md", detail, "escalate_l1")]


def test_record_excludes_the_mechanical_could_not_compute_mergeability_detail_from_decision_tier(monkeypatch):
    """Independent review of #1185 found that reason_class "unknown" is overloaded: it is BOTH the
    genuine free-text fallthrough bucket decision_tier's vocabulary exists to classify AND the
    explicit target of the pre-existing `("could not compute mergeability", "unknown")` rule in
    `_REASON_CLASS_RULES` -- a fixed, machine-generated GitHub-API-transient message (work.py's
    mergeability-retry path), never a human judgment call. Widening `_DECISION_TIER_REASON_CLASSES`
    to include "unknown" wholesale would sweep this mechanical case in too and fabricate a noisy
    escalate_l1 for it -- exactly the outcome that constant's own comment says the gate exists to
    avoid. `_record` must therefore still never call `decision_tier.resolve()` for this specific
    detail, even though its reason_class IS in the gate set and decision_tier is "auto". A raising
    spy on `resolve()` proves the call genuinely never happens (same pattern as
    test_record_never_calls_decision_tier_resolve_for_a_non_needs_decision_park, applied to a
    detail whose reason_class is NOT excluded at the class level)."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base_auto_tier(d)
        lp = _loop()

        def boom(*a, **k):
            raise AssertionError("decision_tier.resolve() must not be called for the mechanical "
                                  "'could not compute mergeability' detail")
        monkeypatch.setattr(lp.decision_tier, "resolve", boom)

        detail = "GitHub could not compute mergeability (still UNKNOWN after retries)"
        sink = _TierSink()
        lp._record(base, sink, "g.md", "parked", detail)          # must not raise
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert events[0]["reason_class"] == "unknown"              # sanity: same class as free text
        assert "decision_tier" not in events[0]
        assert sink.parked == [("g.md", detail, None)]             # tier kwarg never a real value


def test_record_excludes_five_more_mechanical_work_py_park_details_from_decision_tier(monkeypatch):
    """#1240 review: the single hardcoded "could not compute mergeability" needle the test above
    covers is only ONE of SIX of work.py's own fixed, machine-generated park/gate-verdict messages
    that reach reason_class "unknown" by the same plain fallthrough genuine free-text decision prose
    does (no `_REASON_CLASS_RULES` needle happens to cover any of their wording either) --
    gate()/_reconcile_behind() also produce five more with the identical shape: an unreadable
    `gh pr view` (work.py:423), an unreadable local branch tip (work.py:436), a missing headRefOid
    on the remote side (work.py:438-440) or the local side (work.py:441-443), and an exhausted
    BEHIND rebase race (work.py:714-715). #1185's original fix only excluded the first of the six;
    these five slipped through classified exactly like real decision prose would be, fabricating a
    noisy escalate_* tier on an ordinary infra hiccup. `_mechanical_unknown_detail` now excludes all
    six the same way, sourced from `work.MECHANICAL_PARK_PREFIXES` rather than a second hardcoded
    needle per message -- this pins each of the five with an exact, real work.py wording (verified
    end to end against the actual gate()/_reconcile_behind() output for three of them in
    test_work.py's own test_mechanical_park_verdicts_are_excluded_from_decision_tier_end_to_end).
    One shared raising spy on `resolve()` proves it is never called for ANY of the five, not just
    that its result goes unused for each."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base_auto_tier(d)
        lp = _loop()

        def boom(*a, **k):
            raise AssertionError("decision_tier.resolve() must not be called for a mechanical "
                                  "work.py park detail")
        monkeypatch.setattr(lp.decision_tier, "resolve", boom)

        details = [
            # work.py:423 -- `gh pr view` raised on every retry attempt.
            "could not read PR state (RuntimeError: gh: HTTP 502 Bad Gateway)",
            # work.py:436 -- `git rev-parse HEAD` raised.
            "could not read the local branch tip (RuntimeError: fatal: not a git repository)",
            # work.py:438-440 -- GitHub reported no headRefOid at all.
            "GitHub did not report headRefOid, so the PR head cannot be checked against this "
            "worktree — refusing rather than merging an unverifiable head",
            # work.py:441-443 -- the local `git rev-parse HEAD` read back an empty string.
            "the local branch tip read back empty, so the PR head cannot be checked against it — "
            "refusing rather than merging an unverifiable head",
            # work.py:714-715 -- _reconcile_behind() exhausted BEHIND_REBASES without settling.
            "BEHIND race did not settle after 3 rebases -- `main` keeps moving under this PR; a "
            "human should merge it or adopt GitHub's native merge queue",
        ]
        for i, detail in enumerate(details):
            assert lp._reason_class(detail) == "unknown", detail   # sanity: same bucket as free text
            goal = f"g{i}.md"
            sink = _TierSink()
            lp._record(base, sink, goal, "parked", detail)         # must not raise
            events = [e for e in journal_events(lp.ledger, base)
                      if e["kind"] == "park" and e["goal"] == goal]
            assert len(events) == 1
            assert events[0]["reason_class"] == "unknown"
            assert "decision_tier" not in events[0], detail
            assert sink.parked == [(goal, detail, None)]           # tier kwarg never a real value


def test_record_computes_a_decision_tier_for_truly_unclassifiable_free_text_when_auto():
    """The genuine-fallthrough half of "unknown": text that matches NONE of `_REASON_CLASS_RULES`
    (not even the mechanical mergeability needle covered by the test above) is exactly the
    free-text decision prose #1185 widened the gate to reach, and gets decision_tier's own
    safe-middle default (escalate_l1, no matched signal) end to end -- distinct from the
    mechanical case, which the same reason_class value must NOT reach."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base_auto_tier(d)
        lp = _loop()
        sink = _TierSink()
        detail = "something totally unrelated to any known park reason"
        assert lp._reason_class(detail) == "unknown"
        lp._record(base, sink, "g.md", "parked", detail)
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert len(events) == 1
        assert events[0]["reason_class"] == "unknown"
        assert events[0]["decision_tier"] == "escalate_l1"          # _DEFAULT, no pattern matched
        assert sink.parked == [("g.md", detail, "escalate_l1")]


def test_record_omits_decision_tier_for_irreversible_or_unknown_when_config_is_off():
    """Same additivity guarantee as the needs_decision case, for the two reason_classes #1185 newly
    added to the gate: with no `decision_tier: "auto"` key, both still behave exactly as they did
    before this issue -- no ledger key, plain 2-positional-arg `source.park()`."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)                       # no decision_tier key
        lp = _loop()
        for goal, detail in (
            ("g1.md", "this action is irreversible, parking for a human"),
            ("g2.md", "not sure which approach to take on the retry backoff"),
        ):
            lp._record(base, _Sink(), goal, "parked", detail)     # must not raise
            events = [e for e in journal_events(lp.ledger, base)
                      if e["kind"] == "park" and e["goal"] == goal]
            assert len(events) == 1
            assert "decision_tier" not in events[0]


def test_record_omits_decision_tier_when_config_is_off_even_for_a_needs_decision_park():
    """The gate is `decision_tier.resolve()`'s own, honored unchanged: with no `decision_tier: "auto"`
    key (the default, and every existing adopter's config), a `needs_decision` park behaves exactly
    as it did before #953 -- no ledger key, and `source.park()` gets the plain 2-positional-arg call
    (proven with `_Sink`, whose `park(self, g, r)` has no `tier` parameter at all -- any kwarg leak
    would raise TypeError here, not silently pass)."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)                       # no decision_tier key
        lp = _loop()
        detail = "PR #1 changes requested — this touches the pricing budget, address them"
        lp._record(base, _Sink(), "g.md", "parked", detail)     # must not raise
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert len(events) == 1
        assert events[0]["reason_class"] == "needs_decision"
        assert "decision_tier" not in events[0]


def test_record_never_calls_decision_tier_resolve_for_a_non_needs_decision_park(monkeypatch):
    """The strongest form of the additivity proof: even with `decision_tier: "auto"` ON, a park
    whose reason_class ISN'T `needs_decision` must never even CALL decision_tier.resolve() -- not
    just "the result is unused". A spy that raises if invoked proves the call genuinely never
    happens, which a return-value assertion alone could not."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base_auto_tier(d)
        lp = _loop()

        def boom(*a, **k):
            raise AssertionError("decision_tier.resolve() must not be called for this reason_class")
        monkeypatch.setattr(lp.decision_tier, "resolve", boom)

        lp._record(base, _TierSink(), "g.md", "parked", "rebase deferred: could not apply")
        events = [e for e in journal_events(lp.ledger, base) if e["kind"] == "park"]
        assert events[0]["reason_class"] == "merge_conflict"        # sanity: this IS the other class
        assert "decision_tier" not in events[0]


def test_record_every_other_reason_class_is_byte_for_byte_unaffected_by_decision_tier():
    """#953's own bar, updated by #1185: not merely "no failing test" but an explicit assertion,
    for every OTHER documented reason_class (all of #139's table rows except needs_decision,
    irreversible and unknown -- the three `_DECISION_TIER_REASON_CLASSES` #1185 widened the gate
    to), run WITH `decision_tier: "auto"` ON (the feature at its most active) and prove two things
    per row:
      1. the ledger `park` event carries the correct reason_class and NO `decision_tier` key at all
      2. `source.park()` was invoked with the exact historical 2-positional-arg shape (`_StrictSink`
         below has no `tier` parameter, so a kwarg leak raises TypeError instead of passing quietly)

    NOTE: two "unknown"-classed details (the mechanical "could not compute mergeability" park text,
    and genuinely unclassifiable free text) used to live in `other_cases` below, back when "unknown"
    itself was excluded from the gate. #1185 widened the gate to include "unknown", so both details'
    reason_class is now IN `_DECISION_TIER_REASON_CLASSES` -- this table's own sanity assertion
    (`expected not in lp._DECISION_TIER_REASON_CLASSES`) would fail for them, because being
    unaffected (or not) for "unknown" text is no longer a class-level fact, it depends on whether
    the text matched an explicit `_REASON_CLASS_RULES` needle or fell through. Their coverage lives
    in the two dedicated tests immediately above this one instead:
    test_record_excludes_the_mechanical_could_not_compute_mergeability_detail_from_decision_tier
    (still unaffected) and test_record_computes_a_decision_tier_for_truly_unclassifiable_free_text_when_auto
    (now affected, by design)."""
    class _StrictSink:
        """Deliberately the OLD, pre-#953 `park()` shape -- no `tier` parameter anywhere. If
        `_record()` ever passes `tier=` (even `tier=None`) to a non-needs_decision park, this raises
        TypeError instead of silently accepting it, which is the whole point of this test."""
        def __init__(self):
            self.parked = []

        def complete(self, g):
            pass

        def fail(self, g, r):
            pass

        def park(self, g, r):
            self.parked.append((g, r))

    other_cases = [
        ("no PR for this goal — run `work.py pr` first", "dependency"),
        ("no fresh verify evidence for this run (no verify evidence for this goal)", "no_evidence"),
        ("rebase deferred: could not apply", "merge_conflict"),
        ("conflicts with the base branch — a human has to resolve them", "merge_conflict"),
        ("STALE HEAD — the PR is at abc1234 but this worktree is at def5678", "merge_conflict"),
        ("not safe to merge (mergeStateStatus=BLOCKED)", "failing_check"),
        ("post-PR review did not converge after 3 cycles on PR #1", "review_cap"),
        ("could not confirm whether a decomposition was already filed — check comments", "no_evidence"),
        ("decomposition already filed — see comments", "dependency"),
        ("too large per goal_size (12 independent ## sections (>= 6)) — decomposition filed as #901",
         "dependency"),
        ("source error recording 'done' (RuntimeError: gh issue close 42 failed: "
         "API rate limit exceeded for installation)", "quota"),                    # #1242
    ]
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base_auto_tier(d)      # decision_tier auto-ON -- the feature at its loudest
        lp = _loop()
        for i, (detail, expected) in enumerate(other_cases):
            assert expected not in lp._DECISION_TIER_REASON_CLASSES  # sanity: this table is the "every OTHER" set
            goal = f"g{i}.md"
            sink = _StrictSink()
            lp._record(base, sink, goal, "parked", detail)         # must not raise
            assert sink.parked == [(goal, detail)]                 # exact old 2-positional-arg call
            events = [e for e in journal_events(lp.ledger, base)
                      if e["kind"] == "park" and e["goal"] == goal]
            assert len(events) == 1
            assert events[0]["reason_class"] == expected
            assert "decision_tier" not in events[0]


def test_record_fail_less_source_with_a_needs_decision_detail_never_crashes():
    """Independent review of #953 found a real gap: `result == "failed"` on a source with no
    `fail()` falls through to the SAME branch as `"parked"` (see `_record`'s own comment, "parked
    (or failed on a fail-less source)") -- with `decision_tier: "auto"` ON and a `needs_decision`-
    classifying detail, `tier` resolves to a real value, and the OLD guard (`if tier is not None`)
    would still try to call `source.park(goal, reason, tier=tier)` even against a source whose
    `park()` predates #953 entirely (a plain 2-positional-arg shape, no `tier` parameter at all) --
    a TypeError, reproduced live during review. The fix checks the source's own `park` signature
    (`inspect.signature`, the same capability-check spirit as `hasattr(source, "fail")` one line
    up) before ever passing `tier=`, not just whether a tier value exists. This test uses a source
    that is BOTH fail-less (to reach the vulnerable branch) AND old-shape (`park(self, g, r)`, no
    `tier` param) -- the exact combination review found unguarded -- and pins that it must not
    raise, degrading gracefully to the plain 2-arg call instead."""
    class _OldShapeFailLessSource:
        """Deliberately BOTH gaps at once: no fail() (routes 'failed' through the shared
        park-handling branch) and no `tier` param on park() (the pre-#953 shape)."""
        def __init__(self):
            self.parked = []

        def complete(self, g):
            pass

        def park(self, g, r):
            self.parked.append((g, r))

    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base_auto_tier(d)
        lp = _loop()
        sink = _OldShapeFailLessSource()
        detail = "PR #1 changes requested — this touches the pricing budget, address them"

        lp._record(base, sink, "g.md", "failed", detail)      # must not raise TypeError

        assert sink.parked == [("g.md", detail)]               # degraded to the plain 2-arg call
        # `result == "failed"` keeps `outcome == "failed"` regardless of which internal branch
        # handled it (a fail-less source still routes through the park-shaped branch above, but
        # the OUTCOME classification a few lines down is `result`-driven, not branch-driven) -- so
        # no EVENTS-stream "park" entry is expected here at all; that's pre-existing, independent
        # of this fix. What this test actually pins is that _record() runs to completion instead
        # of aborting mid-call: the cursor advances and the real "failed" KINDS-stream entry lands.
        failed_entries = [e for e in lp.ledger.read_all(base) if e["kind"] == "failed"]
        assert len(failed_entries) == 1 and failed_entries[0]["goal"] == "g.md"
        state_md = (pathlib.Path(base) / "state" / "STATE.md").read_text()
        assert "g.md -> failed" in state_md


def test_reason_class_table_matches_every_documented_park_source():
    """One assertion per row of #139's verified needle table (Design decision 4, as corrected by
    plan review), pinning the mapping against regressions if work.py's park wording ever drifts."""
    lp = _loop()
    cases = [
        ("this action is irreversible, parking for a human", "irreversible"),
        # #1242: the quota/rate-limit park detail from #1201's `source.complete()` downgrade path.
        ("source error recording 'done' (RuntimeError: gh issue close 42 failed: "
         "API rate limit exceeded for installation)", "quota"),
        ("gh api graphql failed: You have exceeded a secondary rate limit — please wait", "quota"),
        ("no PR for this goal — run `work.py pr` first", "dependency"),
        ("no fresh verify evidence for this run (no verify evidence for this goal)", "no_evidence"),
        ("rebase deferred: could not apply", "merge_conflict"),
        ("conflicts with the base branch — a human has to resolve them", "merge_conflict"),
        ("STALE HEAD — the PR is at abc1234 but this worktree is at def5678", "merge_conflict"),
        ("GitHub could not compute mergeability (still UNKNOWN after retries)", "unknown"),
        ("not safe to merge (mergeStateStatus=BLOCKED)", "failing_check"),
        ("changes requested by someone on PR #1 — address them", "needs_decision"),
        ("2 unresolved review thread(s) on PR #1 — resolve them", "needs_decision"),
        ("PR #1 is not approved yet (reviewDecision=none)", "needs_decision"),
        ("a `sigma:block` comment is on PR #1 — address it", "needs_decision"),
        ("post-PR review did not converge after 3 cycles on PR #1", "review_cap"),
        ("too large per goal_size (12 independent ## sections (>= 6)) — needs manual decomposition",
         "needs_decision"),
        # #522: goal_decompose's `file` mode -- five more decompose_check park details.
        ("too large per goal_size (12 independent ## sections (>= 6)) — needs manual decomposition "
         "(file mode needs an issue tracker)", "needs_decision"),
        ("could not confirm whether a decomposition was already filed — check comments", "no_evidence"),
        ("decomposition already filed — see comments", "dependency"),
        ("too large — failed to file decomposition goal: could not open the tracked issue — "
         "needs a human", "needs_decision"),
        ("too large per goal_size (12 independent ## sections (>= 6)) — decomposition filed as #901",
         "dependency"),
        ("something totally unrelated to any known park reason", "unknown"),
    ]
    for detail, expected in cases:
        assert lp._reason_class(detail) == expected, detail


def test_run_loop_survives_a_raising_ledger_append(monkeypatch):
    """The module's fail-open test: would fail if `_next`/`_record`/`verify_goal` ever called
    `ledger.append` directly instead of `ledger.safe_append`.

    #905 plan-review Defect 4: also sets SIGMA_RUN_ID so the (now-gated) run_stop path is
    actually exercised here too, not silently skipped before `ledger.append` is ever reached --
    without this, the new attribution gate would make this test stop covering run_stop's own
    fail-open behaviour entirely, with nothing else replacing that coverage. `calls` proves the
    run_stop write was genuinely attempted (and survived), not merely that no event exists."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 3)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        cfg["ledger"] = {"enabled": True, "actor": "rae"}
        cfg["journal"] = {"enabled": True}
        (pathlib.Path(base) / "config.json").write_text(json.dumps(cfg))
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        lp = _loop()
        calls = []
        def raiser(*a, **k):
            calls.append(a[2] if len(a) > 2 else None)   # ledger.append(sdlc_dir, config, kind, ...)
            raise RuntimeError("ledger broke")
        monkeypatch.setattr(lp.ledger, "append", raiser)
        res = lp.run_loop(base, lambda g: ("done", ""))
        assert res["done"] == 3 and res["parked"] == 0 and res["stopped"] == "backlog-empty"
        assert "run_stop" in calls    # the run_stop write itself hit the raising append and
                                       # survived -- not skipped by the new attribution/dedupe gate


def test_run_loop_survives_a_raising_source_op_on_the_middle_goal():
    """F4: a transient gh error recording ONE goal (e.g. complete()'s `issue close` raising) must not
    abort the whole drain — `run_loop([a,b,c])` with `b` raising must still process `a` and `c`. The
    failed goal is downgraded to a recorded PARK, never silently counted as done."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0, max_iter=10)
        lp = _loop()

        class Fake:
            def __init__(s):
                s.q = ["a", "b", "c"]; s.done = []; s.parked = []
            def next_pending(s, skip=()): return next((g for g in s.q if g not in skip), None)
            def mark_in_progress(s, g): pass
            def complete(s, g):
                if g == "b":
                    raise RuntimeError("gh: HTTP 502 Bad Gateway")   # NOT removed from q yet
                s.q.remove(g); s.done.append(g)
            def park(s, g, r):
                s.q.remove(g); s.parked.append(g)   # only the fallback park() call de-lists "b"

        fake = Fake()
        lp.sources.get_source = lambda sdlc_dir, config: fake
        res = lp.run_loop(base, lambda g: ("done", ""))
        assert res["stopped"] == "backlog-empty"
        assert fake.done == ["a", "c"]              # b's completion could not be confirmed
        assert fake.parked == ["b"]                 # downgraded to a park, not silently dropped
        assert res["done"] == 2 and res["parked"] == 1 and res["failed"] == 0


def test_run_loop_does_not_spin_forever_when_the_fallback_park_also_raises():
    """POST-REVIEW FIX: if BOTH the primary record AND the fallback park-record fail for the same
    goal, run_loop must still TERMINATE — not spin on that one goal forever. An independent review
    reproduced exactly this as an unbounded, ~100%-CPU hang that silently defeats `max_iterations`
    (worse than the original crash: loud-and-bounded beats silent-and-unbounded). The goal is
    poisoned for the rest of THIS run only; `a` and `c` still complete normally."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0, max_iter=10)
        lp = _loop()

        class Fake:
            def __init__(s):
                s.q = ["a", "b", "c"]; s.done = []; s.calls_for_b = 0
            def next_pending(s, skip=()): return next((g for g in s.q if g not in skip), None)
            def mark_in_progress(s, g): pass
            def complete(s, g):
                if g == "b":
                    s.calls_for_b += 1
                    if s.calls_for_b > 5:
                        raise AssertionError("run_loop is spinning on 'b' — poisoning did not work")
                    raise RuntimeError("gh: HTTP 502 Bad Gateway")
                s.q.remove(g); s.done.append(g)
            def park(s, g, r):
                if g == "b":
                    raise RuntimeError("gh: HTTP 502 Bad Gateway — park ALSO fails")   # b never de-listed
                s.q.remove(g)

        fake = Fake()
        lp.sources.get_source = lambda sdlc_dir, config: fake
        res = lp.run_loop(base, lambda g: ("done", ""))          # must return promptly, not hang
        assert res["stopped"] == "backlog-empty"
        assert fake.done == ["a", "c"]                            # b could not be recorded either way
        assert res["parked"] == 1                                 # still counted, not silently dropped
        assert fake.calls_for_b == 1                              # picked up exactly once this run


# ---------------------------------------------------------------- #140: `emit` verb + `spend` extension
# Every emit test below reuses `_telemetry_base` (ledger+journal ON) unless a test is specifically
# about the default-off behaviour, in which case it builds its own config.


def _events(base, kind=None):
    lp = _loop()
    evs = journal_events(lp.ledger, base)
    return [e for e in evs if kind is None or e["kind"] == kind]


def test_emit_writes_phase_event_and_reads_back():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "phase", "--phase", "implement", "--state", "start"])
        assert rc == 0
        evs = _events(base, "phase")
        assert len(evs) == 1
        assert evs[0]["phase"] == "implement" and evs[0]["state"] == "start" and evs[0]["goal"] == "g.md"


def test_emit_writes_gate_event_plan_review():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "gate", "--gate", "plan_review", "--verdict", "pass"])
        assert rc == 0
        evs = _events(base, "gate")
        assert len(evs) == 1 and evs[0]["gate"] == "plan_review" and evs[0]["verdict"] == "pass"


def test_emit_writes_gate_event_alignment():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "(alignment)", "gate", "--gate", "alignment", "--verdict", "warn"])
        assert rc == 0
        evs = _events(base, "gate")
        assert len(evs) == 1 and evs[0]["gate"] == "alignment" and evs[0]["verdict"] == "warn"


def test_emit_writes_retro_event():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "retro", "--grade", "achieved"])
        assert rc == 0
        evs = _events(base, "retro")
        assert len(evs) == 1 and evs[0]["grade"] == "achieved"


def test_emit_writes_spend_event():
    """#140 owns `spend` as one of `emit`'s four allowed kinds too (amendment A's allowlist),
    even though the prose only ever instructs it through the extended `spend` verb (design
    decision 3) — `emit ... spend` must still work and stay in-vocabulary."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "spend", "--model", "sonnet",
                      "--tokens_in", "10", "--tokens_out", "20"])
        assert rc == 0
        evs = _events(base, "spend")
        assert len(evs) == 1 and evs[0]["model"] == "sonnet" and evs[0]["tokens_in"] == "10"


def test_emit_rejects_unknown_kind():
    """`ValueError` from `append()` itself (kind not in EVENT_KINDS at all) — one source of
    truth for that message, per design decision 1."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "scan", "--category", "x"])
        assert rc == 2
        assert _events(base) == []


def test_emit_rejects_a_class1_kind_not_in_the_140_allowlist(capsys):
    """Amendment A: `emit` must not be able to forge Class-1 events. `verify` is a real
    `EVENT_KINDS` member (so `append()` alone would happily accept it) but it belongs to the
    deterministic `verify_goal` emitter, not to anything an agent should be able to type."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "verify", "--ok", "true", "--exit", "0"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "phase" in err and "gate" in err and "retro" in err and "spend" in err  # names what IS allowed
        assert _events(base) == []


def test_emit_rejects_a_forged_merge_gate():
    """Amendment A: `--gate merge --verdict pass` must not be forgeable through `emit` even
    though `merge` is a real `ledger.GATE_KINDS` member — `emit` only owns plan_review/alignment."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "gate", "--gate", "merge", "--verdict", "pass"])
        assert rc == 2
        assert _events(base) == []


def test_emit_rejects_unknown_flag_name():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "phase", "--phase", "implement", "--bogus", "x"])
        assert rc == 2
        assert _events(base) == []


def test_emit_rejects_out_of_vocabulary_phase():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "phase", "--phase", "sleeping"])
        assert rc == 2
        assert _events(base) == []


def test_emit_rejects_out_of_vocabulary_gate():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "gate", "--gate", "bogus"])
        assert rc == 2
        assert _events(base) == []


def test_emit_rejects_out_of_vocabulary_retro_grade():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "retro", "--grade", "meh"])
        assert rc == 2
        assert _events(base) == []


# #141: `test_emit_caps_and_flattens_why` (a `why` with an embedded newline succeeds, flattened)
# is DELIBERATELY RETIRED and split into two tests below, each pinning one half of the now-
# decoupled contract: a newline is a hard REJECT (not a flatten) at this agent-facing CLI verb,
# while length alone (no newline) still succeeds, capped. This is an intentional behavior change
# the issue's own done-when requires ("a payload containing a newline is rejected") — the old test
# pinned the OPPOSITE contract (succeeds, flattened) and would now fail by design.


def test_emit_rejects_why_with_newline(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        why = "line one\nline two"
        rc = lp.main(["loop.py", "emit", base, "g.md", "gate", "--gate", "plan_review",
                      "--verdict", "warn", "--why", why])
        assert rc == 2
        assert "newline" in capsys.readouterr().err
        assert _events(base) == []


def test_emit_caps_why_at_200_chars_without_newline():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        why = "x" * 300      # long, but no newline
        rc = lp.main(["loop.py", "emit", base, "g.md", "gate", "--gate", "plan_review",
                      "--verdict", "warn", "--why", why])
        assert rc == 0
        evs = _events(base, "gate")
        assert len(evs) == 1
        assert len(evs[0]["why"]) <= 200


def test_emit_scrubs_a_planted_secret_in_why():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        SECRET = "AK" "IAIOSFODNN7EXAMPLE"
        rc = lp.main(["loop.py", "emit", base, "g.md", "gate", "--gate", "plan_review",
                      "--verdict", "warn", "--why", f"key: {SECRET}"])
        assert rc == 0
        evs = _events(base, "gate")
        assert len(evs) == 1
        assert SECRET not in evs[0]["why"] and "[REDACTED" in evs[0]["why"]


def test_spend_rejects_model_with_newline():
    """`--model` with a `\\n` refuses the whole call (exit 2, nothing written) — but tokens are
    still counted, since `state.add_tokens` runs FIRST and unconditionally (existing amendment
    behavior, regression-pinned here for the newline check too)."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "spend", base, "10", "g.md", "--model", "a\nb"])
        assert rc == 2
        assert lp.state.load_cursor(base)["run_tokens"] == 10
        assert _events(base) == []


def test_spend_scrubs_a_planted_secret_in_model():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        SECRET = "AK" "IAIOSFODNN7EXAMPLE"
        rc = lp.main(["loop.py", "spend", base, "10", "g.md", "--model", f"sonnet-{SECRET}"])
        assert rc == 0
        evs = _events(base, "spend")
        assert len(evs) == 1
        assert SECRET not in evs[0]["model"] and "[REDACTED" in evs[0]["model"]


def test_spend_caps_model_at_200_chars():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "spend", base, "10", "g.md", "--model", "x" * 300])
        assert rc == 0
        evs = _events(base, "spend")
        assert len(evs) == 1
        assert len(evs[0]["model"]) <= 200


def test_emit_is_off_when_telemetry_disabled(capsys):
    """Default-off: `ledger.enabled` on but `journal.enabled` NOT `is True` → `emit` is a
    clean no-op (exit 0), never a refusal — the events gate is inherited from `append()`,
    not re-implemented by `emit`."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        cfg = {"ledger": {"enabled": True, "actor": "rae"}}     # no journal block at all
        (base / "config.json").write_text(json.dumps(cfg))
        (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
        lp = _loop()
        rc = lp.main(["loop.py", "emit", str(base), "g.md", "phase", "--phase", "implement",
                      "--state", "start"])
        assert rc == 0
        assert "OFF" in capsys.readouterr().out
        assert not (base / "ledger").exists()


def test_emit_survives_a_write_failure_after_valid_input(monkeypatch, capsys):
    """Amendment B: fail-open even after validation passes. A write failure (full disk, an
    unwritable directory — anything `OSError`) must not crash `emit`; it must degrade to a
    non-fatal warning and exit 0, exactly like every other fail-open ledger call site."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(lp.ledger, "append", boom)
        rc = lp.main(["loop.py", "emit", base, "g.md", "phase", "--phase", "implement", "--state", "start"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "entry skipped (non-fatal)" in err


def test_spend_two_arg_form_writes_no_event():
    """Non-regression: the 2-arg call (everything that exists today) touches budget only —
    `test_spend_cli_verb_accumulates` already pins the CLI subprocess path; this pins the
    in-process `main()` path plus the "no event" half amendment C's fix must not disturb."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "spend", base, "500"])
        assert rc == 0
        assert lp.state.load_cursor(base)["run_tokens"] == 500
        assert _events(base) == []


def test_spend_with_goal_and_flags_writes_a_spend_event():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "spend", base, "500", "g.md", "--phase", "implement",
                      "--tokens_in", "10", "--tokens_out", "20"])
        assert rc == 0
        assert lp.state.load_cursor(base)["run_tokens"] == 500
        evs = _events(base, "spend")
        assert len(evs) == 1
        assert evs[0]["goal"] == "g.md" and evs[0]["phase"] == "implement"
        assert evs[0]["tokens_in"] == "10" and evs[0]["tokens_out"] == "20"


def test_spend_flags_with_no_goal_does_not_misattribute_a_flag_as_the_goal():
    """Amendment C: `spend .sdlc 500 --tokens_in 10 --tokens_out 20` previously made
    `argv[4] == "--tokens_in"` the literal goal and silently dropped the bare `"10"`
    (no `--` prefix), landing a `spend` event with `goal="--tokens_in"`, no `tokens_in`, and one
    stray `tokens_out`. The fix: `argv[4]` is only a goal when it does NOT start with `--`.
    Budget must still accumulate (the unconditional first line), and nothing may land with a
    flag name masquerading as a goal."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "spend", base, "500", "--phase", "implement"])
        assert rc == 0
        assert lp.state.load_cursor(base)["run_tokens"] == 500
        evs = _events(base, "spend")
        assert not any(e.get("goal", "").startswith("--") for e in evs)


def test_spend_cli_verb_accumulates_is_unmodified():
    """Confirms the pre-existing subprocess-level test (`test_spend_cli_verb_accumulates`
    above) still exists unedited and still passes — the plan's own non-regression demand."""
    import inspect
    src = inspect.getsource(test_spend_cli_verb_accumulates)
    assert 'subprocess.run([sys.executable, str(S / "loop.py"), "spend", base, n]' in src


# ---------------------------------------------------------------- #140 PR review gaps: shared validator
# FINDING 1: `spend`'s `phase` field was never checked against PHASE_KINDS (only `emit ... phase`
# was). FINDING 2: the extended `spend` verb bypassed every one of `emit`'s refusal checks — an
# unknown flag NAME silently dropped instead of refusing. Both are fixed by one shared validator
# (`_validate_event`) called from both `emit` and `spend`'s event path.


def test_emit_spend_rejects_out_of_vocabulary_phase():
    """Finding 1: `emit ... spend --phase <bogus>` must be refused — `EVENT_FIELDS["spend"]`
    carries the same `phase` field (same PHASE_KINDS vocabulary) as `phase`-kind events, but the
    per-kind branch in `emit` only ever checked `kind == "phase"`, not the `phase` field itself."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "spend",
                      "--phase", "totally_bogus_phase", "--tokens_in", "5"])
        assert rc == 2
        assert _events(base) == []


def test_emit_spend_accepts_valid_phase():
    """The other half of finding 1: a legitimate `--phase implement` on `spend` must still work
    — the fix must validate the vocabulary, not merely reject everything."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "spend",
                      "--phase", "implement", "--tokens_in", "5"])
        assert rc == 0
        evs = _events(base, "spend")
        assert len(evs) == 1 and evs[0]["phase"] == "implement"


def test_spend_rejects_unknown_flag_name_but_still_counts_tokens():
    """Finding 2: the extended `spend` verb called `ledger.safe_append` directly, so an unknown
    flag NAME silently dropped instead of refusing — reintroducing the exact silent-drop failure
    mode #140 exists to close. Budget accounting (`state.add_tokens`) is the FIRST thing `spend`
    does and must run even when the event itself is refused, so both halves are asserted here."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "spend", base, "500", "goal.md", "--bogus_flag_name", "yes"])
        assert rc == 2
        assert lp.state.load_cursor(base)["run_tokens"] == 500     # tokens still counted
        assert _events(base) == []                                 # but no event written


def test_spend_rejects_out_of_vocabulary_phase_but_still_counts_tokens():
    """Finding 1, exercised through the `spend` verb directly (not `emit`) — the reproduction
    from the review. Tokens must still accumulate even though the event is refused."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "spend", base, "500", "goal.md", "--phase", "bogus"])
        assert rc == 2
        assert lp.state.load_cursor(base)["run_tokens"] == 500
        assert _events(base) == []


def test_spend_token_accumulation_across_every_arg_shape():
    """Re-assert every arg shape's token accumulation still holds after the fix — the whole risk
    of fixing the validation gap is breaking budget accounting. 500 -> 1000 -> 1500 -> 2000."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()

        rc = lp.main(["loop.py", "spend", base, "500"])
        assert rc == 0 and lp.state.load_cursor(base)["run_tokens"] == 500

        rc = lp.main(["loop.py", "spend", base, "500", "goal.md"])
        assert rc == 0 and lp.state.load_cursor(base)["run_tokens"] == 1000

        rc = lp.main(["loop.py", "spend", base, "500", "goal.md", "--phase", "implement",
                      "--model", "x", "--tokens_in", "1", "--tokens_out", "2"])
        assert rc == 0 and lp.state.load_cursor(base)["run_tokens"] == 1500

        rc = lp.main(["loop.py", "spend", base, "500", "--phase", "implement"])
        assert rc == 0 and lp.state.load_cursor(base)["run_tokens"] == 2000


def test_emit_and_spend_share_one_validator():
    """The same unknown flag name is refused with the same message shape from both verbs —
    proof the fix is one shared validator, not a third copy of the checks."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()

        import subprocess as _sp

        proc_emit = _sp.run([sys.executable, str(S / "loop.py"), "emit", base, "g.md",
                             "spend", "--bogus_flag_name", "yes"], capture_output=True, text=True)
        assert proc_emit.returncode == 2

        proc_spend = _sp.run([sys.executable, str(S / "loop.py"), "spend", base, "500",
                              "g.md", "--bogus_flag_name", "yes"], capture_output=True, text=True)
        assert proc_spend.returncode == 2

        # Same unknown-flag message shape (kind name + flag name + "expected one of") from both.
        assert "bogus_flag_name" in proc_emit.stderr and "bogus_flag_name" in proc_spend.stderr
        assert "unknown flag" in proc_emit.stderr and "unknown flag" in proc_spend.stderr


# ------------------------------------------------------------- post-review fix: THE LEAK, closed
# An independent PR review BLOCKED #249 by demonstrating, by execution, that a numeric-looking
# field (`tokens_in`, `cycle`, `debt_count`) accepted ANY string with no literal newline — including
# one carrying a full secret shape — because nothing on the write path ever checked the VALUE, only
# whether the field's NAME was on a "safe" list. These four tests are the reviewer's own repro,
# verbatim in shape, now asserting the opposite outcome: refused, not written.

_LEAK_SECRET_1 = "AK" "IAIOSFODNN7EXAMPLE"
_LEAK_SECRET_2 = "gh" "p_ABCDEFGHIJ1234567890ABCD"
_LEAK_PAYLOAD = f"prose padding {_LEAK_SECRET_1} more padding {_LEAK_SECRET_2} trailing text"
assert "\n" not in _LEAK_PAYLOAD          # the whole point: no newline, so the old guard missed it


def test_emit_phase_refuses_the_reviewers_leaked_tokens_in_payload(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "phase", "--phase", "plan",
                      "--state", "start", "--tokens_in", _LEAK_PAYLOAD])
        assert rc == 2
        err = capsys.readouterr().err
        assert _LEAK_SECRET_1 not in err and _LEAK_SECRET_2 not in err
        assert _events(base) == []


def test_emit_gate_refuses_the_reviewers_leaked_cycle_payload(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "gate", "--gate", "plan_review",
                      "--verdict", "warn", "--cycle", _LEAK_PAYLOAD])
        assert rc == 2
        err = capsys.readouterr().err
        assert _LEAK_SECRET_1 not in err and _LEAK_SECRET_2 not in err
        assert _events(base) == []


def test_spend_refuses_the_reviewers_leaked_tokens_in_payload(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "spend", base, "10", "g.md", "--tokens_in", _LEAK_PAYLOAD])
        assert rc == 2
        err = capsys.readouterr().err
        assert _LEAK_SECRET_1 not in err and _LEAK_SECRET_2 not in err
        assert _events(base) == []
        # budget accounting is unconditional and first — same contract as every other spend refusal
        assert lp.state.load_cursor(base)["run_tokens"] == 10


def test_emit_retro_refuses_the_reviewers_leaked_debt_count_payload(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "retro", "--grade", "achieved",
                      "--debt_count", _LEAK_PAYLOAD])
        assert rc == 2
        err = capsys.readouterr().err
        assert _LEAK_SECRET_1 not in err and _LEAK_SECRET_2 not in err
        assert _events(base) == []


def test_emit_still_accepts_a_genuinely_numeric_tokens_in():
    """The refusal is scoped to non-numeric values only — a legitimate numeric flag must keep
    working exactly as before this fix."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "phase", "--phase", "plan",
                      "--state", "start", "--tokens_in", "123"])
        assert rc == 0
        evs = _events(base, "phase")
        assert len(evs) == 1 and evs[0]["tokens_in"] == "123"


def test_emit_refuses_a_non_numeric_cycle_with_a_usable_message(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        rc = lp.main(["loop.py", "emit", base, "g.md", "gate", "--gate", "plan_review",
                      "--verdict", "warn", "--cycle", "not-a-number"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "cycle" in err and "whole number" in err


def test_emit_refuses_a_cycle_past_signed_64bit_but_accepts_the_max(capsys):
    """#787: the CLI's shared numeric guard must refuse a --cycle above signed 64-bit (a value no
    downstream `cycle` column, now BIGINT, could store) with exit 2 and NOTHING written, while
    still accepting the largest storable value. This is the CLI half of the storability contract;
    the same `_looks_numeric` refusal backs append() so the two cannot drift."""
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        lp = _loop()
        # one past signed 64-bit -> refused, nothing written
        rc = lp.main(["loop.py", "emit", base, "g.md", "gate", "--gate", "plan_review",
                      "--verdict", "warn", "--cycle", str(2**63)])
        assert rc == 2
        err = capsys.readouterr().err
        assert "cycle" in err and "whole number" in err
        assert _events(base) == []
        # exactly the signed 64-bit maximum -> accepted and carried verbatim
        rc = lp.main(["loop.py", "emit", base, "g.md", "gate", "--gate", "plan_review",
                      "--verdict", "warn", "--cycle", str(2**63 - 1)])
        assert rc == 0
        evs = _events(base, "gate")
        assert len(evs) == 1
        assert evs[0]["cycle"] == str(2**63 - 1)


# ------------------------------------------------------------- post-review fix: shared newline helper
# Item E from the retrospective: the newline-reject rule was hand-written twice — this file's own
# `_validate_event` and `work.py`'s post-review branch in `main()` — with near-identical but
# unshared wording. `ledger.reject_newline` is now the one shared helper both call; the
# cross-module "same message shape from both" proof lives in test_work.py (it already has the
# harness — `_sdlc`/`_started` — to drive `work.py post-review` for real), right next to the
# existing `test_cli_post_review_rejects_a_newline_in_reason`.


def test_validate_event_uses_the_shared_reject_newline_helper():
    """`_validate_event`'s own newline message is exactly `ledger.reject_newline`'s output, per
    flag — not a separately hand-written string that happens to also say "newline"."""
    lp = _loop()
    err = lp._validate_event("gate", {"gate": "plan_review", "verdict": "warn", "why": "a\nb"},
                             kind_allowlist=lp._EMIT_KINDS)
    assert err == lp.ledger.reject_newline("a\nb", "--why")


# ------------------------------------------------------------- local-only action log (#463): one
# regression test per Python-layer call site in loop.py — a future edit that quietly drops the
# actionlog.safe_append call at one of these sites should fail a test, not go unnoticed (matches
# this repo's own "hardened-sibling-divergence" concern, see plan section 5 §7).


def test_next_emits_claimed_to_the_action_log():
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 1)
        cfg = _with_action_log(base)
        lp = _loop()
        kind, goal = lp._next(base, lp.sources.get_source(base, cfg), cfg)
        assert kind == "goal"
        entries = lp.actionlog.read_goal(base, goal)
        assert [e["kind"] for e in entries] == ["claimed"]


def test_verify_goal_emits_verify_run_to_the_action_log():
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command="true")
        _with_action_log(base)
        lp = _loop()
        assert lp.verify_goal(base, goal) == 0
        entries = lp.actionlog.read_goal(base, goal)
        hits = [e for e in entries if e["kind"] == "verify_run"]
        assert len(hits) == 1
        assert hits[0]["ok"] == "True" and hits[0]["exit"] == "0"


def test_verify_goal_absent_command_does_not_emit_to_the_action_log():
    """The NO-COMMAND early-return path is deliberately NOT one of the actionlog call sites (plan
    section 5 §1: only the real-command path, right after the ledger try/except) — `verify_run`'s
    own field whitelist (ok/exit/ms) has no `absent` field, so trying to log this path would raise."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command=None)
        _with_action_log(base)
        lp = _loop()
        assert lp.verify_goal(base, goal) == 3
        entries = lp.actionlog.read_goal(base, goal)
        assert [e for e in entries if e["kind"] == "verify_run"] == []


# ------------------------------------------------- the always-on timing store (working time, S4)


def _every_store_off(base):
    """Every opt-in store disabled — the state a stock install is actually in, and the only state
    in which "always-on" means anything."""
    path = pathlib.Path(base) / "config.json"
    cfg = json.loads(path.read_text())
    cfg["journal"] = {"enabled": False}
    cfg["ledger"] = {"enabled": False}
    cfg["action_log"] = {"enabled": False}
    path.write_text(json.dumps(cfg))


def test_a_verify_duration_reaches_the_timing_store_with_every_store_off():
    """S4's acceptance criterion. The ledger `verify` event and the action log's `verify_run` both
    already carry this exact millisecond figure, and both are gated — so on a stock install the
    duration is computed and discarded. The timing store keeps it."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command="true")
        _every_store_off(base)
        lp = _loop()
        assert lp.verify_goal(base, goal) == 0

        entries = lp.timing_store.read_goal(base, goal)
        proving = [e for e in entries if e["kind"] == "verify" and e["name"] == "command"]
        assert len(proving) == 1, f"expected one proving-command interval, got {entries}"
        assert proving[0]["ms"] >= 0

        # ... and the gated stores really were shut.
        assert not list((pathlib.Path(base) / "ledger").rglob("*.jsonl"))
        assert lp.actionlog.read_goal(base, goal) == []


def test_the_timing_store_never_holds_the_raw_verify_command():
    """Mirrors `test_verify_event_never_contains_the_raw_command` for the new store. A verify
    command can carry a token or an internal hostname; the ledger deliberately records only a
    hash of it, and a durable local file must not be the place that leaks what the event stream
    was careful to hide."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command="echo super-secret-marker-xyz123")
        _every_store_off(base)
        lp = _loop()
        lp.verify_goal(base, goal)
        blob = json.dumps(lp.timing_store.read_goal(base, goal))
        assert "super-secret-marker-xyz123" not in blob


def test_a_flaky_but_passing_verify_still_records_its_duration():
    """Exit 5 means the suite passed but is not deterministic, and it returns EARLY. The write is
    placed before that return deliberately: the work happened and took real time, and a goal that
    burns twenty minutes proving itself flaky must not report as having taken none."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command="true")
        _every_store_off(base)
        lp = _loop()
        lp._flake_verdict = lambda *a, **k: {"verdict": "unverified", "runs": [],
                                             "disagreement": "run 2 disagreed", "ms": 7}
        assert lp.verify_goal(base, goal) == 5

        names = [e["name"] for e in lp.timing_store.read_goal(base, goal) if e["kind"] == "verify"]
        assert "command" in names


def test_the_flake_and_revert_passes_contribute_their_own_durations():
    """Both re-run tests and both already measure themselves, and both figures are currently
    discarded. On a green goal they can dominate the proving command, so omitting them makes
    `background` materially short."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command="true")
        _every_store_off(base)
        lp = _loop()
        lp._flake_verdict = lambda *a, **k: {"verdict": "verified", "runs": [],
                                             "disagreement": None, "ms": 4242}
        lp._diff_revert_verdict = lambda *a, **k: {"verdict": "absent", "kills": [], "survivors": [],
                                                   "reason": "none", "ms": 3131}
        assert lp.verify_goal(base, goal) == 0

        by_name = {e["name"]: e["ms"] for e in lp.timing_store.read_goal(base, goal)
                   if e["kind"] == "verify"}
        assert by_name.get("flake") == 4242
        assert by_name.get("diff_revert") == 3131


def test_a_pass_that_never_ran_contributes_no_interval():
    """`flake_check` returns `ms: 0` for the case where it did not run at all. Zero is not a
    measurement, and recording it would claim the pass happened instantaneously."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d, verify_command="true")
        _every_store_off(base)
        lp = _loop()
        lp._flake_verdict = lambda *a, **k: {"verdict": "absent", "runs": [],
                                             "disagreement": None, "ms": 0}
        assert lp.verify_goal(base, goal) == 0

        names = [e["name"] for e in lp.timing_store.read_goal(base, goal)]
        assert "flake" not in names


def test_record_emits_recorded_to_the_action_log():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        _with_action_log(base)
        lp = _loop()
        lp._record(base, _Sink(), "g.md", "parked", "blocked on a decision")
        entries = lp.actionlog.read_goal(base, "g.md")
        hits = [e for e in entries if e["kind"] == "recorded"]
        assert len(hits) == 1
        assert hits[0]["result"] == "parked" and hits[0]["detail"] == "blocked on a decision"


def test_record_done_emits_recorded_with_no_detail():
    with tempfile.TemporaryDirectory() as d:
        base = _telemetry_base(d)
        _with_action_log(base)
        lp = _loop()
        lp._record(base, _Sink(), "g.md", "done")
        entries = lp.actionlog.read_goal(base, "g.md")
        hits = [e for e in entries if e["kind"] == "recorded"]
        assert len(hits) == 1
        assert hits[0]["result"] == "done"
        assert "detail" not in hits[0]


# --------------------------------------------------------------------- #498: run-id attribution
# verify_goal now stamps the evidence file with the writing run's id (state.run_identity, from
# SIGMA_RUN_ID) and pid, so a CONCURRENT sibling's green can no longer satisfy THIS run's
# `record done` (done_refusal requires the id to match). It also warns loudly when it would clobber
# an evidence file a DIFFERENT run wrote. Legacy evidence (no run key) stays handled gracefully.


def _verify_base(d, command="true"):
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "--local",
                    "sigma.allowRepositoryShellCommands", "true"], check=True)
    base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True); (base / "state").mkdir()
    (base / "config.json").write_text(json.dumps({"verify": {"command": command}}))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    return str(base)


def test_verify_goal_stamps_the_evidence_with_this_runs_id_and_pid(monkeypatch):
    """The evidence write now carries `run` (this run's id) and `pid` (the verify process), alongside
    the existing command/exit/at/tail keys."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d)
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        assert lp.main(["loop.py", "verify", base, "158"]) == 0
        ev = json.loads((pathlib.Path(base) / "state" / "verify" / "158.json").read_text())
        assert ev["run"] == "worker-A"
        assert ev["pid"] == os.getpid()          # main() runs verify_goal in THIS process
        assert ev["exit"] == 0 and "command" in ev and "at" in ev and "tail" in ev


def test_verify_goal_stamps_verify_state_pass_or_fail_in_the_evidence():
    """issue #997: the evidence write also carries `verify_state` ("pass"/"fail"), derived from
    the SAME exit code as the existing `exit` key -- the literal field a downstream ingest reader
    lands onto its goal table's verify_state, so its verify metric's status CASE
    (which already maps 'pass'->PASS, 'fail'->FAIL) finally has something to read. Two goals, two
    commands -- a passing one and a failing one -- so both branches of the one-line ternary are
    actually exercised, not just asserted for one of them."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")
        assert lp.main(["loop.py", "verify", base, "158"]) == 0
        ev = json.loads((pathlib.Path(base) / "state" / "verify" / "158.json").read_text())
        assert ev["verify_state"] == "pass"
        assert ev["exit"] == 0

    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="false")
        assert lp.main(["loop.py", "verify", base, "159"]) == 1
        ev = json.loads((pathlib.Path(base) / "state" / "verify" / "159.json").read_text())
        assert ev["verify_state"] == "fail"
        assert ev["exit"] != 0


def test_verify_goal_leaves_run_none_when_unattributed(monkeypatch):
    """No SIGMA_RUN_ID -> `run` is stamped None (not absent, not crashing): an unattributed run
    behaves exactly as before, done_refusal falls through to freshness."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d)
        monkeypatch.delenv("SIGMA_RUN_ID", raising=False)
        assert lp.main(["loop.py", "verify", base, "158"]) == 0
        ev = json.loads((pathlib.Path(base) / "state" / "verify" / "158.json").read_text())
        assert ev["run"] is None


def test_verify_goal_warns_when_overwriting_a_different_live_runs_evidence(monkeypatch, capsys):
    """Suggestion #2: overwriting an evidence file a DIFFERENT run wrote, whose writer pid is still
    alive, prints a loud WARNING to stderr -- best-effort, never fatal (the run still writes its own
    correctly-attributed evidence and exits 0)."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d)
        ev = pathlib.Path(base) / "state" / "verify" / "158.json"
        ev.parent.mkdir(parents=True, exist_ok=True)
        # A sibling run's evidence, whose writer (this test process) is verifiably still alive:
        ev.write_text(json.dumps({"command": "true", "exit": 0, "at": time.time(),
                                  "run": "worker-A", "pid": os.getpid(), "tail": []}))
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-B")
        assert lp.main(["loop.py", "verify", base, "158"]) == 0     # never fatal
        err = capsys.readouterr().err
        assert "WARNING" in err and "different" in err.lower()
        assert json.loads(ev.read_text())["run"] == "worker-B"      # our evidence did get written


def test_verify_goal_does_not_warn_overwriting_our_own_or_a_dead_runs_evidence(monkeypatch, capsys):
    """No false alarm: overwriting our OWN run's evidence, or a different run whose writer pid is
    dead, must NOT warn."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d)
        ev = pathlib.Path(base) / "state" / "verify" / "158.json"
        ev.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        # (i) our own run's prior evidence -> no warning
        ev.write_text(json.dumps({"command": "true", "exit": 0, "at": time.time(),
                                  "run": "worker-A", "pid": os.getpid(), "tail": []}))
        assert lp.main(["loop.py", "verify", base, "158"]) == 0
        assert "WARNING" not in capsys.readouterr().err
        # (ii) a different run whose writer pid is dead -> no warning
        ev.write_text(json.dumps({"command": "true", "exit": 0, "at": time.time(),
                                  "run": "worker-Z", "pid": 2**30, "tail": []}))
        assert lp.main(["loop.py", "verify", base, "158"]) == 0
        assert "WARNING" not in capsys.readouterr().err


# --------------------------------------------------------------------- #1902: verify keeps the
# watchers alive too, not just goal-pick time. `_ensure_watcher` and `_ensure_ledger_delivery`
# (both unit-tested in isolation above) used to be called only from `start`/`next`/`next-batch`'s
# CLI dispatch -- a single goal's own Research-through-Retro lifecycle can run for hours through
# nothing but `verify`/`commit`/`pr`/`post-review`/`merge`, entirely outside that coverage. This
# tests the `verify` half of the fix; `tests/test_work.py` tests the `commit`/`pr`/`post-review`/
# `merge` half.
#
# #2578 CUT THIS LIST FROM FIVE TO TWO. The three private-side starters #2578 removed were not
# core's, and the core no longer starts those daemons at all --
# the private side's own git hooks are now the only revival path for them. The
# two that remain are core's own. The expected list below is the assertion that carries that: it
# compares the recorded CALLS, so a survivor silently dropped from a call site fails here.

def test_cli_verify_triggers_both_ensure_functions(monkeypatch):
    """Same call shape `next`/`next-batch` already use (loop.py's own `_ensure_watcher(argv[2],
    config)` etc.) -- proven here by monkeypatching the two module-level names to recorders and
    confirming `main(["verify", ...])` calls both, regardless of what `verify_goal` itself finds
    (a goal with a real, passing verify_command -- the ensure calls fire before/independent of
    whether verify passes, fails, or has no command at all)."""
    lp = _loop()
    calls = []
    monkeypatch.setattr(lp, "_ensure_watcher", lambda *a, **k: calls.append("watcher"))
    monkeypatch.setattr(lp, "_ensure_ledger_delivery", lambda *a, **k: calls.append("ledger-delivery"))
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d)
        assert lp.main(["loop.py", "verify", base, "158"]) == 0
    assert calls == ["watcher", "ledger-delivery"]


def test_cli_verify_triggers_ensure_functions_even_with_no_verify_command(monkeypatch):
    """The ensure calls are a loop-trigger concern, independent of verify's own NO-COMMAND early
    return (exit 3) -- a goal with nothing to prove is still a live loop trigger."""
    lp = _loop()
    calls = []
    monkeypatch.setattr(lp, "_ensure_watcher", lambda *a, **k: calls.append("watcher"))
    monkeypatch.setattr(lp, "_ensure_ledger_delivery", lambda *a, **k: calls.append("ledger-delivery"))
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True); (base / "state").mkdir()
        (base / "config.json").write_text(json.dumps({}))          # no verify.command declared
        (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
        assert lp.main(["loop.py", "verify", str(base), "158"]) == 3     # NO-COMMAND
    assert calls == ["watcher", "ledger-delivery"]


# #2393: the ledger-delivery check is wired at `run_loop`/`next`/`next-batch` as well as at
# `verify` above. These three sites predate the #1902 spy-test convention and have no
# "all N ensure functions" spy test of their own to extend -- a pre-existing gap, narrowed
# but not closed by #2578 (which cut the daemons those sites armed from five to two).

def test_run_loop_triggers_the_ledger_delivery_check(monkeypatch):
    lp = _loop()
    calls = []
    monkeypatch.setattr(lp, "_ensure_ledger_delivery", lambda *a, **k: calls.append(1))
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0)
        lp.run_loop(base, lambda g: ("done", ""))
    assert calls == [1]


def test_cli_next_triggers_the_ledger_delivery_check(monkeypatch):
    lp = _loop()
    calls = []
    monkeypatch.setattr(lp, "_ensure_ledger_delivery", lambda *a, **k: calls.append(1))
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0)
        assert lp.main(["loop.py", "next", base]) == 0
    assert calls == [1]


def test_cli_next_batch_triggers_the_ledger_delivery_check(monkeypatch):
    lp = _loop()
    calls = []
    monkeypatch.setattr(lp, "_ensure_ledger_delivery", lambda *a, **k: calls.append(1))
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0)
        assert lp.main(["loop.py", "next-batch", base]) == 0
    assert calls == [1]


def test_cli_record_triggers_the_ledger_delivery_check():
    """Mirrors `test_record_arms_the_daemons_so_finishing_a_goal_restarts_a_stalled_shipper`
    below (same call site, same "record is the one verb whose failure loses data" wrapped-try
    posture) -- the ledger-delivery check joined that same guarded block."""
    lp = _loop()
    calls = []
    real = lp._ensure_ledger_delivery
    lp._ensure_ledger_delivery = lambda *a, **k: calls.append(1)
    try:
        with tempfile.TemporaryDirectory() as d:
            base = _lease_base(d, actor="me", claims=[])
            goals = pathlib.Path(base) / "goals"; goals.mkdir()
            g = str(goals / "0042.md")
            pathlib.Path(g).write_text("---\nid: 0042\nstatus: pending\n---\nx\n")
            lp.main(["loop.py", "record", base, g, "done"])
        assert calls, "record never armed the ledger-delivery check"
    finally:
        lp._ensure_ledger_delivery = real


def test_end_to_end_a_sibling_green_cannot_satisfy_this_runs_record_done():
    """The #310 sequence, reproduced end to end as separate loop.py PROCESSES (the real slot-worker
    shape: verify and record are distinct invocations sharing one .sdlc). Worker A's OWN verify
    FAILS; a sibling (worker B) green overwrites the shared evidence file; worker A's `record done`
    must still be REFUSED -- while worker B's own matching `record done` is accepted. Executed
    negative control per north-star Rule #1: injecting the mismatch and watching `record done` fail.
    Reverting the guard makes A's `record done` return 0 (the bug)."""
    def _cfg(base, command):
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"verify": {"enforce": True, "command": command}}))
    def run(env, *a):
        return subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True,
                              text=True, env={**os.environ, **env})
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d)
        goal = str(pathlib.Path(base) / "goals" / "0158.md")   # a real goal file so `record` can complete
        pathlib.Path(goal).write_text("---\nid: 0158\nstatus: in_progress\n---\nbody\n")
        (pathlib.Path(base) / "acceptance").mkdir()             # #1338: enforce-on verify needs a record
        (pathlib.Path(base) / "acceptance" / "0158.md").write_text(
            '---\nkind: "acceptance"\ngoal: "0158"\nverify_command: null\n---\n'
            "## Done when\n- [ ] one\n- [ ] two\n- [ ] three\n")
        _loop().main(["loop.py", "start-run", base])            # stamp run_started_at (freshness base)
        # Worker A's own verify genuinely FAILS (a red chain), stamping run=worker-A, exit!=0:
        _cfg(base, "false")
        a = run({"SIGMA_RUN_ID": "worker-A"}, "verify", base, goal)
        assert a.returncode == 1 and "FAILED" in a.stdout
        # Worker B's concurrent GREEN overwrites the shared evidence, stamping run=worker-B, exit 0:
        _cfg(base, "true")
        b = run({"SIGMA_RUN_ID": "worker-B"}, "verify", base, goal)
        assert b.returncode == 0 and "VERIFIED" in b.stdout
        # Worker A tries to record done on the (B-authored) green -> REFUSED (exit 4):
        rec_a = run({"SIGMA_RUN_ID": "worker-A"}, "record", base, goal, "done")
        assert rec_a.returncode == 4, (rec_a.returncode, rec_a.stderr)
        assert "different run" in rec_a.stderr
        # Worker B records done on its OWN green -> accepted (exit 0):
        rec_b = run({"SIGMA_RUN_ID": "worker-B"}, "record", base, goal, "done")
        assert rec_b.returncode == 0, (rec_b.returncode, rec_b.stderr)


# --- #265: a managing loop has its own age-based heartbeat -----------------

def test_session_heartbeat_is_written_at_start_and_removed_on_clean_end():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0)
        generation = lp.session_start(base, os.getpid())
        hb = lp.session_heartbeat_path(base, os.getpid())
        assert json.loads(hb.read_text())["pid"] == os.getpid()
        lp.session_end(base, os.getpid(), generation=generation)
        assert not hb.exists()


def test_session_heartbeat_age_distinguishes_idle_from_dead_without_signalling_a_process():
    """The dead-loop control uses an impossible PID and an old timestamp, never SIGSTOP/SIGKILL."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0)
        dead = 99999999
        lp.session_start(base, dead)
        hb = lp.session_heartbeat_path(base, dead)
        lp.write_session_heartbeat(base, dead, now=1000.0)
        assert lp.session_heartbeat_liveness(base, dead, now=1001.0) == ("idle", 1.0)
        assert lp.session_heartbeat_liveness(base, dead, now=5000.0)[0] == "dead"


def test_next_refreshes_the_managing_session_heartbeat_even_when_the_backlog_is_idle():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0)
        pid = os.getpid(); lp.session_start(base, pid)
        lp.write_session_heartbeat(base, pid, now=1.0)
        lp._next(base, lp.sources.get_source(base, lp.state.load_config(base)), lp.state.load_config(base),
                 session_pid=pid)
        assert json.loads(lp.session_heartbeat_path(base, pid).read_text())["last_seen"] > 1.0


def test_next_batch_refreshes_before_a_blocked_reconciliation_sweep(monkeypatch):
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); pid = os.getpid(); lp.session_start(base, pid)
        lp.write_session_heartbeat(base, pid, now=1.0)
        seen = []
        def blocked(sdlc_dir, config):
            seen.append(json.loads(lp.session_heartbeat_path(base, pid).read_text())["last_seen"])
        monkeypatch.setattr(lp, "_reconcile_sweep", blocked)
        lp.next_batch(base, lp.sources.get_source(base, lp.state.load_config(base)), lp.state.load_config(base),
                      session_pid=pid)
        assert seen and seen[0] > 1.0


def test_next_batch_writes_one_heartbeat_for_its_prologue_and_not_each_slot(monkeypatch):
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 2); pid = os.getpid(); lp.session_start(base, pid)
        writes = []
        monkeypatch.setattr(lp, "write_session_heartbeat", lambda *args, **kwargs: writes.append(args[1]) or True)
        lp.next_batch(base, lp.sources.get_source(base, lp.state.load_config(base)), lp.state.load_config(base),
                      max_concurrent=2, session_pid=pid)
        assert writes == [pid]


def test_session_end_removes_its_heartbeat_while_holding_its_session_lock(monkeypatch):
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); pid = os.getpid(); generation = lp.session_start(base, pid)
        calls = []
        real = lp._session_locked
        def locked(sdlc_dir, session_pid, fn, **kwargs):
            def probe():
                calls.append(lp.session_heartbeat_path(base, pid).exists())
                return fn()
            return real(sdlc_dir, session_pid, probe, **kwargs)
        monkeypatch.setattr(lp, "_session_locked", locked)
        lp.session_end(base, pid, generation=generation)
        assert calls == [True]
        assert not lp.session_heartbeat_path(base, pid).exists()


def test_session_start_cannot_write_an_orphan_heartbeat_after_an_interleaved_end(monkeypatch):
    """The interleave runs immediately after start releases its lock, the old orphan window."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); pid = os.getpid(); real = lp._session_locked; ending = [False]
        def interleaved(sdlc_dir, session_pid, fn, **kwargs):
            result = real(sdlc_dir, session_pid, fn, **kwargs)
            if not ending[0]:
                ending[0] = True
                lp.session_end(base, pid, generation="owner")
            return result
        monkeypatch.setattr(lp, "_session_locked", interleaved)
        lp.session_start(base, pid, generation="owner")
        assert not lp._session_marker_path(base, pid).exists()
        assert not lp.session_heartbeat_path(base, pid).exists()


def test_prior_session_end_cannot_delete_a_successor_generation():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); pid = os.getpid()
        first = lp.session_start(base, pid, generation="first")
        second = lp.session_start(base, pid, generation="second")
        lp.session_end(base, pid, generation=first)
        marker = json.loads(lp._session_marker_path(base, pid).read_text())
        heartbeat = json.loads(lp.session_heartbeat_path(base, pid).read_text())
        assert second == "second" and marker["generation"] == heartbeat["generation"] == "second"


def test_bare_session_end_cannot_delete_a_successor_generation():
    """The old no-token cleanup must not guess it owns a generation-bearing marker."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); pid = os.getpid()
        lp.session_start(base, pid, generation="first")
        lp.session_start(base, pid, generation="second")
        lp.session_end(base, pid)
        marker = json.loads(lp._session_marker_path(base, pid).read_text())
        heartbeat = json.loads(lp.session_heartbeat_path(base, pid).read_text())
        assert marker["generation"] == heartbeat["generation"] == "second"


def test_registered_session_refresh_never_recreates_an_orphan_after_clean_end():
    """A delayed phase-report refresh after session-end leaves neither lifecycle file behind."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); pid = os.getpid()
        generation = lp.session_start(base, pid)
        lp.session_end(base, pid, generation=generation)
        assert lp.refresh_registered_session_heartbeat(base, pid) is False
        assert not lp._session_marker_path(base, pid).exists()
        assert not lp.session_heartbeat_path(base, pid).exists()


def test_cli_start_end_generation_prevents_a_prior_owner_from_clearing_a_successor(capsys):
    """The documented separate-process lifecycle has to carry the token, not merely the helper."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); pid = os.getppid()
        assert lp.main(["loop.py", "start", base, "--session-pid", str(pid)]) == 0
        first = capsys.readouterr().out.strip()
        assert lp.main(["loop.py", "start", base, "--session-pid", str(pid)]) == 0
        second = capsys.readouterr().out.strip()
        assert first and second and first != second
        assert lp.main(["loop.py", "session-end", base, "--session-pid", str(pid),
                        "--session-generation", first]) == 0
        assert lp._session_marker_path(base, pid).exists()
        assert lp.main(["loop.py", "session-end", base, "--session-pid", str(pid),
                        "--session-generation", second]) == 0
        assert not lp._session_marker_path(base, pid).exists()


def test_run_loop_cleanup_cannot_clear_an_overlapping_same_pid_successor(monkeypatch):
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _backlog(d, 0); real_start = lp.session_start; first = []
        def interleaved(sdlc_dir, pid, generation=None):
            owned = real_start(sdlc_dir, pid, generation=generation)
            first.append(owned)
            real_start(sdlc_dir, pid, generation="successor")
            return owned
        monkeypatch.setattr(lp, "session_start", interleaved)
        lp.run_loop(base, lambda _: ("done", ""))
        marker = json.loads(lp._session_marker_path(base, os.getpid()).read_text())
        assert first and marker["generation"] == "successor"


def test_readme_session_end_gesture_captures_and_returns_the_generation_token():
    readme = (pathlib.Path(__file__).resolve().parent.parent / "docs/routines.md").read_text()
    match = re.search(r'`(session_generation=\$\(python3 <installed-sigma>/skills/sigma-loop/scripts/loop\.py '
                      r'start \.sdlc --session-pid "\$PPID"\))`', readme)
    assert match, "README must contain one complete, copyable generation-capture command"
    # Shell parsing is the control: removing the final `)` makes this exact documented gesture red.
    assert subprocess.run(["bash", "-n"], input=match.group(1), text=True, capture_output=True).returncode == 0
    assert '--session-generation "$session_generation"' in readme


# --- #1391 step 5e: the throttled, opt-in reconciliation sweep -----------------------------------
# Runs ONCE per batch in next_batch's PROLOGUE, deliberately not inside _next() where
# _auto_unpark_sweep sits -- next_batch calls _next() up to max_concurrent times, so anything there
# is paid 8x per batch on this repo's own config.


def _with_reconcile(base, mode="on", ttl_minutes=60):
    p = pathlib.Path(base) / "config.json"
    cfg = json.loads(p.read_text())
    cfg.setdefault("discovery", {})["reconcile"] = {"mode": mode, "ttl_minutes": ttl_minutes}
    p.write_text(json.dumps(cfg))
    return cfg


def test_reconcile_mode_defaults_to_off_and_imports_nothing(monkeypatch):
    """An adopter who never opts in pays literally nothing -- not even the import."""
    lp = _loop()

    def boom(name):
        raise AssertionError(f"must not _load({name!r}) when reconcile.mode is off")

    monkeypatch.setattr(lp, "_load", boom)
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        assert lp._reconcile_sweep(base, lp.state.load_config(base)) == frozenset()


def test_reconcile_mode_stays_off_on_an_unrecognised_value(monkeypatch):
    lp = _loop()
    monkeypatch.setattr(lp, "_load", lambda name: (_ for _ in ()).throw(AssertionError("no")))
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_reconcile(base, mode="always")
        assert lp._reconcile_sweep(base, lp.state.load_config(base)) == frozenset()


def test_reconcile_sweep_runs_when_enabled_and_due(monkeypatch):
    lp = _loop()
    calls = []

    class FakeReconcile:
        def sweep_reconcile(self, sdlc_dir, config, apply=False, run=None):
            calls.append((sdlc_dir, apply))
            return {"actions": [{"issue": "42", "result": "done"}]}

    real = lp._load
    monkeypatch.setattr(lp, "_load",
                        lambda n: FakeReconcile() if n == "reconcile" else real(n))
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_reconcile(base)
        swept = lp._reconcile_sweep(base, lp.state.load_config(base))
    assert calls == [(base, True)]
    assert swept == frozenset({"42"})


def test_the_single_pick_path_also_sweeps(monkeypatch):
    """#1445: `_reconcile_sweep` was called ONLY from `next_batch`'s prologue, so an operator using
    `next` never swept -- `os` sat ~23h past a 60-minute TTL while `next` ran repeatedly, and the
    sweeper that should have caught 38 stale closed issues simply never fired.

    Asserted at the source: `_next` must reference the sweep. The TTL watermark (gate 2) is what
    keeps this cheap when it is not due, which is why the original amplification worry does not
    apply -- a not-due call is one file read."""
    import inspect
    lp = _loop()
    assert "_reconcile_sweep" in inspect.getsource(lp._next_untraced)   # #1607: `_next` wraps it
    assert "_next_untraced" in inspect.getsource(lp._next)
    assert "_reconcile_sweep" in inspect.getsource(lp.next_batch)   # prologue call still there


def test_reconcile_sweep_is_throttled_by_the_ttl_watermark(monkeypatch):
    """The census costs ~13 gh calls; a backlog's label state does not drift between two picks
    minutes apart."""
    lp = _loop()
    calls = []

    class FakeReconcile:
        def sweep_reconcile(self, sdlc_dir, config, apply=False, run=None):
            calls.append(sdlc_dir)
            return {"actions": []}

    real = lp._load
    monkeypatch.setattr(lp, "_load",
                        lambda n: FakeReconcile() if n == "reconcile" else real(n))
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_reconcile(base, ttl_minutes=60)
        cfg = lp.state.load_config(base)
        lp._reconcile_sweep(base, cfg)          # first: due
        lp._reconcile_sweep(base, cfg)          # second, immediately: throttled
        assert len(calls) == 1
        lp._reconcile_sweep(base, cfg, now=time.time() + 3601)   # past the TTL: due again
        assert len(calls) == 2


def test_reconcile_sweep_treats_a_corrupt_watermark_as_due(monkeypatch):
    """Safer to run a read-mostly sweep one extra time than to skip it forever on a bad stamp."""
    lp = _loop()
    calls = []

    class FakeReconcile:
        def sweep_reconcile(self, sdlc_dir, config, apply=False, run=None):
            calls.append(sdlc_dir)
            return {"actions": []}

    real = lp._load
    monkeypatch.setattr(lp, "_load",
                        lambda n: FakeReconcile() if n == "reconcile" else real(n))
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_reconcile(base)
        (pathlib.Path(base) / "state").mkdir(parents=True, exist_ok=True)
        lp._reconcile_watermark_path(base).write_text("not json")
        lp._reconcile_sweep(base, lp.state.load_config(base))
    assert len(calls) == 1


def test_reconcile_sweep_fails_open_when_the_sweep_raises(monkeypatch, capsys):
    lp = _loop()

    class FakeReconcile:
        def sweep_reconcile(self, *a, **k):
            raise RuntimeError("simulated transient gh failure")

    real = lp._load
    monkeypatch.setattr(lp, "_load",
                        lambda n: FakeReconcile() if n == "reconcile" else real(n))
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_reconcile(base)
        assert lp._reconcile_sweep(base, lp.state.load_config(base)) == frozenset()  # no raise
    assert "reconcile sweep failed non-fatally" in capsys.readouterr().err


def test_next_batch_sweeps_once_not_once_per_slot(monkeypatch):
    """The whole reason it lives in the prologue: _auto_unpark_sweep sits inside _next(), so it is
    paid max_concurrent times per batch. This must not copy that amplification."""
    lp = _loop()
    calls = []

    class FakeReconcile:
        def sweep_reconcile(self, sdlc_dir, config, apply=False, run=None):
            calls.append(sdlc_dir)
            return {"actions": []}

    real = lp._load
    monkeypatch.setattr(lp, "_load",
                        lambda n: FakeReconcile() if n == "reconcile" else real(n))
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_reconcile(base)
        src = _Queue(["a", "b", "c"])
        lp.next_batch(base, src, lp.state.load_config(base), max_concurrent=3)
    assert len(calls) == 1


# --- #1391: the worktree-safety fixes -------------------------------------------------------------
# A real data-loss chain: _release closed the claim AND wiped the liveness marker, worktree paths
# are deterministic, work.start's resume gate consulted only the (now-closed) claim -> a second
# agent attached to a LIVE agent's directory, where work.commit() runs `git add -A`.


def test_agent_heartbeat_refreshes_the_marker_mtime():
    """The marker used to record START time, so a goal worked longer than the lease read DEAD while
    its agent was still running -- and this repo ships max_minutes 900 against ttl_hours 12."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        lp.agent_start(base, "42", os.getpid(), {})
        path = lp._agent_marker_path(base, "42", "main")
        old = time.time() - 10_000
        os.utime(path, (old, old))
        assert path.stat().st_mtime < time.time() - 5000
        assert lp.agent_heartbeat(base, "42") is True
        assert path.stat().st_mtime > time.time() - 60


def test_agent_heartbeat_does_not_change_the_recorded_pid():
    """The pid is the agent's identity and must not drift -- a heartbeat moves only the timestamp."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        lp.agent_start(base, "42", 4242, {})
        lp.agent_heartbeat(base, "42")
        assert lp._agent_marker_path(base, "42", "main").read_text().strip() == "4242"


def test_codex_agent_marker_keeps_thread_identity_and_foreign_heartbeat_cannot_refresh_it(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        thread_a = "01999aaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
        thread_b = "01999bbb-bbbb-7bbb-8bbb-bbbbbbbbbbbb"
        monkeypatch.setenv("CODEX_THREAD_ID", thread_a)
        monkeypatch.setenv("CODEX_SESSION_ID", thread_a)
        assert lp.agent_start(base, "42", os.getpid(), {}) is True
        path = lp._agent_marker_path(base, "42")
        assert json.loads(path.read_text()) == {"pid": os.getpid(), "codex_thread_id": thread_a}
        assert lp.agent_alive(base, "42", {}) == ("alive", os.getpid())
        old = time.time() - 100
        os.utime(path, (old, old))
        monkeypatch.setenv("CODEX_THREAD_ID", thread_b)
        monkeypatch.setenv("CODEX_SESSION_ID", thread_b)
        assert lp.agent_heartbeat(base, "42") is False
        assert path.stat().st_mtime < time.time() - 60


def test_fresh_unreadable_agent_marker_still_counts_as_registered_worker():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        path = lp._agent_marker_path(base, "42")
        path.parent.mkdir(parents=True)
        path.write_text("{partial")
        assert lp._goal_has_registered_worker(base, "42", {}) is True
        stale = time.time() - (lp.ledger.DEFAULT_LEASE_TTL_HOURS * 3600 + 60)
        os.utime(path, (stale, stale))
        assert lp._goal_has_registered_worker(base, "42", {}) is False


def test_codex_agent_start_cli_refuses_missing_thread_id(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        monkeypatch.setenv("CODEX_SESSION_ID", "01999aaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa")
        assert lp._dispatch(["loop.py", "agent-start", base, "42", "--pid", str(os.getpid())]) == 2
        assert "cannot register agent identity" in capsys.readouterr().err
        assert not lp._agent_marker_path(base, "42").exists()


def test_agent_heartbeat_is_a_safe_no_op_with_no_marker():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        assert lp.agent_heartbeat(base, "42") is False        # must not raise


def test_a_long_running_goal_stays_alive_once_heartbeated():
    """The whole point: liveness must survive past the lease TTL for an agent that is still working."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[], ttl_hours=1)
        cfg = lp.state.load_config(base)
        lp.agent_start(base, "42", os.getpid(), cfg)
        path = lp._agent_marker_path(base, "42", "main")
        stale = time.time() - 2 * 3600                        # older than the 1h lease
        os.utime(path, (stale, stale))
        assert lp.agent_alive(base, "42", cfg)[0] == "dead"    # the bug, reproduced
        lp.agent_heartbeat_all(base, "42")
        assert lp.agent_alive(base, "42", cfg)[0] == "alive"   # and closed


def test_release_no_longer_wipes_the_liveness_marker():
    """_release is a RECLAIM, not a terminal outcome. If it is wrong about abandonment, wiping the
    marker destroys the only evidence that would have stopped the next picker."""
    lp = _loop()
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:in-progress"}]}]
    with tempfile.TemporaryDirectory() as d:
        base = _github_ledger_base(
            d, claims=[("me", "42", "claimed", _stale_ts(1))], ttl_hours=12)
        cfg = lp.state.load_config(base)
        lp.agent_start(base, "42", os.getpid(), cfg)
        calls = []
        src = lp.sources.GitHubSource(cfg, run=_in_progress_gh_run(issues, calls), sdlc_dir=base)
        src._LABEL_SWAP_RETRY_BASE = 0
        lp._release(base, src, "42", reason="test")
        assert lp._agent_marker_path(base, "42", "main").exists()
        assert lp._goal_has_registered_worker(base, "42", cfg) is True


def test_record_still_clears_the_marker_on_a_terminal_outcome():
    """The distinction that makes removing it from _release safe: a goal that genuinely FINISHED
    must still stop being reported as in-flight."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        cfg = lp.state.load_config(base)
        lp.agent_start(base, "42", os.getpid(), cfg)

        class _Terminal:
            def complete(self, goal): pass
            def park(self, goal, reason, tier=None): pass
            def fail(self, goal, reason): pass

        lp._record(base, _Terminal(), "42", "done")
        assert not lp._agent_marker_path(base, "42", "main").exists()


# --- #1393: precheck resolves the blockers a `blocked-by` park names ---------------------------


def _pack(*refs, kind="blocked-by", confident=True):
    return {"goal": "42", "degraded": [],
            "findings": [{"kind": kind, "ref": r, "confident": confident, "score": 1.0}
                         for r in refs]}


def _loop_with_stub_blockers(resolve):
    """A fresh `loop` module whose `_load("blockers")` returns a STUB carrying the given `resolve`.

    Deliberately a stub rather than monkeypatching the real module: `blockers.resolve` is the thing
    under test in `test_blockers.py`, and mutating it here would leak across every test that runs
    afterwards in the same process. What belongs HERE is only how `loop` uses the result."""
    loop = _loop()
    real = _bl_mod()

    class _Stub:
        PROMOTED, CHAINED, PICKABLE = real.PROMOTED, real.CHAINED, real.PICKABLE
        render = staticmethod(real.render)
        park_reason = staticmethod(real.park_reason)
    _Stub.resolve = staticmethod(resolve)
    original = loop._load
    loop._load = lambda name: _Stub if name == "blockers" else original(name)
    return loop


def _bl_mod():
    spec = importlib.util.spec_from_file_location("blockers", S / "blockers.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class _NullSource:
    pass


def test_resolve_blockers_for_park_says_so_when_everything_is_now_workable():
    """A goal whose blockers were ALL resolved is still parked -- it genuinely cannot proceed yet --
    but it is parked behind work that is now moving, and `auto_unpark` resumes it when that work
    closes. The park text has to say which, or a human cannot tell the two apart."""
    seen = {}

    def resolve(sdlc_dir, config, source, goal, refs, run=None, apply=True):
        seen["refs"] = list(refs)
        return {"results": [{"ref": r, "verdict": "promoted", "detail": "d", "acted": True}
                            for r in refs], "resolved": list(refs), "surfaced": []}
    loop = _loop_with_stub_blockers(resolve)
    reason, transition = loop._resolve_blockers_for_park(
        ".sdlc", {}, _NullSource(), "42", _pack("9", "7", "7"),
        "backlog cross-check: blocked by #7", None)
    assert transition == "blocked", "a fully-resolvable block is not a park"
    assert seen["refs"] == ["7", "9"]                      # deduped and numerically sorted
    assert "every named blocker is now workable" in reason
    assert "resumes automatically" in reason


def test_resolve_blockers_for_park_names_what_survived():
    def resolve(sdlc_dir, config, source, goal, refs, run=None, apply=True):
        return {"results": [{"ref": "7", "verdict": "chained",
                             "detail": "#7 is parked — run /sigma-unpark on #7", "acted": False}],
                "resolved": [], "surfaced": ["7"]}
    loop = _loop_with_stub_blockers(resolve)
    reason, transition = loop._resolve_blockers_for_park(".sdlc", {}, _NullSource(), "42",
                                                        _pack("7"), "base", None)
    assert transition == "park", "a blocker needing a human IS a park"
    assert "/sigma-unpark" in reason and "#7" in reason and reason.startswith("base")


def test_resolve_blockers_for_park_is_a_no_op_without_a_confident_blocked_by_finding():
    """Only a CONFIDENT blocked-by finding names a real blocker. A duplicate finding, a weak one, or
    none at all must leave the park reason byte-identical -- this runs on the park path."""
    def resolve(*a, **k):
        raise AssertionError("resolve must not be called")
    loop = _loop_with_stub_blockers(resolve)
    for pack in (_pack("7", kind="duplicate"), _pack("7", confident=False), _pack()):
        assert loop._resolve_blockers_for_park(".sdlc", {}, _NullSource(), "42", pack, "base",
                                               None) == ("base", "park")


def test_resolve_blockers_for_park_never_breaks_the_park_path():
    """It runs on the park path, and `run_loop`'s #335 handler downgrades an exception into another
    park -- so a raising resolver could park-loop. Every failure degrades to today's behaviour."""
    def boom(*a, **k):
        raise RuntimeError("resolver exploded")
    loop = _loop_with_stub_blockers(boom)
    assert loop._resolve_blockers_for_park(".sdlc", {}, _NullSource(), "42", _pack("7"), "base",
                                           None) == ("base", "park")


def test_resolve_blockers_for_park_ignores_a_non_numeric_ref():
    """In LOCAL mode `handoff_key` falls back to the goal itself, so a ledger-sourced blocked-by
    finding can carry a file path rather than an issue number -- there is nothing to resolve, and
    handing it to a GitHub resolver would be worse than doing nothing."""
    def resolve(*a, **k):
        raise AssertionError("resolve must not be called")
    loop = _loop_with_stub_blockers(resolve)
    pack = _pack("goals/0001-thing.md")
    assert loop._resolve_blockers_for_park(".sdlc", {}, _NullSource(), "42", pack, "base",
                                           None) == ("base", "park")


# --- #889: a BARE session (no supervise.sh) is attributed and so can emit run_stop -------------
# The whole point of this issue for the budget-exhaustion rate: every test above arms SIGMA_RUN_ID by hand,
# which is exactly what production could NOT do outside supervise.sh. These prove the CLI arms
# itself from its own --session-pid.

def test_bare_session_next_arms_a_run_id_from_its_explicit_session_pid(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 1)
        # setenv("") first, THEN delenv: `_arm_run_id` writes os.environ directly, which monkeypatch
        # cannot track unless it already owns the key -- without this the derived id survives
        # teardown and leaks into whatever test runs next in this process.
        monkeypatch.setenv("SIGMA_RUN_ID", "")
        monkeypatch.delenv("SIGMA_RUN_ID", raising=False)
        lp = _loop()
        lp._arm_run_id("31857")
        assert os.environ["SIGMA_RUN_ID"] == "session-31857"
        assert lp.state.run_identity() == "session-31857"


def test_arming_never_overwrites_a_launchers_own_id(monkeypatch):
    """supervise.sh's id is stable across a worker's relaunches; a per-session one is not, so it
    must never shadow one already exported."""
    monkeypatch.setenv("SIGMA_RUN_ID", "supervise-1-2-3")
    lp = _loop()
    lp._arm_run_id("31857")
    assert os.environ["SIGMA_RUN_ID"] == "supervise-1-2-3"


def test_arming_leaves_an_unusable_session_pid_unattributed(monkeypatch):
    """THE load-bearing negative (plan-review F2). `--session-pid` with no value parses to the
    literal "true", and loop.py's `start` falls back to os.getppid() -- neither is stable across a
    session's separate invocations. Minting an id from either would make each idle `next` poll
    claim a fresh (run_id, run_started_at) slot and emit a DUPLICATE run_stop, the bug #905 closed.
    Unattributed is the correct, already-supported outcome."""
    lp = _loop()
    monkeypatch.setenv("SIGMA_RUN_ID", "")      # see the leak note above: give monkeypatch the key
    for bad in (None, "true", "", "not-a-pid"):
        monkeypatch.delenv("SIGMA_RUN_ID", raising=False)
        lp._arm_run_id(bad)
        assert os.environ.get("SIGMA_RUN_ID") is None, bad
        assert lp.state.run_identity() is None, bad


def test_a_bare_attributed_drain_emits_exactly_one_run_stop_and_polling_does_not_duplicate(monkeypatch):
    """End-to-end for the budget-exhaustion rate: attribution via the derived id is enough to get a run_stop row,
    and #905's (run_id, run_started_at) dedupe still holds across a repeat poll."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 1)
        monkeypatch.setenv("SIGMA_RUN_ID", "")    # see the leak note above
        monkeypatch.delenv("SIGMA_RUN_ID", raising=False)
        lp = _loop()
        lp._arm_run_id("31857")                       # what the CLI's `next` verb now does for itself
        res = lp.run_loop(base, lambda g: ("done", ""))
        assert res["stopped"] == "backlog-empty"
        stops = _run_stops(lp, base)
        assert len(stops) == 1
        assert stops[0]["reason_class"] == "backlog-empty"
        lp._emit_run_stop_once(base, lp.state.load_config(base), None, "backlog-empty")
        assert len(_run_stops(lp, base)) == 1         # the idle-poll repeat is still deduped


def test_the_cli_next_verb_arms_itself_end_to_end_via_subprocess():
    """THE wiring test (code-review finding 1). Every other test here calls `_arm_run_id` by hand,
    so all three call sites in `start`/`next`/`next-batch` could be deleted with the suite still
    green -- and bare sessions would silently go back to unattributed, which is the entire bug.

    This runs the real CLI in a real child process with SIGMA_RUN_ID scrubbed from the
    environment, exactly as a bare `/sigma-loop` does, and asserts a run_stop row lands carrying the
    DERIVED id. A subprocess is what makes it honest: an in-process call would inherit this
    interpreter's own os.environ."""
    with tempfile.TemporaryDirectory() as d:
        base = _backlog_with_telemetry(d, 0)          # already drained -> `next` returns DONE
        env = {k: v for k, v in os.environ.items() if k != "SIGMA_RUN_ID"}
        loop_py = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts" / "loop.py"
        subprocess.run([sys.executable, str(loop_py), "start", base, "--session-pid", "31857"],
                       env=env, capture_output=True, text=True, timeout=60)
        r = subprocess.run([sys.executable, str(loop_py), "next", base, "--session-pid", "31857"],
                           env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        stops = _run_stops(_loop(), base)
        assert len(stops) == 1, f"expected one run_stop, got {stops} (stdout={r.stdout!r})"
        assert stops[0]["reason_class"] == "backlog-empty"
        # `run_stop` events carry only (reason_class, why) -- the run id is the ATTRIBUTION gate and
        # the dedupe key, never a field on the event -- so the derived id is proven by its own
        # #905 marker file instead. Its mere existence is the proof: `_emit_run_stop_once` writes it
        # via `claim_run_stop(sdlc_dir, run_id, ...)`, so a marker named for the derived id can only
        # exist if the CLI minted that id for itself.
        marker = pathlib.Path(base) / "state" / "run_stop" / "session-31857.json"
        assert marker.exists(), sorted((pathlib.Path(base) / "state" / "run_stop").glob("*")) \
            if (pathlib.Path(base) / "state" / "run_stop").exists() else "no run_stop dir at all"


# ------------------------------------------------- #1962: a claim reachable off the pick path
# `claimed` had exactly ONE emitter -- `_next()` -- so it was a side effect of goal SELECTION, and
# there was no way to claim a goal you had already chosen. Every entry point that names its own goal
# (`/sigma-goal <issue>`, direct dispatch) reached `loop.py record ... done` unclaimed; measured on
# this repo's real store, 197 of 440 terminal goals had no claim, capping a downstream autonomy rate
# at 55.2% no matter how autonomous the loop actually was.



def _claimable(d, actor="me", claims=(), ttl_hours=0):
    """A `_lease_base` that also has a real goal FILE. Local mode resolves a goal to a path
    (`mark_in_progress` opens it), so a bare "42" is not a claimable goal there."""
    base = _lease_base(d, actor=actor, claims=claims, ttl_hours=ttl_hours)
    goals = pathlib.Path(base) / "goals"; goals.mkdir(exist_ok=True)
    goal = goals / "0042.md"
    goal.write_text("---\nid: 0042\nstatus: pending\n---\nx\n")
    return base, str(goal)

def test_next_still_emits_claimed_through_the_extracted_helper():
    """CHARACTERISATION LOCK for the `_claim()` extraction. Written against the PRE-refactor code
    and must stay green through it: `next` still marks in progress, writes the ledger claim, and
    writes the actionlog claim, in that order."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_action_log(base)
        lp = _loop(); src = _Queue(["42"])
        assert lp._next(base, src, lp.state.load_config(base)) == ("goal", "42")
        assert [e["kind"] for e in lp.ledger.read_all(base)] == ["claimed"]
        assert "claimed" in [e["kind"] for e in lp.actionlog.read_goal(base, "42")]
        assert src.marked == ["42"]


# --- #2392: a genuinely exhausted claim-label write (mark_in_progress returning False, mirroring
# GitHubSource once its own retries are spent — see sources.py's mark_in_progress/
# _escalate_claim_label_failure) must NOT block the pick. That is a deliberate SCOPE decision, not
# an oversight: the issue names "genuinely block the pick on a failed durable-signal write" as a
# separate, bigger availability-vs-exclusion trade-off needing its own explicit human sign-off, and
# this fix does not make that call. `_QueueClaimLabelFails` models the real, post-#2392
# `GitHubSource` shape (`False`), distinct from plain `_Queue`'s `None` (a source with no external
# label to fail, e.g. `LocalSource`, which `_claim`'s own `is False` check deliberately excludes).


class _QueueClaimLabelFails(_Queue):
    def mark_in_progress(self, g):
        self.marked.append(g)
        return False


def test_next_still_picks_when_the_claim_label_write_is_exhausted():
    """The direct regression test for scope, not just a feature test: `next` must still mark in
    progress and still claim even though the claim-label write itself genuinely failed -- the exact
    same outcome as the unmodified characterization test above, now under a source that reports a
    real, exhausted failure instead of quiet success."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_action_log(base)
        lp = _loop(); src = _QueueClaimLabelFails(["42"])
        assert lp._next(base, src, lp.state.load_config(base)) == ("goal", "42")
        assert [e["kind"] for e in lp.ledger.read_all(base)] == ["claimed"]
        assert "claimed" in [e["kind"] for e in lp.actionlog.read_goal(base, "42")]
        assert src.marked == ["42"]


def test_claimed_ledger_entry_is_flagged_when_the_claim_label_write_is_exhausted():
    """#2392: the ledger's own `claimed` entry carries the same fact `mark_in_progress`'s own loud
    stderr escalation already reported (`_escalate_claim_label_failure`), so it is not lost once
    that stderr line scrolls away -- durable, not just momentary. Reuses the existing `why`
    OPTIONAL_FIELD rather than a new one."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_action_log(base)
        lp = _loop(); src = _QueueClaimLabelFails(["42"])
        lp._next(base, src, lp.state.load_config(base))
        claim = [e for e in lp.ledger.read_all(base) if e["kind"] == "claimed"][0]
        assert "claim-label write failed" in (claim.get("why") or "")


def test_claim_why_appends_the_failure_note_rather_than_overwriting_an_existing_why():
    """An armed claim's own `why` ("armed at first observed work...") must survive alongside the
    new claim-label-failure note, not be silently replaced by it -- both facts matter, and `_claim`
    is called directly here (rather than through `note`) to isolate exactly that append behaviour."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_action_log(base)
        lp = _loop(); src = _QueueClaimLabelFails(["42"])
        lp._claim(base, src, "42", lp.state.load_config(base),
                 why="armed at first observed work (#1962)")
        claim = [e for e in lp.ledger.read_all(base) if e["kind"] == "claimed"][0]
        why = claim.get("why") or ""
        assert "armed at first observed work" in why
        assert "claim-label write failed" in why


def test_next_still_picks_when_mark_in_progress_reports_no_news_at_all():
    """The fail-open contract this fix must not narrow: `_Queue.mark_in_progress` returns `None`
    (no news either way -- what every non-GitHub source, and every source written before #2392,
    still returns). `_claim`'s new `is False` check must not misread that as a failure -- the
    ledger's `why` stays exactly as it was before this fix, byte-for-byte, matching
    `test_next_still_emits_claimed_through_the_extracted_helper` above."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        _with_action_log(base)
        lp = _loop(); src = _Queue(["42"])
        lp._next(base, src, lp.state.load_config(base))
        claim = [e for e in lp.ledger.read_all(base) if e["kind"] == "claimed"][0]
        assert claim.get("why") in (None, "")


# --- Task 2: the `claim` verb -------------------------------------------------------------

def test_claim_verb_emits_a_claimed_event_for_a_named_goal():
    with tempfile.TemporaryDirectory() as d:
        base, goal = _claimable(d)
        lp = _loop()
        assert lp.main(["loop.py", "claim", base, goal]) == 0
        assert [e["kind"] for e in lp.ledger.read_all(base)] == ["claimed"]


def test_claim_verb_is_idempotent_and_does_not_double_write():
    with tempfile.TemporaryDirectory() as d:
        base, goal = _claimable(d)
        lp = _loop()
        lp.main(["loop.py", "claim", base, goal])
        assert lp.main(["loop.py", "claim", base, goal]) == 0
        assert [e["kind"] for e in lp.ledger.read_all(base)].count("claimed") == 1


def test_claim_verb_refuses_a_goal_another_actor_holds(capsys):
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[("alice", "42", "claimed")], ttl_hours=12)
        lp = _loop()
        assert lp.main(["loop.py", "claim", base, "42"]) == 2
        assert "alice" in capsys.readouterr().err


# --- Task 3: self-arming, and the two controls that keep it honest -------------------------

def test_note_on_an_unclaimed_goal_arms_the_claim():
    with tempfile.TemporaryDirectory() as d:
        base, goal = _claimable(d)
        lp = _loop()
        lp.main(["loop.py", "note", base, goal, "RESEARCH: something"])
        assert [e["kind"] for e in lp.ledger.read_all(base)].count("claimed") == 1


def test_an_armed_claim_is_marked_so_it_is_distinguishable_from_a_picked_one():
    with tempfile.TemporaryDirectory() as d:
        base, goal = _claimable(d)
        lp = _loop()
        lp.main(["loop.py", "note", base, goal, "RESEARCH: something"])
        claim = [e for e in lp.ledger.read_all(base) if e["kind"] == "claimed"][0]
        assert "armed" in (claim.get("why") or "")


def test_record_does_NOT_arm_a_claim_the_control_against_synthesized_timestamps():
    """THE CONTROL. Arming at `record` would write claimed_ts ~= terminal_ts -- a fabricated span,
    which #1962's done_when forbids outright ("Never synthesize a timestamp to make a number look
    better"). A goal whose first and only loop.py call is `record` must stay honestly unclaimed,
    and the Coverage gap rule is what makes that residue visible instead of silent."""
    with tempfile.TemporaryDirectory() as d:
        base = _lease_base(d, actor="me", claims=[])
        goals = pathlib.Path(base) / "goals"; goals.mkdir()
        goal = str(goals / "0042.md")
        pathlib.Path(goal).write_text("---\nid: 0042\nstatus: pending\n---\nx\n")
        lp = _loop()
        lp.main(["loop.py", "record", base, goal, "done"])
        kinds = [e["kind"] for e in lp.ledger.read_all(base)]
        assert "claimed" not in kinds
        assert "done" in kinds


def test_an_advisory_model_tier_query_does_NOT_claim_the_goal():
    """THE SECOND CONTROL (plan-review F1). predict.py's `_emit_model_choice` shells out to
    `loop.py emit <goal> model_choice` from an ADVISORY /sigma-model question that can be asked
    about any goal. Arming there would mark_in_progress an issue nobody is working, which
    `_next()` then reads as claimed and SKIPS -- a speculative tier query silently deleting a goal
    from the backlog."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _claimable(d)          # a REAL goal file: the only reason no claim appears
        lp = _loop()                            # must be that `emit` is excluded, nothing else
        lp.main(["loop.py", "emit", base, goal, "model_choice", "--model", "opus"])
        assert "claimed" not in [e["kind"] for e in lp.ledger.read_all(base)]
        lp.main(["loop.py", "note", base, goal, "x"])    # same base, an INCLUDED verb
        assert "claimed" in [e["kind"] for e in lp.ledger.read_all(base)], \
            "the base is not claimable at all -- this control would pass vacuously"


def test_a_prework_precheck_does_NOT_claim_the_goal():
    """Same control, other half: `precheck` is loop.py's own opt-in PRE-work backlog cross-check."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _claimable(d)          # ditto -- see the sibling control above
        lp = _loop()
        lp.main(["loop.py", "precheck", base, goal])
        assert "claimed" not in [e["kind"] for e in lp.ledger.read_all(base)]
        lp.main(["loop.py", "note", base, goal, "x"])    # same base, an INCLUDED verb
        assert "claimed" in [e["kind"] for e in lp.ledger.read_all(base)], \
            "the base is not claimable at all -- this control would pass vacuously"


def test_arming_does_not_fire_twice():
    with tempfile.TemporaryDirectory() as d:
        base, goal = _claimable(d)
        lp = _loop()
        lp.main(["loop.py", "note", base, goal, "one"])
        lp.main(["loop.py", "note", base, goal, "two"])
        assert [e["kind"] for e in lp.ledger.read_all(base)].count("claimed") == 1


def test_arming_reads_the_whole_ledger_at_most_once_per_goal():
    """PLAN-REVIEW F2, as an executable assertion rather than a promise. `ledger.read_all` is
    O(the whole ledger) -- measured 31 ms mean / 224 ms cold over 1,591 entries in 1,193 files on
    this repo, so ~3.1 s at 100x -- and `note` runs once per phase of every goal. The per-goal
    marker must make every call after the first O(1)."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _claimable(d)
        lp = _loop()
        calls = []
        real = lp.ledger.read_all
        lp.ledger.read_all = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        for _ in range(4):
            lp.main(["loop.py", "note", base, goal, "x"])
        assert len(calls) == 1, f"read_all called {len(calls)}x for one goal; must be 1"


def test_arming_does_nothing_at_all_when_the_ledger_is_switched_off():
    """OPT-IN. `_claim` calls `mark_in_progress`, which on a github-mode repo writes
    `sdlc:in-progress` to a real issue. A repo that never switched the ledger on gets no claimed
    event out of arming anyway, so without this guard a `note` would silently relabel the board of
    an operator who opted out -- an external side effect for no benefit. `next` may mark
    unconditionally because marking IS picking; arming is not picking."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _claimable(d)
        p = pathlib.Path(base) / "config.json"
        cfg = json.loads(p.read_text()); cfg["ledger"]["enabled"] = False
        p.write_text(json.dumps(cfg))
        lp = _loop(); src_before = pathlib.Path(goal).read_text()
        lp.main(["loop.py", "note", base, goal, "x"])
        assert pathlib.Path(goal).read_text() == src_before, "mark_in_progress ran on an opted-out repo"
        assert not (pathlib.Path(base) / "state" / "claims" / "0042.claimed").exists(), \
            "a marker was written, so arming would stay dead after the ledger is switched on"


# ---------------------------------------- #2029: arming may not relabel a ticket that is no goal
# The ledger opt-in was the ONLY guard on the arming claim, and it answers "may Sigma record
# anything here?", never "is this ticket a goal?". `sigma-goal-review` closes on two `loop.py note`
# calls that deliberately target tickets which are never goals -- the Dossier (`story`, SKILL.md
# §4d) and the Spec/Epic (`epic`, §5g) -- so on 2026-09-01 arming wrote `sdlc:in-progress` onto
# story #2017 and epic #2020, two seconds before each comment landed. That is the ORPHAN class
# `docs/label-model.md` §4a exists to forbid, manufactured by the tool that reports it.


class _GhLike:
    """A GitHubSource stand-in for the arming guard, carrying only the surface arming can reach:
    `goal_label`, `fetch_body_labels` (raw `[{"name": ...}]`, exactly what the real one returns),
    `mark_in_progress` and `note`. `LocalSource` deliberately has NONE of the label surface, which
    is why `_arming_may_mark` gates on `fetch_body_labels` rather than on a mode string."""
    goal_label = "sdlc:goal"

    def __init__(self, labels=("sdlc:goal",), raises=None):
        self.labels = list(labels); self.raises = raises
        self.marked = []; self.noted = []; self.reads = 0

    def fetch_body_labels(self, goal):
        self.reads += 1
        if self.raises:
            raise self.raises
        return {"body": "", "labels": [{"name": n} for n in self.labels]}

    def mark_in_progress(self, goal): self.marked.append(goal)
    def note(self, goal, text): self.noted.append((goal, text))
    def release(self, goal, reason): pass


def _gh_base(d, actor="me"):
    """A ledger-enabled, github-MODE base: the arming path resolves its source through
    `sources.get_source`, so the test patches that rather than passing a source in."""
    base = _lease_base(d, actor=actor, claims=(), ttl_hours=12)
    p = pathlib.Path(base) / "config.json"
    cfg = json.loads(p.read_text())
    cfg["mode"] = "github"
    cfg["discovery"] = {"source": "github", "github": {"repo": "o/r", "goal_label": "sdlc:goal",
                                                       "in_progress_label": "sdlc:in-progress"}}
    p.write_text(json.dumps(cfg))
    return base


def test_note_on_a_non_goal_ticket_does_not_relabel_it():
    """#2029, the defect itself. A `note` on a Dossier writes the COMMENT and nothing else."""
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d)
        lp = _loop(); src = _GhLike(labels=["story", "sdlc:designed"])   # #2017's real labels
        lp.sources.get_source = lambda *a, **k: src
        lp.main(["loop.py", "note", base, "2017", "goal-review: CONFIRMED"])
        assert src.marked == [], "arming wrote sdlc:in-progress onto a ticket that is not a goal"
        assert src.noted, "the verb the caller actually asked for did not run"


def test_note_on_a_non_goal_ticket_still_records_the_local_claim():
    """The asymmetry, stated as its own assertion: fail CLOSED on the external label, OPEN on the
    local journal. #1962's whole reason for arming is `claimed_ts` for goals the picker never
    chose; suppressing the ledger entry too would fix the label by re-breaking the metric."""
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d); _with_action_log(base)
        lp = _loop(); src = _GhLike(labels=["story"])
        lp.sources.get_source = lambda *a, **k: src
        lp.main(["loop.py", "note", base, "2017", "goal-review: CONFIRMED"])
        claims = [e for e in lp.ledger.read_all(base) if e["kind"] == "claimed"]
        assert len(claims) == 1 and "armed" in (claims[0].get("why") or "")
        assert "claimed" in [e["kind"] for e in lp.actionlog.read_goal(base, "2017")]
        assert (pathlib.Path(base) / "state" / "claims" / "2017.claimed").exists()


def test_note_on_a_real_goal_still_marks_it_in_progress():
    """THE ANTI-VACUOUS MIRROR. Without this the guard above passes just as well if arming stopped
    marking ANYTHING, which would break every `/sigma-goal <issue>` run rather than fix one."""
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d)
        lp = _loop(); src = _GhLike(labels=["sdlc:goal", "priority:P1"])
        lp.sources.get_source = lambda *a, **k: src
        lp.main(["loop.py", "note", base, "2018", "RESEARCH: something"])
        assert src.marked == ["2018"], "a real goal stopped being marked in progress"


def test_verify_on_a_non_goal_ticket_does_not_relabel_it_either():
    """`_ARMS_CLAIM` is ("note", "verify", "agent-start") and all three reach ONE `_ensure_claimed`
    call site, so the guard must hold for a verb that is not `note`. Pinned rather than assumed."""
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d)
        lp = _loop(); src = _GhLike(labels=["epic", "priority:P1"])      # #2020's real labels
        lp.sources.get_source = lambda *a, **k: src
        lp.main(["loop.py", "verify", base, "2020"])
        assert src.marked == [], "verify armed a claim that relabelled a non-goal ticket"


def test_verify_on_a_real_goal_still_marks_it_in_progress():
    """The mirror for `verify`, for the same reason the `note` mirror exists."""
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d)
        lp = _loop(); src = _GhLike(labels=["sdlc:goal"])
        lp.sources.get_source = lambda *a, **k: src
        lp.main(["loop.py", "verify", base, "2021"])
        assert src.marked == ["2021"]


def test_an_unreadable_label_answer_skips_the_label_and_keeps_the_ledger_entry():
    """`fetch_body_labels` RAISES on a transport failure by design, and a blip is NOT evidence a
    ticket is a goal. The write that leaves this machine fails closed; the record that stays on it
    fails open."""
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d)
        lp = _loop(); src = _GhLike(raises=RuntimeError("gh: 502"))
        lp.sources.get_source = lambda *a, **k: src
        lp.main(["loop.py", "note", base, "2017", "x"])
        assert src.marked == [], "an unreadable label answer was treated as membership"
        assert [e["kind"] for e in lp.ledger.read_all(base)].count("claimed") == 1


def test_a_source_with_no_label_surface_keeps_marking_exactly_as_before():
    """LOCAL MODE IS UNCHANGED, and that is the correct answer rather than a concession: a local
    goal resolves to a file under `.sdlc/goals/`, so membership is inherent in the argument and
    there is no external surface to protect. `hasattr(source, "fetch_body_labels")` is the same
    gate `feature_stamp.unit_of` uses to tell the two sources apart."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _claimable(d, ttl_hours=12)
        lp = _loop(); src = _Queue([goal])          # no fetch_body_labels, like LocalSource
        lp.sources.get_source = lambda *a, **k: src
        lp.main(["loop.py", "note", base, goal, "x"])
        assert src.marked == [goal]


def test_the_membership_question_is_asked_at_most_once_per_goal():
    """The guard costs one `gh issue view` per goal per MACHINE, not one per phase: it sits behind
    `_ensure_claimed`'s O(1) marker, on the same miss path the O(ledger) read already pays. Pinned
    the same way `test_arming_reads_the_whole_ledger_at_most_once_per_goal` pins that one."""
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d)
        lp = _loop(); src = _GhLike(labels=["story"])
        lp.sources.get_source = lambda *a, **k: src
        for _ in range(4):
            lp.main(["loop.py", "note", base, "2017", "x"])
        assert src.reads == 1, f"fetch_body_labels called {src.reads}x for one goal; must be 1"


def test_the_guard_asks_the_real_GitHubSource_the_question_it_answers():
    """THE DOUBLE-DIVERGENCE CONTROL. Every test above answers through `_GhLike`; if the real
    `GitHubSource` returned labels in a shape `_arming_may_mark` misreads, all of them would still
    pass while the bug stayed live. So ask the REAL class, with only its subprocess stubbed, using
    a payload copied from an actual `gh issue view 2017 --json body,labels` on 2026-09-01."""
    import importlib.util as _il
    spec = _il.spec_from_file_location("sources_2029", S / "sources.py")
    sources = _il.module_from_spec(spec); spec.loader.exec_module(sources)
    payload = json.dumps({"body": "", "labels": [
        {"name": "sdlc:in-progress"}, {"name": "story"}, {"name": "sdlc:designed"}]})
    cfg = {"discovery": {"source": "github", "github": {"repo": "o/r"}}}
    lp = _loop()
    story = sources.GitHubSource(cfg, run=lambda args, **k: payload)
    assert lp._arming_may_mark(story, "2017") is False
    goal = sources.GitHubSource(cfg, run=lambda args, **k: json.dumps(
        {"body": "", "labels": [{"name": "sdlc:goal"}]}))
    assert lp._arming_may_mark(goal, "2018") is True, \
        "the real source never says yes -- the False above would be vacuous"


# ------------------------------------------- #1933: the flake check, wired into verify_goal

def test_verify_records_the_flake_verdict_in_its_own_key_not_in_verify_state():
    """THE VOCABULARY DECISION, pinned so a later change cannot quietly undo it.

    A downstream verify metric folds ANY verify_state value other than 'pass'/'fail' into ABSENT,
    and its ingest reader accepts only those two before falling back to `exit`. So
    writing 'unverified' into verify_state would silently render a flaky goal as "no evidence
    recorded for this goal at all" -- strictly worse than the truth, since the goal WAS verified and
    was found non-deterministic. The verdict lives in its own `flake` key; verify_state keeps
    meaning exactly what a downstream verify metric already reads."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")
        assert lp.verify_goal(base, "42") == 0
        ev = json.loads((pathlib.Path(base) / "state" / "verify" / "42.json").read_text())
        assert ev["verify_state"] in ("pass", "fail")
        assert "flake" in ev, "the flake verdict was never recorded"
        assert ev["flake"]["verdict"] in ("verified", "unverified", "absent")


def test_a_goal_that_changed_no_test_files_is_absent_and_pays_no_3x_cost():
    """THE SCOPING CONTROL (done_when 3). A goal touching only implementation must not pay the 3x
    cost, and must not be marked unverified merely for having no tests to re-run. `absent` is the
    honest state, the same ABSENT-vs-PASS distinction a downstream gap evaluator draws."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")
        calls = []
        real = lp.flake_check.check
        lp.flake_check.check = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        try:
            lp.verify_goal(base, "42")
        finally:
            lp.flake_check.check = real
        ev = json.loads((pathlib.Path(base) / "state" / "verify" / "42.json").read_text())
        assert ev["flake"]["verdict"] == "absent"
        assert ev["flake"]["ms"] == 0, "the 3x check ran despite no changed test files"
        # Same lesson as the sibling test below: assert the CALL, so this control cannot pass
        # vacuously if the verdict happens to be "absent" for some unrelated reason.
        assert calls == [], "flake_check.check was reached with no changed test files"


def test_a_failing_verify_does_not_also_run_the_flake_check():
    """No point re-running tests three times to ask whether an already-red suite is deterministic;
    the red IS the finding. Keeps the 3x cost off the failure path entirely.

    EXERCISES `_flake_verdict` DIRECTLY, WITH CHANGED FILES STUBBED IN, and the shape is the whole
    point. TWO earlier versions of this control could not fail, each seen to pass with the guard
    deliberately DELETED:

      v1 asserted `flake["verdict"] == "absent"` after a red `verify_goal` -- but this fixture has
         no changed test files, so the verdict is "absent" whether the guard exists or not.
      v2 spied on `flake_check.check` instead -- still could not fail, because `_flake_verdict`'s
         OTHER short-circuit (`if changed else empty`) already prevents the call on that fixture.

    Stubbing `changed_test_files` to return a file is what finally makes the `passed` guard the only
    remaining thing that can stop the call. Both directions are asserted, so an INVERTED guard fails
    too rather than trading one silent bug for another."""
    lp = _loop()
    calls = []
    real_changed, real_check = lp.tamper_scan.changed_test_files, lp.flake_check.check
    lp.tamper_scan.changed_test_files = lambda _diff: ["tests/test_x.py"]
    lp.flake_check.check = lambda *a, **k: (calls.append(1),
                                            {"verdict": "verified", "runs": [],
                                             "disagreement": None, "ms": 1})[1]
    try:
        with tempfile.TemporaryDirectory() as d:
            base = _verify_base(d, command="true")
            assert lp._flake_verdict(base, "42", d, passed=False)["verdict"] == "absent"
            assert calls == [], "the flake check ran on an already-red verify"
            lp._flake_verdict(base, "42", d, passed=True)
            assert calls == [1], "the flake check did NOT run on a green verify -- guard inverted"
    finally:
        lp.tamper_scan.changed_test_files = real_changed
        lp.flake_check.check = real_check


def test_flake_scope_says_changed_files_on_the_diff_path(monkeypatch, capsys):
    """#1571: with no planned ## Tests the scope is the goal's diff, and the label says so.

    The node count is not known here (`check` collects it), so only the kind and the file COUNT
    are pinned; the stderr line names the kind either way."""
    lp = _loop()
    monkeypatch.setattr(lp.tamper_scan, "changed_test_files", lambda _diff: ["tests/test_x.py"])
    monkeypatch.setattr(lp.flake_check, "check",
                        lambda *a, **k: {"verdict": "verified", "workers": 3})
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")
        result = lp._flake_verdict(base, "42", d, passed=True)
    assert "scope" in result
    assert result["scope"]["kind"] == "changed files"
    assert result["scope"]["files"] == 1
    assert result["verdict"] == "verified"
    assert "loop: flake check scope: changed files -- " in capsys.readouterr().err


def test_verify_evidence_carries_the_witness_verdict_alongside_the_flake_one():
    """#1934: `unverified` must be a DISTINCT, VISIBLE state in the evidence -- never silently
    equivalent to a passing test. Asserted positively, because the write goes through a fail-open
    path: a rejected or skipped write would leave the key absent and the goal reading as fine."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")
        assert lp.verify_goal(base, "42") == 0
        ev = json.loads((pathlib.Path(base) / "state" / "verify" / "42.json").read_text())
        assert "witness" in ev, "the witness verdict never reached the evidence"
        assert ev["witness"]["verdict"] in ("verified", "unverified", "absent")
        assert "flake" in ev and ev["flake"]["verdict"] in ("verified", "unverified", "absent")


def test_a_red_verify_writes_a_witness_naming_the_failure_kind():
    """The red path is the ONLY moment the failure kind and the at-red hash exist. Stubbed at the
    seam rather than driven through a real red suite, so the assertion is about what
    _witness_verdict RECORDS, not about pytest's output format."""
    lp = _loop()
    real = lp.flake_check._failed_ids
    lp.flake_check._failed_ids = lambda _out: ["tests/test_x.py::test_a"]
    try:
        with tempfile.TemporaryDirectory() as d:
            base = _verify_base(d, command="true")
            lp._witness_verdict(base, "42", d, passed=False,
                                output="E   AssertionError: assert 3 == 4")
            got = lp.witness.witnesses(base, "42")
            assert [w["kind"] for w in got] == ["assertion"], got
    finally:
        lp.flake_check._failed_ids = real


# ------------------- #2235: wrong base ref + blanket-stamp laundering


def _git(cwd, *args):
    """A thin real-git runner, matching test_work.py's / test_doctor.py's own `_git` helper --
    every claim below is a claim about what git reports, so a fake `run` would only ever agree
    with whichever ref string the implementation already assumed."""
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr or proc.stdout}"
    return proc.stdout.strip()


def test_changed_tests_uses_the_remote_tracking_base_not_the_stale_local_branch(tmp_path):
    """#2235(a): `_changed_tests` diffed against the BARE local `"main"` -- not the recorded
    remote-tracking ref. A goal's worktree shares `refs/heads/main` with every other worktree of
    the same repo, and nothing here ever advances it (`git fetch` only moves
    `refs/remotes/<remote>/<base>`), so once another goal's own commit lands on `origin/main`
    behind this goal's back, local `main` is stale while the goal branch -- cut fresh from
    `origin/main`, exactly as `work.start()` does -- is not. Diffing against the stale local ref
    pulls that OTHER goal's own changed test file into THIS goal's changed-test set: the live
    mechanism behind #1810's 574 wrong-test witness records.

    REAL git, a REAL bare origin, a REAL second clone landing a commit behind this repo's back --
    a fake `run` stub settles nothing here, same reasoning test_work.py's own `test_real_git_*`
    cases give for their fixture."""
    lp = _loop()
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "main")

    # ANOTHER goal lands its own test change on origin/main, via a second clone -- so this repo's
    # local `main` never moves.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "O")
    _git(other, "config", "commit.gpgsign", "false")
    (other / "tests").mkdir()
    (other / "tests" / "test_unrelated.py").write_text("def test_u():\n    assert True\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "an unrelated goal's own test change")
    _git(other, "push", "-q", "origin", "main")

    # This repo fetches (as work.start() does on every pick), which moves the REMOTE-TRACKING ref
    # -- but nothing here ever moves local `refs/heads/main` itself.
    _git(repo, "fetch", "-q", "origin")
    assert _git(repo, "rev-parse", "main") != _git(repo, "rev-parse", "origin/main"), (
        "fixture is broken: local main already matches origin/main")

    # THIS goal's own branch, cut from the up-to-date origin/main, then its own test change.
    _git(repo, "checkout", "-q", "-b", "sdlc/2235", "origin/main")
    (repo / "tests" / "test_x.py").write_text("def test_a():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "this goal's own change")

    d = str(repo / ".sdlc")
    pathlib.Path(d, "state").mkdir(parents=True)
    lp.work._save(d, "42", {"worktree": str(repo), "branch": "sdlc/2235",
                            "base": "main", "remote": "origin", "pr": ""})

    tests = lp._changed_tests(d, "42", str(repo))
    files = {t.split("::")[0] for t in tests}
    assert "tests/test_x.py" in files, tests
    assert "tests/test_unrelated.py" not in files, (
        "the OTHER goal's own test leaked into THIS goal's changed-test set -- the bare local "
        f"`main` (stale, behind origin/main) was used instead of the remote-tracking ref: {tests}")


def test_an_unattributable_red_does_not_blanket_stamp_every_changed_test_strong():
    """#2235(b): the red path was `for node in flake_check._failed_ids(output) or tests:` -- one
    `classify(output)` for the WHOLE run, stamped onto every element of the fallback. When the run
    could not be attributed to any specific node id at all (an INTERNALERROR, a collection abort,
    a timeout -- none of which print a `FAILED <nodeid>` line), `or tests` silently substituted
    EVERY changed test for the empty attribution. `classify()` reads the assertion pattern ANYWHERE
    in the blob, so an INTERNALERROR traceback that happens to contain the substring
    "AssertionError" (plausible -- pytest's own internals raise it) fabricated a STRONG witness
    (`witness.STRONG_KINDS`) for tests that were never individually run, let alone failed. The live
    incident wrote 574 such records on #1810.

    `_changed_tests` is stubbed -- ITS resolution is defect (a), proven by the real-git control
    above; this test's subject is `_witness_verdict`'s handling of an UNATTRIBUTABLE red, so nothing
    here should turn on how "the changed tests" were discovered. `flake_check._failed_ids`,
    `witness.classify`, `witness.record` and `witness.witnesses` are all the REAL functions,
    unstubbed -- only the fact of failed-id attribution is exercised, the same seam
    test_a_red_verify_writes_a_witness_naming_the_failure_kind already uses for the opposite case."""
    lp = _loop()
    real_changed = lp._changed_tests
    lp._changed_tests = lambda *a, **k: ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]
    output = (
        "INTERNALERROR> Traceback (most recent call last):\n"
        "INTERNALERROR>   File \"_pytest/main.py\", line 1, in wrap_session\n"
        "INTERNALERROR>     session.exitstatus = doit(config, session) or 0\n"
        "INTERNALERROR>   File \"someplugin.py\", line 2, in pytest_collectstart\n"
        "INTERNALERROR>     assert False\n"
        "INTERNALERROR> AssertionError\n"
    )
    assert lp.flake_check._failed_ids(output) == [], "fixture is broken: output names a failed id"
    assert lp.witness.classify(output) == "assertion", "fixture is broken: not the STRONG kind"
    try:
        with tempfile.TemporaryDirectory() as d:
            base = _verify_base(d, command="true")
            lp._witness_verdict(base, "42", d, passed=False, output=output)
            got = lp.witness.witnesses(base, "42")
    finally:
        lp._changed_tests = real_changed
    assert got == [], (
        "an unattributable INTERNALERROR blanket-stamped every changed test with a fabricated "
        f"STRONG witness instead of recording nothing honest: {got}")
def test_verify_evidence_carries_the_diff_revert_verdict_key():
    """#2240: same visibility contract as the flake/witness siblings above -- a rejected or
    skipped fail-open write must not leave `diff_revert` silently absent from the evidence."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")
        assert lp.verify_goal(base, "42") == 0
        ev = json.loads((pathlib.Path(base) / "state" / "verify" / "42.json").read_text())
        assert "diff_revert" in ev, "the diff-revert verdict never reached the evidence"
        assert ev["diff_revert"]["verdict"] in ("verified", "unverified", "absent")


def test_diff_revert_verdict_does_not_run_on_a_red_verify():
    """Same reasoning as `_flake_verdict`'s sibling guard: 'does it survive without the change'
    means nothing when the suite does not even survive WITH it. Spies on `diff_revert.run` itself
    (not just reading the verdict) so an inverted guard that happens to still land on `absent` for
    an unrelated reason cannot pass this vacuously."""
    lp = _loop()
    calls = []
    real = lp.diff_revert.run
    lp.diff_revert.run = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    try:
        with tempfile.TemporaryDirectory() as d:
            base = _verify_base(d, command="true")
            lp.work._save(base, "42", {"worktree": d, "branch": "sdlc/42", "base": "main",
                                        "base_resolved": "main", "remote": "origin", "pr": ""})
            r = lp._diff_revert_verdict(base, "42", d, passed=False)
            assert r["verdict"] == lp.diff_revert.ABSENT
            assert calls == [], "diff_revert.run was reached on an already-red verify"
    finally:
        lp.diff_revert.run = real


def test_a_real_kill_through_diff_revert_verdict_is_recorded_as_a_strong_mutation_witness():
    """#2240 END TO END THROUGH loop.py's OWN GLUE, not diff_revert.py in isolation --
    tests/test_diff_revert.py already proves the module's own revert-and-rerun mechanism against
    real git+pytest; this proves loop.py's `_diff_revert_verdict` actually WIRES that result into
    a witness, which nothing else exercises. A real repo, a real UNCOMMITTED goal diff (a new
    function plus a new test importing it -- SKILL.md: no phase subagent commits before step 6, so
    this is the state verify actually runs against most of the time), a real `work` record naming
    the fork -- and then the assertion that matters: `witness.witnesses` names the new test with
    kind 'mutation', the STRONG kind #1935 was designed to write and, being unreachable (mutmut
    absent, wired into nothing), never had until now."""
    lp = _loop()
    d = tempfile.mkdtemp()
    try:
        def run(*argv):
            return subprocess.run(argv, cwd=d, capture_output=True, text=True)
        run("git", "init", "-q")
        run("git", "checkout", "-q", "-b", "main")
        run("git", "config", "user.email", "t@example.com")
        run("git", "config", "user.name", "t")
        (pathlib.Path(d) / "pkg").mkdir()
        (pathlib.Path(d) / "pkg" / "__init__.py").write_text("")
        (pathlib.Path(d) / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (pathlib.Path(d) / "tests").mkdir()
        (pathlib.Path(d) / "tests" / "test_calc.py").write_text(
            "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "base")
        run("git", "update-ref", "refs/remotes/origin/main", "HEAD")
        run("git", "checkout", "-q", "-b", "sdlc/77")
        # The goal's own UNCOMMITTED change: real new behaviour, plus a test that checks it.
        (pathlib.Path(d) / "pkg" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n")
        (pathlib.Path(d) / "tests" / "test_calc.py").write_text(
            "from pkg.calc import add, multiply\n\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n\n\n"
            "def test_multiply():\n    assert multiply(2, 3) == 6\n")

        base = str(pathlib.Path(d) / ".sdlc")
        lp.work._save(base, "77", {"worktree": d, "branch": "sdlc/77", "base": "main",
                                    "base_resolved": "main", "remote": "origin", "pr": ""})

        result = lp._diff_revert_verdict(base, "77", d, True)
        assert result["verdict"] == lp.diff_revert.VERIFIED, result
        assert result["kills"] == ["tests/test_calc.py::test_multiply"], result

        got = lp.witness.witnesses(base, "77")
        kinds = {w["test"]: w["kind"] for w in got}
        assert kinds.get("tests/test_calc.py::test_multiply") == "mutation", got
        # And the ordinary, unrelated `test_add` -- present in the SAME file, but genuinely
        # untouched since the fork -- must NOT also be credited (the over-crediting trap
        # tests/test_diff_revert.py's own real-repo control caught).
        assert "tests/test_calc.py::test_add" not in kinds, got
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ------------------- verify evidence names the tree it actually verified

def test_verify_evidence_records_WHICH_TREE_it_verified():
    """A verify record that does not say which tree it ran against is not evidence.

    work.root() falls back to the PROJECT ROOT for any goal with no work record -- documented and
    intentional -- so a goal whose worktree was made by hand, or whose record was lost, is verified
    against whatever the main checkout happens to contain. That happened live on 2026-09-01: six
    merged goals were verified against a checkout 77 commits behind origin/main, which produced six
    genuine failures of the STALE tree and refused `record done` for every one of them. Nothing in
    the evidence said so, and the numbers were misread as a truncated run of the right tree.

    AGENTS.md's own words: a green status file is not freshness."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")
        assert lp.verify_goal(base, "42") == 0
        ev = json.loads((pathlib.Path(base) / "state" / "verify" / "42.json").read_text())
        assert "root" in ev, "the evidence never says which tree was verified"
        assert ev["root"] == lp.work.root(base, "42")
        assert "head" in ev, "the evidence never says which commit was verified"


def test_verify_warns_LOUDLY_when_it_falls_back_to_the_project_root(capsys):
    """The fallback is correct behaviour; being SILENT about it is the defect. A goal with no work
    record gets verified somewhere other than its own worktree, and the operator must be told at the
    moment it happens, not left to infer it from a test count weeks later."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")
        lp.verify_goal(base, "42")
        err = capsys.readouterr().err
        assert "no work record" in err, err
        assert "PROJECT ROOT" in err, err
        assert "wrong code" in err, err


def test_verify_does_NOT_warn_when_the_goal_has_its_own_worktree():
    """The control that keeps the warning meaningful. A warning on every run is a warning nobody
    reads -- it must fire only on the fallback it is about."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")
        wt = pathlib.Path(d) / "wt"; wt.mkdir()
        rec = pathlib.Path(base) / "state" / "work"; rec.mkdir(parents=True, exist_ok=True)
        (rec / "42.json").write_text(json.dumps({"worktree": str(wt), "branch": "sdlc/42"}))
        if lp.work.root(base, "42") != str(wt):
            import pytest as _p; _p.skip("work record layout differs; the fallback path is covered above")
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            lp.verify_goal(base, "42")
        assert "no work record" not in buf.getvalue()


def test_head_is_None_not_fabricated_when_the_root_is_not_a_git_tree():
    """A sha we cannot read is None. Inventing one -- or silently omitting the key -- would make an
    unverifiable record look like a verified one.

    ASSERTS `is None` EXACTLY. The first version of this test read `is None or isinstance(str)`,
    which is true of every possible value: it stayed GREEN with the code deliberately fabricating
    `"0"*40`. A tautology wearing an assertion's clothes, and it was caught only by running the
    control -- the same failure mode this session found five other times."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")          # a bare temp dir: not a git checkout
        lp.verify_goal(base, "42")
        ev = json.loads((pathlib.Path(base) / "state" / "verify" / "42.json").read_text())
        assert ev["head"] is None, f"a non-git root must yield None, got {ev['head']!r}"


def test_verify_does_NOT_claim_the_project_root_when_it_recovered_a_worktree(capsys):
    """#1985's warning must stay TRUE once `work.root` recovers a worktree from disk. `fell_back`
    used to be read off the work record, which for a hand-made worktree (no record, tree present)
    would print "verifying the PROJECT ROOT (<the goal's own worktree>)" — naming a worktree while
    calling it the project root. A warning that lies is worse than no warning."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")
        wt = pathlib.Path(base) / "work" / "42"; wt.mkdir(parents=True)
        assert lp.verify_goal(base, "42") == 0
        err = capsys.readouterr().err
        assert "PROJECT ROOT" not in err, err
        ev = json.loads((pathlib.Path(base) / "state" / "verify" / "42.json").read_text())
        assert ev["root"] == str(wt.resolve()), ev["root"]


def test_verify_warns_when_a_record_names_a_worktree_that_is_GONE(capsys):
    """The other direction, and a hole that predates the recovery: a record whose worktree directory
    has been deleted sends `work.root` to the project root, while the old record-presence test said
    it had NOT fallen back — so the loudest case of all was silent. Asking where we landed, rather
    than whether a record exists, answers both directions with one comparison."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _verify_base(d, command="true")
        rec = pathlib.Path(base) / "state" / "work"; rec.mkdir(parents=True, exist_ok=True)
        (rec / "42.json").write_text(json.dumps(
            {"worktree": str(pathlib.Path(d) / "deleted-wt"), "branch": "sdlc/42"}))
        assert lp.verify_goal(base, "42") == 0
        err = capsys.readouterr().err
        assert "PROJECT ROOT" in err, err


def test_record_still_succeeds_when_arming_raises():
    """Fail-open, like every other arming call site. A daemon that cannot start must never cost a
    goal its terminal record.

    #2578 RE-TARGETED THIS RATHER THAN DELETING IT. The guard under test is `record`'s own
    `try/except` in loop.py -- the one its comment calls "the one verb whose failure LOSES DATA"
    (#1201) -- and that guard SURVIVES the deletion of the private-side starters #2578 removed.
    Only the raiser moved, from the removed emit starter to `_ensure_watcher`, which is one of the two functions
    the wrapped block still calls. Deleting this test with the guard still in place would have
    left the fail-open branch with no control on it at all."""
    lp = _loop()
    real = lp._ensure_watcher
    def boom(*a, **k): raise RuntimeError("spawn exploded")
    lp._ensure_watcher = boom
    try:
        with tempfile.TemporaryDirectory() as d:
            base = _lease_base(d, actor="me", claims=[])
            goals = pathlib.Path(base) / "goals"; goals.mkdir()
            g = str(goals / "0042.md")
            pathlib.Path(g).write_text("---\nid: 0042\nstatus: pending\n---\nx\n")
            lp.main(["loop.py", "record", base, g, "done"])
            kinds = [e["kind"] for e in lp.ledger.read_all(base)]
            assert "done" in kinds, kinds
    finally:
        lp._ensure_watcher = real


# --- #2527: agent_start() unconditionally overwrote a live worker's OWN marker ------------------
# `_write()`'s "alive" branch compared only CODEX THREAD IDENTITY (`owner != codex_thread`), never
# the marker's own PID. For two plain Claude sessions `_session_codex_thread()` returns `None` on
# BOTH sides, so `None != None` is False and the guard silently fell through to an unconditional
# overwrite -- destroying the exact evidence `work.py`'s `_blocked_by_a_live_foreign_agent` depends
# on one step later, so a genuine collision produced NO refusal at all. The fix adds a PID
# comparison alongside the existing owner comparison, mirroring the identical boolean shape
# `_blocked_by_a_live_foreign_agent` already trusts (`work.py`: `pid not in mine or
# marker_owner != codex_thread`).


def test_agent_start_refuses_to_overwrite_a_different_live_claude_pid():
    """THE issue's own repro shape. Two plain Claude sessions (no Codex identity on either side)
    collide on one (goal, thread): the second `agent_start` call must be refused, and -- the actual
    defect, not just a return value -- the marker must still read back the FIRST pid afterward.
    Returning False while the overwrite already happened would fix nothing."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        proc = _spawn_sleeper()
        try:
            assert lp.agent_start(sdlc, "g.md", proc.pid, AGENT_WATCH_ON) is True
            assert lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON) is False
            assert lp.agent_alive(sdlc, "g.md", AGENT_WATCH_ON) == ("alive", proc.pid)
        finally:
            proc.kill(); proc.wait()


def test_agent_start_cli_exits_2_on_a_live_foreign_pid_collision(capsys):
    """The ACTUAL gesture SKILL.md step 3a invokes -- the CLI verb, not a stronger Python-only
    call. Existing stderr contract ("cannot register agent identity...") already covers this case
    without needing its own new message."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = _backlog(d, 1)
        goal = sdlc + "/goals/0001.md"
        proc = _spawn_sleeper()
        try:
            assert lp.main(["loop.py", "agent-start", sdlc, goal, "--pid", str(proc.pid)]) == 0
            rc = lp.main(["loop.py", "agent-start", sdlc, goal, "--pid", str(os.getpid())])
            assert rc == 2
            assert "cannot register agent identity" in capsys.readouterr().err
        finally:
            proc.kill(); proc.wait()


def test_agent_start_same_pid_reregistration_still_succeeds():
    """THE compatibility pin. The identical session calling `agent_start` twice in a row for the
    same pid and thread, marker alive THROUGHOUT (no `agent_end` between calls), must still
    succeed both times -- this is the case the fix must not break, and unlike every existing
    double-call test (which either brackets an `agent_end` or targets a different thread), nothing
    in the suite pinned it without one."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        assert lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON) is True
        assert lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON) is True   # no agent_end
        assert lp.agent_alive(sdlc, "g.md", AGENT_WATCH_ON) == ("alive", os.getpid())


def test_agent_start_a_dead_foreign_pid_can_still_be_overwritten():
    """The new comparison lives INSIDE the `marker_state == "alive"` branch only -- a marker naming
    a genuinely dead pid is unaffected and still silently reclaimable, exactly as before. Guards
    against an over-broad fix that starts refusing dead-marker overwrites too."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        dead_pid = 2**30                                # not a real pid on any sane system
        assert lp.agent_start(sdlc, "g.md", dead_pid, AGENT_WATCH_ON) is True
        assert lp.agent_start(sdlc, "g.md", os.getpid(), AGENT_WATCH_ON) is True
        assert lp.agent_alive(sdlc, "g.md", AGENT_WATCH_ON) == ("alive", os.getpid())


def test_codex_marker_still_refuses_on_thread_mismatch_regardless_of_pid(monkeypatch):
    """The EXISTING Codex protection, provably unchanged by the new PID half of the `or`: a
    DIFFERENT thread refuses even when the pid is identical, since the owner check alone already
    covers it."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        thread_a = "01999aaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
        thread_b = "01999bbb-bbbb-7bbb-8bbb-bbbbbbbbbbbb"
        monkeypatch.setenv("CODEX_THREAD_ID", thread_a)
        monkeypatch.setenv("CODEX_SESSION_ID", thread_a)
        assert lp.agent_start(sdlc, "42", os.getpid(), {}) is True
        monkeypatch.setenv("CODEX_THREAD_ID", thread_b)
        monkeypatch.setenv("CODEX_SESSION_ID", thread_b)
        assert lp.agent_start(sdlc, "42", os.getpid(), {}) is False    # SAME pid, different thread


def test_the_preserved_marker_still_trips_the_foreign_agent_guard(monkeypatch):
    """The actual integration claim (plan review round 1's Test 6 was vacuous -- see .sdlc/plans/
    2527.md -- `tests/test_work.py:4807`'s existing coverage only ever registers a FIRST, uncontested
    marker, never drives a real collision through `agent_start`). This one does: a genuine collision
    is refused, the SURVIVING (first) marker is fed into `work.py`'s independent downstream guard,
    and that guard refuses too -- proving the two layers compose rather than being accidentally
    redundant substitutes for each other."""
    lp = _loop()
    wk = _work()
    with tempfile.TemporaryDirectory() as d:
        sdlc = d + "/.sdlc"
        proc = _spawn_sleeper()
        try:
            assert lp.agent_start(sdlc, "42", proc.pid, {}) is True
            assert lp.agent_start(sdlc, "42", os.getpid(), {}) is False   # the fix: refused
            assert lp.agent_alive(sdlc, "42", {}) == ("alive", proc.pid)  # still the FIRST pid
            blocker = wk._blocked_by_a_live_foreign_agent(sdlc, {}, "42")
            assert blocker and "REFUSED" in blocker and "DIFFERENT live process" in blocker
        finally:
            proc.kill(); proc.wait()


# --- D7 first-run refusals (#1245, #1247): record must say what is wrong ----------------------------

def _record_goal(d, name="0001-x.md"):
    base = _backlog(d, 0)
    goal = pathlib.Path(base) / "goals" / name
    goal.write_text("---\nid: 0001\nstatus: pending\n---\nx\n")
    return base, str(goal)


def test_record_refuses_an_unknown_status_and_leaves_the_goal_alone(capsys):
    """#1245: `record <goal> bogus` used to exit 0 and park the goal. It must refuse (exit 2), name
    the accepted statuses, and not touch the goal file."""
    with tempfile.TemporaryDirectory() as d:
        base, goal = _record_goal(d)
        before = pathlib.Path(goal).read_text()
        assert _loop().main(["loop.py", "record", base, goal, "bogus-status"]) == 2
        err = capsys.readouterr().err
        assert "bogus-status" in err and "done|review|parked|failed" in err
        assert pathlib.Path(goal).read_text() == before


@pytest.mark.parametrize("status", ["done", "parked", "failed"])
def test_record_still_accepts_every_documented_status(status):
    with tempfile.TemporaryDirectory() as d:
        base, goal = _record_goal(d)
        assert _loop().main(["loop.py", "record", base, goal, status]) == 0


def test_record_done_of_a_goal_that_does_not_exist_says_so_not_the_phase_record(capsys):
    """#1247: the phase-record refusal (six phases to run) was printed for a goal that is not
    there. The real problem is named first, exit 2."""
    with tempfile.TemporaryDirectory() as d:
        base, _ = _record_goal(d)
        assert _loop().main(["loop.py", "record", base, "9999", "done"]) == 2
        err = capsys.readouterr().err
        assert "no such goal 9999" in err and "phase record" not in err


def test_pick_time_model_choice_reads_line_one_of_a_two_line_why(monkeypatch, capsys):
    """#1950: `predict.py why` prints the effort signal on a second line; the pick-time parser must
    still record the tier from line one, not report an unexpected shape."""
    lp = _loop()

    class Result:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def run(argv, **_kwargs):
        return Result("model=opus in=body hits=2 signal=migrat\neffort=high in=body hits=2 signal=auth\n"
                      if argv[2] == "why" else "model=gpt-6-astra effort=high\n")

    monkeypatch.setattr(lp.subprocess, "run", run)
    with tempfile.TemporaryDirectory() as d:
        base = _model_choice_base(d)
        src = _QueueWithTitleBody(["a"], {"title": "Add a retry", "body": "x"})
        lp._next(base, src, lp.state.load_config(base))
        events = _model_choice_events(base)
    assert [(e["model"], e.get("signal")) for e in events] == [("opus", "migrat")], capsys.readouterr().err
