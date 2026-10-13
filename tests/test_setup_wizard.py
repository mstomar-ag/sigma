import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "skills" / "sigma-init" / "scripts"))
import setup_wizard  # noqa: E402
import pytest  # noqa: E402

#: The real #236 adoption gate, kept so the tests that exercise it can restore it.
REAL_ADOPTED = setup_wizard.adopted_by_sigma


@pytest.fixture(autouse=True)
def _adopted(monkeypatch):
    """#236 put an adoption gate in front of `wizard_status()` (an unadopted repo returns before
    doctor is asked). The tests in this file exercise what happens BEHIND that gate --
    classification, dismissal, the cache, the writers' own consent guard -- so they run as if
    adopted. The gate itself is tested with `REAL_ADOPTED` restored (end of this file)."""
    monkeypatch.setattr(setup_wizard, "adopted_by_sigma", lambda sdlc_dir: True)


def _fake_doctor_check(results):
    """A drop-in for setup_wizard's own doctor.check import — returns `results` verbatim,
    ignoring every argument, matching doctor.py's own real signature shape."""
    def _check(sdlc_dir=".sdlc", run=None, scheduled_tasks_dir=None, site_packages_dirs=None,
               cheap_only=False):
        return results
    return _check


def test_a_fully_healthy_repo_needs_no_wizard(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_wizard, "_doctor_check",
                         _fake_doctor_check([{"name": "project layer", "ok": True, "fix": ""}]))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"))
    assert status == {"needs_wizard": False, "steps": []}


def test_missing_sdlc_classifies_as_auto_fixable(monkeypatch, tmp_path):
    """Classification only: the fake doctor reports "project layer" failing inside an ADOPTED
    `.sdlc` (#236: an unadopted one returns before doctor is asked -- see the test below)."""
    (tmp_path / ".sdlc").mkdir()
    (tmp_path / ".sdlc" / "config.json").write_text("{}")
    monkeypatch.setattr(setup_wizard, "_doctor_check", _fake_doctor_check([
        {"name": "project layer", "ok": False, "fix": "run /sigma-init to scaffold .sdlc/"},
    ]))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"), allow_cache=False)
    assert status["needs_wizard"] is True
    assert status["steps"] == [{
        "name": "project layer",
        "fix": "run /sigma-init to scaffold .sdlc/",
        "mode": "auto_fixable",
        "degraded": "Without this, nothing in Sigma works at all -- no loop, no board, "
                    "no journal. Nothing runs until this exists.",
    }]


def test_gh_auth_classifies_as_human_command(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_wizard, "_doctor_check", _fake_doctor_check([
        {"name": "gh auth", "ok": False, "fix": "run: gh auth login"},
    ]))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"))
    assert status["steps"][0]["mode"] == "human_command"
    assert "browser" in status["steps"][0]["degraded"].lower() or \
           "cannot" in status["steps"][0]["degraded"].lower()


def test_board_marks_closed_items_done_classifies_as_guidance_only(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_wizard, "_doctor_check", _fake_doctor_check([
        {"name": "board marks closed items Done", "ok": False,
         "fix": "open the board's Workflows page, pick 'Item closed', set Status: Done, and Enable."},
    ]))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"))
    assert status["steps"][0]["mode"] == "guidance_only"


def test_stranded_done_card_check_uses_correct_name(monkeypatch, tmp_path):
    """Regression test: the exact check name from doctor.py:1032 must match _MODES key.
    A mismatched key silently falls through to _DEFAULT_MODE generic text instead of the
    specific authored guidance. This test prevents future accidental renames from regressing."""
    monkeypatch.setattr(setup_wizard, "_doctor_check", _fake_doctor_check([
        {"name": "no open issue stranded at board Done", "ok": False,
         "fix": "reopen the issue or manually move the card"},
    ]))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"))
    assert status["needs_wizard"] is True
    assert status["steps"] == [{
        "name": "no open issue stranded at board Done",
        "fix": "reopen the issue or manually move the card",
        "mode": "guidance_only",
        "degraded": "A reopened issue's card can get stuck showing Done even though the work isn't. "
                    "Cosmetic only -- the issue's real state is unaffected.",
    }]


