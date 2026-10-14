"""feature_sync.py -- the pick-path registry sync (#1473, epic #1464, story #1427).

`feature_registry` (#1469) is the store and `feature_doc` (#1470) is the human-readable half. This
is the thing that RUNS: on every goal pick it records the goal under the unit its issue declares,
cross-checks what the registry claims against the branches that actually exist, and brings
`<name>.md` into line. The design's own sentence is the spec -- "live state comes from the remote;
history survives locally; NEITHER IS TRUSTED ALONE" -- and each half of it is a separate class of
test here.

THE ONE REQUIREMENT THAT IS NOT ABOUT CORRECTNESS-ON-ONE-INPUT. Recording a goal under its unit is
a read-modify-write of ONE shard, and #1469 measured a bare one losing 6-8 of 10 goal numbers across
10 real processes -- never corrupt, just silently absent, which is worse than a crash. Two goals on
one unit picked at once is not an exotic case: it is what a feature in flight IS, and what a
parallel drain produces by design. So the headline test here spawns REAL SUBPROCESSES -- not threads,
which share a GIL and an interpreter and would prove nothing about `os.replace` racing itself -- and
asserts that not one goal number is lost. A test that passed on threads and failed on processes is
exactly the shape of the bug being fixed.

WHAT IS ASSERTED, AND WHY IT IS ASSERTED THAT WAY:

  1. the four definition-of-done clauses, each on its own: idempotent append, unknown unit creates
     BOTH entry and file, a branch that no longer exists is reconciled AND surfaced, an emptied unit
     is closed and its file survives;
  2. IGNORANCE IS NEVER RECONCILED. A remote that could not be read reconciles nothing at all --
     "the branch list came back empty" and "there are no branches" are different facts, and reading
     the first as the second deletes every branch in the registry at once;
  3. the pick path adds NO second issue read. Asserted against the recorded command list of a real
     `work.start()`, because the only honest form of "we did not ask twice" is counting the asks;
  4. the pick path never writes `index.json`. Asserted on the file's BYTES, because the whole point
     of the shard layout is that concurrent picks stop rewriting one shared file;
  5. nothing here can break a pick. A sync that raises must cost the registry, never the goal.

WRITTEN AGAINST A MUTATION RUN, with `tests/test_feature_registry.py`'s discipline inherited whole:
`_mod_with` asserts its target exists and replaces EVERY occurrence (this module quotes its own
rules in docstrings above the lines implementing them), and every mutation test asserts the mutant's
BEHAVIOUR differs rather than merely that the suite went red.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
P = SCRIPTS / "feature_sync.py"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _mod():
    return _load("feature_sync")


def _registry():
    return _load("feature_registry")


def _doc():
    return _load("feature_doc")


def _ledger():
    return _load("ledger")


def _mod_with(old, new):
    """The module rebuilt with a source substitution applied.

    Both of `tests/test_feature_registry.py::_mod_with`'s guards, for both of its reasons: the
    target is asserted to exist, so a drifted `old` cannot report "no survivors" over untouched
    code, and EVERY occurrence is replaced, because this module quotes its own rules in the
    docstrings above the lines implementing them."""
    src = P.read_text(encoding="utf-8")
    assert old in src, "mutation target has drifted out of the source: %r" % (old,)
    namespace = {"__name__": "feature_sync_variant", "__file__": str(P)}
    exec(compile(src.replace(old, new), str(P), "exec"), namespace)          # noqa: S102 - test-only
    return types.SimpleNamespace(**namespace)


REPO = "org/repo"
CONFIG = {"work": {"enabled": True, "remote": "origin"},
          "discovery": {"source": "github", "github": {"repo": REPO}}}


def _entry(**over):
    """A full, canonical entry -- every field present, so a test that changes ONE field is testing
    that field and nothing else."""
    base = {"title": "Manifest duration contract", "owner": "@unit-owner", "open": True,
            "parent": None, "tracking_issue": "org/repo#3100", "priority": None,
            "repos": {REPO: {"branch": "feature/int-contract", "owner": "@unit-owner",
                             "authorized": True, "goals": [2871]}}}
    base.update(over)
    return base


def _sdlc(tmp_path, adopted=True, ledger_on=False):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True, exist_ok=True)
    settings = {"enabled": bool(ledger_on), "actor": "tester"}
    sdlc.joinpath("config.json").write_text(
        json.dumps(dict(CONFIG, ledger=settings)), encoding="utf-8")
    if adopted:
        (sdlc / "features").mkdir(parents=True, exist_ok=True)
    return sdlc


def _seed(sdlc, name, entry):
    """Put a unit in the registry the way a real repo would carry one -- through the write surface."""
    return _registry().write_unit(sdlc / "features", name, entry)


def _runner(branches=("feature/int-contract",), handlers=(), url="git@github.com:org/repo.git"):
    """The `(cwd, argv) -> stdout` runner shape `work.py` injects everywhere. `branches` is the
    live set `git ls-remote` reports; `None` makes that call raise, which is the ONLY way a caller
    is allowed to learn "the remote could not be read"."""
    calls = []

    def run(cwd, argv):
        line = " ".join(str(a) for a in argv)
        calls.append(line)
        for token, resp in handlers:
            if token in line:
                if isinstance(resp, Exception):
                    raise resp
                return resp(line) if callable(resp) else resp
        if "ls-remote" in line:
            if branches is None:
                raise RuntimeError("fatal: could not read from remote repository")
            return "\n".join("%040d\trefs/heads/%s" % (i, b) for i, b in enumerate(branches))
        if "remote get-url" in line:
            return url
        return ""

    run.calls = calls
    return run


def _sync(sdlc, goal="2879", unit="int-contract", run=None, config=None, **kw):
    return _mod().sync_at_pick(str(sdlc), config or CONFIG, goal, unit,
                               run=run or _runner(), cwd=str(sdlc.parent), **kw)


def _read(sdlc):
    return _registry().read(sdlc / "features")


def _goals(sdlc, unit, repo=REPO):
    entry = _read(sdlc).get(unit) or {}
    return ((entry.get("repos") or {}).get(repo) or {}).get("goals") or []


# --------------------------------------------------------------------------- 1. the four DoD clauses


def test_a_pick_on_a_known_unit_records_its_goal_under_it(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    report = _sync(sdlc, goal="2879")
    assert report["outcome"] == _mod().SYNCED
    assert _goals(sdlc, "int-contract") == [2871, 2879]
    assert report["recorded"] is True and report["created"] is False


def test_a_re_pick_does_not_duplicate_the_goal_number(tmp_path):
    """Idempotence, asserted on the BYTES as well as the list: a second pick that rewrites the same
    content is still a write, and a registry that churns a file on every pass makes every pick a
    diff in a repo that commits it."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    _sync(sdlc, goal="2879")
    shard = _registry().unit_path(sdlc / "features", "int-contract")
    before = shard.read_bytes()
    report = _sync(sdlc, goal="2879")
    assert _goals(sdlc, "int-contract") == [2871, 2879]
    assert shard.read_bytes() == before
    assert report["changed"] == []


def test_a_pick_on_an_unknown_unit_creates_both_the_entry_and_the_file(tmp_path):
    sdlc = _sdlc(tmp_path)
    report = _sync(sdlc, goal="2879", unit="voice-interview",
                   run=_runner(branches=("feature/voice-interview",)))
    entry = _read(sdlc)["voice-interview"]
    assert entry["repos"][REPO]["goals"] == [2879]
    assert entry["repos"][REPO]["branch"] == "feature/voice-interview"
    assert (sdlc / "features" / "voice-interview.md").is_file()
    assert report["created"] is True


def test_an_entry_naming_a_branch_that_no_longer_exists_is_reconciled_and_surfaced(tmp_path):
    """DoD 3. Both halves matter and they are separate assertions: the record stops CLAIMING a
    branch that is not there (reconciled), and the fact is reported rather than absorbed
    (surfaced). Silently trusting it is what the whole `neither is trusted alone` sentence rules
    out."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    report = _sync(sdlc, goal="2879", run=_runner(branches=()))
    assert _read(sdlc)["int-contract"]["repos"][REPO]["branch"] is None
    kinds = [d["kind"] for d in report["divergences"]]
    assert _mod().BRANCH_MISSING in kinds
    assert any(d["detail"] == "feature/int-contract" for d in report["divergences"])


def test_a_unit_whose_branch_is_gone_keeps_every_goal_it_recorded(tmp_path):
    """The registry's whole purpose in one assertion: the branch is deleted, and what belonged to
    that unit is still knowable."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    _sync(sdlc, goal="2879", run=_runner(branches=()))
    assert _goals(sdlc, "int-contract") == [2871, 2879]


def test_an_emptied_unit_is_marked_closed_and_its_file_survives(tmp_path):
    """DoD 4, and `open: false` NOT removal -- the registry is a record, not a cache. The unit here
    is not the one being picked: a unit that just recorded a goal is by definition not empty, so the
    only honest way to reach rule 5 is the reconcile pass over everything else."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    _seed(sdlc, "old-thing", _entry(title="Old", repos={REPO: {"branch": "feature/old-thing",
                                                              "goals": []}}))
    _doc().sync(sdlc / "features", "old-thing", _read(sdlc)["old-thing"])
    report = _sync(sdlc, goal="2879")
    entry = _read(sdlc)["old-thing"]
    assert entry["open"] is False
    assert (sdlc / "features" / "old-thing.md").is_file()
    assert (sdlc / "features" / "units" / "old-thing.json").is_file()
    assert _mod().CLOSED in [d["kind"] for d in report["divergences"]]


def test_a_unit_with_a_live_branch_is_never_closed(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "old-thing", _entry(repos={REPO: {"branch": "feature/old-thing", "goals": []}}))
    _sync(sdlc, goal="2879", run=_runner(branches=("feature/int-contract", "feature/old-thing")))
    assert _read(sdlc)["old-thing"]["open"] is True


def test_a_unit_that_still_remembers_a_goal_is_never_closed(tmp_path):
    """"No branch AND no open goals" is an AND. A unit whose branch merged away but whose goals are
    recorded is exactly the thing the backup exists to preserve, and closing it would be the
    registry forgetting the one fact it was built to keep."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "old-thing", _entry(repos={REPO: {"branch": None, "goals": [11]}}))
    _sync(sdlc, goal="2879", run=_runner(branches=()))
    assert _read(sdlc)["old-thing"]["open"] is True


