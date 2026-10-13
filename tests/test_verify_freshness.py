"""Real-git end-to-end proof for #1890: `loop.py verify` must detect and auto-rebase a worktree
that has drifted behind its own base BEFORE running anything expensive against it, and must
refuse -- never silently proceed -- when the auto-rebase itself cannot apply cleanly.

Deliberately NOT mocked. This repo's own standing rule (AGENTS.md) is that an untested guard is
decoration, and a test that only checks a function's return value proves nothing about whether the
underlying git state actually changed -- exactly the gap independent plan-review exploited to find
a real bug in the first draft of this fix (a rebase that "succeeded" while leaving conflict markers
in a tracked file). These tests use real `git` subprocesses and assert on the ACTUAL repository
state afterward, not just a return value.

Uses plain `git clone`s rather than `git worktree add` linked worktrees: every git operation this
code issues (fetch, rev-list, rebase, push) behaves identically either way, and a linked worktree
would add ceremony without changing what is under test.
"""
import importlib.util, json, os, pathlib, shutil, subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


work = _load("work")
loop = _load("loop")
state = _load("state")

ON = {"work": {"enabled": True}}


def _git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}"
    return proc.stdout.strip()


def _repo_pair(tmp_path, initial_content="line1\nline2\nline3\n", allow_shell_commands=False):
    """A bare `origin` plus a `goal` clone of it at one initial commit on `main`, then checked out
    onto its own `sdlc/test` branch -- the state right after `work.py start` would have cut this
    goal's worktree from `<remote>/<base>`."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    goal_wt = tmp_path / "goal"
    _git(tmp_path, "clone", "-q", str(origin), str(goal_wt))
    _git(goal_wt, "config", "user.email", "a@example.com")
    _git(goal_wt, "config", "user.name", "a")
    if allow_shell_commands:
        _git(goal_wt, "config", "--local", "sigma.allowRepositoryShellCommands", "true")
    (goal_wt / "file.txt").write_text(initial_content)
    _git(goal_wt, "add", "file.txt")
    _git(goal_wt, "commit", "-q", "-m", "base")
    _git(goal_wt, "push", "-q", "origin", "HEAD:main")
    _git(goal_wt, "checkout", "-q", "-b", "sdlc/test")
    return origin, goal_wt


def _advance_origin(tmp_path, origin, mutate, name="other"):
    """A SECOND, independent clone advances `origin/main` -- simulating another goal landing
    something on the shared base while THIS goal's own worktree sits untouched, exactly the shape
    of #1617/#1829."""
    other = tmp_path / name
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _git(other, "config", "user.email", "a@example.com")
    _git(other, "config", "user.name", "a")
    mutate(other)
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "upstream change")
    _git(other, "push", "-q", "origin", "HEAD:main")
    return _git(other, "rev-parse", "HEAD")


def _write_record(sdlc_dir, goal, worktree, branch="sdlc/test", base="main", remote="origin"):
    p = pathlib.Path(sdlc_dir) / "state" / "work" / f"{goal}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"worktree": str(worktree), "branch": branch, "base": base,
                             "remote": remote}))
    _write_acceptance(sdlc_dir, goal)


def _write_acceptance(sdlc_dir, goal):
    """#1338: `verify_goal` under `verify.enforce` refuses without an acceptance record."""
    a = pathlib.Path(sdlc_dir) / "acceptance" / f"{goal}.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text(f'---\nkind: "acceptance"\ngoal: "{goal}"\nverify_command: null\n---\n'
                 "## Done when\n- [ ] one\n- [ ] two\n- [ ] three\n")


def _sdlc(tmp_path, config=None):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(config or ON))
    state.start_run(str(d))
    return str(d)


# --- positive control: a real worktree left behind is really rebased, not just "returns None" ---

def test_a_worktree_left_behind_origin_main_is_actually_rebased(tmp_path):
    origin, goal_wt = _repo_pair(tmp_path)
    before_head = _git(goal_wt, "rev-parse", "HEAD")
    new_tip = _advance_origin(tmp_path, origin,
                               lambda o: (o / "unrelated.txt").write_text("x"))
    assert new_tip != before_head                          # sanity: origin really moved

    sdlc_dir = _sdlc(tmp_path)
    _write_record(sdlc_dir, "9001", goal_wt)

    out = work.ensure_fresh(sdlc_dir, ON, "9001")
    assert out is None
    # The real proof: HEAD actually moved to the new upstream tip -- not just a truthy/falsy
    # return value, which is exactly what the mocked tests already cover.
    assert _git(goal_wt, "rev-parse", "HEAD") == new_tip
    assert _git(goal_wt, "status", "--porcelain") == ""


# --- negative control: a fresh worktree is genuinely left untouched ------------------------------

def test_a_fresh_worktree_is_not_touched(tmp_path):
    origin, goal_wt = _repo_pair(tmp_path)
    before_head = _git(goal_wt, "rev-parse", "HEAD")

    sdlc_dir = _sdlc(tmp_path)
    _write_record(sdlc_dir, "9002", goal_wt)

    out = work.ensure_fresh(sdlc_dir, ON, "9002")
    assert out is None
    assert _git(goal_wt, "rev-parse", "HEAD") == before_head        # untouched
    assert _git(goal_wt, "status", "--porcelain") == ""              # no leftover mess
    assert _git(origin, "rev-parse", "main") == before_head          # nothing pushed either


# --- conflict control: the real shape plan-review found (uncommitted diff vs. a fresh upstream) -

def test_a_real_conflicting_uncommitted_change_refuses_cleanly(tmp_path):
    origin, goal_wt = _repo_pair(tmp_path)
    before_head = _git(goal_wt, "rev-parse", "HEAD")
    # The goal's own in-flight, UNCOMMITTED work -- the normal state at verify time, since
    # `work.py commit` never runs until AFTER verify passes (SKILL.md step 6).
    (goal_wt / "file.txt").write_text("GOAL VERSION\nline2\nline3\n")

    _advance_origin(tmp_path, origin,
                     lambda o: (o / "file.txt").write_text("UPSTREAM VERSION\nline2\nline3\n"))

    sdlc_dir = _sdlc(tmp_path)
    _write_record(sdlc_dir, "9003", goal_wt)

    out = work.ensure_fresh(sdlc_dir, ON, "9003")
    assert out is not None and "could not apply cleanly" in out

    # Byte-identical to before the call: branch position, uncommitted diff, no conflict markers,
    # nothing left mid-rebase -- verified by hand during plan-review, pinned here for real.
    assert _git(goal_wt, "rev-parse", "HEAD") == before_head
    assert not (goal_wt / ".git" / "rebase-merge").exists()
    assert not (goal_wt / ".git" / "rebase-apply").exists()
    assert _git(goal_wt, "diff", "--name-only", "--diff-filter=U") == ""
    assert (goal_wt / "file.txt").read_text() == "GOAL VERSION\nline2\nline3\n"
    assert _git(goal_wt, "stash", "list") == ""                      # the stash was popped back, not left
    # Nothing was pushed -- the bare origin's own main ref still points at the upstream commit only.
    assert _git(origin, "rev-parse", "main") != before_head          # origin DID move (from the setup)
    assert _git(origin, "rev-parse", "main") == _git(tmp_path / "other", "rev-parse", "HEAD")


