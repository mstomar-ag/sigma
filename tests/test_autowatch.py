import importlib.util
import json
import os
import pathlib
import time

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


autowatch = _mod("autowatch")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _operator_trusts_the_harness(monkeypatch):
    """#707: a non-default drive command needs the Git-local opt-in; these tests ARE the operator."""
    monkeypatch.setattr(autowatch.shell_policy, "repository_shell_commands_allowed", lambda p: True)
ledger = _mod("ledger")
loop = _mod("loop")

ME = "watcher"          # ledger.actor for these tests
OTHER = "amy"            # the mention/hand-off author

ON = {
    "ledger": {"enabled": True, "actor": ME},
    "ledger_autowatch": None,   # placeholder, real key set below via helper
}


def _config(autowatch_overrides=None, ledger_overrides=None):
    cfg = {"ledger": {"enabled": True, "actor": ME}}
    if ledger_overrides:
        cfg["ledger"].update(ledger_overrides)
    aw = {"enabled": True}
    if autowatch_overrides:
        aw.update(autowatch_overrides)
    cfg["ledger"]["autowatch"] = aw
    return cfg


def _sdlc(tmp_path, config):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(config))
    return d


def _note_or_handoff(d, kind, to, issue=None, goal="g", actor=OTHER, ts=None, **fields):
    ledger.entries_dir(d).mkdir(parents=True, exist_ok=True)
    path = ledger.entry_file(d, actor)
    entry = {"id": f"{actor}:1", "ts": ts or ledger._stamp(), "actor": actor,
              "kind": kind, "goal": goal, "to": to}
    if issue is not None:
        entry["issue"] = issue
    entry.update(fields)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    n = len([l for l in existing.splitlines() if l.strip()]) + 1
    entry["id"] = f"{actor}:{n}"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def _all_gates_pass_deps(agents=None, gh=True):
    """DI kwargs that make every precondition pass, so a test can isolate exactly one failure."""
    return {
        "run_gh_auth": (lambda: gh),
        "run_agents_probe": (lambda sdlc_dir: agents if agents is not None else []),
    }


def _mark_done(d, goal, outcome="done"):
    """#1332: mirrors what a REAL successful `/sigma-loop` drive does — `loop.py record` writing a
    genuine `done`/`parked`/`failed` ledger entry — so a fake `run_drive` produces the same
    post-drive ledger state a real one would, exercising the real outcome-verification path
    rather than the old exit-code-only shortcut. Returns None (falsy), so callers chain it with
    the file's existing `x() or y()` idiom, e.g.
    `lambda cmd, prompt, cwd, env, timeout: (calls.append(prompt) or _mark_done(d, "1") or (0, "{}"))`."""
    ledger.safe_append(d, outcome, goal, config=ledger._config(d))


def _notes(d):
    """autowatch's OWN outcome notes -- excludes the raw candidate fixtures `_note_or_handoff`
    writes (as `OTHER`), which also carry kind="note" for the `mentions`-scope test cases and
    would otherwise be indistinguishable by kind alone."""
    return [e for e in ledger.read_all(d) if e["kind"] == "note" and e["actor"] == ME]


# ------------------------------------------------------------------ enabled()/no-op


def test_tick_is_a_true_noop_when_autowatch_key_absent(tmp_path):
    cfg = {"ledger": {"enabled": True, "actor": ME}}
    d = _sdlc(tmp_path, cfg)
    calls = []
    result = autowatch.tick(d, run_drive=lambda *a, **k: calls.append(a) or (0, "{}"))
    assert result == ""
    assert ledger.read_all(d) == []
    assert calls == []


def test_tick_is_a_true_noop_when_enabled_is_false(tmp_path):
    d = _sdlc(tmp_path, _config({"enabled": False}))
    calls = []
    result = autowatch.tick(d, run_drive=lambda *a, **k: calls.append(a) or (0, "{}"))
    assert result == ""
    assert ledger.read_all(d) == []
    assert calls == []


def test_tick_is_a_true_noop_when_enabled_is_a_truthy_string(tmp_path):
    """Strict `is True`, mirroring ledger.enabled()'s own idiom -- a truthy-but-not-bool value
    must not silently switch this on."""
    d = _sdlc(tmp_path, _config({"enabled": "true"}))
    result = autowatch.tick(d)
    assert result == ""
    assert ledger.read_all(d) == []


# ------------------------------------------------------------------ nothing to do


def test_tick_nothing_to_do_writes_a_ledger_note(tmp_path):
    d = _sdlc(tmp_path, _config())
    result = autowatch.tick(d, **_all_gates_pass_deps())
    assert result == "nothing to do"
    notes = _notes(d)
    assert len(notes) == 1
    assert "nothing to do" in notes[0]["why"]
    assert notes[0]["goal"] == "autowatch"


# ------------------------------------------------------------------ candidate discovery / scope