# --------------------------------------------------------------------------- 2. never trusted alone


def test_a_remote_that_could_not_be_read_reconciles_nothing(tmp_path):
    """The single most destructive available bug: an unreadable remote read as an EMPTY live set
    deletes every branch in the registry at once and closes every unit, in one pass, silently."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    _seed(sdlc, "old-thing", _entry(repos={REPO: {"branch": "feature/old-thing", "goals": []}}))
    report = _sync(sdlc, goal="2879", run=_runner(branches=None))
    assert report["live"] is False
    assert _read(sdlc)["int-contract"]["repos"][REPO]["branch"] == "feature/int-contract"
    assert _read(sdlc)["old-thing"]["open"] is True
    assert not [d for d in report["divergences"] if d["kind"] == _mod().BRANCH_MISSING]


def test_an_empty_live_set_that_was_actually_read_is_a_real_answer(tmp_path):
    """The converse, and the reason `None` and `frozenset()` cannot be collapsed: a remote with no
    feature branches at all answered the question, and its answer is "none of them exist"."""
    sdlc = _sdlc(tmp_path)
    m = _mod()
    assert m.live_branches(_runner(branches=()), ".", "origin") == (frozenset(), "")
    branches, why = m.live_branches(_runner(branches=None), ".", "origin")
    assert branches is None
    assert "could not read from remote repository" in why, (
        "the REASON has to come back too: a caller handed only `None` has no way to tell an "
        "operator WHY nothing was cross-checked")


def test_a_recorded_branch_outside_the_feature_namespace_is_never_judged(tmp_path):
    """The live set is fetched with a `feature/*` pattern, so it CANNOT contain `main`. Judging a
    recorded `main` against it would report every such entry as a deleted branch -- a conclusion
    drawn from a question that was never asked."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry(repos={REPO: {"branch": "main", "goals": [1]}}))
    report = _sync(sdlc, goal="2879", run=_runner(branches=("feature/int-contract",)))
    assert _read(sdlc)["int-contract"]["repos"][REPO]["branch"] == "main"
    assert not [d for d in report["divergences"] if d["kind"] == _mod().BRANCH_MISSING]


def test_only_this_repos_half_of_a_cross_repo_entry_is_reconciled(tmp_path):
    """§7.1: every participating repo holds the WHOLE entry, including branches on remotes this
    checkout cannot see. `git ls-remote` answered about ONE remote, so a sibling repo's branch is
    unmeasured -- and unmeasured is never "gone"."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry(repos={
        REPO: {"branch": "feature/int-contract", "goals": [1]},
        "org/other": {"branch": "feature/int-contract", "goals": [2]}}))
    _sync(sdlc, goal="2879", run=_runner(branches=()))
    got = _read(sdlc)["int-contract"]["repos"]
    assert got[REPO]["branch"] is None
    assert got["org/other"]["branch"] == "feature/int-contract"


def test_a_declared_unit_whose_branch_does_not_exist_yet_is_recorded_and_flagged(tmp_path):
    """The first goal of a new unit: the branch has not been cut yet, so the entry must not claim
    one -- and the absence is exactly what `work.start()`'s own `git fetch` is about to fail on, so
    saying it here is what turns that failure into a diagnosis."""
    sdlc = _sdlc(tmp_path)
    report = _sync(sdlc, goal="2879", unit="brand-new", run=_runner(branches=()))
    assert _read(sdlc)["brand-new"]["repos"][REPO]["branch"] is None
    assert _mod().BRANCH_ABSENT in [d["kind"] for d in report["divergences"]]
    assert _goals(sdlc, "brand-new") == [2879]


def test_a_goal_picked_onto_a_closed_unit_is_surfaced_not_silently_reopened(tmp_path):
    """`open: false` is somebody's statement that the unit is finished. A pick against it is a
    disagreement between two humans, and the registry's job is to report that, not to adjudicate
    it by overwriting one of them."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry(open=False))
    report = _sync(sdlc, goal="2879")
    assert _read(sdlc)["int-contract"]["open"] is False
    assert _goals(sdlc, "int-contract") == [2871, 2879]
    assert _mod().PICKED_WHILE_CLOSED in [d["kind"] for d in report["divergences"]]


# --------------------------------------------------------------------------- 3. concurrency

#: The driver a real, separate OS process runs. Written to disk and executed with `sys.executable`
#: rather than forked, deliberately: a fork shares the parent's already-imported modules and its
#: open file descriptors -- including any lock fd -- so a fork-based "process test" can pass for
#: reasons that have nothing to do with what a second `python3 work.py start` would do.
_DRIVER = '''
import importlib.util, os, pathlib, random, sys, time
scripts, sdlc, unit, goals, mode = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], \\
    [int(g) for g in sys.argv[4].split(",")], sys.argv[5]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, scripts / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


sync, registry = _load("feature_sync"), _load("feature_registry")
features = pathlib.Path(sdlc) / "features"


def amend(goal):
    def mutate(entry):
        repo = entry["repos"].setdefault("org/repo", {"branch": None, "owner": None,
                                                      "authorized": False, "goals": []})
        repo["goals"].append(goal)
    if mode == "bare":
        entry = registry.normalise_entry(registry.read(features).get(unit) or {})
        mutate(entry)
        registry.write_unit(features, unit, entry)
    else:
        sync.amend(sdlc, unit, mutate)


random.seed(os.getpid())
for g in goals:
    time.sleep(random.uniform(0, 0.004))
    amend(g)
'''