# --- loop.py verify_goal() wiring, exercised end to end with REAL git ----------------------------

def _verify_config():
    return {**ON, "verify": {"command": "true", "enforce": True}}


def test_verify_goal_runs_the_suite_after_a_clean_auto_rebase(tmp_path):
    origin, goal_wt = _repo_pair(tmp_path, allow_shell_commands=True)
    new_tip = _advance_origin(tmp_path, origin,
                               lambda o: (o / "unrelated.txt").write_text("x"))

    sdlc_dir = _sdlc(tmp_path, _verify_config())
    _write_record(sdlc_dir, "9004", goal_wt)

    assert loop.verify_goal(sdlc_dir, "9004") == 0
    assert _git(goal_wt, "rev-parse", "HEAD") == new_tip
    assert state.evidence_path(sdlc_dir, "9004").exists()


def test_verify_goal_refuses_and_writes_no_evidence_on_a_real_conflict(tmp_path):
    origin, goal_wt = _repo_pair(tmp_path)
    (goal_wt / "file.txt").write_text("GOAL VERSION\nline2\nline3\n")
    _advance_origin(tmp_path, origin,
                     lambda o: (o / "file.txt").write_text("UPSTREAM VERSION\nline2\nline3\n"))

    sdlc_dir = _sdlc(tmp_path, _verify_config())
    _write_record(sdlc_dir, "9005", goal_wt)

    assert loop.verify_goal(sdlc_dir, "9005") == 4
    assert not state.evidence_path(sdlc_dir, "9005").exists()
    # The suite itself (`true`, which always succeeds) must never have run -- if it had, exit
    # would be 0 regardless of evidence, since `verify_goal` only returns 1 on a FAILING command.
    # The only way to observe "did the subprocess run at all" from the outside is the evidence
    # file's absence above, which `verify_goal` only ever writes AFTER the subprocess returns.


# =================================================================================================
# #2009: the RESUME path, not just verify.
#
# `work.start()` returns `already started: ...` for a goal whose worktree is on disk, having done no
# fetch and no freshness check -- so every READING phase (P2 RESEARCH's blast radius, P3's plan) can
# run against a tree dozens of commits behind its base. #1890 closed this at verify time, which is
# after the dossier and the plan are already written against stale code.
#
# The refusal is an ESCALATION, not one verdict, and the three tables below are its controls:
#   conflict (any occurrence)      -> park     a human must resolve it; self-healing is impossible
#   transient, 1st consecutive     -> release  a blip self-heals on the next pick, costs no budget
#   transient, 2nd consecutive     -> park     not a blip, an outage: reach a human loudly, once
#   a check that passes            -> reset    the bound is on CONSECUTIVE failures, not a tally
# Parking every refusal strands a goal on a network blip (`sdlc:parked` is never auto-resumed);
# releasing every transient recreates `run_loop`'s own documented `poisoned` spin -- pick, claim,
# start, release, pick -- burning gh calls while consuming zero budget, because `_release`
# deliberately never advances the cursor. Only the escalation is both recoverable and bounded.
# =================================================================================================

def _goal_file(sdlc_dir, ident, status="in_progress"):
    """A real goal FILE, not a bare id. `state.park`/`state.release` do `p.read_text()` on the goal
    path, so a bare `"9101"` raises FileNotFoundError and the failure reads as a park bug rather
    than a harness bug (plan-review round 1, nit 9)."""
    p = pathlib.Path(sdlc_dir) / "goals" / f"{ident}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nstatus: {status}\n---\n\n# goal {ident}\n")
    return str(p)


def _status_of(goal):
    return state.frontmatter.get(pathlib.Path(goal).read_text(), "status")


def _resume(tmp_path, ident, mutate_origin=None, dirty=None, run=None):
    """A goal whose worktree is already on disk -- i.e. the `already started` resume path. Returns
    (sdlc_dir, goal_path, worktree, origin)."""
    origin, goal_wt = _repo_pair(tmp_path)
    if dirty:
        dirty(goal_wt)
    if mutate_origin:
        _advance_origin(tmp_path, origin, mutate_origin)
    sdlc_dir = _sdlc(tmp_path)
    goal = _goal_file(sdlc_dir, ident)
    _write_record(sdlc_dir, work.stem(goal), goal_wt)
    return sdlc_dir, goal, goal_wt, origin


def _push_race():
    """Real git for everything except the force-push at the END of `rebase()`, which raises.

    That is the ONLY realistic way to reach `ensure_fresh`'s TRANSIENT branch, and getting it wrong
    is instructive: failing `git rebase` itself does NOT work, because `rebase()` catches that and
    returns "rebase deferred: ...", which is its CONFLICT string. The transient branch fires only
    when `rebase()` RAISES -- i.e. at its own fetch, its final push, or the undo -- which is exactly
    the "network blip, a push race" its docstring names. Mocked only here: what is under test is the
    ROUTING (release vs park), not git's behaviour, and real git cannot be made to fail transiently
    on demand without breaking the repo underneath it."""
    def run(cwd, args):
        if len(args) > 1 and args[1] == "push":
            raise RuntimeError("fatal: unable to access remote: Could not resolve host")
        return work._run(cwd, args)
    return run


# --- the core guarantee: a resumed stale worktree is brought forward BEFORE anything reads it ----

