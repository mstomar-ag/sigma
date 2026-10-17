import json, pathlib, importlib.util, tempfile, subprocess, sys

import pytest

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _state():
    spec = importlib.util.spec_from_file_location("state", S / "state.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _sdlc(d):
    base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True); (base / "state").mkdir()
    (base / "config.json").write_text(json.dumps({"budget": {"max_iterations": 3}}))
    (base / "state" / "STATE.md").write_text(
        "# Loop State\n\n<!-- Do not hand-edit during a run. -->\n\n"
        "iteration: 0\nrun_iteration: 0\nlast_run: none\n\n## Items\n<!-- x -->\n")
    (base / "state" / "review-queue.md").write_text("# Morning Review Queue\n")
    g = base / "goals" / "0001-x.md"; g.write_text("---\nid: 0001\nstatus: pending\n---\nbody\n")
    return str(base), str(g)


def test_complete_sets_done():
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d); st.complete(base, g)
        assert "status: done" in pathlib.Path(g).read_text()


def test_park_sets_parked_and_appends_queue():
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d); st.park(base, g, "hit a deploy gate")
        assert "status: parked" in pathlib.Path(g).read_text()
        q = (pathlib.Path(base) / "state" / "review-queue.md").read_text()
        assert "0001-x.md" in q and "hit a deploy gate" in q and q.startswith("# Morning Review Queue")


def test_park_with_no_tier_writes_the_exact_same_entry_as_before_953():
    """#953 regression pin: `park()`'s default call shape (no `tier` arg — every caller that isn't
    a `needs_decision` park with decision_tier auto-on) must render BYTE-FOR-BYTE the same
    `review-queue.md` entry it always has. No `- tier:` line, no trailing whitespace change."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d); st.park(base, g, "hit a deploy gate")
        q = (pathlib.Path(base) / "state" / "review-queue.md").read_text()
        assert q == "# Morning Review Queue\n\n## 0001-x.md\n- reason: hit a deploy gate\n- needs: human review\n"
        assert "tier" not in q


def test_park_with_a_tier_adds_a_tier_line_to_the_queue_entry():
    """#953: when a decision tier was computed (a `needs_decision` park with decision_tier auto-on),
    it must show up in the review-queue.md entry so a human skimming sees severity without opening
    anything else."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d); st.park(base, g, "PR #1 changes requested", tier="escalate_l0")
        q = (pathlib.Path(base) / "state" / "review-queue.md").read_text()
        assert q == ("# Morning Review Queue\n\n## 0001-x.md\n- reason: PR #1 changes requested\n"
                      "- needs: human review\n- tier: escalate_l0\n")


def test_release_sets_status_back_to_pending():
    """#841: `release` undoes a `set_in_progress` claim that was never started — unlike
    complete/park/fail it is not a terminal SDLC outcome, so the goal goes back to `pending`
    (not a new "released" status), making it indistinguishable from a goal that was never picked
    at all."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        st.set_in_progress(base, g)
        assert "status: in_progress" in pathlib.Path(g).read_text()
        st.release(base, g)
        assert "status: pending" in pathlib.Path(g).read_text()


def test_release_never_touches_the_review_queue():
    """Unlike park/fail, release needs no human decision — it must never append to
    review-queue.md, the morning-read file reserved for goals a human has to look at."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        st.release(base, g)
        q = (pathlib.Path(base) / "state" / "review-queue.md").read_text()
        assert q == "# Morning Review Queue\n"


def test_release_returns_true_when_it_actually_releases_a_claim():
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        st.set_in_progress(base, g)
        assert st.release(base, g) is True


def test_release_refuses_an_already_terminal_goal_leaving_it_untouched():
    """PR #1107 review, Finding 3: `release` used to call `_set_status(goal_path, "pending")`
    unconditionally — reproduced live, a `status: done` goal became `status: pending` after a bare
    `release()` call, no confirmation, no warning, no guard. Release is only meaningful for a goal
    that is still claimed/in-progress; a goal that already reached done/parked/failed reflects a
    completed DECISION, and silently un-completing it is a correctness bug. Fixed: a terminal goal
    is a clean no-op — status untouched, `release()` returns False instead of True."""
    st = _state()
    for status in ("done", "parked", "failed"):
        with tempfile.TemporaryDirectory() as d:
            base, g = _sdlc(d)
            pathlib.Path(g).write_text(f"---\nid: 0001\nstatus: {status}\n---\nbody\n")
            assert st.release(base, g) is False, status
            assert f"status: {status}" in pathlib.Path(g).read_text(), status


def test_release_never_moves_a_proposed_goal_to_pending():
    """#1550: `proposed` is the human-promotion gate; release is a claim-undo, and a proposed goal
    was never claimed, so release must not be the path that promotes it to `pending`."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        pathlib.Path(g).write_text("---\nid: 0001\nstatus: proposed\n---\nbody\n")
        assert st.release(base, g) is False
        assert "status: proposed" in pathlib.Path(g).read_text()


def test_queue_with_no_tier_omits_the_tier_line():
    """Direct test of `_queue()` itself (park()'s and fail()'s shared write path), not just through
    park() — `fail()` never passes a tier at all, so this pins that omitted-arg call shape too."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        st._queue(base, g, "some reason", "a fix (the loop could not resolve this)")
        q = (pathlib.Path(base) / "state" / "review-queue.md").read_text()
        assert "- tier:" not in q