def test_an_unclassified_check_never_becomes_a_wizard_step(monkeypatch, tmp_path):
    """_MODES IS AN ALLOW-LIST, and this is the test that pins it. doctor.check() has ~26 checks,
    only 6 of which are genuine first-run SETUP gaps; the rest are ongoing hygiene rows a mature,
    perfectly-working repo fails routinely ("team ledger initialized", "hand-off owner roster
    configured", names with live-changing counters embedded). Before this, any one of them set
    needs_wizard=True and told the agent to run the setup wizard "before anything else the user
    asked for" -- forever, on repos whose first run was months ago.

    This test previously asserted the OPPOSITE (needs_wizard True, mode guidance_only). That was
    the bug, not the contract: an unclassified check must now be ignored by wizard_status()
    entirely. `_classify`'s own conservative fallback still exists and is proved directly, in
    isolation, by the test immediately below."""
    monkeypatch.setattr(setup_wizard, "_doctor_check", _fake_doctor_check([
        {"name": "some brand new check nobody wrote a classification for yet",
         "ok": False, "fix": "do the thing"},
    ]))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"))
    assert status == {"needs_wizard": False, "steps": []}


def test_an_unclassified_check_does_not_suppress_a_real_one(monkeypatch, tmp_path):
    """The allow-list filters, it does not short-circuit: a genuine setup gap listed alongside
    noise must still surface, with only the noise dropped."""
    monkeypatch.setattr(setup_wizard, "_doctor_check", _fake_doctor_check([
        {"name": "team ledger initialized", "ok": False, "fix": "run /sigma-ledger"},
        {"name": "gh auth", "ok": False, "fix": "run: gh auth login"},
        {"name": "dependency markers: comments checked against body (3/12 open goal(s))",
         "ok": False, "fix": "re-file the dependency"},
    ]))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"))
    assert [s["name"] for s in status["steps"]] == ["gh auth"]
    assert status["needs_wizard"] is True


def test_classify_still_falls_back_safely_when_called_directly(tmp_path):
    """_classify()'s own defensive fallback, as a property of that function in isolation. It is
    unreachable from wizard_status() now that the loop filters on _MODES membership first, but it
    is cheap, harmless, and a real safety property for any other direct caller: an unrecognised
    name must return the most conservative mode, never raise."""
    mode, degraded = setup_wizard._classify("a name no one has ever classified")
    assert mode == "guidance_only"
    assert degraded == setup_wizard._DEFAULT_MODE[1]
    # and a classified name still resolves to its own authored entry, not the fallback
    assert setup_wizard._classify("gh auth")[0] == "human_command"


def test_north_star_filled_is_excluded_from_the_wizard_entirely(monkeypatch, tmp_path):
    """north-star completeness is a content/strategic-quality concern already owned by the
    plan-review gate's own alignment axis and /sigma-align (both already treat an incomplete
    north-star as a soft no-op, never a hard blocker) -- not a functional "nothing works" setup
    gap the way every check in _MODES is. It must never surface as a step and must never set
    needs_wizard, even when it is the ONLY failing check.

    The MECHANISM changed (the dedicated _EXCLUDED set is gone; the _MODES allow-list now excludes
    it automatically, since "north-star filled" was never a key there) but the OUTCOME this test
    exists to protect is identical, so the test is repointed rather than deleted -- exclusion must
    stay proven by a test, not merely implied by a table's contents."""
    monkeypatch.setattr(setup_wizard, "_doctor_check", _fake_doctor_check([
        {"name": "north-star filled", "ok": False,
         "fix": "Vision tier has no section at all — run /sigma-vision to fill the tiers."},
    ]))
    assert "north-star filled" not in setup_wizard._MODES
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"))
    assert status == {"needs_wizard": False, "steps": []}


def test_a_dismissed_check_is_excluded_from_needs_wizard(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_wizard, "_doctor_check", _fake_doctor_check([
        {"name": "gh project scope", "ok": False, "fix": "run: gh auth refresh -s project"},
    ]))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"), dismissed={"gh project scope"})
    assert status == {"needs_wizard": False, "steps": []}