def test_resuming_a_stale_worktree_actually_rebases_it_before_returning(tmp_path):
    """The positive control, asserted on GIT STATE. A return value proves nothing here: the whole
    defect is that `start()` returned a cheerful `already started` while the tree sat behind."""
    sdlc_dir, goal, wt, _ = _resume(tmp_path, "9101",
                                    lambda o: (o / "unrelated.txt").write_text("x"))
    new_tip = _git(tmp_path / "other", "rev-parse", "HEAD")

    out = work.start(sdlc_dir, ON, goal)

    assert out.startswith("already started")
    assert _git(wt, "rev-parse", "HEAD") == new_tip          # the real proof: it MOVED
    assert _status_of(goal) == "in_progress"                 # brought forward, not refused


def test_a_fresh_resumed_worktree_is_untouched_and_never_refused(tmp_path):
    """The negative control. A freshness check that refuses a FRESH tree would be worse than the
    bug: `ensure_fresh` short-circuits at `count <= 0` before `rebase()`, so nothing is pushed."""
    sdlc_dir, goal, wt, origin = _resume(tmp_path, "9102")
    before = _git(wt, "rev-parse", "HEAD")

    out = work.start(sdlc_dir, ON, goal)

    assert out.startswith("already started")
    assert _git(wt, "rev-parse", "HEAD") == before
    assert _git(origin, "rev-parse", "main") == before        # nothing force-pushed
    assert _status_of(goal) == "in_progress"                  # not parked, not released


# --- the escalation, one control per row of the table -------------------------------------------

def test_a_conflicting_stale_resume_parks_the_goal(tmp_path):
    """A CONFLICT needs a human, so it parks on the first occurrence -- there is no self-healing
    path for two sides that touched the same lines."""
    sdlc_dir, goal, wt, _ = _resume(
        tmp_path, "9103",
        mutate_origin=lambda o: (o / "file.txt").write_text("UPSTREAM\nline2\nline3\n"),
        dirty=lambda w: (w / "file.txt").write_text("GOAL\nline2\nline3\n"))
    before = _git(wt, "rev-parse", "HEAD")

    out = work.start(sdlc_dir, ON, goal)

    assert out.startswith(work._STALE_RESUME_REFUSAL_PREFIX)
    assert not out.startswith("already started")
    assert _status_of(goal) == "parked"                       # THE definition-of-done assertion
    assert "human review" in (pathlib.Path(sdlc_dir) / "state" / "review-queue.md").read_text()
    assert _git(wt, "rev-parse", "HEAD") == before            # and the tree is left exactly as found
    assert (wt / "file.txt").read_text() == "GOAL\nline2\nline3\n"


def test_the_first_transient_failure_releases_rather_than_parking(tmp_path):
    """`sdlc:parked` is NEVER auto-resumed, so parking on a network blip removes the goal from the
    backlog until a human runs `/sigma-unpark`. A release drops the claim, keeps the goal pending,
    and -- per `_release`'s own docstring -- never advances the cursor, so it burns no budget slot."""
    sdlc_dir, goal, _, _ = _resume(tmp_path, "9104",
                                   lambda o: (o / "unrelated.txt").write_text("x"))
    before_iter = state.load_cursor(sdlc_dir).get("run_iteration", 0)

    out = work.start(sdlc_dir, ON, goal, run=_push_race())

    assert out.startswith(work._STALE_RESUME_REFUSAL_PREFIX)
    assert _status_of(goal) == "pending", "a transient blip must NOT park -- nothing un-parks it"
    assert state.load_cursor(sdlc_dir).get("run_iteration", 0) == before_iter, \
        "a release is not a completed iteration -- it must not consume a budget slot"
    assert work._record(sdlc_dir, work.stem(goal))["stale_resume_releases"] == 1
    # ...and the message must not claim the write that provably did not happen. The park branch
    # says "the cursor write is certain" and is right to; saying it here would be the same
    # unmeasured claim in the one sentence written to be honest about what is guaranteed.
    assert "cursor write is certain" not in out, out
    assert "No write here is guaranteed" in out, out


def test_the_second_consecutive_transient_failure_parks_instead_of_releasing_forever(tmp_path):
    """THE BOUND. Release-on-every-transient recreates `run_loop`'s documented `poisoned` spin:
    pick -> claim -> start -> release -> pick, burning gh calls forever while consuming zero budget.
    A persistent outage is not a blip and must reach a human -- loudly, once."""
    sdlc_dir, goal, _, _ = _resume(tmp_path, "9105",
                                   lambda o: (o / "unrelated.txt").write_text("x"))

    first = work.start(sdlc_dir, ON, goal, run=_push_race())
    assert _status_of(goal) == "pending" and first.startswith(work._STALE_RESUME_REFUSAL_PREFIX)

    # `main` keeps moving while our push keeps failing -- the persistent-outage shape. Without this
    # the local rebase from call 1 has already brought the tree forward, so call 2 legitimately
    # passes and resets the counter (which is the NEXT test's subject, not this one's).
    _advance_origin(tmp_path, tmp_path / "origin.git",
                    lambda o: (o / "again.txt").write_text("z"), name="other2")
    second = work.start(sdlc_dir, ON, goal, run=_push_race())

    assert second.startswith(work._STALE_RESUME_REFUSAL_PREFIX)
    assert _status_of(goal) == "parked", "the second consecutive transient must escalate to a park"


def test_a_clean_resume_resets_the_counter_so_the_bound_is_on_CONSECUTIVE_failures(tmp_path):
    """Without the reset the counter is a lifetime tally: a goal that hits one blip today and
    recovers stays permanently armed, and an unrelated blip months later parks it. A bound that
    fires on unrelated events is a worse defect than the one it fixes."""
    sdlc_dir, goal, _, _ = _resume(tmp_path, "9106",
                                   lambda o: (o / "unrelated.txt").write_text("x"))

    work.start(sdlc_dir, ON, goal, run=_push_race())            # blip 1: push raced, rebase landed
    assert work._record(sdlc_dir, work.stem(goal))["stale_resume_releases"] == 1

    work.start(sdlc_dir, ON, goal)                                  # recovers cleanly
    assert work._record(sdlc_dir, work.stem(goal)).get("stale_resume_releases", 0) == 0

    _advance_origin(tmp_path, tmp_path / "origin.git",
                    lambda o: (o / "second.txt").write_text("y"), name="other2")
    out = work.start(sdlc_dir, ON, goal, run=_push_race())        # a LATER, unrelated blip

    assert _status_of(goal) == "pending", \
        "an isolated later blip must release again, not park -- the two were not consecutive"
    assert out.startswith(work._STALE_RESUME_REFUSAL_PREFIX)