def _race(tmp_path, mode, writers=10, each=4):
    """Run `writers` REAL processes, each recording `each` goal numbers onto ONE unit.
    -> (expected, landed) counts."""
    sdlc = _sdlc(tmp_path)
    driver = tmp_path / "race_driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    expected, procs = [], []
    for w in range(writers):
        mine = [1000 + w * each + i for i in range(each)]
        expected += mine
        procs.append(subprocess.Popen(
            [sys.executable, str(driver), str(SCRIPTS), str(sdlc), "alpha",
             ",".join(str(g) for g in mine), mode],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1")))
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err.decode("utf-8", "replace")
    landed = set(_goals(sdlc, "alpha"))
    return sorted(expected), landed


def test_ten_concurrent_processes_on_one_unit_lose_no_goal_number(tmp_path):
    """THE test this goal exists for. 10 real processes x 4 goals each on ONE unit -- the shard a
    parallel drain contends on by design. Measured before the fix, at this exact shape: 17-19 of 40
    lost, every run. A lost goal number is not cosmetic; it is the registry failing at the one job
    it has, which is knowing what belonged to a unit after its branch is deleted."""
    expected, landed = _race(tmp_path, "locked")
    assert sorted(landed) == expected, "lost: %s" % sorted(set(expected) - landed)


def test_two_units_never_contend_because_the_lock_is_per_unit(tmp_path):
    """Same-unit serialisation must not become whole-registry serialisation: different units were
    already structurally isolated by the shard layout (#1469) and must stay that way."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    assert m.lock_path(str(sdlc), "alpha") != m.lock_path(str(sdlc), "beta")
    held = m._acquire(m.lock_path(str(sdlc), "alpha"), timeout=1.0)
    assert held is not None
    try:
        other = m._acquire(m.lock_path(str(sdlc), "beta"), timeout=1.0)
        assert other is not None
        os.close(other)
    finally:
        os.close(held)


def test_the_lock_never_lands_in_the_committed_registry(tmp_path):
    """`.sdlc/features/` is deliberately NOT gitignored -- a gitignored backup is not a backup -- so
    a lock file beside the shards would be a permanent untracked-file in every adopter's status.
    `.sdlc/state/` is what `setup.RUNTIME_IGNORES` already covers."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    lock = m.lock_path(str(sdlc), "alpha")
    assert lock.parent == pathlib.Path(sdlc) / "state" / m.LOCK_DIRNAME
    _sync(sdlc)
    assert not [p for p in (sdlc / "features").rglob("*") if p.suffix == m.LOCK_SUFFIX]


def test_a_platform_with_no_flock_still_records_the_goal(tmp_path, monkeypatch):
    """FAIL-OPEN, and reported. `loop.py`'s own claim lock takes the same posture on `fcntl is
    None`. Losing the lock must cost serialisation, never the write -- but the caller is told, so
    "it was recorded" is never confused with "it was recorded safely"."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    monkeypatch.setattr(m, "fcntl", None)
    report = m.sync_at_pick(str(sdlc), CONFIG, "2879", "int-contract",
                            run=_runner(branches=("feature/int-contract",)), cwd=str(tmp_path))
    assert _goals(sdlc, "int-contract") == [2879]
    assert report["serialised"] is False


def test_a_wedged_lock_holder_does_not_hang_the_pick(tmp_path):
    """A bounded wait, not a blocking `flock`. A holder that never releases must cost this pick its
    serialisation and nothing else -- a pick that hangs forever on a lock is a worse outcome than
    the data loss the lock exists to prevent."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    held = m._acquire(m.lock_path(str(sdlc), "int-contract"), timeout=1.0)
    assert held is not None
    try:
        report = m.amend(str(sdlc), "int-contract", lambda e: e["repos"].setdefault(
            REPO, {"goals": [7]}), timeout=0.05)
    finally:
        os.close(held)
    assert report["serialised"] is False
    assert _goals(sdlc, "int-contract") == [7]


def test_a_write_that_did_not_land_is_retried(tmp_path, monkeypatch):
    """The backstop for the fail-open case above: the write is verified by reading the shard back
    while the lock is still held, so a clobber by an unserialised writer is noticed rather than
    reported as success."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    real, seen = _registry().write_unit, []

    def clobber(features_dir, name, entry):
        path = real(features_dir, name, entry)
        seen.append(name)
        if len(seen) == 1:                        # somebody else's unserialised write, right after ours
            real(features_dir, name, _entry(repos={REPO: {"branch": None, "goals": [999]}}))
        return path

    monkeypatch.setattr(m.registry, "write_unit", clobber)
    report = m.amend(str(sdlc), "int-contract",
                     lambda e: e["repos"].setdefault(REPO, {"goals": []})["goals"].append(7))
    assert report["attempts"] == 2
    assert 7 in _goals(sdlc, "int-contract")


def test_the_retry_is_bounded_so_a_hostile_writer_cannot_spin_the_pick_forever(tmp_path,
                                                                              monkeypatch):
    m, sdlc = _mod(), _sdlc(tmp_path)
    real = _registry().write_unit
    calls = []

    def always_clobber(features_dir, name, entry):
        real(features_dir, name, entry)
        calls.append(name)
        return real(features_dir, name, _entry(repos={REPO: {"branch": None, "goals": [999]}}))

    monkeypatch.setattr(m.registry, "write_unit", always_clobber)
    report = m.amend(str(sdlc), "int-contract",
                     lambda e: e["repos"].setdefault(REPO, {"goals": []})["goals"].append(7))
    assert report["attempts"] == m.WRITE_ATTEMPTS
    assert report["landed"] is False


# --------------------------------------------------------------------------- 3b. priority (#2261)


def test_amend_is_the_write_surface_for_a_units_priority(tmp_path):
    """#2261. A unit's priority is set through `amend` and through nothing else: the read-modify-
    write happens under the unit's own lock, and the WHOLE entry goes back, which is what makes a
    priority written here survive a concurrent pick recording a goal on the same unit.

    No second write surface is added for it, deliberately. `write_unit` is already documented as
    replacing rather than merging, so a caller that set a priority by writing a one-field entry
    would erase the unit's repos, goals and tracking issue -- the exact failure
    `test_a_shard_written_as_a_delta_erases_the_rest_of_the_entry` measures over in the registry's
    own suite."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())

    def set_priority(entry):
        entry["priority"] = "P0"

    report = m.amend(str(sdlc), "int-contract", set_priority)
    assert report["changed"] is True and report["landed"] is True
    assert report["entry"]["priority"] == "P0"
    assert _read(sdlc)["int-contract"]["priority"] == "P0"
    # On disk, in the shard the write surface owns -- not merely in the report.
    shard = json.loads(_registry().unit_path(sdlc / "features", "int-contract")
                       .read_text(encoding="utf-8"))
    assert shard["features"]["int-contract"]["priority"] == "P0"


def test_recording_a_goal_does_not_disturb_a_priority_somebody_else_set(tmp_path):
    """The other direction, and the one a delta write would break. A pick records a goal and knows
    nothing about priority; because `amend` hands `mutate` the whole normalised entry and writes the
    whole thing back, the field it never mentions comes through untouched."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry(priority="P1"))
    _sync(sdlc, goal="2879")
    entry = _read(sdlc)["int-contract"]
    assert entry["priority"] == "P1"
    assert entry["repos"][REPO]["goals"] == [2871, 2879]


def test_amend_reports_no_change_when_the_priority_written_is_the_one_already_there(tmp_path):
    """`bump-priority` is idempotent and human-invoked, so re-stating a unit's current priority is
    an ordinary gesture rather than an odd one. NOTHING IS WRITTEN WHEN NOTHING CHANGED is what
    keeps that gesture out of the git history of a directory that is committed on purpose."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry(priority="P1"))
    before = _registry().unit_path(sdlc / "features", "int-contract").read_bytes()
    report = m.amend(str(sdlc), "int-contract", lambda e: e.__setitem__("priority", "P1"))
    assert report["changed"] is False and report["written"] is True
    assert _registry().unit_path(sdlc / "features", "int-contract").read_bytes() == before
    # And it is unchanged BECAUSE it already said P1, not because nobody records a priority at all
    # -- without this line the test passes just as readily on a build that drops the field entirely.
    assert _read(sdlc)["int-contract"]["priority"] == "P1"


# --------------------------------------------------------------------------- 4. no second read


def test_the_pick_path_reads_the_issue_exactly_once(tmp_path):
    """REQUIREMENT, not a nicety. `work.start()` receives only a goal id, so #1467 added exactly one
    `gh api repos/<slug>/issues/<n>`; #1472 then DELETED its own reader to delegate to that one,
    because two readers of one declaration was a real bug on this epic. This sync gets the unit
    handed to it in-process and asks nobody."""
    work = _load("work")
    sdlc = _sdlc(tmp_path)
    run = _runner(branches=("feature/billing",), handlers=[
        ("api repos/", json.dumps({"number": 1473, "title": "t", "body": "Feature: billing",
                                   "labels": []}))])
    work.start(str(sdlc), CONFIG, "1473", run=run)
    assert [c for c in run.calls if "issues/" in c] == ["gh api repos/org/repo/issues/1473"]
    assert [c for c in run.calls if c.startswith("gh ")] == ["gh api repos/org/repo/issues/1473"]


def test_start_records_the_goal_under_the_unit_it_resolved(tmp_path):
    """The wiring, end to end: the unit `start()` bases the worktree on is the unit the registry
    records the goal under. One resolution, one answer."""
    work = _load("work")
    sdlc = _sdlc(tmp_path)
    run = _runner(branches=("feature/billing",), handlers=[
        ("api repos/", json.dumps({"number": 1473, "title": "t", "body": "Feature: billing",
                                   "labels": []}))])
    work.start(str(sdlc), CONFIG, "1473", run=run)
    assert work._record(str(sdlc), "1473")["base"] == "feature/billing"
    assert _goals(sdlc, "billing") == [1473]


def test_start_syncs_before_it_fetches_so_a_missing_branch_is_still_recorded(tmp_path):
    """Order is load-bearing. A brand-new unit has no branch yet, and `start()`'s `git fetch` fails
    closed on exactly that -- correctly. If the sync ran after it, the first goal of every new unit
    would be the one goal the registry never learned about."""
    work = _load("work")
    sdlc = _sdlc(tmp_path)
    run = _runner(branches=(), handlers=[
        ("api repos/", json.dumps({"number": 1473, "title": "t", "body": "Feature: billing",
                                   "labels": []})),
        ("git fetch", RuntimeError("couldn't find remote ref feature/billing"))])
    with pytest.raises(RuntimeError):
        work.start(str(sdlc), CONFIG, "1473", run=run)
    assert _goals(sdlc, "billing") == [1473]


def test_a_project_that_has_not_adopted_the_registry_pays_nothing(tmp_path):
    """The cheapest step is the opt-out, exactly as `cross_repo.check_at_pick` does it: no
    `.sdlc/features/` means no reconcile, no `ls-remote`, and no new failure mode for an adopter who
    never asked for the branching model."""
    work = _load("work")
    sdlc = _sdlc(tmp_path, adopted=False)
    run = _runner(handlers=[("api repos/", json.dumps(
        {"number": 1473, "title": "t", "body": "Feature: billing", "labels": []}))])
    work.start(str(sdlc), CONFIG, "1473", run=run)
    assert not any("ls-remote" in c for c in run.calls)
    assert not (sdlc / "features").exists()


def test_a_sync_that_blows_up_never_costs_the_pick_its_goal(tmp_path, monkeypatch):
    """A registry is a record. Losing one is bad; losing the goal because the record could not be
    written is worse, and is the failure mode every fail-open in this kit exists to prevent."""
    work = _load("work")
    sdlc = _sdlc(tmp_path)
    run = _runner(branches=("feature/billing",), handlers=[
        ("api repos/", json.dumps({"number": 1473, "title": "t", "body": "Feature: billing",
                                   "labels": []}))])
    monkeypatch.setattr(work, "_feature_sync", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = work.start(str(sdlc), CONFIG, "1473", run=run)
    assert "worktree" in out
    assert work._record(str(sdlc), "1473")["base"] == "feature/billing"


# --------------------------------------------------------------------------- 5. the chart sheet


def test_the_pick_path_never_writes_the_chart_sheet(tmp_path):
    """The layout amendment moved the write surface off `index.json` precisely so concurrent picks
    stop rewriting one shared file. Folding it back in on every pick would undo that, and would put
    a merge conflict on the one committed file that exists to be a backup."""
    sdlc = _sdlc(tmp_path)
    _registry().write_index(sdlc / "features", {"int-contract": _entry()})
    before = _registry().index_path(sdlc / "features").read_bytes()
    _sync(sdlc, goal="2879")
    assert _registry().index_path(sdlc / "features").read_bytes() == before
    assert _goals(sdlc, "int-contract") == [2871, 2879]


def test_a_unit_that_exists_only_in_the_chart_sheet_is_amended_not_replaced(tmp_path):
    """A fresh clone carries `index.json` and no shards at all. Recording a goal has to start from
    the sheet's entry, or the first pick after a clone erases the unit's whole history."""
    sdlc = _sdlc(tmp_path)
    _registry().write_index(sdlc / "features", {"int-contract": _entry()})
    _sync(sdlc, goal="2879")
    entry = _read(sdlc)["int-contract"]
    assert entry["repos"][REPO]["goals"] == [2871, 2879]
    assert entry["title"] == "Manifest duration contract"
    assert entry["tracking_issue"] == "org/repo#3100"


def test_the_fold_verb_materialises_the_sheet_and_keeps_every_shard(tmp_path):
    """Folding is a deliberate materialisation, off the pick path. It does NOT remove the shards it
    folded: deleting a shard right after reading it would drop any write a concurrent process made
    in between, and `read`'s shard-wins union is already correct with both present."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    _mod().fold(str(sdlc))
    sheet = json.loads(_registry().index_path(sdlc / "features").read_text(encoding="utf-8"))
    assert sheet["features"]["int-contract"]["repos"][REPO]["goals"] == [2871]
    assert (sdlc / "features" / "units" / "int-contract.json").is_file()


# --------------------------------------------------------------------------- 6. the .md half


def test_the_managed_block_is_written_from_the_entry_the_sync_just_wrote(tmp_path):
    """#1470 built `sync()` as a pure function of (dir, name, entry) and left it unwired. This is
    the wire: the block a human reads is regenerated from the registry on the same pass that
    changed it, so the two can never be a pick apart."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    _sync(sdlc, goal="2879")
    text = (sdlc / "features" / "int-contract.md").read_text(encoding="utf-8")
    assert "2879" in text and _doc().BEGIN_OPEN in text


def test_a_hand_edit_inside_the_managed_block_is_reported_by_the_pick(tmp_path):
    """Rule 3 reaches the caller. `feature_doc` already refuses to swallow it; the sync's job is to
    not swallow `feature_doc`."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    _sync(sdlc, goal="2879")
    path = sdlc / "features" / "int-contract.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Manifest", "MEDDLED"),
                    encoding="utf-8")
    report = _sync(sdlc, goal="2880")
    assert report["doc"]["int-contract"] == _doc().OVERWRITTEN
    assert _mod().BLOCK_DIVERGED in [d["kind"] for d in report["divergences"]]


def test_the_human_region_of_the_file_survives_a_pick(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    _sync(sdlc, goal="2879")
    path = sdlc / "features" / "int-contract.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n## Notes\nmine, and staying\n",
                    encoding="utf-8")
    _sync(sdlc, goal="2880")
    assert "mine, and staying" in path.read_text(encoding="utf-8")


def test_a_doc_write_that_fails_still_leaves_the_registry_written(tmp_path, monkeypatch):
    """The `.md` is the readable projection; the shard is the record. Losing the projection must
    never lose the record."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    monkeypatch.setattr(m.doc, "sync", lambda *a, **kw: (_ for _ in ()).throw(OSError("read-only")))
    report = m.sync_at_pick(str(sdlc), CONFIG, "2879", "int-contract",
                            run=_runner(), cwd=str(tmp_path))
    assert _goals(sdlc, "int-contract") == [2871, 2879]
    assert report["outcome"] == m.SYNCED


# --------------------------------------------------------------------------- 7. the repo key


def test_the_repo_key_is_the_slug_the_goal_number_came_from(tmp_path):
    """`discovery.github.repo` is where the issue was read from, so it is the repo the goal belongs
    to. Anything else would file the goal under a repo it is not in."""
    m = _mod()
    assert m.repo_slug(CONFIG, _runner(), ".", "origin") == REPO


def test_an_unset_repo_falls_back_to_the_remote_url_never_to_ghs_placeholder(tmp_path):
    """The template ships `repo` empty, so this is the real install path, not a theoretical one --
    and `{owner}/{repo}` is a `gh` placeholder, which would become a literal registry key and a
    literal filename if it were ever trusted."""
    m = _mod()
    config = {"discovery": {"source": "github", "github": {"repo": ""}}}
    assert m.repo_slug(config, _runner(), ".", "origin") == REPO
    assert m.repo_slug(config, _runner(url="github.com-ag:org/repo.git"), ".", "origin") == REPO
    assert m.repo_slug(config, _runner(url="https://github.com/org/repo"), ".", "origin") == REPO
    assert m.repo_slug({"discovery": {"github": {"repo": "{owner}/{repo}"}}},
                       _runner(url=""), ".", "origin") is None


def test_a_pick_with_no_resolvable_repo_records_nothing_and_says_so(tmp_path):
    """`repos.<slug>.goals` is the only place a goal number can live. With no slug there is no
    honest place to put it, and inventing one is worse than reporting the gap."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    report = m.sync_at_pick(str(sdlc), {"discovery": {"github": {"repo": ""}}}, "2879",
                            "int-contract", run=_runner(url=""), cwd=str(tmp_path))
    assert report["repo"] is None and report["recorded"] is False
    assert m.NO_REPO in [d["kind"] for d in report["divergences"]]


# --------------------------------------------------------------------------- 8. never raises


def test_an_illegal_unit_name_is_refused_without_taking_the_pick_down(tmp_path):
    """CROSS-MODULE CLASS IDENTITY. `_load` hands every loader its own module object, so the
    `InvalidUnitName` this module's `feature_registry` copy raises is a DIFFERENT class from the one
    `feature_doc`'s copy raises. Catching either by identity misses the other; both are
    `ValueError` subclasses, which is the form that holds across instances."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    report = m.sync_at_pick(str(sdlc), CONFIG, "2879", "../../etc/passwd",
                            run=_runner(), cwd=str(tmp_path))
    assert report["outcome"] == m.FAILED
    assert not list((sdlc / "features").glob("**/*passwd*"))


def test_the_registrys_exception_and_the_docs_are_two_classes_and_both_are_caught(tmp_path):
    """The gotcha stated as an assertion rather than a comment: if these two were the same object,
    the broad-form catch below would be untested belt over a braces that never slips."""
    assert _registry().InvalidUnitName is not _doc().InvalidUnitName
    assert issubclass(_registry().InvalidUnitName, ValueError)
    assert issubclass(_doc().InvalidUnitName, ValueError)


def test_a_corrupt_shard_costs_its_own_unit_and_not_the_pick(tmp_path):
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    (sdlc / "features" / "units" / "broken.json").write_text("{not json", encoding="utf-8")
    report = _sync(sdlc, goal="2879")
    assert report["outcome"] == _mod().SYNCED
    assert _goals(sdlc, "int-contract") == [2871, 2879]


def test_an_unwritable_registry_is_reported_rather_than_raised(tmp_path, monkeypatch):
    m, sdlc = _mod(), _sdlc(tmp_path)
    monkeypatch.setattr(m.registry, "write_unit",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    report = m.sync_at_pick(str(sdlc), CONFIG, "2879", "int-contract",
                            run=_runner(), cwd=str(tmp_path))
    assert report["outcome"] == m.FAILED and "disk full" in report["why"]


def test_a_pick_that_declares_no_unit_still_reconciles(tmp_path):
    """"local and remote reconcile on every pass" is about the PASS, not about the unit. A goal
    with no declaration still runs one, which is what keeps a registry from drifting simply because
    nobody happened to pick the unit that went stale."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "old-thing", _entry(repos={REPO: {"branch": "feature/old-thing", "goals": []}}))
    report = _sync(sdlc, goal="2879", unit=None, run=_runner(branches=()))
    assert report["recorded"] is False
    assert _read(sdlc)["old-thing"]["open"] is False


# --------------------------------------------------------------------------- 9. the ledger edge


def test_a_divergence_reaches_the_units_owner_through_the_ledger(tmp_path):
    """§7.3: a note that reaches a stream nobody reads is not surfacing. The unit's own `owner` is
    the addressee, which is what makes it reach a person."""
    sdlc = _sdlc(tmp_path, ledger_on=True)
    _seed(sdlc, "int-contract", _entry())
    _sync(sdlc, goal="2879", run=_runner(branches=()))
    notes = [e for e in _ledger().read_all(str(sdlc)) if e.get("kind") == "note"]
    # #1574: the registry writes `@unit-owner`; the ledger stores the comparison spelling.
    assert notes and any(e.get("to") == "unit-owner" for e in notes)
    assert any("feature/int-contract" in (e.get("why") or "") for e in notes)


def test_the_ledger_being_off_costs_the_note_and_never_the_sync(tmp_path):
    sdlc = _sdlc(tmp_path, ledger_on=False)
    _seed(sdlc, "int-contract", _entry())
    report = _sync(sdlc, goal="2879", run=_runner(branches=()))
    assert report["outcome"] == _mod().SYNCED
    assert _goals(sdlc, "int-contract") == [2871, 2879]


# --------------------------------------------------------------------------- 10. mutants


def test_reconciling_an_unknown_live_set_is_a_mutant_this_suite_kills(tmp_path):
    """The loosening edge: `if live is None: return []` deleted. The mutant runs the same lines and
    only widens what it will act on -- which is exactly the class of change a coverage number
    cannot see."""
    m = _mod_with("    if live is None:\n        return []",
                  "    if live is None:\n        live = frozenset()")
    entry = _registry().normalise_entry(_entry())
    assert m.reconcile(entry, "int-contract", REPO, None)
    assert entry["repos"][REPO]["branch"] is None
    intact = _registry().normalise_entry(_entry())
    assert _mod().reconcile(intact, "int-contract", REPO, None) == []
    assert intact["repos"][REPO]["branch"] == "feature/int-contract"


def test_closing_a_unit_that_still_has_goals_is_a_mutant_this_suite_kills(tmp_path):
    """`and not goals` dropped from rule 5 -- a unit whose branch merged away would be closed while
    its goals are the only reason the registry exists."""
    m = _mod_with("if entry[\"open\"] and not branches and not goals:",
                  "if entry[\"open\"] and not branches:")
    entry = _registry().normalise_entry(_entry(repos={REPO: {"branch": None, "goals": [11]}}))
    m.reconcile(entry, "int-contract", REPO, frozenset())
    assert entry["open"] is False
    intact = _registry().normalise_entry(_entry(repos={REPO: {"branch": None, "goals": [11]}}))
    _mod().reconcile(intact, "int-contract", REPO, frozenset())
    assert intact["open"] is True


def test_judging_a_non_feature_branch_is_a_mutant_this_suite_kills(tmp_path):
    """The pattern guard removed: a recorded `main` is measured against a live set that was
    fetched with a `feature/*` pattern and could never have contained it."""
    m = _mod_with("branch.startswith(features.BRANCH_PREFIX) and branch not in live",
                  "branch not in live")
    entry = _registry().normalise_entry(_entry(repos={REPO: {"branch": "main", "goals": [1]}}))
    assert m.reconcile(entry, "int-contract", REPO, frozenset({"feature/int-contract"}))
    assert entry["repos"][REPO]["branch"] is None
    intact = _registry().normalise_entry(_entry(repos={REPO: {"branch": "main", "goals": [1]}}))
    assert _mod().reconcile(intact, "int-contract", REPO,
                            frozenset({"feature/int-contract"})) == []
    assert intact["repos"][REPO]["branch"] == "main"


def test_swallowing_a_declared_unit_on_an_unadopted_registry_is_a_mutant_this_suite_kills(
        tmp_path, capsys):
    """The 1.4.1 behaviour, restored exactly: the opt-out with its diagnostic deleted. Every
    assertion about the RECORD still holds under this mutant -- nothing is written either way —
    which is why the incident lasted as long as it did, and why the kill has to be scored on the
    console rather than on the registry."""
    m = _mod_with('        if unit:\n            _note(_NOT_ADOPTED_NOTE'
                  ' % {"goal": goal, "unit": unit, "dir": features_dir})\n', "")
    sdlc = _sdlc(tmp_path, adopted=False)
    assert m.sync_at_pick(str(sdlc), CONFIG, "1594", "quota-visibility",
                          run=_runner(), cwd=str(tmp_path))["outcome"] == m.NOT_ADOPTED
    assert capsys.readouterr().err == "", "the mutant is 1.4.1: it must be silent"
    _sync(sdlc, goal="1594", unit="quota-visibility")
    assert "quota-visibility" in capsys.readouterr().err


def test_warning_a_goal_that_declared_nothing_is_a_mutant_this_suite_kills(tmp_path, capsys):
    """The LOOSENING edge, which line coverage cannot see: the condition dropped, so every project
    that never adopted the model is told to adopt it on every pick. That is not a louder version of
    the fix — it is a new failure mode for the majority of installs, and the reason the guard reads
    `if unit` and not `if not adopted`."""
    m = _mod_with("        if unit:\n            _note(_NOT_ADOPTED_NOTE",
                  "        if True:\n            _note(_NOT_ADOPTED_NOTE")
    sdlc = _sdlc(tmp_path, adopted=False)
    m.sync_at_pick(str(sdlc), CONFIG, "2879", None, run=_runner(), cwd=str(tmp_path))
    assert capsys.readouterr().err != "", "the mutant speaks where the goal declared nothing"
    _mod().sync_at_pick(str(sdlc), CONFIG, "2879", None, run=_runner(), cwd=str(tmp_path))
    assert capsys.readouterr().err == ""


def test_a_warning_without_the_path_or_the_gesture_is_a_mutant_this_suite_kills(tmp_path, capsys):
    """The wording, mutated to the version an author writes first: correct, and useless. It names
    the condition and leaves the reader exactly where the silence did — no way to tell WHICH
    `.sdlc` (a worktree has several), no command to run, no warning that the goals already picked
    stay missing."""
    m = _mod_with('but there is no %(dir)s directory, "\n    "so this project has not adopted the '
                  'branching registry: the goal still runs and NOTHING "\n    "records it under '
                  'the unit. `mkdir -p %(dir)s` is the whole gesture (docs/branching-model.md, "\n'
                  '    "`Opening a unit`); goals picked before that directory exists are never '
                  'backfilled.',
                  'the branching registry is not adopted here.')
    sdlc = _sdlc(tmp_path, adopted=False)
    m.sync_at_pick(str(sdlc), CONFIG, "1594", "quota-visibility",
                   run=_runner(), cwd=str(tmp_path))
    err = capsys.readouterr().err
    assert "quota-visibility" in err, "the mutant still says something — that is the point"
    assert str(sdlc / "features") not in err and "mkdir" not in err
    _sync(sdlc, goal="1594", unit="quota-visibility")
    intact = capsys.readouterr().err
    assert str(sdlc / "features") in intact and "mkdir -p" in intact
    assert "never backfilled" in intact


# --------------------------------------------------------------------------- 11. the edges


def test_the_default_runner_raises_on_a_non_zero_exit(tmp_path):
    """A direct CLI caller gets no injected runner, and the fallback has to keep the ONE distinction
    everything here turns on: a failed command RAISES (the question went unanswered) and a
    successful one returns its stdout, even when that stdout is empty (the answer is nothing)."""
    m = _mod()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    assert m._run(str(tmp_path), ["git", "rev-parse", "--is-inside-work-tree"]) == "true"
    assert m._run(str(tmp_path), ["git", "ls-remote", "--heads", ".", "feature/*"]) == ""
    with pytest.raises(RuntimeError) as caught:
        m._run(str(tmp_path), ["git", "cat-file", "-e", "definitely-not-an-object"])
    assert "definitely-not-an-object" in str(caught.value)


def test_a_lock_path_that_cannot_be_opened_degrades_to_unserialised(tmp_path):
    """Same fail-open posture as `loop._try_acquire_claim_lock`: a directory this process cannot
    create is a lost lock, never a lost write."""
    m = _mod()
    blocker = tmp_path / "state"
    blocker.write_text("not a directory", encoding="utf-8")
    assert m._acquire(m.lock_path(str(tmp_path), "alpha"), timeout=0.05) is None


def test_releasing_nothing_is_a_no_op(tmp_path):
    m = _mod()
    assert m._release(None) is None
    fd = m._acquire(m.lock_path(str(_sdlc(tmp_path)), "alpha"), timeout=1.0)
    m._release(fd)
    m._release(fd)                                # a double release must not raise into a pick


def test_a_remote_that_cannot_be_read_yields_no_slug(tmp_path):
    m = _mod()
    assert m.repo_slug({"discovery": {"github": {"repo": ""}}},
                       _runner(handlers=[("remote get-url", RuntimeError("no such remote"))]),
                       ".", "origin") is None


def test_an_illegal_name_reaching_the_doc_half_costs_the_file_and_not_the_record(tmp_path,
                                                                                monkeypatch):
    """The cross-module catch, exercised: `feature_doc` raises ITS OWN `InvalidUnitName` class, and
    the broad `ValueError` form is what catches it from here."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    boom = _doc().InvalidUnitName("not a unit name")
    monkeypatch.setattr(m.doc, "sync", lambda *a, **kw: (_ for _ in ()).throw(boom))
    report = m.sync_at_pick(str(sdlc), CONFIG, "2879", "int-contract",
                            run=_runner(), cwd=str(tmp_path))
    assert report["outcome"] == m.SYNCED and report["doc"] == {}
    assert _goals(sdlc, "int-contract") == [2879]


def test_a_stderr_that_cannot_be_written_is_not_an_exception(tmp_path, monkeypatch):
    m = _mod()

    class Dead:
        def write(self, _text):
            raise OSError("stderr is closed")

    monkeypatch.setattr(m, "sys", type("S", (), {"stderr": Dead()})())
    assert m._note("anything\n") is None


def test_the_cli_folds_shows_and_refuses_anything_else(tmp_path, capsys):
    m, sdlc = _mod(), _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    assert m.main(["feature_sync.py", "fold", str(sdlc)]) == 0
    assert _registry().index_path(sdlc / "features").is_file()
    assert m.main(["feature_sync.py", "show", str(sdlc)]) == 0
    assert "int-contract" in capsys.readouterr().out
    assert m.main(["feature_sync.py"]) == 2
    assert m.main(["feature_sync.py", "sync", str(sdlc)]) == 2, (
        "there is no `sync` verb: the sync belongs to the pick, and a second way to run it is a "
        "second answer")


def test_a_deleted_md_comes_back_on_the_next_pick_even_when_nothing_changed(tmp_path):
    """The definition of done says a pick creates BOTH the entry and the file, so the doc half is
    synced unconditionally rather than only when the entry moved. A file somebody deleted has to be
    restored by the next pick, and the entry that pick records is by then already identical."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    _sync(sdlc, goal="2879")
    (sdlc / "features" / "int-contract.md").unlink()
    report = _sync(sdlc, goal="2879")
    assert report["changed"] == []
    assert (sdlc / "features" / "int-contract.md").is_file()
    assert report["doc"]["int-contract"] == _doc().CREATED


def test_the_ordinary_first_pick_of_a_new_unit_does_not_ledger_anything(tmp_path):
    """`BRANCH_ABSENT` is the NORMAL state of a unit whose branch has not been cut yet. Ledgering it
    would put a note in front of an owner for the ordinary case, which is exactly how a channel
    stops being read. It is on stderr and in the report; it is not an event."""
    sdlc = _sdlc(tmp_path, ledger_on=True)
    report = _sync(sdlc, goal="2879", unit="brand-new", run=_runner(branches=()))
    assert _mod().BRANCH_ABSENT in [d["kind"] for d in report["divergences"]]
    assert [e for e in _ledger().read_all(str(sdlc)) if e.get("kind") == "note"] == []


# --------------------------------------------------------------------------- 12. what the fail-open
#                                                                                  path may claim


def _clobber_between_the_write_and_the_recheck(m, monkeypatch):
    """A co-writer with stale state, landing AFTER `amend`'s own verify and BEFORE the pass re-reads.

    Hooked on the `.md` sync, which is the first thing the pass does after the picked unit's amend
    returns — not on `write_unit`, which `amend`'s retry would simply notice and correct. The point
    of the re-read is exactly the window `amend` cannot see, so the fixture has to open its clobber
    in that window and nowhere else."""
    real_doc, fired = m.doc.sync, []

    def clobber_then_sync(features_dir, name, entry, *a, **kw):
        if not fired:
            fired.append(name)
            m.registry.write_unit(features_dir, name,
                                  _entry(repos={REPO: {"branch": None, "goals": [999]}}))
        return real_doc(features_dir, name, entry, *a, **kw)

    monkeypatch.setattr(m.doc, "sync", clobber_then_sync)


def test_landed_needs_the_lock_and_written_does_not(tmp_path, monkeypatch):
    """B1. `landed` used to mean "the write was read back", which the fail-open path can satisfy and
    then be clobbered a microsecond later — measured at 40 of 40 writes reporting success with 11 to
    16 of them absent afterwards. It now means `written and serialised`, which is the only
    combination that is a fact rather than a likelihood, and `written` carries the part that IS
    provable so nothing is lost by tightening it."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    locked = m.amend(str(sdlc), "int-contract",
                     lambda e: e["repos"].setdefault(REPO, {"goals": []})["goals"].append(7))
    assert (locked["written"], locked["serialised"], locked["landed"]) == (True, True, True)

    monkeypatch.setattr(m, "fcntl", None)
    unlocked = m.amend(str(sdlc), "int-contract",
                       lambda e: e["repos"].setdefault(REPO, {"goals": []})["goals"].append(8))
    assert unlocked["written"] is True, "the bytes did go in, and saying otherwise would be a lie too"
    assert unlocked["serialised"] is False
    assert unlocked["landed"] is False, (
        "without the lock the write cannot be shown to have STAYED, so `landed` must not say it was")
    assert 8 in _goals(sdlc, "int-contract")


def test_a_write_that_never_landed_at_all_is_still_reported_exactly(tmp_path, monkeypatch):
    """The half the verify genuinely establishes, kept: a write that is never on disk is caught by
    the retry and reported `landed: False` after `WRITE_ATTEMPTS`. Tightening `landed` must not blunt
    the case it was right about."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    real = _registry().write_unit

    def never_lands(features_dir, name, entry):
        real(features_dir, name, entry)
        return real(features_dir, name, _entry(repos={REPO: {"branch": None, "goals": [999]}}))

    monkeypatch.setattr(m.registry, "write_unit", never_lands)
    report = m.amend(str(sdlc), "int-contract",
                     lambda e: e["repos"].setdefault(REPO, {"goals": []})["goals"].append(7))
    assert (report["written"], report["landed"], report["attempts"]) == (False, False,
                                                                        m.WRITE_ATTEMPTS)


def test_an_unserialised_write_is_flagged_on_every_pass(tmp_path, monkeypatch):
    """`serialised` and `recorded` had no consumer at all: `work._sync_registry` reads only `note`.
    The flag now becomes a divergence, so it reaches the clause `start()` prints — honesty in a field
    nobody reads is decoration."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    monkeypatch.setattr(m, "fcntl", None)
    report = m.sync_at_pick(str(sdlc), CONFIG, "2879", "int-contract",
                            run=_runner(branches=("feature/int-contract",)), cwd=str(tmp_path))
    assert m.UNSERIALISED in [d["kind"] for d in report["divergences"]]
    assert "unserialised" in report["note"]
    assert report["serialised"] is False and report["recorded"] is True


def test_the_locked_path_pays_nothing_for_the_recheck(tmp_path):
    """The re-read exists for the fail-open path and must not become a cost on the ordinary one."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    report = _sync(sdlc, goal="2879")
    kinds = [d["kind"] for d in report["divergences"]]
    assert m.UNSERIALISED not in kinds and m.LOST not in kinds
    assert report["serialised"] is True and report["note"] == ""


def test_a_goal_clobbered_after_an_unserialised_write_is_measured_not_assumed(tmp_path,
                                                                             monkeypatch):
    """The re-read at the END of the pass. `LOST` is a measurement: the goal was written, the rest of
    the pass ran, and it was gone when the pass looked again. `recorded` is corrected with it, so the
    field and the divergence can never disagree."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    monkeypatch.setattr(m, "fcntl", None)
    _clobber_between_the_write_and_the_recheck(m, monkeypatch)
    report = m.sync_at_pick(str(sdlc), CONFIG, "2879", "int-contract",
                            run=_runner(branches=("feature/int-contract",)), cwd=str(tmp_path))
    lost = [d for d in report["divergences"] if d["kind"] == m.LOST]
    assert lost and lost[0]["detail"] == "2879"
    assert report["recorded"] is False
    assert "lost" in report["note"]


def test_a_lost_goal_reaches_the_units_owner(tmp_path, monkeypatch):
    """`LOST` is the one entry in `LEDGERED` that is not a change this pass made, and it earns the
    exception: it is a goal number measured to be gone, which is the exact failure the registry
    exists to prevent."""
    m, sdlc = _mod(), _sdlc(tmp_path, ledger_on=True)
    _seed(sdlc, "int-contract", _entry())
    monkeypatch.setattr(m, "fcntl", None)
    _clobber_between_the_write_and_the_recheck(m, monkeypatch)
    m.sync_at_pick(str(sdlc), CONFIG, "2879", "int-contract",
                   run=_runner(branches=("feature/int-contract",)), cwd=str(tmp_path))
    notes = [e for e in _ledger().read_all(str(sdlc)) if e.get("kind") == "note"]
    assert any("2879" in (e.get("why") or "") and e.get("to") == "unit-owner" for e in notes)


# --------------------------------------------------------------------------- 13. the silent no-op


def test_an_unreadable_remote_is_reported_rather_than_passed_over(tmp_path, capsys):
    """B2. The semantics were already right — nothing deleted, nothing closed — and the REPORTING
    was not: zero divergences, zero stderr, an empty clause, and `outcome: "synced"`, which actively
    asserts that reality was cross-checked. An operator could not tell "everything agrees" from
    "nothing was compared". Same collapse `_declared_unit` refuses one function along on this very
    pick path, for the same stated reason."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    _seed(sdlc, "int-contract", _entry())
    report = _sync(sdlc, goal="2879", run=_runner(branches=None))
    found = [d for d in report["divergences"] if d["kind"] == m.REMOTE_UNREADABLE]
    assert found, "a pass that cross-checked nothing must not look like one that cross-checked all"
    assert "origin" in found[0]["detail"] and "could not read from remote" in found[0]["detail"]
    assert "remote-unreadable" in report["note"]
    assert "NOTHING was cross-checked" in capsys.readouterr().err
    assert _read(sdlc)["int-contract"]["repos"][REPO]["branch"] == "feature/int-contract"


def test_an_unreadable_remote_is_not_a_state_change_so_it_is_not_ledgered(tmp_path):
    """It reports ignorance, not an event with an owner, and it recurs on every pick while the
    network is down — the exact shape that teaches a person to stop reading the channel."""
    sdlc = _sdlc(tmp_path, ledger_on=True)
    _seed(sdlc, "int-contract", _entry())
    _sync(sdlc, goal="2879", run=_runner(branches=None))
    assert [e for e in _ledger().read_all(str(sdlc)) if e.get("kind") == "note"] == []


def test_a_goal_declaring_no_unit_still_reports_an_unreadable_remote(tmp_path):
    """`REMOTE_UNREADABLE` is filed against `report["unit"]`, which is None on such a goal — so it
    still matches `_clause`'s own-unit test and still reaches `start()`'s output."""
    sdlc = _sdlc(tmp_path)
    report = _sync(sdlc, goal="2879", unit=None, run=_runner(branches=None))
    assert "remote-unreadable" in report["note"]


# ---------------------------------------------------------------- 13b. the OTHER silent no-op (#1598)
#
# Found by running the branching model live for the first time, on 1.4.1, against a real repository:
# `docs/branching-model.md` §14's three steps opened a real unit, two goals were cut from its
# branch, and the registry recorded neither -- `_sync_registry` returned `not-adopted` and swallowed
# it, with no error, no warning and no stderr line, while §14 promised "recorded against it" one
# paragraph below. The semantics of the opt-out were never wrong; the REPORTING was, in exactly the
# way section 13 above records for the unreadable remote.
#
# The condition is the whole design. A goal that declares a unit is the adopter ASKING for a record,
# so silence there is the failure. A goal that declares none is a project that never asked, and its
# pick has to stay byte-identical -- which is what makes the model adoptable without disturbing
# anyone. Both directions are pinned, because a guard that only fires is half a guard.


def test_a_declared_unit_meeting_an_unadopted_registry_is_told_not_swallowed(tmp_path, capsys):
    """The measured incident. One stderr line is the whole difference between a repository that is
    half-adopted for a week and a thirty-second correction."""
    m, sdlc = _mod(), _sdlc(tmp_path, adopted=False)
    report = _sync(sdlc, goal="1594", unit="quota-visibility")
    err = capsys.readouterr().err
    assert report["outcome"] == m.NOT_ADOPTED
    assert "quota-visibility" in err and "1594" in err
    assert report["note"] == "", "the opt-out still claims nothing on the goal's own start line"


def test_the_unadopted_warning_names_the_directory_the_gesture_and_the_limit(tmp_path, capsys):
    """A diagnostic that says only "not adopted" leaves the reader exactly where the silence did.
    Three things make it actionable, and each was missing from the live incident: WHICH directory
    (the trial ran inside a worktree, so there were several `.sdlc` trees to choose from), the
    command that creates it, and the fact that the goals already picked do not come back."""
    sdlc = _sdlc(tmp_path, adopted=False)
    _sync(sdlc, goal="1594", unit="quota-visibility")
    err = capsys.readouterr().err
    assert str(sdlc / "features") in err                 # the directory, named rather than described
    assert "mkdir -p %s" % (sdlc / "features") in err    # the gesture, runnable as pasted
    assert "never backfilled" in err                     # the limit, said while it is still free


def test_a_goal_declaring_no_unit_is_untouched_by_that_warning(tmp_path, capsys):
    """The other direction, and the one that keeps adoption cheap: a project that declares no unit
    must be byte-identical — same report, same output, same calls. Asserted against the whole
    report rather than a field of it, because "unchanged" is a claim about all of it."""
    m, sdlc, run = _mod(), _sdlc(tmp_path, adopted=False), _runner()
    report = m.sync_at_pick(str(sdlc), CONFIG, "2879", None, run=run, cwd=str(tmp_path))
    expected = m._report("2879", None)
    expected["outcome"] = m.NOT_ADOPTED
    assert report == expected
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    assert run.calls == []


def test_start_says_it_on_the_real_pick_path_and_still_creates_nothing(tmp_path, capsys):
    """Where it actually has to appear. The live defect was on `work.start()`, not on a direct call,
    and the second assertion is the scope line: this closes the SILENCE (#1598) and deliberately
    does not create the registry (#1576), which must land last because it arms #1564."""
    work = _load("work")
    sdlc = _sdlc(tmp_path, adopted=False)
    run = _runner(handlers=[("api repos/", json.dumps(
        {"number": 1594, "title": "t", "body": "Feature: quota-visibility", "labels": []}))])
    work.start(str(sdlc), CONFIG, "1594", run=run)
    err = capsys.readouterr().err
    assert "quota-visibility" in err and "mkdir -p" in err
    assert not (sdlc / "features").exists()


# --------------------------------------------------------------------------- 14. trusting local alone


def test_a_branch_nobody_measured_is_never_written_into_the_entry(tmp_path):
    """B3 / the surviving mutant. This is the "only when MEASURED" half of `_record_goal`, and it is
    the issue's headline invariant: with the remote unreadable, writing `feature/<name>` would be the
    registry asserting a branch exists on the strength of a naming convention. Nothing tested it."""
    sdlc = _sdlc(tmp_path)
    report = _sync(sdlc, goal="2879", unit="brand-new", run=_runner(branches=None))
    assert _read(sdlc)["brand-new"]["repos"][REPO]["branch"] is None, (
        "an unmeasured branch name must never reach the entry — that is trusting local alone")
    assert _goals(sdlc, "brand-new") == [2879]
    assert _mod().BRANCH_ABSENT not in [d["kind"] for d in report["divergences"]], (
        "and it is not 'absent' either: nothing was measured, so nothing may be declared missing")


def test_record_goal_writes_no_branch_when_the_live_set_is_unknown(tmp_path):
    """The same invariant at the unit level, so the guard is pinned where it lives rather than only
    through three layers of pass."""
    m = _mod()
    entry = _registry().normalise_entry({"repos": {REPO: {"branch": None, "goals": []}}})
    assert m._record_goal(entry, "alpha", REPO, 7, None) == []
    assert entry["repos"][REPO]["branch"] is None
    measured = _registry().normalise_entry({"repos": {REPO: {"branch": None, "goals": []}}})
    m._record_goal(measured, "alpha", REPO, 7, frozenset({"feature/alpha"}))
    assert measured["repos"][REPO]["branch"] == "feature/alpha"


# --------------------------------------------------------------------------- 15. the reporting surface


def test_the_lock_path_refuses_every_name_that_is_not_a_unit_name():
    """Defence in depth, and held rather than merely justified. Unreachable from the pick path today
    (`registry.read` filters through `is_unit_name`, so a swept name is always legal) — but `unit`
    reaches `amend` from a caller, and `lock_path` is the one place that name becomes a path under
    `.sdlc/state/`, which `unit_path`'s own guard does not cover."""
    m = _mod()
    for bad in ("../escape", "a/b", "", "..", ".hidden", "x.lock", "a b", None, 7):
        with pytest.raises(ValueError):
            m.lock_path("/tmp/g1473", bad)
    assert m.lock_path("/tmp/g1473", "alpha").name == "alpha" + m.LOCK_SUFFIX


def test_a_regenerated_block_is_reported_exactly_once(tmp_path, capsys):
    """`feature_doc._tell` already puts this on stderr and in the ledger, addressed to the owner, at
    the moment it happens. `SURFACED_BY_THE_DOC` is what stops this module saying it a second time —
    and a duplicate report is how a reader learns to distrust the count."""
    m, sdlc = _mod(), _sdlc(tmp_path, ledger_on=True)
    _seed(sdlc, "int-contract", _entry())
    _sync(sdlc, goal="2879")
    path = sdlc / "features" / "int-contract.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Manifest", "MEDDLED"),
                    encoding="utf-8")
    capsys.readouterr()
    _sync(sdlc, goal="2880")
    said = capsys.readouterr().err
    assert said.count("regenerated; whether it had been edited could not be told") == 1
    assert m.BLOCK_DIVERGED in m.SURFACED_BY_THE_DOC


def test_the_clause_names_at_most_two_findings_and_counts_the_rest(tmp_path):
    """A clause that silently stopped at two read identically for two findings and for nine, which
    is the under-reporting this whole surface was reviewed for."""
    m = _mod()
    report = m._report("2879", "alpha")
    kinds = (m.BRANCH_MISSING, m.PICKED_WHILE_CLOSED, m.BRANCH_ABSENT)
    report["divergences"] = [m._div(k, "alpha", REPO, "d%d" % i) for i, k in enumerate(kinds)]
    clause = m._clause(report)
    assert len([k for k in kinds if k in clause]) == m._CLAUSE_CAP == 2
    assert "+1 more" in clause


def test_another_units_finding_never_lands_in_this_units_clause(tmp_path):
    """The sweep reconciles every unit; a clause naming another unit's findings on this goal's start
    line reads as though this goal caused them. `_surface` routes those to their own owners."""
    m = _mod()
    report = m._report("2879", "alpha")
    report["divergences"] = [m._div(m.BRANCH_MISSING, "somebody-elses-unit", REPO, "feature/x")]
    assert m._clause(report) == ""
    report["divergences"].append(m._div(m.BRANCH_MISSING, "alpha", REPO, "feature/alpha"))
    assert "feature/alpha" in m._clause(report)
    assert "feature/x" not in m._clause(report)


# --------------------------------------------------------------------------- 16. the sweep's cost


def test_a_unit_that_cannot_change_is_not_locked_re_read_or_written(tmp_path):
    """The sweep used to `amend` every unit on every pick, and `amend` re-reads the WHOLE registry —
    O(N^2), measured at ~10,200 shard reads and 0.30s per pick at 100 units, almost always to write
    nothing. A dry run of `reconcile` on the copy already in hand answers "could this change?" for
    free; a No means no lock file, no re-read, no write. Asserted on the LOCK FILES, because that is
    the observable a result-shaped assertion would miss entirely."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    for name in ("alpha", "beta", "gamma"):
        _seed(sdlc, name, _entry(repos={REPO: {"branch": "feature/" + name, "goals": [1]}}))
    live = ("feature/int-contract", "feature/alpha", "feature/beta", "feature/gamma")
    _sync(sdlc, goal="2879", run=_runner(branches=live))
    locks = sorted(p.stem for p in (sdlc / "state" / m.LOCK_DIRNAME).glob("*" + m.LOCK_SUFFIX)) \
        if (sdlc / "state" / m.LOCK_DIRNAME).is_dir() else []
    assert locks == ["int-contract"], (
        "only the picked unit could change, so only the picked unit may be touched: %s" % locks)


def test_the_dry_run_is_only_a_hint_and_the_lock_still_decides(tmp_path):
    """The pre-filter reads a snapshot; the authoritative decision is still made under the unit's
    lock against a fresh read. A unit the snapshot says WILL change still gets the real reconcile."""
    sdlc = _sdlc(tmp_path)
    _seed(sdlc, "old-thing", _entry(repos={REPO: {"branch": "feature/old-thing", "goals": []}}))
    report = _sync(sdlc, goal="2879", run=_runner(branches=("feature/int-contract",)))
    assert "old-thing" in report["changed"]
    assert _read(sdlc)["old-thing"]["open"] is False


def test_a_resumed_start_does_not_sync_and_that_is_the_documented_choice(tmp_path):
    """F6. `already started` returns above `_sync_registry`, so a supervisor relaunch neither records
    nor reconciles. Pinned rather than left to be rediscovered: the goal was already recorded by the
    start that cut the worktree, and paying an `ls-remote` plus a whole-registry sweep on every
    relaunch would be real cost for a unit reconciled minutes earlier."""
    work = _load("work")
    sdlc = _sdlc(tmp_path)
    run = _runner(branches=("feature/billing",), handlers=[
        ("api repos/", json.dumps({"number": 1473, "title": "t", "body": "Feature: billing",
                                   "labels": []}))])
    work.start(str(sdlc), CONFIG, "1473", run=run)
    pathlib.Path(work._record(str(sdlc), "1473")["worktree"]).mkdir(parents=True, exist_ok=True)
    del run.calls[:]
    out = work.start(str(sdlc), CONFIG, "1473", run=run)
    assert out.startswith("already started")
    # #2009: the resume now runs a freshness check -- and ONLY that. The registry sweep this test
    # exists to pin is still skipped: no `ls-remote`, no issue read, no whole-registry reconcile.
    # The distinction the original "no calls at all" collapsed is the one that matters: that sweep
    # was declined as real cost "for a unit reconciled minutes earlier", whereas base drift is
    # unbounded and silent, and two local git calls against one branch is what it costs to see it.
    assert run.calls == ["git fetch origin feature/billing",
                         "git rev-list --count HEAD..origin/feature/billing"], \
        "a resume makes no sync calls — not the issue read, not `ls-remote`; only the #2009 check"
    assert _goals(sdlc, "billing") == [1473], "and the first start's record stands, undisturbed"


def test_a_swept_units_unserialised_write_is_flagged_too(tmp_path, monkeypatch):
    """The sweep writes as well as the picked unit does, so it owes the same disclosure. Without it
    a reconcile that silently lost a branch clearing would report `synced` with no caveat at all."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    _seed(sdlc, "old-thing", _entry(repos={REPO: {"branch": "feature/old-thing", "goals": []}}))
    monkeypatch.setattr(m, "fcntl", None)
    report = m.sync_at_pick(str(sdlc), CONFIG, "2879", "int-contract",
                            run=_runner(branches=("feature/int-contract",)), cwd=str(tmp_path))
    flagged = [d for d in report["divergences"] if d["kind"] == m.UNSERIALISED]
    assert sorted(d["unit"] for d in flagged) == ["int-contract", "old-thing"]
    assert _read(sdlc)["old-thing"]["open"] is False


def test_a_goal_the_registry_could_never_store_is_not_reported_lost(tmp_path, monkeypatch):
    """`_recheck` asks "is my goal in this list?" through `normalise_entry`, the ONE rule for what a
    goal number is. A goal that reduces to nothing was never stored, so it cannot be missing — and
    reporting it `lost` would be an accusation drawn from a value the registry never accepted."""
    m, sdlc = _mod(), _sdlc(tmp_path)
    assert m._goal_number("not-a-number") is None
    assert m._goal_number("2879") == 2879 and m._goal_number(2879) == 2879
    monkeypatch.setattr(m, "fcntl", None)
    _clobber_between_the_write_and_the_recheck(m, monkeypatch)
    report = m.sync_at_pick(str(sdlc), CONFIG, "not-a-number", "int-contract",
                            run=_runner(branches=("feature/int-contract",)), cwd=str(tmp_path))
    assert [d for d in report["divergences"] if d["kind"] == m.LOST] == []


# ------------------------------------------------------- #1566: one unit, one lock


def test_two_casings_of_one_unit_take_the_same_lock(tmp_path):
    """#1566, the half that matters more than the shard. Two shards lose data; two LOCKS lose the
    serialisation the shard write depends on -- each casing holds a different file, so neither
    excludes the other and both read-modify-write the unit at once.

    `lock_path`'s own docstring says it refuses the same names `unit_path` refuses "because this path
    is derived from the same string". The fold belongs here for exactly that reason: same string,
    same answer. Asserted on the derived path because this host's filesystem is case-insensitive and
    would hide the divergence."""
    m = _mod()
    assert m.lock_path(tmp_path, "Voice") == m.lock_path(tmp_path, "voice")
    assert m.lock_path(tmp_path, "VOICE") == m.lock_path(tmp_path, "voice")


def test_the_lock_and_the_shard_agree_on_which_string_they_folded(tmp_path):
    """These two folds are independent code in two modules, so nothing but a test stops one from
    being changed without the other -- which would put the lock on one unit while the write went to
    another, the precise failure the lock exists to prevent.

    A PAIRWISE CHECK, NOT A COUNT. This docstring used to say "the two folds" when there were
    already four, and every restatement of the number since has gone stale within two goals -- most
    recently #1673, which folded two more. So there is no number here: the inventory that knows the
    real one is `tests/test_feature_registry.py`, and it is the one that should be updated."""
    m = _mod()
    reg = _load("feature_registry")
    for spelling in ("Voice", "voice", "VOICE", "vOiCe"):
        assert m.lock_path(tmp_path, spelling).name.startswith(
            reg.unit_path(tmp_path / "features", spelling).name[:-len(reg.UNIT_SUFFIX)])


# ------------------------------------- #1565: an unreadable shard is not a shard we may replace


def _corrupt_shard(sdlc, name, text):
    """Put a file at the unit's own path that `read` cannot use, and return that path."""
    reg = _load("feature_registry")
    p = reg.unit_path(reg.registry_dir(sdlc), name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_amend_refuses_rather_than_replacing_a_shard_it_cannot_read(tmp_path):
    """#1565. `read` DROPS an unreadable shard's unit outright -- deliberately, and its docstring
    argues for it. But `amend` then reads `existed=False`, starts from `{}`, and `write_unit`
    REPLACES the file: title, owner, tracking issue, every hand-written `authorized` grant, every
    sibling repo and every recorded goal, gone, while the report says it synced.

    §12 requires humans to hand-edit this file, so malformed JSON is exactly what hand-editing
    produces -- and the usual defence is absent, because nothing stages `.sdlc/features/`, so there
    is no committed pre-image to restore from. `feature_propagate._their_entry` already refuses on
    the REMOTE side for this reason; this is the same rule applied locally."""
    m = _mod()
    (tmp_path / "features").mkdir(parents=True, exist_ok=True)
    p = _corrupt_shard(tmp_path, "int-contract", '{"schema": "sigma/features@1",,}')
    before = p.read_text(encoding="utf-8")

    rep = m.amend(tmp_path, "int-contract", lambda e: e.setdefault("repos", {}).setdefault("o/r", {}))

    assert rep.get("refused"), "an unreadable shard must be refused, not replaced"
    assert rep["written"] is False and rep["changed"] is False
    assert p.read_text(encoding="utf-8") == before, "the operator's bytes must survive untouched"


def test_the_refusal_names_the_file_and_what_to_do_with_it(tmp_path):
    """A refusal the operator cannot act on wedges the pick. The one thing they need is which file,
    because the whole point is that they are going to open it and fix their own typo."""
    m = _mod()
    (tmp_path / "features").mkdir(parents=True, exist_ok=True)
    p = _corrupt_shard(tmp_path, "int-contract", "not json at all")
    rep = m.amend(tmp_path, "int-contract", lambda e: None)
    why = str(rep.get("refused") or "")
    assert "int-contract" in why and str(p) in why, why
    assert "repair" in why.lower() or "delete" in why.lower(), why


def test_an_absent_shard_is_still_created_normally(tmp_path):
    """The refusal is about a file that IS there and cannot be read. A unit with no shard yet is the
    ordinary first-pick case and must be untouched by this guard -- otherwise the fix for a corrupt
    registry would stop a healthy one from ever being written."""
    m = _mod()
    (tmp_path / "features").mkdir(parents=True, exist_ok=True)
    rep = m.amend(tmp_path, "int-contract", lambda e: e.setdefault("repos", {}).setdefault("o/r", {}))
    assert not rep.get("refused")
    assert rep["written"] is True and rep["existed"] is False


def test_a_regenerated_block_message_does_not_assert_an_edit(tmp_path, capsys):
    """#1346: the digest not vouching for the block does not show WHO changed it or whether anyone did."""
    m, sdlc = _mod(), _sdlc(tmp_path, ledger_on=True)
    _seed(sdlc, "int-contract", _entry())
    _sync(sdlc, goal="2879")
    path = sdlc / "features" / "int-contract.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Manifest", "MEDDLED"),
                    encoding="utf-8")
    capsys.readouterr()
    _sync(sdlc, goal="2880")
    said = capsys.readouterr().err
    assert "was regenerated; whether it had been edited could not be told from what was on disk" in said
    assert "over an edit" not in said


def test_a_unit_opened_without_a_recorded_branch_stays_open_while_its_branch_is_live(tmp_path):
    """#1396: `define open` leaves the repo's branch unrecorded. The unit's own `feature/<name>`
    exists on the remote, so the reconcile pass must not close it; with the branch absent it still does."""
    m = _mod()
    bare = {"branch": None, "goals": []}
    live_entry = m.registry.normalise_entry(_entry(repos={REPO: dict(bare)}))
    assert m.reconcile(live_entry, "fresh-unit", REPO, frozenset({"feature/fresh-unit"})) == []
    assert live_entry["open"] is True
    gone_entry = m.registry.normalise_entry(_entry(repos={REPO: dict(bare)}))
    kinds = [d["kind"] for d in m.reconcile(gone_entry, "fresh-unit", REPO, frozenset())]
    assert m.CLOSED in kinds and gone_entry["open"] is False