def test_dismissed_state_round_trips_through_disk(tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    assert setup_wizard.read_dismissed(str(sdlc)) == set()
    setup_wizard.write_dismissed(str(sdlc), {"gh auth", "gh project scope"})
    assert setup_wizard.read_dismissed(str(sdlc)) == {"gh auth", "gh project scope"}
    # and it lives under state/, already covered by RUNTIME_IGNORES -- not a new ignore entry
    assert (sdlc / "state" / "setup-wizard-dismissed.json").exists()


def test_dismissed_file_that_is_corrupt_reads_as_empty_not_a_crash(tmp_path):
    sdlc = tmp_path / ".sdlc" / "state"
    sdlc.mkdir(parents=True)
    (sdlc / "setup-wizard-dismissed.json").write_text("{not json")
    assert setup_wizard.read_dismissed(str(tmp_path / ".sdlc")) == set()


# --- throttling (added at plan-review, finding A) -----------------------------------------------
# doctor.check() in GitHub mode makes real `gh` calls -- confirmed directly against doctor.py.
# Firing it on every SessionStart forever is a real, ongoing cost. These tests prove the cache
# short-circuits the CLEAN case only -- a real failure must never hide behind a stale cache.

def test_a_recent_clean_cache_skips_the_real_check_entirely(monkeypatch, tmp_path):
    """Proof by explosion: `_doctor_check` raises if it is ever called at all, so this test can
    only pass if the cache genuinely short-circuits before reaching it -- not merely "returns the
    right answer by coincidence."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()

    def explodes(**kwargs):
        raise AssertionError("doctor.check() was called despite a fresh clean cache")
    monkeypatch.setattr(setup_wizard, "_doctor_check", explodes)

    setup_wizard._write_cache(str(sdlc), needs_wizard=False, now=1000.0)
    status = setup_wizard.wizard_status(str(sdlc), dismissed=set(), now=1000.0 + 60)
    assert status == {"needs_wizard": False, "steps": []}


def test_an_expired_cache_runs_the_real_check_again(monkeypatch, tmp_path):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    calls = []
    def fake(**kwargs):
        calls.append(1)
        return [{"name": "project layer", "ok": True, "fix": ""}]
    monkeypatch.setattr(setup_wizard, "_doctor_check", fake)

    setup_wizard._write_cache(str(sdlc), needs_wizard=False, now=1000.0)
    # one full TTL (3600s) plus a second later -- must be treated as stale, not fresh
    setup_wizard.wizard_status(str(sdlc), dismissed=set(), now=1000.0 + 3601)
    assert calls == [1], "an expired cache did not trigger a real recheck"


def test_a_dirty_cache_never_suppresses_a_real_failure(monkeypatch, tmp_path):
    """THE ASSERTION THAT MATTERS MOST for this fix. The cache may only ever short-circuit a
    CLEAN result -- caching a failure and silently serving it as if it were fresh would hide a
    real, active problem exactly as long as its TTL, the opposite of what a health check is for."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    calls = []
    def fake(**kwargs):
        calls.append(1)
        return [{"name": "project layer", "ok": False, "fix": "run: gh auth login"}]
    monkeypatch.setattr(setup_wizard, "_doctor_check", fake)

    # a cache written moments ago recording needs_wizard=True must NOT be trusted -- re-checked
    setup_wizard._write_cache(str(sdlc), needs_wizard=True, now=1000.0)
    status = setup_wizard.wizard_status(str(sdlc), dismissed=set(), now=1000.0 + 5)
    assert calls == [1], "a cached FAILURE was trusted instead of rechecked"
    assert status["needs_wizard"] is True


def test_allow_cache_false_always_forces_a_real_check(monkeypatch, tmp_path):
    """The wizard's own in-conversation recheck-after-action step (skills/sigma-wizard/SKILL.md)
    must verify an action's effect LIVE, never against a cache that predates the action."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    calls = []
    def fake(**kwargs):
        calls.append(1)
        return [{"name": "project layer", "ok": True, "fix": ""}]
    monkeypatch.setattr(setup_wizard, "_doctor_check", fake)

    setup_wizard._write_cache(str(sdlc), needs_wizard=False, now=1000.0)
    setup_wizard.wizard_status(str(sdlc), dismissed=set(), now=1000.0 + 1, allow_cache=False)
    assert calls == [1], "allow_cache=False did not force a real check"


def test_a_backwards_clock_does_not_read_as_a_fresh_cache(monkeypatch, tmp_path):
    """A NEGATIVE age (the clock moved backwards since the cache was written -- a resumed laptop,
    an NTP step, a restored machine) must read as unusable, not as "fresher than fresh". Without
    the `0 <=` lower bound, `-86400 < 3600` is True and the stale clean cache is served for as
    long as the skew lasts, with no upper bound at all."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    calls = []
    def fake(**kwargs):
        calls.append(1)
        return [{"name": "project layer", "ok": False, "fix": "run: gh auth login"}]
    monkeypatch.setattr(setup_wizard, "_doctor_check", fake)

    setup_wizard._write_cache(str(sdlc), needs_wizard=False, now=100_000.0)
    status = setup_wizard.wizard_status(str(sdlc), dismissed=set(), now=100_000.0 - 86_400)
    assert calls == [1], "a cache written in the 'future' was trusted instead of rechecked"
    assert status["needs_wizard"] is True


def test_a_cache_with_wrongly_typed_fields_reads_as_no_cache(tmp_path):
    """Shaped-but-corrupt: valid JSON, both keys present, wrong types. The arithmetic in
    wizard_status() would raise TypeError on this; the hook's own outer catch would hide it, but
    the SKILL.md-documented direct call has no such net. It must degrade to "no cache"."""
    sdlc = tmp_path / ".sdlc" / "state"
    sdlc.mkdir(parents=True)
    for bad in ('{"checked_at": "yesterday", "needs_wizard": false}',
                '{"checked_at": 1000.0, "needs_wizard": "no"}',
                '{"checked_at": null, "needs_wizard": null}'):
        (sdlc / "setup-wizard-cache.json").write_text(bad)
        assert setup_wizard._read_cache(str(tmp_path / ".sdlc")) is None, bad


def test_a_well_formed_cache_still_reads_back(tmp_path):
    """The control for the test above -- proving the new isinstance guard rejects only bad data,
    and did not accidentally reject everything (which would silently disable the cache)."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    setup_wizard._write_cache(str(sdlc), needs_wizard=False, now=1234.0)
    assert setup_wizard._read_cache(str(sdlc)) == {"checked_at": 1234.0, "needs_wizard": False}


# --- consent: never write into a repo that has not adopted Sigma (final-review finding C1) ---
# The SessionStart hook fires in EVERY repo the user opens. An unconditional mkdir in the two
# writers below left an untracked .sdlc/state/setup-wizard-cache.json -- and a dirty `git status`
# -- in repos that had never heard of Sigma, before the user was asked anything at all.

def test_no_cache_is_written_into_a_repo_with_no_sdlc_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_wizard, "_doctor_check", _fake_doctor_check(
        [{"name": "project layer", "ok": False, "fix": "run /sigma-init to scaffold .sdlc/"}]))
    status = setup_wizard.wizard_status(str(tmp_path / ".sdlc"))
    assert status["needs_wizard"] is True                  # it still REPORTS, it just writes nothing
    assert list(tmp_path.iterdir()) == [], "the wizard created state in an unadopted repo"


def test_no_cache_is_written_for_a_clean_unadopted_repo_either(monkeypatch, tmp_path):
    """The clean path writes a cache too -- and is the far more common one for a stranger's repo,
    since with `.sdlc/` absent the only failing check is "project layer"."""
    monkeypatch.setattr(setup_wizard, "_doctor_check",
                        _fake_doctor_check([{"name": "project layer", "ok": True, "fix": ""}]))
    setup_wizard.wizard_status(str(tmp_path / ".sdlc"))
    assert list(tmp_path.iterdir()) == []


def test_write_dismissed_is_a_no_op_without_an_sdlc_dir(tmp_path):
    setup_wizard.write_dismissed(str(tmp_path / ".sdlc"), {"gh auth"})
    assert list(tmp_path.iterdir()) == []
    # and it stays fail-silent, exactly as before -- the caller can still proceed
    assert setup_wizard.read_dismissed(str(tmp_path / ".sdlc")) == set()


def test_both_writers_still_work_once_the_repo_has_adopted(tmp_path):
    """THE CONTROL for the three tests above. The guard must gate on adoption, not disable the
    feature: with `.sdlc/` present, both files are written exactly as before."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    setup_wizard.write_dismissed(str(sdlc), {"gh auth"})
    setup_wizard._write_cache(str(sdlc), needs_wizard=False, now=1000.0)
    assert (sdlc / "state" / "setup-wizard-dismissed.json").exists()
    assert (sdlc / "state" / "setup-wizard-cache.json").exists()


def test_verify_trap_is_a_first_run_wizard_step_against_the_real_doctor(tmp_path):
    """#228: enforce ON + empty command refuses every `done`, and an older /sigma-init shipped it as
    the default -- a first-run gap, so the wizard raises it. Run against the REAL doctor.check (no
    fake), so a rename on either side of the name coupling turns this red."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text(json.dumps(
        {"discovery": {"source": "local-goals"}, "verify": {"command": "", "enforce": True}}))
    status = setup_wizard.wizard_status(str(sdlc), allow_cache=False, dismissed=set())
    step = [s for s in status["steps"] if s["name"] == "verify command present (enforce is on)"]
    assert step and step[0]["mode"] == "human_command"
    assert "verify_detect.py detect ." in step[0]["fix"] and "confirm .sdlc <n> <id>" in step[0]["fix"]


def test_236_an_unadopted_repo_never_reaches_doctor(monkeypatch, tmp_path):
    """#186: the adoption gate runs BEFORE the (cheap) doctor sweep -- a stranger's repo costs
    nothing and hears nothing. Control: the same fake, adopted, does reach doctor."""
    monkeypatch.setattr(setup_wizard, "adopted_by_sigma", REAL_ADOPTED)
    calls = []

    def _check(**kwargs):
        calls.append(kwargs)
        return [{"name": "project layer", "ok": False, "fix": "gh auth login"}]
    monkeypatch.setattr(setup_wizard, "_doctor_check", _check)
    assert setup_wizard.wizard_status(str(tmp_path / ".sdlc")) == {"needs_wizard": False, "steps": []}
    assert calls == []
    (tmp_path / ".sdlc").mkdir()
    (tmp_path / ".sdlc" / "config.json").write_text("{}")
    assert setup_wizard.wizard_status(str(tmp_path / ".sdlc"), allow_cache=False)["needs_wizard"]
    assert len(calls) == 1


def test_236_adoption_is_config_json_and_no_other_owner(tmp_path):
    sdlc = tmp_path / ".sdlc"
    assert REAL_ADOPTED(str(sdlc)) is False                      # nothing there
    sdlc.mkdir()
    assert REAL_ADOPTED(str(sdlc)) is False                      # a bare directory is not adoption
    (sdlc / "config.json").write_text("{}")
    assert REAL_ADOPTED(str(sdlc)) is True
    (sdlc / "state").mkdir()
    (sdlc / "state" / "owner.json").write_text('{"plugin": "sigma"}')
    assert REAL_ADOPTED(str(sdlc)) is True
    (sdlc / "state" / "owner.json").write_text('{"plugin": "another-plugin"}')
    assert REAL_ADOPTED(str(sdlc)) is False
    (sdlc / "state" / "owner.json").write_text("{not json")
    assert REAL_ADOPTED(str(sdlc)) is True                       # unreadable marker: config decides


def test_236_an_interrupted_sigma_scaffold_still_nudges_init(monkeypatch, tmp_path):
    """Review of PR #286: `.sdlc/` that Sigma owns (state/owner.json, written by init BEFORE the
    scaffold) but with no config.json is an interrupted `/sigma-init`: say so. Another plugin's, or
    an ownerless bare `.sdlc/`, stays silent (the tests above)."""
    monkeypatch.setattr(setup_wizard, "adopted_by_sigma", REAL_ADOPTED)
    monkeypatch.setattr(setup_wizard, "_doctor_check", lambda **kw: [])
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    assert setup_wizard.wizard_status(str(sdlc), allow_cache=False)["needs_wizard"] is False
    (sdlc / "state" / "owner.json").write_text('{"schema": 1, "plugin": "sigma"}')
    status = setup_wizard.wizard_status(str(sdlc), allow_cache=False)
    assert status["needs_wizard"] is True
    assert [s["name"] for s in status["steps"]] == ["project layer"]
    assert "/sigma-init" in status["steps"][0]["fix"] and status["steps"][0]["degraded"]
    (sdlc / "state" / "owner.json").write_text('{"schema": 1, "plugin": "another-plugin"}')
    assert setup_wizard.wizard_status(str(sdlc), allow_cache=False)["needs_wizard"] is False


def test_236_the_interrupted_scaffold_fix_uses_the_shared_python_and_quoting(monkeypatch, tmp_path):
    """Review of PR #286: the fix text hardcoded `python3` and an unquoted path. It is built by
    verify_detect's python_command()/_q: a path with a space is quoted, and a host with only
    `python` (or Windows' `py`) gets that interpreter."""
    import shlex
    import shutil
    spaced = tmp_path / "my plugins" / "init_flow.py"
    monkeypatch.setattr(setup_wizard, "INIT_FLOW", spaced)
    fix = setup_wizard._interrupted_step()["fix"]
    if os.name != "nt":
        assert shlex.quote(str(spaced)) + " ." in fix, fix
        assert shlex.split(fix.split("Codex/Cursor: ", 1)[1].split(")", 1)[0])[1] == str(spaced)
    else:
        assert f'"{spaced}" .' in fix, fix
    monkeypatch.setattr(shutil, "which", lambda name: "/x/python" if name == "python" else None)
    assert "Codex/Cursor: python " in setup_wizard._interrupted_step()["fix"]