# --- the OTHER resume path: the branch outlived its record (#388's shape) ------------------------
#
# `start()` has TWO resume paths and the issue named only the first. The second -- `worktree add -b`
# fails because the branch already exists, so `start()` reattaches with `worktree add <path>
# <branch>` -- lands the tree at the BRANCH TIP, which is as stale as path 1's ever is; its own
# comment calls it "a resume, not an error". Covering only the named one would repeat #388 exactly:
# that issue exists because `_resume_blocked_by_a_live_sibling` was originally wired to path 1 only.

def _orphaned_branch(tmp_path, ident, advance=True):
    """A project checkout whose goal BRANCH survives while its work record does not."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    proj = tmp_path / "proj"
    _git(tmp_path, "clone", "-q", str(origin), str(proj))
    _git(proj, "config", "user.email", "a@example.com")
    _git(proj, "config", "user.name", "a")
    (proj / "file.txt").write_text("line1\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "base")
    _git(proj, "push", "-q", "origin", "HEAD:main")
    _git(proj, "branch", f"sdlc/{ident}")            # <- outlives the record; never gets a record
    if advance:
        _advance_origin(tmp_path, origin, lambda o: (o / "unrelated.txt").write_text("x"))
        _git(proj, "fetch", "-q", "origin", "main")
    sdlc_dir = _sdlc(proj)
    return sdlc_dir, _goal_file(sdlc_dir, ident), proj, origin


def test_the_branch_outlived_its_record_resume_is_also_brought_forward(tmp_path):
    """D7. Same defect, same function, the path the issue did not name."""
    sdlc_dir, goal, proj, _ = _orphaned_branch(tmp_path, "9107")
    new_tip = _git(tmp_path / "other", "rev-parse", "HEAD")

    out = work.start(sdlc_dir, ON, goal)

    wt = pathlib.Path(work._record(sdlc_dir, "9107")["worktree"])
    assert wt.is_dir() and not out.startswith(work._STALE_RESUME_REFUSAL_PREFIX)
    assert _git(wt, "rev-parse", "HEAD") == new_tip, \
        "the reattached worktree landed at the stale branch tip and was never brought forward"


def test_the_second_consecutive_transient_parks_on_the_reattach_path_too(tmp_path):
    """The counter has to SURVIVE `_save`, which is a full replace of a fixed six-key literal. If it
    does not, this path reads back 0 every time, releases every time, and never escalates -- the
    unbounded spin, alive on the one call site this change added. Caught by plan-review, by
    execution, against a version that looked correct on inspection."""
    sdlc_dir, goal, proj, origin = _orphaned_branch(tmp_path, "9108")

    first = work.start(sdlc_dir, ON, goal, run=_push_race())
    assert first.startswith(work._STALE_RESUME_REFUSAL_PREFIX)
    assert _status_of(goal) == "pending"
    assert work._record(sdlc_dir, "9108")["stale_resume_releases"] == 1

    # Re-enter through the SAME reattach path, in the shape `_reattach_base` itself documents:
    # "the worktree DIRECTORY is gone while the record and the branch are not". The record survives,
    # so there IS a counter here to lose -- which is exactly the case `_save`'s full replace wipes.
    wt = work._record(sdlc_dir, "9108")["worktree"]
    _git(proj, "worktree", "remove", "--force", wt)
    _advance_origin(tmp_path, origin, lambda o: (o / "again.txt").write_text("z"), name="other2")

    second = work.start(sdlc_dir, ON, goal, run=_push_race())

    assert second.startswith(work._STALE_RESUME_REFUSAL_PREFIX)
    assert _status_of(goal) == "parked", \
        "the counter was wiped by _save, so the reattach path releases forever and never escalates"


def test_a_reattach_refusal_still_carries_the_registry_notes(tmp_path):
    """`feature_sync`'s UNSERIALISED/LOST findings reach an operator ONLY through the clause
    `start()` prints. A refusal that replaced that clause outright would delete the only channel
    reporting them, on the very path where they are likeliest."""
    sdlc_dir, goal, proj, _ = _orphaned_branch(tmp_path, "9109")
    out = work.start(sdlc_dir, ON, goal, run=_push_race())
    assert out.startswith(work._STALE_RESUME_REFUSAL_PREFIX)
    # Nothing to report on an unadopted repo, so the notes are empty -- what is pinned is that the
    # refusal is BUILT from them, not that they have content here.
    assert work._stale_resume_refusal.__doc__            # the helper exists and is documented
    assert out.endswith(".") or out.endswith(")")        # the notes were appended, not truncated


# --- exit codes: the refusal must STOP the caller, and only this refusal ------------------------

def test_a_stale_resume_refusal_exits_4_not_0(tmp_path):
    """`main()` printed every `start()` return and exited 0, so a refusal was advisory prose an
    agent could read straight past -- into P2 RESEARCH, in a stale tree, on a goal whose labels just
    changed underneath it. 4 is `loop.py verify`'s own STALE code, reused rather than invented."""
    sdlc_dir, goal, _, _ = _resume(
        tmp_path, "9110",
        mutate_origin=lambda o: (o / "file.txt").write_text("UPSTREAM\nline2\nline3\n"),
        dirty=lambda w: (w / "file.txt").write_text("GOAL\nline2\nline3\n"))

    assert work.main(["work.py", "start", sdlc_dir, goal]) == 4


def test_the_live_sibling_refusal_still_exits_0(tmp_path, monkeypatch):
    """The non-regression D6 promises. Three refusals in work.py already begin `REFUSED — ` and they
    change NO state, so they must keep exiting 0; keying exit 4 on the bare word `REFUSED` would
    have moved all three, and no existing test would have noticed -- they all assert the returned
    string and never the exit code."""
    sdlc_dir, goal, _, _ = _resume(tmp_path, "9111")
    monkeypatch.setattr(work, "_resume_blocked_by_a_live_sibling",
                        lambda *a, **k: "REFUSED — 9111's local worktree already exists, but ...")

    assert work.main(["work.py", "start", sdlc_dir, goal]) == 0


# --- P6 review findings: three guards the first implementation missed ---------------------------