def test_explicit_issue_bypasses_discovery_even_with_a_candidate_present(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=99)
    calls = []
    result = autowatch.tick(
        d, issue="42",
        run_drive=lambda cmd, prompt, cwd, env, timeout: (
            calls.append(prompt) or _mark_done(d, "42") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"
    assert "`42`" in calls[0]
    note = [e for e in _notes(d) if e["goal"] == "42"][0]
    assert note["state"] == "resolved"


def test_scope_restricted_to_mentions_skips_a_handoff_candidate(tmp_path):
    d = _sdlc(tmp_path, _config({"scope": ["mentions"]}))
    _note_or_handoff(d, "handoff", ME, issue=7, state="open")
    result = autowatch.tick(d, **_all_gates_pass_deps())
    assert result == "nothing to do"


def test_scope_restricted_to_blockers_picks_up_a_handoff_candidate(tmp_path):
    d = _sdlc(tmp_path, _config({"scope": ["blockers"]}))
    _note_or_handoff(d, "handoff", ME, issue=7, state="open")
    calls = []
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (
            calls.append(prompt) or _mark_done(d, "7") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"
    assert "`7`" in calls[0]


def test_multiple_candidates_takes_the_oldest_unacked_one(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=2, ts="2026-08-10T00:00:00Z")
    _note_or_handoff(d, "note", ME, issue=1, ts="2026-08-01T00:00:00Z")
    calls = []
    autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (
            calls.append(prompt) or _mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert "`1`" in calls[0]


def test_a_note_addressed_to_someone_else_is_never_a_candidate(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", "someone-else", issue=1)
    result = autowatch.tick(d, **_all_gates_pass_deps())
    assert result == "nothing to do"


def test_an_own_unaddressed_write_is_never_a_candidate(tmp_path):
    d = _sdlc(tmp_path, _config())
    ledger.safe_append(d, "note", "g", config=ledger._config(d))  # no `to` -- not addressed
    result = autowatch.tick(d, **_all_gates_pass_deps())
    assert result == "nothing to do"


def test_handoff_already_accepted_by_someone_else_is_no_longer_unacked(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "handoff", ME, issue=7, state="open")
    ledger.safe_append(d, "ack", "g", config=ledger._config(d), issue=7, state="accepted")
    result = autowatch.tick(d, **_all_gates_pass_deps())
    assert result == "nothing to do"


def test_a_successfully_resolved_note_is_not_reselected_on_a_later_tick(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    first = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert first == "success"
    second = autowatch.tick(d, **_all_gates_pass_deps())
    assert second == "nothing to do"


def test_a_precondition_block_does_not_suppress_a_later_retry(tmp_path):
    """Blocked (unlike success) is retriable -- the same candidate must be picked up again once
    the environment recovers."""
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    first = autowatch.tick(d, **_all_gates_pass_deps(gh=False))
    assert first == "blocked: require_gh_auth"
    calls = []
    second = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (
            calls.append(prompt) or _mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert second == "success"
    assert "`1`" in calls[0]


# ------------------------------------------------------------------ preconditions, each blocking alone


def test_require_gh_auth_blocks_in_isolation(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    calls = []
    result = autowatch.tick(
        d, run_drive=lambda *a, **k: calls.append(a) or (0, "{}"),
        **_all_gates_pass_deps(gh=False))
    assert result == "blocked: require_gh_auth"
    assert calls == []
    assert "require_gh_auth" in _notes(d)[0]["why"]


def test_require_gh_auth_can_be_toggled_off(tmp_path):
    d = _sdlc(tmp_path, _config({"preconditions": {"require_gh_auth": False}}))
    _note_or_handoff(d, "note", ME, issue=1)
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "1") or (0, "{}")),
        run_gh_auth=lambda: (_ for _ in ()).throw(AssertionError("must not be called")),
        run_agents_probe=lambda sdlc_dir: [])
    assert result == "success"


def test_no_concurrent_session_blocks_via_agents_probe(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    calls = []
    result = autowatch.tick(
        d, run_drive=lambda *a, **k: calls.append(a) or (0, "{}"),
        run_gh_auth=lambda: True,
        run_agents_probe=lambda sdlc_dir: [{"id": "agent-1"}])
    assert result == "blocked: no_concurrent_session"
    assert calls == []


def test_no_concurrent_session_blocks_when_probe_cannot_determine_state(tmp_path):
    """Fail CLOSED: a probe that cannot answer must block, not pass."""
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    result = autowatch.tick(
        d, run_gh_auth=lambda: True,
        run_agents_probe=lambda sdlc_dir: None)
    assert result == "blocked: no_concurrent_session"


def test_no_concurrent_session_blocks_via_session_active(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    loop.session_start(d, os.getpid())
    calls = []
    result = autowatch.tick(
        d, run_drive=lambda *a, **k: calls.append(a) or (0, "{}"),
        run_gh_auth=lambda: True, run_agents_probe=lambda sdlc_dir: [])
    assert result == "blocked: no_concurrent_session"
    assert calls == []


def test_no_concurrent_session_blocks_via_registered_goal_worker(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    config = ledger._config(d)
    loop.agent_start(d, "1", os.getpid(), config)   # this test process is genuinely alive
    calls = []
    result = autowatch.tick(
        d, run_drive=lambda *a, **k: calls.append(a) or (0, "{}"),
        run_gh_auth=lambda: True, run_agents_probe=lambda sdlc_dir: [])
    assert result == "blocked: no_concurrent_session"
    assert calls == []


def test_load_average_ceiling_skipped_when_null(tmp_path, monkeypatch):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    monkeypatch.setattr(autowatch.os, "getloadavg",
                         lambda: (_ for _ in ()).throw(AssertionError("must not be called")))
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"


def test_load_average_ceiling_blocks_when_exceeded(tmp_path, monkeypatch):
    d = _sdlc(tmp_path, _config({"preconditions": {"load_average_ceiling": 0.1}}))
    _note_or_handoff(d, "note", ME, issue=1)
    monkeypatch.setattr(autowatch.os, "getloadavg", lambda: (99.0, 99.0, 99.0))
    monkeypatch.setattr(autowatch.os, "cpu_count", lambda: 1)
    calls = []
    result = autowatch.tick(
        d, run_drive=lambda *a, **k: calls.append(a) or (0, "{}"),
        **_all_gates_pass_deps())
    assert result == "blocked: load_average_ceiling"
    assert calls == []


def test_load_average_ceiling_passes_when_under(tmp_path, monkeypatch):
    d = _sdlc(tmp_path, _config({"preconditions": {"load_average_ceiling": 50.0}}))
    _note_or_handoff(d, "note", ME, issue=1)
    monkeypatch.setattr(autowatch.os, "getloadavg", lambda: (0.5, 0.5, 0.5))
    monkeypatch.setattr(autowatch.os, "cpu_count", lambda: 4)
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"


def test_spend_ceiling_skipped_when_null(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    (d / "state").mkdir(parents=True, exist_ok=True)
    (d / "state" / "autowatch-spend.json").write_text(
        json.dumps({"records": [{"ts": time.time(), "tokens": 10_000_000, "cost_usd": 999}]}))
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"


def test_spend_ceiling_blocks_when_exceeded(tmp_path):
    d = _sdlc(tmp_path, _config({"preconditions": {"spend_ceiling_tokens_per_week": 100}}))
    _note_or_handoff(d, "note", ME, issue=1)
    (d / "state").mkdir(parents=True, exist_ok=True)
    (d / "state" / "autowatch-spend.json").write_text(
        json.dumps({"records": [{"ts": time.time(), "tokens": 5000, "cost_usd": 1.0}]}))
    calls = []
    result = autowatch.tick(
        d, run_drive=lambda *a, **k: calls.append(a) or (0, "{}"),
        **_all_gates_pass_deps())
    assert result == "blocked: spend_ceiling_tokens_per_week"
    assert calls == []


def test_spend_ceiling_ignores_records_older_than_7_days(tmp_path):
    d = _sdlc(tmp_path, _config({"preconditions": {"spend_ceiling_tokens_per_week": 100}}))
    _note_or_handoff(d, "note", ME, issue=1)
    stale_ts = time.time() - (8 * 24 * 3600)
    (d / "state").mkdir(parents=True, exist_ok=True)
    (d / "state" / "autowatch-spend.json").write_text(
        json.dumps({"records": [{"ts": stale_ts, "tokens": 5_000_000, "cost_usd": 500}]}))
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"


def test_spend_ceiling_records_a_new_entry_after_a_successful_drive(tmp_path):
    d = _sdlc(tmp_path, _config({"preconditions": {"spend_ceiling_tokens_per_week": 1_000_000}}))
    _note_or_handoff(d, "note", ME, issue=1)
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (
            _mark_done(d, "1") or (0, json.dumps({"total_cost_usd": 2.0}))),
        **_all_gates_pass_deps())
    assert result == "success"
    data = json.loads((d / "state" / "autowatch-spend.json").read_text())
    assert len(data["records"]) == 1
    assert data["records"][0]["cost_usd"] == 2.0
    assert data["records"][0]["tokens"] > 0


def test_spend_ceiling_fails_closed_when_state_file_is_corrupt(tmp_path):
    """A present-but-unreadable/corrupt autowatch-spend.json is a "cannot be answered" case, not
    an "empty history" case -- the ceiling check must BLOCK, never silently pass as zero spend
    (the fail-open bug this test pins: collapsing absent and corrupt into the same `[]` made a
    corrupted spend file permanently defeat the ceiling)."""
    d = _sdlc(tmp_path, _config({"preconditions": {"spend_ceiling_tokens_per_week": 100}}))
    _note_or_handoff(d, "note", ME, issue=1)
    (d / "state").mkdir(parents=True, exist_ok=True)
    (d / "state" / "autowatch-spend.json").write_text("{not valid json")
    calls = []
    result = autowatch.tick(
        d, run_drive=lambda *a, **k: calls.append(a) or (0, "{}"),
        **_all_gates_pass_deps())
    assert result == "blocked: spend_ceiling_tokens_per_week"
    assert calls == []
    notes = _notes(d)
    assert len(notes) == 1
    assert "unreadable" in notes[0]["why"] or "corrupt" in notes[0]["why"]


def test_spend_ceiling_passes_when_state_file_is_genuinely_absent(tmp_path):
    """The complementary case to the corrupt-file test above: a file that has simply never been
    written yet (the common "no spend history" case) must NOT be treated as unverifiable -- only
    a present-but-broken file fails closed."""
    d = _sdlc(tmp_path, _config({"preconditions": {"spend_ceiling_tokens_per_week": 100}}))
    _note_or_handoff(d, "note", ME, issue=1)
    assert not (d / "state" / "autowatch-spend.json").exists()
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"


# ------------------------------------------------------------------ all preconditions passing


def test_all_preconditions_passing_lets_it_through(tmp_path, monkeypatch):
    d = _sdlc(tmp_path, _config({"preconditions": {
        "load_average_ceiling": 50.0, "spend_ceiling_tokens_per_week": 1_000_000}}))
    _note_or_handoff(d, "note", ME, issue=1)
    monkeypatch.setattr(autowatch.os, "getloadavg", lambda: (0.1, 0.1, 0.1))
    monkeypatch.setattr(autowatch.os, "cpu_count", lambda: 4)
    calls = []
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (
            calls.append(env) or _mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"
    assert len(calls) == 1


# ------------------------------------------------------------------ hop limit


def test_hop_limit_refuses_when_it_would_be_exceeded(tmp_path):
    d = _sdlc(tmp_path, _config({"hop_limit": 3}))
    _note_or_handoff(d, "note", ME, issue=1, autowatch_hop="3")
    calls = []
    result = autowatch.tick(
        d, run_drive=lambda *a, **k: calls.append(a) or (0, "{}"),
        **_all_gates_pass_deps())
    assert result == "blocked: hop_limit"
    assert calls == []
    note = _notes(d)[0]
    assert "hop_limit" in note["why"]


def test_hop_limit_allows_the_final_permitted_hop(tmp_path):
    d = _sdlc(tmp_path, _config({"hop_limit": 3}))
    _note_or_handoff(d, "note", ME, issue=1, autowatch_hop="2")
    envs = []
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (
            envs.append(env) or _mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"
    assert envs[0]["SIGMA_AUTOWATCH_HOP"] == "3"
    note = [e for e in _notes(d) if e.get("state") == "resolved"][0]
    assert note["autowatch_hop"] == "3"


def test_hop_limit_defaults_to_a_conservative_value_when_unconfigured(tmp_path):
    d = _sdlc(tmp_path, _config())   # no hop_limit key at all
    _note_or_handoff(d, "note", ME, issue=1, autowatch_hop=str(autowatch.DEFAULT_HOP_LIMIT))
    calls = []
    result = autowatch.tick(
        d, run_drive=lambda *a, **k: calls.append(a) or (0, "{}"),
        **_all_gates_pass_deps())
    assert result == "blocked: hop_limit"
    assert calls == []


def test_hop_limit_accumulates_across_repeated_never_recorded_retries_of_the_same_candidate(tmp_path):
    """#1334 (review of #1332): a candidate whose driven run exits 0 but never calls `loop.py
    record` at all -- the exact live-reproduced bug #1332's fix exists for -- is deliberately
    left retriable rather than permanently resolved. Without the hop actually accumulating
    across those retries, `hop_limit` could never trip, and the SAME stuck candidate would get a
    full driven `/sigma-loop` subprocess re-launched on every single tick, forever. It must
    actually stop once `hop_limit` is reached."""
    d = _sdlc(tmp_path, _config({"hop_limit": 2}))
    _note_or_handoff(d, "note", ME, issue=1)
    drives = []
    drive = lambda cmd, prompt, cwd, env, timeout: (
        drives.append(env["SIGMA_AUTOWATCH_HOP"]) or (0, "{}"))

    first = autowatch.tick(d, run_drive=drive, **_all_gates_pass_deps())
    second = autowatch.tick(d, run_drive=drive, **_all_gates_pass_deps())
    third = autowatch.tick(d, run_drive=drive, **_all_gates_pass_deps())

    assert [first, second, third] == ["failed", "failed", "blocked: hop_limit"]
    # Exactly 2 real driven attempts, at hop 1 then hop 2 -- the 3rd tick refused BEFORE driving,
    # instead of launching a 3rd real subprocess against the same still-stuck candidate.
    assert drives == ["1", "2"]


def test_hop_limit_accumulates_across_repeated_genuinely_failed_retries_of_the_same_candidate(tmp_path):
    """Same accumulation requirement as the never-recorded case above, for a driven run that DOES
    properly call `loop.py record ... failed` each time (a real, recurring bug) rather than
    recording nothing."""
    d = _sdlc(tmp_path, _config({"hop_limit": 2}))
    _note_or_handoff(d, "note", ME, issue=1)
    drive = lambda cmd, prompt, cwd, env, timeout: (
        _mark_done(d, "1", outcome="failed") or (0, "{}"))

    first = autowatch.tick(d, run_drive=drive, **_all_gates_pass_deps())
    second = autowatch.tick(d, run_drive=drive, **_all_gates_pass_deps())
    third = autowatch.tick(d, run_drive=drive, **_all_gates_pass_deps())

    assert [first, second, third] == ["failed", "failed", "blocked: hop_limit"]


def test_explicit_issue_starts_the_hop_chain_at_zero_regardless_of_prior_ledger_state(tmp_path):
    d = _sdlc(tmp_path, _config({"hop_limit": 1}))
    envs = []
    result = autowatch.tick(
        d, issue="5",
        run_drive=lambda cmd, prompt, cwd, env, timeout: (
            envs.append(env) or _mark_done(d, "5") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"
    assert envs[0]["SIGMA_AUTOWATCH_HOP"] == "1"


def test_explicit_issue_resolves_the_matching_ledger_candidate_and_stamps_ref(tmp_path):
    """#1337 (webhook.ts's own instructed command always uses `--issue`): when a real ledger
    candidate exists for this issue, the `--issue` path must recover the SAME candidate id
    `_find_candidate` would have picked -- otherwise its outcome notes never carry `ref`, and
    channel_notify.py's own retry-detection (which scans notes by `ref`) can never see anything
    autowatch does for a channel-triggered tick, in real deployment, regardless of hop."""
    d = _sdlc(tmp_path, _config())
    entry = _note_or_handoff(d, "note", ME, issue=5, goal="5")
    result = autowatch.tick(
        d, issue="5",
        run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "5") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"
    resolved = [e for e in _notes(d) if e.get("state") == "resolved"][0]
    assert resolved["ref"] == entry["id"]


def test_explicit_issue_accumulates_hop_across_repeat_calls_against_the_same_candidate(tmp_path):
    """#1337: without resolving to the real candidate, EVERY `--issue` invocation starts
    incoming_hop back at 0 (candidate stays None), so hop_limit can never trip no matter how many
    times the same underlying candidate is driven via `--issue` -- silently defeating the exact
    safety check webhook.ts's own instructions claim protects this call. With the fix, a second
    `--issue` tick against the SAME already-resolved candidate must build on the first's recorded
    hop and get refused once hop_limit is reached, instead of driving a real, duplicate spend."""
    d = _sdlc(tmp_path, _config({"hop_limit": 1}))
    _note_or_handoff(d, "note", ME, issue=5, goal="5")
    first = autowatch.tick(
        d, issue="5",
        run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "5") or (0, "{}")),
        **_all_gates_pass_deps())
    assert first == "success"

    second = autowatch.tick(
        d, issue="5",
        run_drive=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not drive again")),
        **_all_gates_pass_deps())
    assert second == "blocked: hop_limit"


def test_explicit_issue_with_no_matching_ledger_entry_still_has_no_ref(tmp_path):
    """A human-run manual `--issue` tick against a target with no ledger history at all is still
    valid -- this must not require a matching candidate to exist."""
    d = _sdlc(tmp_path, _config())
    result = autowatch.tick(
        d, issue="99",
        run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "99") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"
    resolved = [e for e in _notes(d) if e.get("state") == "resolved"][0]
    assert resolved.get("ref") is None


# ------------------------------------------------------------------ drive prompt phrasing (#1332)


def test_drive_prompt_says_github_issue_under_github_discovery(tmp_path):
    d = _sdlc(tmp_path, _config(ledger_overrides={}))
    cfg = json.loads((d / "config.json").read_text())
    cfg["discovery"] = {"source": "github"}
    (d / "config.json").write_text(json.dumps(cfg))
    _note_or_handoff(d, "note", ME, issue=1)
    calls = []
    autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (
            calls.append(prompt) or _mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert "GitHub issue #1" in calls[0]
    assert "local goal file" not in calls[0]


def test_drive_prompt_says_local_goal_file_under_local_goals_discovery(tmp_path):
    """local-goals is config.json.tmpl's own DEFAULT discovery mode -- the out-of-the-box case
    for a fresh OSS adopter who never configures GitHub, not an edge case. `_config()`'s own
    helper sets no `discovery` key at all, matching that default exactly."""
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    calls = []
    autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (
            calls.append(prompt) or _mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert "local goal file `1`" in calls[0]
    assert "GitHub issue" not in calls[0]


def test_drive_prompt_names_loop_py_record_as_the_real_stop_condition(tmp_path):
    """#1332: a real driven run, live-tested, stopped after research alone (no plan, no
    implementation, no PR, no `loop.py record` call) and still exited 0. The old wording ("a
    normal stop... then STOP") was ambiguous enough to read that way. Pin the strengthened text
    stays in the prompt, not just in this module's own docstring."""
    prompt = autowatch._drive_prompt("1", {})
    assert "loop.py record" in prompt
    assert "Research and discussion alone are NEVER a stopping point" in prompt


# ------------------------------------------------------------ real outcome verification (#1332)


def test_exit_code_zero_with_no_terminal_ledger_entry_is_not_success(tmp_path):
    """THE live-reproduced bug this fix exists for: a driven run can exit 0 (the subprocess
    didn't crash) while never calling `loop.py record` at all -- no plan, no implementation, no
    PR. The old code trusted exit-code alone and marked this `resolved` forever. Reproduced here
    with NO `_mark_done` call -- the fake drive returns (0, "{}") and writes nothing else,
    exactly matching the real live repro."""
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (0, "{}"),
        **_all_gates_pass_deps())
    assert result == "failed"
    notes = _notes(d)
    assert len(notes) == 1
    assert "never recorded a terminal outcome" in notes[0]["why"]
    assert notes[0].get("state") != "resolved"
    # Not silently lost: a later tick must be able to pick this exact mention up again.
    second = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert second != "nothing to do"


def test_a_real_park_outcome_is_treated_as_a_successful_escalation(tmp_path):
    """A driven run that correctly determines the goal needs a human and calls `loop.py record
    ... parked` has done exactly what autowatch exists to do -- surface it. This is resolved from
    autowatch's own perspective (the parked goal is now visible via the normal sdlc:parked
    mechanism), not a failure."""
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (
            _mark_done(d, "1", outcome="parked") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "success"
    notes = _notes(d)
    resolved = [n for n in notes if n.get("state") == "resolved"]
    assert len(resolved) == 1
    assert "parked" in resolved[0]["why"]


def test_a_real_failed_outcome_is_retriable_not_permanently_resolved(tmp_path):
    """A genuinely, properly-recorded `loop.py record ... failed` (a real bug, a flaky test)
    should get another chance on a later tick, bounded by hop_limit -- unlike a `done` or
    `parked` outcome, it must NOT be marked resolved."""
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (
            _mark_done(d, "1", outcome="failed") or (0, "{}")),
        **_all_gates_pass_deps())
    assert result == "failed"
    notes = _notes(d)
    assert all(n.get("state") != "resolved" for n in notes)
    second = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (_mark_done(d, "1") or (0, "{}")),
        **_all_gates_pass_deps())
    assert second == "success"


# ------------------------------------------------------------------ always writes a ledger entry


def test_a_no_process_group_refusal_reaches_the_ledger_not_only_stderr(tmp_path):
    """#425: on a host that cannot terminate the driven group, `_run_drive` refuses with exit 2;
    the tick's ledger note must carry the refusal itself, or an operator reading the ledger sees
    only "exited 2"."""
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (2, autowatch.NO_PROCESS_GROUP_REFUSAL),
        **_all_gates_pass_deps())
    assert result == "failed"
    why = _notes(d)[0]["why"]
    assert "exited 2" in why and "REFUSED [no-process-group]" in why


def test_writes_a_ledger_entry_on_a_genuine_driven_run_failure(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)
    result = autowatch.tick(
        d, run_drive=lambda cmd, prompt, cwd, env, timeout: (1, "boom"),
        **_all_gates_pass_deps())
    assert result == "failed"
    notes = _notes(d)
    assert len(notes) == 1
    assert "exited 1" in notes[0]["why"]
    assert notes[0].get("state") != "resolved"


def test_writes_a_ledger_entry_when_the_driven_run_raises(tmp_path):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)

    def boom(*a, **k):
        raise RuntimeError("subprocess exploded")

    result = autowatch.tick(d, run_drive=boom, **_all_gates_pass_deps())
    assert result == "failed"
    notes = _notes(d)
    assert len(notes) == 1
    assert "subprocess exploded" in notes[0]["why"]


def test_writes_a_ledger_entry_on_an_unexpected_exception_in_candidate_discovery(tmp_path, monkeypatch):
    d = _sdlc(tmp_path, _config())
    _note_or_handoff(d, "note", ME, issue=1)

    def boom(entries, who, settings):
        raise RuntimeError("candidate discovery exploded")

    monkeypatch.setattr(autowatch, "_find_candidate", boom)
    result = autowatch.tick(d, **_all_gates_pass_deps())
    assert result == "failed: unexpected error"
    notes = _notes(d)
    assert len(notes) == 1
    assert "unexpected error" in notes[0]["why"]
    assert notes[0]["goal"] == "autowatch"


def test_tick_never_raises_even_on_an_unexpected_exception(tmp_path, monkeypatch):
    d = _sdlc(tmp_path, _config())

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(autowatch.ledger, "read_all", boom)
    result = autowatch.tick(d)   # must not raise
    assert result == "failed: unexpected error"


def test_tick_never_raises_when_config_json_is_missing(tmp_path):
    """`ledger._config` is a bare `json.loads(path.read_text())` with no error handling -- it used
    to be called BEFORE tick()'s own try/except, so a missing config.json propagated straight out
    of a function whose own docstring promises it never raises. `.sdlc/` here has no config.json
    at all (only `_sdlc()`'s helper writes one; this test bypasses it on purpose)."""
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    assert not (d / "config.json").exists()
    result = autowatch.tick(d)   # must not raise
    assert result == "failed: could not load config"
    # No ledger note is possible here -- `enabled`/`actor` both live IN the unreadable config.json,
    # so there is nothing safe to write with (see tick()'s own docstring). What matters is that
    # this genuinely does not raise and does not silently masquerade as the enabled:false no-op.
    assert ledger.read_all(d) == []


def test_tick_never_raises_when_config_json_is_malformed(tmp_path):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text("{not valid json")
    result = autowatch.tick(d)   # must not raise
    assert result == "failed: could not load config"
    assert ledger.read_all(d) == []


# ------------------------------------------------------------ surface + adapter_wired (#1322/#1323)


def test_resolve_surface_defaults_to_desktop_when_absent():
    assert autowatch.resolve_surface({"ledger": {"autowatch": {"enabled": True}}}) == "desktop"


def test_resolve_surface_reads_config_value():
    cfg = _config({"surface": "cli"})
    assert autowatch.resolve_surface(cfg) == "cli"


def test_resolve_surface_falls_back_to_default_on_unrecognized_value():
    cfg = _config({"surface": "typo-value"})
    assert autowatch.resolve_surface(cfg) == "desktop"


def test_resolve_surface_env_override_wins_over_config(monkeypatch):
    cfg = _config({"surface": "desktop"})
    monkeypatch.setenv("SIGMA_AUTOWATCH_SURFACE", "cli")
    assert autowatch.resolve_surface(cfg) == "cli"


def test_resolve_surface_invalid_env_override_falls_back_to_config(monkeypatch):
    cfg = _config({"surface": "cli"})
    monkeypatch.setenv("SIGMA_AUTOWATCH_SURFACE", "not-a-real-surface")
    assert autowatch.resolve_surface(cfg) == "cli"


def test_adapter_wired_false_with_nothing_configured(tmp_path):
    cfg = _config()
    assert autowatch.adapter_wired(cfg, scheduled_tasks_dir=str(tmp_path / "none")) is False


def test_adapter_wired_true_via_channel_webhook_url():
    cfg = _config({"channel_webhook_url": "http://127.0.0.1:8788"})
    assert autowatch.adapter_wired(cfg, scheduled_tasks_dir="/does/not/exist") is True


def test_adapter_wired_true_via_desktop_scheduled_task(tmp_path):
    cfg = _config()
    tasks = tmp_path / "scheduled-tasks" / "sigma-autowatch-myrepo"
    tasks.mkdir(parents=True)
    (tasks / "SKILL.md").write_text("run autowatch.py tick .sdlc\n")
    assert autowatch.adapter_wired(cfg, scheduled_tasks_dir=str(tmp_path / "scheduled-tasks")) is True


def test_adapter_wired_ignores_unrelated_scheduled_tasks(tmp_path):
    cfg = _config()
    tasks = tmp_path / "scheduled-tasks" / "daily-code-review"
    tasks.mkdir(parents=True)
    (tasks / "SKILL.md").write_text("review today's commits\n")
    assert autowatch.adapter_wired(cfg, scheduled_tasks_dir=str(tmp_path / "scheduled-tasks")) is False


def test_adapter_wired_ignores_a_coincidental_mention_of_the_filename(tmp_path):
    """#1337 review finding: a bare 'autowatch.py' substring match false-positives on a task whose
    prose merely mentions the filename without ever actually invoking it — e.g. a task someone
    named/described referencing it in passing, or an old unrelated experiment. The real marker is
    the actual invocation loop.py's own setup nudge asks Desktop to create."""
    cfg = _config()
    tasks = tmp_path / "scheduled-tasks" / "old-experiment"
    tasks.mkdir(parents=True)
    (tasks / "SKILL.md").write_text(
        "This task predates autowatch.py and has nothing to do with it.\n")
    assert autowatch.adapter_wired(cfg, scheduled_tasks_dir=str(tmp_path / "scheduled-tasks")) is False


def test_adapter_wired_tolerates_missing_scheduled_tasks_dir(tmp_path):
    cfg = _config()
    assert autowatch.adapter_wired(
        cfg, scheduled_tasks_dir=str(tmp_path / "definitely-does-not-exist")) is False


def test_adapter_wired_tolerates_an_unreadable_skill_md(tmp_path):
    """A permission-denied SKILL.md (owned by a different user, odd filesystem ACLs, ...) must
    degrade to 'not wired', not raise — matches this function's own fail-open direction."""
    cfg = _config()
    tasks = tmp_path / "scheduled-tasks" / "some-task"
    tasks.mkdir(parents=True)
    skill = tasks / "SKILL.md"
    skill.write_text("autowatch.py tick\n")
    os.chmod(skill, 0o000)
    try:
        assert autowatch.adapter_wired(
            cfg, scheduled_tasks_dir=str(tmp_path / "scheduled-tasks")) is False
    finally:
        os.chmod(skill, 0o644)      # restore so tmp_path cleanup can remove it


# ------------------------------------------------------------------ CLI


def test_cli_runs_a_tick_and_prints_the_summary(tmp_path, capsys, monkeypatch):
    """Exercises the real `main() -> tick()` wiring end to end, with the real `gh`/`claude`/driven
    subprocesses swapped out at the module level -- the CLI entrypoint takes no DI kwargs, so this
    is the CLI-equivalent of the DI seam every other test uses directly."""
    d = _sdlc(tmp_path, _config())
    monkeypatch.setattr(autowatch, "_run_gh_auth_status", lambda: True)
    monkeypatch.setattr(autowatch, "_run_claude_agents_probe", lambda sdlc_dir: [])
    monkeypatch.setattr(autowatch, "_run_drive",
                         lambda cmd, prompt, cwd, env, timeout: (
                             _mark_done(d, "9") or (0, "{}")))
    assert autowatch.main(["autowatch.py", "tick", str(d), "--issue", "9"]) == 0
    assert capsys.readouterr().out.strip() == "success"


def test_cli_is_quiet_when_disabled(tmp_path, capsys):
    d = _sdlc(tmp_path, _config({"enabled": False}))
    assert autowatch.main(["autowatch.py", "tick", str(d)]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_cli_usage_error_when_verb_missing(capsys):
    assert autowatch.main(["autowatch.py"]) == 2
    assert "usage" in capsys.readouterr().err


def test_cli_never_fatal_even_when_tick_raises(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(autowatch, "tick", boom)
    assert autowatch.main(["autowatch.py", "tick", "/nonexistent"]) == 1
    assert "tick failed (non-fatal): boom" in capsys.readouterr().err


def test_cli_parses_the_issue_flag(tmp_path, monkeypatch):
    d = _sdlc(tmp_path, _config())
    captured = {}

    def fake_tick(sdlc_dir, issue=None, **kwargs):
        captured["issue"] = issue
        return "ok"

    monkeypatch.setattr(autowatch, "tick", fake_tick)
    autowatch.main(["autowatch.py", "tick", str(d), "--issue", "123"])
    assert captured["issue"] == "123"


# --------------------------------------------------------------------------- _run_drive (#2338)
#
# `on_spawn` (Component H's round-2 REJECT fix): a real, PIPE-based subprocess, exercised with real
# python subprocesses (not a mock) -- the whole point of `on_spawn` is "does the callback genuinely
# fire before the call can block for the run's full duration", which only a real child process, not
# an injected fake, can prove.


def test_run_drive_returns_exit_code_and_stdout_unchanged_without_on_spawn():
    """`on_spawn` is additive -- every existing caller omits it and gets byte-for-byte the same
    return contract as before this parameter existed."""
    code, out = autowatch._run_drive(
        "python3 -c \"import sys; sys.stdout.write('hello'); sys.exit(3)\"", "", ".", dict(os.environ), 30)
    assert (code, out) == (3, "hello")


def test_run_drive_calls_on_spawn_with_the_real_child_pid_before_blocking():
    """The callback fires the INSTANT the child exists -- before `communicate()` returns -- so a
    caller can register a liveness marker for the child's OWN pid, not just its own calling
    process's pid (the gap round 2 of goal-review's review found: the listener's pid is not the
    worker's pid)."""
    seen = []
    code, out = autowatch._run_drive(
        "python3 -c \"import os,sys,time; sys.stdout.write(str(os.getpid())); time.sleep(0.05)\"",
        "", ".", dict(os.environ), 30, on_spawn=lambda pid: seen.append(pid))
    assert code == 0
    assert len(seen) == 1
    assert str(seen[0]) == out.strip()          # the pid on_spawn saw IS the real child's own pid
    assert seen[0] != os.getpid()                 # never this test process's own pid


def test_run_drive_on_spawn_exception_never_breaks_the_drive():
    """A caller's own callback breaking must never take the drive down with it -- the same
    fail-open posture every other injectable seam in this kit holds."""
    def boom(pid):
        raise RuntimeError("boom")

    code, out = autowatch._run_drive(
        "python3 -c \"import sys; sys.stdout.write('ok'); sys.exit(0)\"", "", ".", dict(os.environ),
        30, on_spawn=boom)
    assert (code, out) == (0, "ok")


def test_run_drive_timeout_still_kills_the_child_and_reports_124():
    """Unchanged from before this parameter existed: a real timeout still kills the child (no
    zombie/leaked pipe) and returns the same synthetic `(124, ...)` shape."""
    start = time.time()
    code, out = autowatch._run_drive(
        "python3 -c \"import time; time.sleep(30)\"", "", ".", dict(os.environ), 1)
    elapsed = time.time() - start
    assert code == 124
    assert "timed out" in out
    assert elapsed < 10          # genuinely killed, not left to run out its own 30s sleep


def test_run_drive_launch_failure_never_calls_on_spawn():
    """A command that never even spawns (a nonexistent executable) must not fire `on_spawn` --
    there is no real child pid to report."""
    calls = []
    code, out = autowatch._run_drive(
        "this-executable-does-not-exist-anywhere", "", ".", dict(os.environ), 5,
        on_spawn=lambda pid: calls.append(pid))
    assert code == 1
    assert calls == []


# ------------------------------------------------------------------ #1758: the drive takes an admission lease


def _held_tick(tmp_path, monkeypatch, hold):
    import contextlib
    d = _sdlc(tmp_path, _config())
    seen = []

    class Spy:
        @contextlib.contextmanager
        def session_lease(self, kind, goal, **kw):
            seen.append((kind, goal, kw.get("ttl")))
            yield type("Hold", (), {"reason": "pressure: new work is held", "retry_after": 1.0})() if hold else None

    monkeypatch.setattr(autowatch, "_guard", lambda: Spy())
    calls = []
    result = autowatch.tick(
        d, issue="42",
        run_drive=lambda cmd, prompt, cwd, env, timeout: (
            calls.append(prompt) or _mark_done(d, "42") or (0, "{}")),
        **_all_gates_pass_deps())
    return d, result, calls, seen


def test_a_hold_defers_the_tick_without_spawning(tmp_path, monkeypatch):
    d, result, calls, seen = _held_tick(tmp_path, monkeypatch, hold=True)
    assert result == "deferred: resource hold" and calls == []
    assert seen[0][0] == "autowatch" and seen[0][1] == "42"
    note = [e for e in _notes(d) if e["goal"] == "42"][0]
    assert "deferred" in note["why"] and note.get("state") != "resolved"


def test_no_hold_drives_under_the_lease(tmp_path, monkeypatch):
    d, result, calls, seen = _held_tick(tmp_path, monkeypatch, hold=False)
    assert result == "success" and len(calls) == 1 and len(seen) == 1


def test_enforce_mode_with_no_headroom_defers_the_real_drive(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGMA_RESOURCE_DIR", str(tmp_path / "rs"))
    monkeypatch.setenv("SIGMA_RESOURCE_MODE", "enforce")
    guard = autowatch._guard()
    lease = guard._lease()
    lease.take_sample = lambda: lease.guard.Sample(
        total=16 << 30, available=1 << 20, swap_used=0, swap_total=0, swapouts=0, swapouts_at=0.0, pressure=4,
        cpu_count=8, load1=0.0)
    lease.take_footprint = lambda pid: None
    d = _sdlc(tmp_path, _config())
    calls = []
    result = autowatch.tick(d, issue="42", run_drive=lambda *a, **k: calls.append(a) or (0, "{}"),
                            **_all_gates_pass_deps())
    assert result == "deferred: resource hold" and calls == []