def test_advance_cursor_bumps_by_one_sets_last_run_and_preserves_structure():
    """`save_cursor`'s structure-preserving contract, carried onto its replacement: `save_cursor`
    is deleted (#531) since the `loop.py` `_record` call site -- its only production caller -- now
    goes through `advance_cursor` instead. One call bumps BOTH counters by exactly 1 (never sets
    them to a caller-given value, unlike deleted `save_cursor`) and stamps `last_run`; unrelated
    template structure -- the hand-edit guard comment, the `## Items` section -- must survive the
    read-modify-write untouched."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, _ = _sdlc(d)
        st.advance_cursor(base, "1 done")
        st.advance_cursor(base, "2 done")
        cur = st.load_cursor(base)
        assert cur["iteration"] == 2 and cur["run_iteration"] == 2   # +1 per call, not set-to
        txt = (pathlib.Path(base) / "state" / "STATE.md").read_text()
        assert "last_run: 2 done" in txt
        assert "Do not hand-edit" in txt and "## Items" in txt        # guard + structure survive


def test_start_run_resets_run_iteration_only():
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, _ = _sdlc(d)
        for _ in range(5):
            st.advance_cursor(base, "x")             # 5 calls -> iteration == run_iteration == 5
        st.start_run(base)
        cur = st.load_cursor(base)
        assert cur["run_iteration"] == 0 and cur["iteration"] == 5   # cursor preserved


def test_phase_from_before_a_new_run_cannot_charge_its_budget(tmp_path):
    st = _state()
    base, _ = _sdlc(tmp_path)
    st.start_run(base)
    started = st.load_cursor(base)["run_started_at"]
    first, credit, old_run = st.record_phase_end(base, "old-goal/research", started - 1, 1000)
    assert (first, credit, old_run) == (False, False, True)
    assert st.load_cursor(base)["run_tokens"] == 0
    first, credit, old_run = st.record_phase_end(base, "new-goal/research", started + 1, 1000)
    assert (first, credit, old_run) == (True, True, False)
    assert st.load_cursor(base)["run_tokens"] == 1000


def test_set_line_value_with_backslash_is_literal():
    # the summary flows from a goal filename; backslash-escapes must stay literal,
    # not be treated as re.sub replacement backreferences (re.error / corruption).
    st = _state()
    out = st._set_line("last_run: none\n", "last_run", r"done g\1")
    assert "last_run: done g\\1" in out

def test_state_and_queue_scaffold_themselves_on_a_fresh_clone(tmp_path):
    """Regression: `.sdlc/state/` is gitignored by design, so a teammate cloning an ADOPTED repo has
    config + goals but no state files — and every entry point died on FileNotFoundError before their
    first goal. Found by a two-clone e2e; unit tests all pre-created the files."""
    s = _state()
    d = tmp_path / ".sdlc"
    (d / "goals").mkdir(parents=True)                    # note: no state/ directory at all
    goal = d / "goals" / "0001.md"
    goal.write_text("---\nstatus: pending\n---\nx\n")

    assert s.load_cursor(str(d)) == {"iteration": 0, "run_iteration": 0,
                                     "run_started_at": 0, "run_tokens": 0,
                                     "run_codex_raw_tokens": 0}
    s.start_run(str(d))
    s.advance_cursor(str(d), "last: 0001.md -> done")
    assert s.load_cursor(str(d))["iteration"] == 1
    assert "# Loop State" in (d / "state" / "STATE.md").read_text()

    s.park(str(d), str(goal), "needs a decision")        # queue absent too
    queue = (d / "state" / "review-queue.md").read_text()
    assert "# Morning Review Queue" in queue and "- needs: human review" in queue


def test_load_config_raises_a_clear_error_when_config_json_is_missing(tmp_path):
    """Sibling regression to the scaffold-on-demand test above, but the opposite fix shape (#403):
    `.sdlc/state/` is gitignored runtime state, safe to invent on demand (see `_state_file`'s own
    docstring); `.sdlc/config.json` is NOT — it carries the actual project choices (discovery
    source, ledger, verify command...), so silently defaulting it would run the loop in a mode
    nobody chose. A directory that was never `/sigma-init`'d must say so clearly instead — not
    crash with a raw FileNotFoundError traceback, and not silently invent a config either."""
    s = _state()
    d = tmp_path / ".sdlc"
    d.mkdir()                                             # no config.json at all
    with pytest.raises(s.ConfigMissing) as exc:
        s.load_config(str(d))
    assert "config.json" in str(exc.value) and "/sigma-init" in str(exc.value)


def test_load_config_still_raises_normally_on_malformed_json(tmp_path):
    """Contrast case: a config.json that EXISTS but fails to parse is a real corruption bug, not a
    setup problem — ConfigMissing must stay scoped to "the file is absent" and not also swallow a
    parse failure into the same (wrong, in this case) "run /sigma-init" advice."""
    s = _state()
    d = tmp_path / ".sdlc"
    d.mkdir()
    (d / "config.json").write_text("not valid json{{{")
    with pytest.raises(json.JSONDecodeError):
        s.load_config(str(d))


def test_load_config_raises_clear_error_when_config_json_contains_literal_null(tmp_path):
    """Regression (#453): config.json containing the valid JSON value `null` parses successfully
    but produces a NoneType object. Calling .get() on it crashes with a raw AttributeError. This
    must be caught and reported as ConfigMissing (same as an absent file) — a config.json that
    parses to non-dict is as unusable as one that doesn't exist, and needs the same /sigma-init
    advice."""
    s = _state()
    d = tmp_path / ".sdlc"
    d.mkdir()
    (d / "config.json").write_text("null")
    with pytest.raises(s.ConfigMissing) as exc:
        s.load_config(str(d))
    assert "config.json" in str(exc.value) and "/sigma-init" in str(exc.value)


def test_load_config_raises_clear_error_when_config_json_is_valid_json_but_not_dict(tmp_path):
    """Extended regression (#453): not just `null`, but ANY valid JSON that isn't a dict (a list,
    string, number) should trigger ConfigMissing with the same /sigma-init advice."""
    s = _state()
    d = tmp_path / ".sdlc"
    d.mkdir()

    # Test with a list
    (d / "config.json").write_text("[1, 2, 3]")
    with pytest.raises(s.ConfigMissing):
        s.load_config(str(d))

    # Test with a string
    (d / "config.json").write_text('"just a string"')
    with pytest.raises(s.ConfigMissing):
        s.load_config(str(d))

    # Test with a number
    (d / "config.json").write_text("42")
    with pytest.raises(s.ConfigMissing):
        s.load_config(str(d))


def test_load_config_passes_unknown_top_level_keys_through_unchanged(tmp_path):
    """Pins issue #2572's own load-bearing assumption (research: "state.py:36-50's load_config
    does json.loads with no schema") as a real, running test rather than an inspected-only fact:
    the core reads config.json with no schema at all, so another tool's block -- or any other
    top-level key nothing in skills/ has ever heard of -- rides through untouched, never
    stripped, never validated, never erroring. This is exactly what makes it safe for another
    tool to read the SAME file for its own block without the two ever coordinating."""
    s = _state()
    d = tmp_path / ".sdlc"
    d.mkdir()
    payload = {
        "work": {"enabled": True},
        "other_tool": {"emit": {"watch": True}},
        "totally_made_up_key": 1,
    }
    (d / "config.json").write_text(json.dumps(payload))
    loaded = s.load_config(str(d))
    assert loaded["other_tool"] == {"emit": {"watch": True}}
    assert loaded["totally_made_up_key"] == 1
    assert loaded["work"] == {"enabled": True}


# --- #486/PR #487 independent review: unsafe_goal_reason, the shared path-traversal validator --
# actionlog.py's log_path() had a real, reproduced path-traversal bug (a goal with no `.md` suffix
# skipped work.stem()'s own directory-stripping reduction and was embedded raw). Independent review
# found the SAME unguarded pattern repeated at five more chokepoints across loop.py/work.py/
# slices.py/sigma-log's own independent copy -- one of them (loop.py's agent_end()) an unconditional,
# ungated shutil.rmtree() reachable from the everyday `record` verb. unsafe_goal_reason lives here,
# not duplicated per-caller, so a single implementation protects all of them.


def test_unsafe_goal_reason_rejects_path_traversal_shapes():
    s = _state()
    assert s.unsafe_goal_reason("../../../ESCAPED") is not None
    assert s.unsafe_goal_reason("goals/nested") is not None          # bare '/', no '..' needed
    assert s.unsafe_goal_reason("a\\b") is not None                  # backslash
    assert s.unsafe_goal_reason("C:\\Windows\\evil") is not None     # Windows drive-letter shape
    assert s.unsafe_goal_reason("a:b") is not None                   # bare colon


def test_unsafe_goal_reason_accepts_legitimate_goal_shapes():
    s = _state()
    for legit in ("0001-x", "158", "0007-cache", "goal with spaces", "emoji-🚀-ok"):
        assert s.unsafe_goal_reason(legit) is None


def test_evidence_path_rejects_a_path_traversal_goal(tmp_path):
    """Reproduces the review's own finding: loop.py verify <dir> "<traversal-goal>" wrote a file
    outside .sdlc, reporting VERIFIED (exit 0), before this fix."""
    s = _state()
    d = tmp_path / ".sdlc"
    d.mkdir()
    with pytest.raises(ValueError, match="unsafe goal"):
        s.evidence_path(str(d), "../../../ESCAPED-outside-sdlc")
    # legitimate goals still resolve, unaffected
    assert s.evidence_path(str(d), "0001-x.md").name == "0001-x.json"
    assert s.evidence_path(str(d), "158").name == "158.json"


# --- F11/#341: the whole-second staleness hole in done_refusal ---------------------------------
# `run_started_at` and evidence `at` used to both be `int(time.time())` -- a verify from a PRIOR
# run at T-0.4s and a run starting at T+0.3s both floor to the same integer second, so the stale
# evidence's `at` tied with `run_started_at` and slipped past the `<` check as if it were fresh.
# The fix keeps sub-second float precision on both sides instead of narrowing the comparison itself
# (a naive `<=` was tried and rejected -- it deterministically refuses the normal verify-then-record
# sequence, since two fast subprocess calls routinely land in the same wall-clock second).


def _write_evidence(sdlc_dir, goal, at, exit_code=0):
    st = _state()
    ev = st.evidence_path(sdlc_dir, goal)
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(json.dumps({"command": "true", "exit": exit_code, "at": at, "tail": []}))


def _write_run_started_at(sdlc_dir, value):
    (pathlib.Path(sdlc_dir) / "state" / "STATE.md").write_text(
        f"iteration: 0\nrun_iteration: 0\nrun_started_at: {value}\nrun_tokens: 0\nlast_run: none\n")


def test_read_float_preserves_the_fractional_part():
    """`_read_int`'s `\\d+` regex would silently truncate `run_started_at`'s fractional part on
    read; `_read_float` is the dedicated reader that keeps it."""
    st = _state()
    assert st._read_float("run_started_at: 1700000000.375\n", "run_started_at") == 1700000000.375


def test_read_float_defaults_to_zero_when_absent():
    st = _state()
    assert st._read_float("iteration: 0\n", "run_started_at") == 0.0


def test_start_run_writes_the_raw_time_time_value_not_a_floored_int(monkeypatch):
    """Root cause of F11: `int(time.time())` floored `run_started_at` to a whole second. Confirm
    `start_run` now stamps the raw float `time.time()` returns."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, _ = _sdlc(d)
        monkeypatch.setattr(st.time, "time", lambda: 1700000000.75)
        st.start_run(base)
        assert st.load_cursor(base)["run_started_at"] == 1700000000.75


def test_done_refusal_rejects_sub_second_stale_evidence_that_would_have_tied_under_whole_second_flooring():
    """The issue's repro, reproduced with concrete numbers: a verify lands 0.1s into second T, a
    PRIOR-run evidence write; this run starts 0.3s into that SAME second T. `int()` floors both to
    T (proven inline below) -- precisely how the pre-fix `<` comparison let unambiguously-stale
    evidence (the verify genuinely precedes this run's start by 0.2s) pass as fresh. With
    sub-second precision the real ordering survives and the stale evidence is refused."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        t = 1_700_000_000.0
        _write_run_started_at(base, t + 0.3)
        _write_evidence(base, g, at=t + 0.1)
        assert int(t + 0.1) == int(t + 0.3) == int(t)      # the whole-second collision, confirmed
        assert st.done_refusal(base, g) == "verify evidence predates this run"


def test_done_refusal_accepts_evidence_a_fraction_of_a_second_after_run_start():
    """The other half of F11: a verify completing milliseconds after this run started -- the normal
    verify-then-record sequence -- must still be accepted. Also lands in the same floored second as
    `run_started_at`, so this pins that the fix did not overcorrect into refusing same-second-but-
    genuinely-later evidence (a stricter `<=` comparison would wrongly refuse this case)."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        t = 1_700_000_000.0
        _write_run_started_at(base, t)
        _write_evidence(base, g, at=t + 0.05)
        assert int(t + 0.05) == int(t)                     # same floored second as run_started_at
        assert st.done_refusal(base, g) is None


def test_done_refusal_boundary_at_exact_equality_is_still_accepted():
    """`at == run_started_at` exactly (e.g. a verify and a run start stamped in the same instant)
    is not proof of staleness -- done_refusal's contract is "fresh = at/after this run's start",
    and only a real (now sub-second-precise) `<` predates it."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        t = 1_700_000_000.123456
        _write_run_started_at(base, t)
        _write_evidence(base, g, at=t)
        assert st.done_refusal(base, g) is None


def test_a_dead_holders_run_neither_anchors_its_goal_nor_is_charged_before_any_prune():
    """#955, the liveness filter at its own seam (`live_session_records`). A holder whose pid is
    dead, its marker not yet pruned by a `start`/`next`, must not anchor its goal's verify
    freshness (the checkout's later start does) and must not be charged unattributed spend.
    tests/test_run_cursor_955.py's AC-4 pin cannot see a dropped filter: its bare `start-run` then
    resets the dead session's block, which refuses the same evidence for another reason. Control:
    making `live_session_records` skip `session_pid_live` turns both assertions red."""
    import os, time
    st = _state()
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        sessions = pathlib.Path(base) / "state" / "sessions"; sessions.mkdir()
        t0 = time.time() - 30
        for pid, held in ((dead.pid, ["0001-x"]), (os.getpid(), [])):
            (sessions / f"{pid}.active").write_text(json.dumps(
                {"in_flight": held, "settled_admissions": 0, "run": st.fresh_session_run(t0)}))
        st.start_run(base, now=t0 + 20)
        assert st.goal_run_started_at(base, g) == t0 + 20
        st.add_tokens(base, 50)
        runs = {path.name: json.loads(path.read_text())["run"]["tokens"]
                for path in sessions.glob("*.active")}
        assert runs == {f"{dead.pid}.active": 0, f"{os.getpid()}.active": 50}


# --- #498: run-id attribution so a CONCURRENT sibling's green can't satisfy THIS run's record done -
# F11/#341 only rejects a STALE green (older than this run's start). A concurrent sibling worker on
# the SAME goal writes a green (exit 0) NEWER than this run's start, so it passed the freshness gate
# by design. run_identity() stamps each evidence write with the writing run's id, and done_refusal
# now refuses a green whose run-id differs from THIS run's -- so a sibling's green is ignored, not
# inherited. Legacy evidence (no run key, written by an old loop.py) stays accepted per freshness.


def _write_evidence_run(sdlc_dir, goal, at, run, exit_code=0):
    st = _state()
    ev = st.evidence_path(sdlc_dir, goal)
    ev.parent.mkdir(parents=True, exist_ok=True)
    payload = {"command": "true", "exit": exit_code, "at": at, "tail": []}
    if run is not None:                       # run=None models LEGACY evidence: the key is ABSENT
        payload["run"] = run
    ev.write_text(json.dumps(payload))


def test_run_identity_reads_the_env_var(monkeypatch):
    st = _state()
    monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
    assert st.run_identity() == "worker-A"


def test_run_identity_is_none_when_unset(monkeypatch):
    """Unattributed run -> None -> done_refusal degrades to today's freshness-only behaviour. An
    empty-string env value is treated as unset too (a launcher exporting `SIGMA_RUN_ID=` must
    not read as a real, matchable identity)."""
    st = _state()
    monkeypatch.delenv("SIGMA_RUN_ID", raising=False)
    assert st.run_identity() is None
    monkeypatch.setenv("SIGMA_RUN_ID", "")
    assert st.run_identity() is None


def test_done_refusal_rejects_a_concurrent_sibling_run_green(monkeypatch):
    """The #498 core, with concrete numbers: run A stamps a fresh, exit-0 green (at = run_start + 5s,
    so it PASSES freshness); run B must NOT be able to record done on it."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        t = 1_700_000_000.0
        _write_run_started_at(base, t)
        _write_evidence_run(base, g, at=t + 5, run="worker-A")
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-B")
        r = st.done_refusal(base, g)
        assert r is not None and "different run" in r


def test_done_refusal_accepts_a_matching_run_green(monkeypatch):
    """The legitimate verify-then-record sequence: the same run that produced the green records it."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        t = 1_700_000_000.0
        _write_run_started_at(base, t)
        _write_evidence_run(base, g, at=t + 5, run="worker-A")
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        assert st.done_refusal(base, g) is None


def test_done_refusal_accepts_legacy_evidence_without_a_run_id(monkeypatch):
    """Backward-compat: evidence written by an OLD loop.py has NO `run` key -> fall through to the
    existing freshness gate, never a hard crash and never a spurious refusal -- even when THIS run
    does carry an id."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        t = 1_700_000_000.0
        _write_run_started_at(base, t)
        _write_evidence_run(base, g, at=t + 5, run=None)          # legacy: no run key at all
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-B")
        assert st.done_refusal(base, g) is None


def test_done_refusal_rejects_an_attributed_green_when_this_run_is_unattributed(monkeypatch):
    """Hardened rule (plan-review #2): if the evidence carries a run-id but THIS run has none, we
    cannot prove the green is ours -> refuse, so an un-id'd worker can't inherit a sibling's green."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        t = 1_700_000_000.0
        _write_run_started_at(base, t)
        _write_evidence_run(base, g, at=t + 5, run="worker-A")
        monkeypatch.delenv("SIGMA_RUN_ID", raising=False)
        r = st.done_refusal(base, g)
        assert r is not None and "different run" in r


def test_done_refusal_run_id_check_is_after_exit_and_freshness(monkeypatch):
    """The run-id check is ADDED AFTER the exit-code and freshness guards (F11/#341), never replaces
    them: a matching run-id on a FAILED green is still refused, and a matching run-id on a STALE
    green is still refused as predating the run. Pins that the fix did not weaken either guard."""
    st = _state()
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        t = 1_700_000_000.0
        _write_run_started_at(base, t)
        monkeypatch.setenv("SIGMA_RUN_ID", "worker-A")
        _write_evidence_run(base, g, at=t + 5, run="worker-A", exit_code=1)
        assert st.done_refusal(base, g) == "last verify FAILED (exit 1)"
        _write_evidence_run(base, g, at=t - 5, run="worker-A", exit_code=0)   # matching id, but STALE
        assert st.done_refusal(base, g) == "verify evidence predates this run"


# --- #905: run_stop dedupe marker, keyed (run_id, run_started_at) -------------------------------
# Plan-review's own Defect 1: a marker keyed on the bare run id alone is WRONG, because
# SIGMA_RUN_ID is stable across one supervise_daemon.py worker's ENTIRE lifetime, including every
# budget-stop-then-relaunch cycle (supervise_daemon.py's own documented behaviour) -- a bare-id key would
# record only the FIRST terminal event under that id and silently drop every later, genuinely
# different one (e.g. the eventual real backlog-empty after two budget stops). Keying on the PAIR
# instead -- run_started_at is written fresh by `start_run` at the top of every `/sigma-loop`
# invocation, including every relaunch -- makes a relaunch's own terminal event claim independently
# while still deduping repeated polling WITHIN one unchanged drain.


def test_claim_run_stop_true_first_time_false_on_an_exact_repeat(tmp_path):
    st = _state()
    d = tmp_path / ".sdlc"; d.mkdir()
    assert st.claim_run_stop(str(d), "worker-A", 100.0) is True
    assert st.claim_run_stop(str(d), "worker-A", 100.0) is False


def test_claim_run_stop_true_again_after_a_relaunch_changes_run_started_at(tmp_path):
    st = _state()
    d = tmp_path / ".sdlc"; d.mkdir()
    assert st.claim_run_stop(str(d), "worker-A", 100.0) is True
    assert st.claim_run_stop(str(d), "worker-A", 200.0) is True    # relaunch: fresh run_started_at


def test_claim_run_stop_different_run_ids_claim_independently(tmp_path):
    st = _state()
    d = tmp_path / ".sdlc"; d.mkdir()
    assert st.claim_run_stop(str(d), "worker-A", 100.0) is True
    assert st.claim_run_stop(str(d), "worker-B", 100.0) is True


def test_claim_run_stop_rejects_unsafe_run_id(tmp_path):
    st = _state()
    d = tmp_path / ".sdlc"; d.mkdir()
    with pytest.raises(ValueError, match="unsafe"):
        st.claim_run_stop(str(d), "../../../ESCAPED", 100.0)
    with pytest.raises(ValueError, match="unsafe"):
        st.claim_run_stop(str(d), "a/b", 100.0)


def test_claim_run_stop_fails_open_toward_not_yet_claimed(tmp_path, monkeypatch):
    """Mirrors ledger.safe_append's and _release_claim_lock's own fail-open contracts (both cited
    by plan-review as the established pattern this deep in the call graph): a write failure must
    never raise into `_next()`'s caller, and must fail TOWARD "not yet claimed" -- a lost dedupe
    row is far cheaper than crashing the loop's single most frequently-called verb."""
    st = _state()
    d = tmp_path / ".sdlc"; d.mkdir()

    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(pathlib.Path, "write_text", _boom)
    assert st.claim_run_stop(str(d), "worker-A", 100.0) is False


def test_run_stop_marker_path_rejects_unsafe_run_id(tmp_path):
    st = _state()
    d = tmp_path / ".sdlc"
    with pytest.raises(ValueError, match="unsafe"):
        st.run_stop_marker_path(str(d), "../../../ESCAPED")


def test_run_stop_marker_path_resolves_under_state_run_stop(tmp_path):
    st = _state()
    d = tmp_path / ".sdlc"
    p = st.run_stop_marker_path(str(d), "worker-A")
    assert p == d / "state" / "run_stop" / "worker-A.json"


# --- #531: concurrent STATE.md cursor writers (kernel flock + atomic replace) -------------------
# `save_cursor`/`add_tokens` were unlocked read-modify-write over a file SHARED across the whole
# run; under `parallel.goals` concurrent recorders/spenders lost increments (the issue's own probe:
# 90/120 lost at high contention) and could publish a torn file (a duplicate `run_tokens:` line).
# Fixed with the same kernel-flock primitive that closed the identical race in
# `_try_acquire_claim_lock` (loop.py, #387), plus `os.replace` atomic publish so a lock-free reader
# (`load_cursor` stays deliberately lock-free -- see its own docstring) never observes a half-written
# file.


def test_cursor_lock_blocks_a_second_thread_for_the_full_held_window():
    """Deterministic mutual exclusion, not a timing race: the main thread holds `_cursor_lock`
    across a fixed window; a worker thread's `add_tokens` call must still be blocked on the flock
    when checked partway through, and only completes once the lock is released. `flock` releases the
    GIL while blocked (thread precedent: test_loop.py's claim-lock thread tests), so this is a real
    kernel-mediated wait, not a Python-level illusion -- a LOADED box only makes the worker MORE
    blocked, never less, so there is no flake direction to worry about.
    Skipped when `state.fcntl is None`: on that fail-open platform the lock is legitimately never
    held at all, so "still blocked" would never be true -- that path has its own dedicated test
    below instead of being (mis-)asserted here."""
    st = _state()
    if st.fcntl is None:
        pytest.skip("no fcntl on this platform -- fail-open path, covered separately")
    import threading, time
    with tempfile.TemporaryDirectory() as d:
        base, _ = _sdlc(d)
        worker_done = threading.Event()

        def worker():
            st.add_tokens(base, 1)
            worker_done.set()

        with st._cursor_lock(base):
            t = threading.Thread(target=worker)
            t.start()
            time.sleep(0.3)
            assert not worker_done.is_set()   # still blocked on the flock -- lock genuinely held
        t.join(timeout=5)
        assert worker_done.is_set()
        assert st.load_cursor(base)["run_tokens"] == 1


def test_add_tokens_still_applies_its_increment_when_fcntl_is_unavailable(monkeypatch):
    """Fail-open posture mirrors `_try_acquire_claim_lock` (#387): a platform with no `fcntl` must
    not lose the write it cannot protect -- it just proceeds unlocked. Atomic publish (temp file +
    `os.replace`) still applies on this path too, so a reader still never sees a torn file; only the
    mutual-exclusion guarantee itself is given up, exactly like #387's documented posture."""
    st = _state()
    monkeypatch.setattr(st, "fcntl", None)
    with tempfile.TemporaryDirectory() as d:
        base, _ = _sdlc(d)
        st.add_tokens(base, 7)
        assert st.load_cursor(base)["run_tokens"] == 7


_SPEND_HAMMER_SCRIPT = """
import subprocess, sys
loop_py, sdlc_dir, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
for _ in range(n):
    r = subprocess.run([sys.executable, loop_py, "spend", sdlc_dir, "1"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("spend subprocess failed: " + r.stderr)
"""


def test_spend_cli_totals_exactly_under_six_real_processes_each_driving_twenty_cli_calls(tmp_path):
    """The issue's own probe, pinned as a regression: 6 REAL OS processes, each driving 20 REAL
    `loop.py spend <dir> 1` CLI invocations (120 total spawns -- the probe's own scale, matching its
    90/120-lost-at-high-contention pre-fix finding) -- must total EXACTLY 120, publish exactly ONE
    `run_tokens:` line (no torn/duplicate-line publish), and leave STATE.md still parseable
    (`iteration:` present). The blocking flock serializes every writer, so post-fix this is
    deterministic -- no timing assumptions needed, unlike the pre-fix probe.
    Subprocess pattern (a writer script written to `tmp_path`, `Popen` not sequential `run` so the
    processes genuinely overlap) follows the established precedent at test_actionlog.py's own
    concurrent-real-processes test (repo bar since #387: real processes, not mocked/threaded).
    Nothing is pre-created: `sdlc_dir` starts without even a `state/` directory, so this also races
    `_state_file`'s exclusive-create scaffold (#531 amendment) under the same contention -- a plain
    `if not exists: write_text(...)` scaffold would let a late scaffolder's write clobber an
    already-published `run_tokens` value here."""
    base = str(tmp_path / ".sdlc")            # deliberately nothing pre-created -- see docstring
    script = tmp_path / "spend_hammer.py"
    script.write_text(_SPEND_HAMMER_SCRIPT)
    procs = [subprocess.Popen([sys.executable, str(script), str(S / "loop.py"), base, "20"])
             for _ in range(6)]
    for p in procs:
        assert p.wait(timeout=60) == 0
    st = _state()
    text = (pathlib.Path(base) / "state" / "STATE.md").read_text()
    assert st.load_cursor(base)["run_tokens"] == 120
    assert text.count("run_tokens:") == 1
    assert "iteration:" in text



# --- #550: the review-queue scaffold, and the residual scaffold race #551's review found ---------

_PARK_HAMMER_SCRIPT = """
import importlib.util, pathlib, sys
state_py, sdlc_dir, goal = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("state", state_py)
st = importlib.util.module_from_spec(spec); spec.loader.exec_module(st)
st.park(sdlc_dir, goal, "raced first park")
"""


def test_queue_scaffold_never_truncates_an_already_written_queue(monkeypatch):
    """The #550 bug, pinned deterministically instead of by timing. `_queue`'s old
    `if not q.exists(): q.write_text(TEMPLATE)` is check-then-act: two first-parks on a fresh clone
    can BOTH see "not exists", and the second one's write_text TRUNCATES whatever the first already
    appended. Reproduced by forcing exactly that lost race -- the existence check answers False
    while the file really does exist with a prior entry in it -- so the assertion is about real file
    content, not about the mock. Exclusive-create turns the loser into a clean FileExistsError."""
    st = _state()
    real_exists = pathlib.Path.exists
    with tempfile.TemporaryDirectory() as d:
        base, g = _sdlc(d)
        q = pathlib.Path(base) / "state" / "review-queue.md"
        q.write_text("# Morning Review Queue\n\n## 0000-earlier.md\n- reason: got here first\n")
        monkeypatch.setattr(pathlib.Path, "exists",
                            lambda self, *a, **k: False if self == q else real_exists(self, *a, **k))
        st.park(base, g, "the racing park")
        text = q.read_text()
        assert "0000-earlier.md" in text, "the earlier entry was truncated away by the scaffold"
        assert "got here first" in text
        assert "the racing park" in text            # ...and this park still landed


def test_queue_scaffold_cannot_overwrite_an_append_landing_in_the_create_gap(tmp_path, monkeypatch):
    """The `O_APPEND` on the scaffold fd, pinned deterministically rather than by contention.

    Exclusive-create alone still leaves a residual of the bug #550 is about: the winner creates an
    EMPTY file and is not yet positioned at EOF, so a loser that appends its entry in the gap before
    the template write has that entry overwritten from offset 0. The 8-process test below reaches
    that interleaving only sometimes; this forces it every run by driving a real append THROUGH the
    gap -- wrapping `os.open` the same way the `_cursor_lock` fail-open tests further down do.

    The template is deliberately longer than the planted entry, so a non-appending write at offset 0
    swallows it whole: with `O_APPEND` removed this assertion fails every time."""
    st = _state()
    base = str(tmp_path / ".sdlc")
    goals = pathlib.Path(base) / "goals"
    goals.mkdir(parents=True)
    g = goals / "0001-scaffolder.md"
    g.write_text("---\nstatus: pending\n---\nbody\n")
    q = pathlib.Path(base) / "state" / "review-queue.md"
    real_open = st.os.open

    def open_then_let_a_loser_append(path, *a, **k):
        fd = real_open(path, *a, **k)
        if str(path) == str(q):                     # the exclusive create just won -- file is empty
            with q.open("a") as loser:              # ...and a second parker appends before we write
                loser.write("\n## 0000-loser.md\n- reason: got in first\n")
        return fd

    monkeypatch.setattr(st.os, "open", open_then_let_a_loser_append)
    st.park(base, str(g), "the scaffolder")
    text = q.read_text()
    assert "0000-loser.md" in text, "the template write overwrote an entry appended in the create gap"
    assert "# Morning Review Queue" in text         # the header still got written...
    assert "0001-scaffolder.md" in text             # ...and the scaffolder's own park still landed


def test_queue_survives_concurrent_first_parks_from_real_processes(tmp_path):
    """The same bug at the scale it actually bites: 8 REAL processes racing their very first park on
    a fresh clone, with no `state/` directory pre-created at all, so they genuinely contend on
    scaffolding review-queue.md. Every entry must survive and the header must appear exactly once.
    Real processes rather than threads, following this file's own concurrency precedent above."""
    base = str(tmp_path / ".sdlc")
    goals = pathlib.Path(base) / "goals"
    goals.mkdir(parents=True)
    script = tmp_path / "park_hammer.py"
    script.write_text(_PARK_HAMMER_SCRIPT)
    paths = []
    for i in range(8):
        g = goals / f"{i:04d}-g.md"
        g.write_text("---\nstatus: pending\n---\nbody\n")
        paths.append(str(g))
    procs = [subprocess.Popen([sys.executable, str(script), str(S / "state.py"), base, p])
             for p in paths]
    for p in procs:
        assert p.wait(timeout=60) == 0
    text = (pathlib.Path(base) / "state" / "review-queue.md").read_text()
    for i in range(8):
        assert f"{i:04d}-g.md" in text, f"park {i} was lost to a truncating scaffold"
    assert text.count("# Morning Review Queue") == 1        # scaffolded exactly once, never re-written


def test_lock_free_read_path_never_creates_state_md(tmp_path):
    """#551 review, rider 1. `load_cursor` is deliberately lock-free, so anything it CREATES races
    the locked writers. It now reads the template's own defaults when STATE.md is absent instead of
    scaffolding it, which removes the unlocked creator entirely rather than narrowing its window --
    only `_patch_cursor`, under the lock, ever creates the file."""
    st = _state()
    base = str(tmp_path / ".sdlc")
    (pathlib.Path(base) / "state").mkdir(parents=True)
    assert st.load_cursor(base) == {"iteration": 0, "run_iteration": 0,
                                    "run_started_at": 0.0, "run_tokens": 0,
                                    "run_codex_raw_tokens": 0}
    assert not (pathlib.Path(base) / "state" / "STATE.md").exists(), "the read path created STATE.md"
    st.add_tokens(base, 7)                                   # a LOCKED writer may create it
    assert (pathlib.Path(base) / "state" / "STATE.md").exists()
    assert st.load_cursor(base)["run_tokens"] == 7


def test_codex_raw_phase_credit_is_deduplicated_and_isolated_from_claude_budget(tmp_path):
    st = _state()
    base, _ = _sdlc(tmp_path)
    st.start_run(base)
    started = st.load_cursor(base)["run_started_at"]
    for _ in range(2):
        st.record_phase_end(base, "goal/research/attempt-1", started + 1,
                            codex_raw_tokens=120)
    cursor = st.load_cursor(base)
    assert cursor["run_codex_raw_tokens"] == 120
    assert cursor["run_tokens"] == 0
    st.record_phase_end(base, "old/attempt", started - 1, codex_raw_tokens=999)
    assert st.load_cursor(base)["run_codex_raw_tokens"] == 120
    st.start_run(base)
    assert st.load_cursor(base)["run_codex_raw_tokens"] == 0


def test_scaffolded_state_md_is_not_executable(tmp_path):
    """#551 review, rider 2: the exclusive-create branch passed no mode, so a freshly scaffolded
    STATE.md landed 0o755 -- an executable markdown file. Asserted against `_state_file` itself
    rather than through a public writer, because `_patch_cursor` republishes via `mkstemp` (0o600)
    the instant it scaffolds and would mask the very mode this rider is about."""
    st = _state()
    base = str(tmp_path / ".sdlc")
    mode = st._state_file(base).stat().st_mode & 0o777
    assert not mode & 0o111, f"a scaffolded STATE.md is executable: {oct(mode)}"


def test_cursor_lock_fails_open_when_the_lock_file_cannot_be_opened(tmp_path, monkeypatch):
    """#551 review, rider 3 (first of the two fail-open branches; both were uncovered). A lock this
    call cannot manage must never be what stops the loop: the write still lands, unlocked."""
    st = _state()
    base = str(tmp_path / ".sdlc")
    (pathlib.Path(base) / "state").mkdir(parents=True)
    real_open = st.os.open
    monkeypatch.setattr(st.os, "open", lambda path, *a, **k: (_ for _ in ()).throw(OSError("no lock file"))
                        if str(path).endswith("STATE.md.lock") else real_open(path, *a, **k))
    st.add_tokens(base, 5)
    assert st.load_cursor(base)["run_tokens"] == 5


def test_cursor_lock_fails_open_when_flock_itself_fails(tmp_path, monkeypatch):
    """#551 review, rider 3 (second branch): e.g. ENOLCK on a lock-less NFS mount. The fd is closed
    and the caller proceeds unlocked rather than raising."""
    st = _state()
    if st.fcntl is None:
        pytest.skip("no fcntl on this platform -- the branch under test cannot be reached")
    base = str(tmp_path / ".sdlc")
    (pathlib.Path(base) / "state").mkdir(parents=True)
    monkeypatch.setattr(st.fcntl, "flock",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ENOLCK")))
    st.add_tokens(base, 9)
    assert st.load_cursor(base)["run_tokens"] == 9


# --- #889: derive a stable run id for bare (non-supervise_daemon.py) sessions -------------------------
# a budget-exhaustion rate reads kind='run_stop', which `_emit_run_stop_once` only ever
# writes when `run_identity()` is not None. Before #889 that meant supervise_daemon.py ONLY -- it is the
# sole exporter of SIGMA_RUN_ID -- so every interactive `/sigma-loop` drain terminated
# unrecorded and that rate stayed empty (0 run_stop rows across 1,081 real ledger event files).

def test_derive_run_id_builds_a_marker_safe_id_from_a_session_pid():
    st = _state()
    assert st.derive_run_id(31857) == "session-31857"
    assert st.derive_run_id("31857") == "session-31857"


def test_derive_run_id_returns_none_without_a_usable_pid():
    """The load-bearing negative. An UNSTABLE id is strictly worse than None: `claim_run_stop` is
    keyed (run_id, run_started_at), so a fresh id per invocation claims a fresh slot and every
    idle `next` poll emits a duplicate run_stop -- the exact bug #905 closed."""
    st = _state()
    for bad in (None, "", "  ", "true", "not-a-pid", "12x", -1, 0):
        assert st.derive_run_id(bad) is None, bad


def test_derived_run_id_survives_the_run_stop_marker_path_validator(tmp_path):
    """`run_stop_marker_path` rejects unsafe ids (`../../../ESCAPED`, `a/b`). A derived id that
    could not be written as a marker would silently disable the dedupe it exists to key."""
    st = _state()
    p = st.run_stop_marker_path(str(tmp_path), st.derive_run_id(31857))
    assert p == tmp_path / "state" / "run_stop" / "session-31857.json"


def test_run_identity_still_prefers_an_explicitly_exported_id(monkeypatch):
    """supervise_daemon.py must keep winning. Its id is stable across a worker's whole lifetime including
    relaunches; a session-derived one is not, so a derived id must never shadow it."""
    st = _state()
    monkeypatch.setenv("SIGMA_RUN_ID", "supervise-123-456-789")
    assert st.run_identity() == "supervise-123-456-789"


def test_two_bare_sessions_no_longer_inherit_each_others_green(tmp_path):
    """#889 step 5b / plan-review F1 — PINNED AS INTENDED, not a regression.

    Measured before this change: two bare sessions were BOTH unattributed, evidence carried
    `run: null`, `done_refusal`'s id check was skipped entirely, and session B happily recorded
    on session A's green. That silent inheritance is precisely what #889 was filed to stop
    ("two such bare concurrent workers on one .sdlc ... can still mutually inherit a sibling's
    green"). The cost -- a session that restarts mid-goal must re-run verify -- is the same price
    supervise_daemon.py workers have paid since #498.

    Note this fires even WITHOUT an intervening `start_run`, so the freshness guard above does not
    mask it: `at` is still newer than `run_started_at`, and only the run id differs."""
    st = _state()
    base = tmp_path / ".sdlc"; (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps({"budget": {"max_iterations": 3}}))
    (base / "state" / "STATE.md").write_text(
        "# Loop State\n\niteration: 0\nrun_iteration: 0\nlast_run: none\n\n## Items\n")
    st.start_run(str(base))

    import os, time
    ev = st.evidence_path(str(base), "g1"); ev.parent.mkdir(parents=True, exist_ok=True)
    os.environ["SIGMA_RUN_ID"] = st.derive_run_id(31857)
    try:
        ev.write_text(json.dumps({"exit": 0, "at": time.time(), "run": st.run_identity()}))
        assert st.done_refusal(str(base), "g1") is None          # session A records its own green

        os.environ["SIGMA_RUN_ID"] = st.derive_run_id(42000)  # a DIFFERENT bare session
        reason = st.done_refusal(str(base), "g1")
        assert reason is not None and "different run" in reason
        assert "re-run verify" in reason                          # actionable, not just a diagnosis
    finally:
        os.environ.pop("SIGMA_RUN_ID", None)