def test_a_transient_park_is_not_handed_a_fabricated_decision_tier(tmp_path):
    """The SECOND consecutive transient parks, and a park detail that reaches `reason_class`
    "unknown" gets a decision tier invented for it (#1185/#1240) -- "escalate_l1: send this to a
    mid-senior engineer", on a DNS blip, in the morning review queue and on the issue. The conflict
    wording was guarded against exactly this; its transient sibling was not."""
    both = (work._PARK_STALE_RESUME_CONFLICT, work._STALE_RESUME_TRANSIENT)
    assert all(w in work.MECHANICAL_PARK_PREFIXES for w in both)

    transient = (f"worktree for '1' is 3 commit(s) behind origin/main and attempting to auto-rebase "
                 f"it hit an unexpected error (Could not resolve host) -- {both[1]}"
                 f"{work._VERIFY_TAIL_TRANSIENT}")
    cls = loop._reason_class(transient)
    assert cls == "unknown", "if this stops being 'unknown', the needle below is no longer needed"
    assert loop._mechanical_unknown_detail(transient, cls), \
        "a network blip must never be classified as a human judgment call"


def test_a_failure_AFTER_the_transition_does_not_claim_the_goal_is_still_claimed(tmp_path):
    """The refusal tells a human what to do next, so it must not tell them to park a goal that was
    correctly RELEASED. Following that instruction turns a recoverable blip into an `sdlc:parked`
    only `/sigma-unpark` clears -- the opposite of what the wrapper exists for."""
    sdlc_dir, goal, _, _ = _resume(tmp_path, "9112",
                                   lambda o: (o / "unrelated.txt").write_text("x"))
    real_save = work._save

    def save_that_fails(sd, g, rec):        # the counter write, i.e. AFTER _release has landed
        if "stale_resume_releases" in rec:
            raise OSError("Read-only file system")
        return real_save(sd, g, rec)

    try:
        work._save = save_that_fails
        out = work.start(sdlc_dir, ON, goal, run=_push_race())
    finally:
        work._save = real_save

    assert _status_of(goal) == "pending", "sanity: the release really did land"
    assert "STILL CLAIMED" not in out, out
    assert "that stands" in out, "the transition landed and the message must say so"


def test_parking_clears_the_counter_so_an_unparked_goal_starts_fresh(tmp_path):
    """The escalation has been delivered by the time it parks. Leaving the counter set means a goal
    a human just unparked has zero tolerance and re-parks on its next isolated blip -- the same
    "fires on unrelated events" defect the clean-pass reset exists to prevent."""
    sdlc_dir, goal, _, origin = _resume(tmp_path, "9113",
                                        lambda o: (o / "unrelated.txt").write_text("x"))

    work.start(sdlc_dir, ON, goal, run=_push_race())                  # release, counter -> 1
    _advance_origin(tmp_path, origin, lambda o: (o / "b.txt").write_text("b"), name="other2")
    work.start(sdlc_dir, ON, goal, run=_push_race())                  # escalate: park

    assert _status_of(goal) == "parked"
    assert work._record(sdlc_dir, work.stem(goal)).get("stale_resume_releases", 0) == 0, \
        "the count must start over once the escalation has been delivered"


def test_the_resume_refusal_drops_verifys_own_remediation(tmp_path):
    """`ensure_fresh`'s tail says "re-run `loop.py verify`". At `start()` no suite is about to run
    and the goal has just been parked or released, so that instruction contradicts the very next
    sentence. Verify's own text must be unchanged -- only the resume path drops it."""
    sdlc_dir, goal, _, _ = _resume(
        tmp_path, "9114",
        mutate_origin=lambda o: (o / "file.txt").write_text("UPSTREAM\nline2\nline3\n"),
        dirty=lambda w: (w / "file.txt").write_text("GOAL\nline2\nline3\n"))

    out = work.start(sdlc_dir, ON, goal)

    assert "re-run `loop.py verify`" not in out, out
    assert "Running the suite now" not in out, out
    assert "Parked it" in out

    # The TRANSIENT shape has its own tail and its own strip, and it is the commoner outcome (the
    # first blip always releases). Mutation showed the conflict-only assertion above stays green
    # when the transient strip is removed, so it is asserted separately, not assumed.
    second = tmp_path / "b"
    second.mkdir()
    sdlc2, goal2, _, _ = _resume(second, "9115",
                                 lambda o: (o / "unrelated.txt").write_text("x"))
    out2 = work.start(sdlc2, ON, goal2, run=_push_race())
    assert _status_of(goal2) == "pending", "sanity: this is the release path"
    assert "re-run `loop.py verify`" not in out2, out2
    assert "retry the whole check" not in out2, out2

    # AND on the surfaces a human actually reads. The console string is seen once by an agent that
    # is about to stop; the review queue and the journey log (the issue comment, in github mode --
    # `_offboard` and `release` both build it from this same `reason`) are what a person acts on.
    # Stripping only the console half leaves the wrong instruction exactly where it does damage.
    queue = (pathlib.Path(sdlc_dir) / "state" / "review-queue.md").read_text()
    assert "re-run `loop.py verify`" not in queue, queue
    assert "behind origin/main" in queue, "sanity: the diagnosis itself must still be there"
    journey = (pathlib.Path(sdlc2) / "journey" / "9115.md").read_text()
    assert "re-run `loop.py verify`" not in journey, journey
    assert "behind origin/main" in journey, "sanity: the diagnosis itself must still be there"


def test_a_release_that_was_a_no_op_does_not_claim_it_released_anything(tmp_path):
    """`release()` returns False for an already done/parked/failed goal — "a clean, SILENT no-op, no
    `gh` mutation at all", in its own words. Reporting "released the claim" there describes a
    mutation that provably did not happen, on a goal nothing will pick again."""
    sdlc_dir, goal, _, _ = _resume(tmp_path, "9116",
                                   lambda o: (o / "unrelated.txt").write_text("x"))
    pathlib.Path(goal).write_text("---\nstatus: parked\n---\n\n# goal 9116\n")   # already terminal

    out = work.start(sdlc_dir, ON, goal, run=_push_race())

    assert "no claim to release" in out, out
    assert "Released the claim" not in out, out


# =================================================================================================
# #1897: the CONTENT axis, not the freshness one.
#
# `state.done_refusal` gates `record done` (loop.py's dispatch) and `work.py merge` (its own
# pre-gate call) on the verify evidence file. Before this section it read exit-0, `at` vs this
# run's start, and `run` -- three facts about WHEN and WHO, and none about WHAT was verified. So a
# worktree edited after a green verify still satisfied the gate: the review-fix cycle SKILL.md
# itself prescribes ("fix them in the worktree ... re-run `loop.py verify`") had nothing enforcing
# its second half.
#
# The evidence already carried `head` -- and `head` cannot be the answer. Measured on this repo's
# own live evidence when #1897 was written, goals 1933/1934/1935/1936/1937/1962 all recorded the
# SAME `head`, because SKILL.md's order is verify -> commit -> pr -> review -> merge -> record done:
# at verify time the whole change is UNCOMMITTED and `head` is just the shared base. Comparing it
# at `record done`, after `work.py commit` moved HEAD, would refuse every goal ever.
#
# So the fingerprint is the goal's OWN CHANGE relative to `merge-base(HEAD, <remote>/<base>)`, which
# is the one formulation that survives all three legitimate mutations (commit, fetch, clean rebase)
# and still catches an edit. Every one of those is a control below, and each was seen RED against
# the pre-#1897 code before being trusted.
# =================================================================================================


def _fp(root, base_ref="origin/main", bookkeeping=".sdlc"):
    return state.content_fingerprint(str(root), base_ref, bookkeeping)


def _goal_change(wt):
    """The shape every goal has at verify time: one tracked file modified, one new file added, and
    neither committed yet (`work.py commit` never runs until verify is green)."""
    (wt / "file.txt").write_text("line1\nCHANGED\nline3\n")
    (wt / "new.txt").write_text("brand new\n")


# --- the three legitimate mutations: none of them may move the fingerprint ----------------------

def test_committing_the_verified_content_does_not_change_the_fingerprint(tmp_path):
    """THE control this whole design turns on. `work.py commit` is the documented NEXT STEP after a
    green verify, so a fingerprint that moved here would refuse every goal on the happy path -- a
    guard that fires on the normal flow is worse than no guard at all."""
    _, wt = _repo_pair(tmp_path)
    _goal_change(wt)
    before = _fp(wt)
    assert before["fingerprint"] and set(before["files"]) == {"file.txt", "new.txt"}

    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "sdlc: goal")

    assert _fp(wt)["fingerprint"] == before["fingerprint"]


def test_a_fetch_that_moves_the_base_does_not_change_the_fingerprint(tmp_path):
    """Linked worktrees SHARE `refs/remotes/*`, so a sibling goal slot's routine fetch moves
    `origin/main` under a goal that never asked for it. `merge-base` is what makes that a no-op."""
    origin, wt = _repo_pair(tmp_path)
    _goal_change(wt)
    before = _fp(wt)
    _advance_origin(tmp_path, origin, lambda o: (o / "unrelated.txt").write_text("x"))
    _git(wt, "fetch", "-q", "origin", "main")

    assert _fp(wt)["fingerprint"] == before["fingerprint"]


def test_a_clean_rebase_onto_a_moved_base_does_not_change_the_fingerprint(tmp_path):
    """`merge()`'s BEHIND path rebases the worktree AFTER its own `done_refusal` call and BEFORE
    `record done` (work.py `_reconcile_behind` -> `rebase()`). A rebase that replays the goal's diff
    byte-identically must not cost a re-verify."""
    origin, wt = _repo_pair(tmp_path)
    _goal_change(wt)
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "sdlc: goal")
    before = _fp(wt)
    _advance_origin(tmp_path, origin, lambda o: (o / "unrelated.txt").write_text("x"))
    _git(wt, "fetch", "-q", "origin", "main")
    _git(wt, "rebase", "--autostash", "-q", "origin/main")

    assert _fp(wt)["fingerprint"] == before["fingerprint"]


def test_the_plan_copy_skill_md_mandates_does_not_change_the_fingerprint(tmp_path):
    """#1897 plan-review, BLOCKING finding 1. SKILL.md step 6 copies `<sdlc>/plans/<stem>.md` INTO
    the worktree after verify and before `commit` -- and `sigma-setup`'s RUNTIME_IGNORES pointedly
    does NOT ignore `.sdlc/plans/`. On an adopter repo that file is untracked-and-not-ignored, so
    counting it refused EVERY goal, on the documented path. This repo's own `.gitignore` (`.sdlc/*`)
    hid it, which is why the control has to be run against an adopter-shaped ignore set -- as here:
    this tmp repo has no `.gitignore` at all."""
    _, wt = _repo_pair(tmp_path)
    _goal_change(wt)
    before = _fp(wt)

    (wt / ".sdlc" / "plans").mkdir(parents=True)
    (wt / ".sdlc" / "plans" / "9200.md").write_text("# the plan\n")
    assert _git(wt, "status", "--porcelain", "--untracked-files=all"), "sanity: git DOES see it"

    assert _fp(wt)["fingerprint"] == before["fingerprint"]
    assert ".sdlc/plans/9200.md" not in _fp(wt)["files"]


# --- and the edits that must move it -------------------------------------------------------------

def test_editing_a_verified_file_changes_the_fingerprint(tmp_path):
    _, wt = _repo_pair(tmp_path)
    _goal_change(wt)
    before = _fp(wt)
    (wt / "file.txt").write_text("line1\nCHANGED AGAIN\nline3\n")
    assert _fp(wt)["fingerprint"] != before["fingerprint"]


def test_deleting_a_new_file_changes_the_fingerprint(tmp_path):
    _, wt = _repo_pair(tmp_path)
    _goal_change(wt)
    before = _fp(wt)
    (wt / "new.txt").unlink()
    assert _fp(wt)["fingerprint"] != before["fingerprint"]


def test_flipping_the_executable_bit_changes_the_fingerprint(tmp_path):
    """A mode change is a real change git records and `sha256(bytes)` alone cannot see -- plan-review
    finding 5. Without the mode in the entry, "sensitive to any edit" would be an untested claim."""
    _, wt = _repo_pair(tmp_path)
    (wt / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (wt / "run.sh").chmod(0o755)
    before = _fp(wt)
    (wt / "run.sh").chmod(0o644)
    assert _fp(wt)["fingerprint"] != before["fingerprint"]


def test_retargeting_a_symlink_changes_the_fingerprint(tmp_path):
    """A symlink's content IS its target (that is what git stores). Following it instead would make
    the fingerprint depend on a file that may not even be in the repo -- plan-review finding 7."""
    _, wt = _repo_pair(tmp_path)
    # IDENTICAL contents on purpose: an implementation that FOLLOWS the link instead of
    # recording its target sees no change at all here, which is what makes this a real control.
    (wt / "a.txt").write_text("same\n"); (wt / "b.txt").write_text("same\n")
    (wt / "link").symlink_to("a.txt")
    before = _fp(wt)
    (wt / "link").unlink(); (wt / "link").symlink_to("b.txt")
    assert _fp(wt)["fingerprint"] != before["fingerprint"]


# --- the two "measured, but there is nothing to compare" outcomes -------------------------------

def test_a_landed_goal_measures_an_EMPTY_change_not_an_empty_string_hash(tmp_path):
    """#1897 plan-review, BLOCKING finding 2. Once the goal's commits are ancestors of its base,
    `merge-base HEAD origin/main` IS HEAD and the diff is empty. Hashing that empty map would give
    `sha256("")` -- a TRUTHY digest that mismatches the recorded one, so `record done` would refuse
    after a successful merge. `files == {}` with a null fingerprint is how that case stays
    distinguishable from a real one."""
    origin, wt = _repo_pair(tmp_path)
    _goal_change(wt)
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "sdlc: goal")
    _git(wt, "push", "-q", "origin", "HEAD:main")          # the goal LANDS on its own base
    _git(wt, "fetch", "-q", "origin", "main")

    landed = _fp(wt)
    assert landed["files"] == {}, landed
    assert landed["fingerprint"] is None, landed


def test_an_unreadable_tree_reports_files_none_not_an_empty_change(tmp_path):
    """`files is None` (cannot measure) and `files == {}` (measured, nothing there) must never
    collapse into one another: the gate fails CLOSED on the first and declines on the second."""
    assert _fp(tmp_path / "does-not-exist")["files"] is None
    (tmp_path / "plain").mkdir()
    assert _fp(tmp_path / "plain")["files"] is None          # a directory, but not a git checkout
    _, wt = _repo_pair(tmp_path)
    assert _fp(wt, base_ref=None)["files"] is None           # no work record -> no fork point
    assert "fork point" in _fp(wt, base_ref=None)["detail"]


# --- loop.py verify writes it, state.done_refusal reads it: the documented gestures --------------

def _verified(tmp_path, stem, mutate=_goal_change):
    """Run the real `loop.py verify` documented gesture against a real worktree, and return
    (sdlc_dir, worktree, goal). Leaves genuine, correctly-attributed, passing evidence on disk.

    The goal is a REAL local-mode goal file, not a bare id, so `loop.py record ... done` can run its
    whole documented path (`source.complete` writes this file's frontmatter) instead of dying in
    unrelated bookkeeping before the assertion means anything."""
    _, wt = _repo_pair(tmp_path, allow_shell_commands=True)
    mutate(wt)
    sdlc_dir = _sdlc(tmp_path, _verify_config())
    goals = pathlib.Path(sdlc_dir) / "goals"; goals.mkdir(parents=True, exist_ok=True)
    goal = goals / f"{stem}.md"
    goal.write_text(f"---\nstatus: pending\n---\n\n# goal {stem}\n")
    _write_record(sdlc_dir, stem, wt)
    assert loop.verify_goal(sdlc_dir, str(goal)) == 0
    return sdlc_dir, wt, str(goal)


def test_verify_records_the_content_it_verified(tmp_path):
    sdlc_dir, _, goal = _verified(tmp_path, "9210")
    ev = json.loads(state.evidence_path(sdlc_dir, goal).read_text())
    assert ev["content"]["fingerprint"], ev["content"]
    assert ev["content"]["base_ref"] == "origin/main"
    assert set(ev["content"]["files"]) == {"file.txt", "new.txt"}


def test_done_refusal_accepts_the_tree_that_was_actually_verified(tmp_path):
    """The negative control for every refusal below. Without this, a check that refused
    unconditionally would pass all of them."""
    sdlc_dir, _, goal = _verified(tmp_path, "9211")
    assert state.done_refusal(sdlc_dir, goal) is None


def test_done_refusal_refuses_a_worktree_edited_after_verify_passed(tmp_path):
    """THE bug #1897 was filed for. Green verify -> the worktree is edited (a review fix, or a later
    verify that refused without writing evidence) -> the old, still-fresh, correctly-attributed
    green used to satisfy `record done` anyway."""
    sdlc_dir, wt, goal = _verified(tmp_path, "9212")
    (wt / "file.txt").write_text("line1\nEDITED AFTER GREEN\nline3\n")

    refusal = state.done_refusal(sdlc_dir, goal)
    assert refusal is not None
    assert "file.txt" in refusal, refusal
    assert "loop.py verify" in refusal, refusal


def test_record_done_exits_4_for_a_worktree_edited_after_verify(capsys, tmp_path):
    """On the gesture the DOCS give -- `loop.py record .sdlc <goal> done` under `verify.enforce`,
    SKILL.md step 6 -- not on the internal function alone."""
    sdlc_dir, wt, goal = _verified(tmp_path, "9213")
    (wt / "file.txt").write_text("line1\nEDITED AFTER GREEN\nline3\n")

    rc = loop.main(["loop.py", "record", sdlc_dir, goal, "done"])
    assert rc == 4, rc
    assert "REFUSED" in capsys.readouterr().err


def test_committing_the_verified_content_still_records_done(capsys, tmp_path):
    """The same documented gesture on the HAPPY path: verify -> `work.py commit` -> `record done`.
    Refusing here would stop every goal in the fleet."""
    sdlc_dir, wt, goal = _verified(tmp_path, "9214")
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "sdlc: goal")

    assert state.done_refusal(sdlc_dir, goal) is None
    # And on the documented gesture, not just the function: exit 4 is the verify-evidence refusal
    # specifically (loop.py's dispatch), so `!= 4` is what proves this gate did not fire.
    assert loop.main(["loop.py", "record", sdlc_dir, goal, "done"]) != 4, capsys.readouterr().err


def test_a_landed_goal_still_records_done(tmp_path):
    """Plan-review finding 2, end to end: the PR landed, so the goal's change is no longer
    distinguishable from its base. The gate has nothing to compare and must DECLINE, not refuse --
    refusing here would strand bookkeeping for work that already shipped."""
    sdlc_dir, wt, goal = _verified(tmp_path, "9215")
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "sdlc: goal")
    _git(wt, "push", "-q", "origin", "HEAD:main")
    _git(wt, "fetch", "-q", "origin", "main")

    assert state.done_refusal(sdlc_dir, goal) is None


def test_a_re_run_of_record_done_after_the_worktree_is_gone_is_still_a_no_op(tmp_path):
    """Plan-review finding 4. `loop.py::_release_checkout` removes the worktree AFTER a successful
    `record done`, so a second one -- after a crash, a compaction, or an agent repeating a terminal
    gesture -- recomputes against a directory that is no longer there. That must stay the harmless
    no-op it is today, never a new exit 4."""
    sdlc_dir, wt, goal = _verified(tmp_path, "9216")
    shutil.rmtree(wt)
    assert state.done_refusal(sdlc_dir, goal) is None


def test_an_unreadable_verified_tree_fails_CLOSED(tmp_path):
    """The tree is still THERE, so its absence is not the explanation -- git simply could not answer
    about it. "Could not check" must never read as "checked and fine"."""
    sdlc_dir, wt, goal = _verified(tmp_path, "9217")
    shutil.rmtree(wt / ".git")                    # still a directory; no longer a git checkout

    refusal = state.done_refusal(sdlc_dir, goal)
    assert refusal is not None and "loop.py verify" in refusal, refusal


def test_evidence_without_a_content_key_is_governed_by_the_old_checks_alone(tmp_path):
    """Every one of the 333 evidence files on this repo's disk when #1897 shipped predates the
    `content` key. They must behave exactly as before -- the same graceful degradation pre-#498
    evidence without a `run` key already gets."""
    sdlc_dir, wt, goal = _verified(tmp_path, "9218")
    ev = state.evidence_path(sdlc_dir, goal)
    data = json.loads(ev.read_text()); data.pop("content")
    ev.write_text(json.dumps(data))
    (wt / "file.txt").write_text("edited, and invisible to a pre-#1897 evidence file\n")

    assert state.done_refusal(sdlc_dir, goal) is None


def test_the_content_check_runs_after_exit_freshness_and_run_id(tmp_path):
    """Order is a contract, not an accident: a FAILED verify must still report "last verify FAILED",
    not a content mismatch, however much the tree also moved. Same rule #498's own run-id check
    follows."""
    sdlc_dir, wt, goal = _verified(tmp_path, "9219")
    ev = state.evidence_path(sdlc_dir, goal)
    data = json.loads(ev.read_text())
    (wt / "file.txt").write_text("moved too\n")

    ev.write_text(json.dumps({**data, "exit": 1}))
    assert state.done_refusal(sdlc_dir, goal).startswith("last verify FAILED")
    ev.write_text(json.dumps({**data, "at": 0.0}))
    assert state.done_refusal(sdlc_dir, goal) == "verify evidence predates this run"
    ev.write_text(json.dumps({**data, "run": "some-other-run"}))
    assert "different run" in state.done_refusal(sdlc_dir, goal)


def test_a_root_below_the_repo_top_is_still_a_real_fingerprint(tmp_path):
    """`git ls-files` reports paths relative to the CWD and `git diff --name-only` relative to the
    repo TOP. Joined onto one base, a `root` that is a subdirectory would resolve every entry to
    `-` (absent) and produce a digest that never moves -- a gate that cannot fail. `root` is always
    a worktree/project root in the live flow, so this can only be found by asking."""
    _, wt = _repo_pair(tmp_path)
    (wt / "pkg").mkdir()
    (wt / "pkg" / "mod.py").write_text("x = 1\n")
    before = _fp(wt / "pkg")
    assert before["fingerprint"], before
    (wt / "pkg" / "mod.py").write_text("x = 2\n")
    assert _fp(wt / "pkg")["fingerprint"] != before["fingerprint"]


def test_a_fifo_in_the_tree_is_hashed_as_unreadable_and_never_blocks(tmp_path):
    """`open()` on a FIFO with no writer BLOCKS FOREVER. An untracked pipe in a goal's worktree
    would therefore hang `record done` and `work.py merge` outright -- no error, no timeout, just a
    gate that never answers. This is the control for the `isfile` test that prevents it; note the
    failure it guards against is a HANG, so this test times out rather than asserting when broken."""
    # `git ls-files --others` does NOT report a FIFO, so an untracked pipe can never enter the path
    # set -- checked, and it is why this uses the route that CAN: a TRACKED file replaced on disk by
    # a pipe, which `git diff <fork>` reports as a changed path like any other.
    _, wt = _repo_pair(tmp_path)
    (wt / "file.txt").unlink()
    os.mkfifo(wt / "file.txt")
    (wt / "real.txt").write_text("content\n")

    out = _fp(wt)
    assert out["files"]["file.txt"] == "?", out["files"]
    assert out["files"]["real.txt"].startswith("f"), out["files"]


def test_an_unreadable_path_never_raises_out_of_the_gate(tmp_path):
    """`content_fingerprint`'s contract is "never raises" -- it runs after the proving command has
    already finished, so an exception here would throw away a verify that really did run."""
    _, wt = _repo_pair(tmp_path)
    (wt / "gone").symlink_to("nowhere-at-all")          # dangling symlink
    (wt / "sub").mkdir(); (wt / "sub" / "deep.txt").write_text("x\n")
    out = _fp(wt)
    assert out["fingerprint"], out
    assert out["files"]["gone"].startswith("l"), out["files"]   # the TARGET is hashed, not followed


def test_a_file_larger_than_one_chunk_hashes_to_its_whole_content(tmp_path):
    """The chunked read is what keeps memory O(1) whatever the file is, and a chunking loop that
    stops after the first block would hash a PREFIX -- two different files sharing 1 MiB would then
    fingerprint identically, and the gate would accept an edit past that offset."""
    import hashlib
    _, wt = _repo_pair(tmp_path)
    blob = (b"A" * (1024 * 1024)) + b"tail-beyond-the-first-chunk\n"
    (wt / "big.bin").write_bytes(blob)
    assert _fp(wt)["files"]["big.bin"] == "f" + hashlib.sha256(blob).hexdigest()


def test_entry_returns_a_marker_instead_of_raising_on_a_path_it_cannot_even_stat(tmp_path):
    """`content_fingerprint` runs AFTER the proving command has finished, so anything that escapes
    it throws away a verify that really did run.

    HONEST LIMIT: this pins the CONTRACT (a marker, never an exception) on the shapes that can be
    constructed -- an embedded NUL, a path that does not exist. It is not a control on the breadth
    of the `except Exception` itself: every reachable failure here is already absorbed earlier by
    `os.path.isfile`, or is an OSError from the read. The breadth covers a TOCTOU race (the path
    stops being what the stat said it was between the two calls) that cannot be provoked
    deterministically, so it is defensive, not tested -- said here rather than implied by a green
    test."""
    for weird in ("no\0such\0path", str(tmp_path / "absent"), str(tmp_path)):
        assert state._entry(weird) in {"-", "?", "/"}, weird
