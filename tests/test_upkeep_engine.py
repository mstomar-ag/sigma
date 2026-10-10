"""Goal 922: the rest of the upkeep pass under the opt-in. Pure functions first, then real local git repositories.

No network, no model. Gate closed means every path below is byte-identical to before; the closed-gate tests prove it."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

import upkeep_support as support

OPEN = {"upkeep": {"enabled": True}}
ROOT = pathlib.Path(__file__).resolve().parents[1]


def mod():
    return support.script("feature_upkeep_pass")


def work():
    return support.script("work")


def brief():
    path = ROOT / "skills" / "sigma-rebase" / "scripts" / "rebase_brief.py"
    spec = importlib.util.spec_from_file_location("rebase_brief_922", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def git(cwd, *argv, check=True):
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": os.environ["PATH"]}
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *argv],
                         cwd=str(cwd), capture_output=True, text=True, check=check, env=env)
    return out.stdout.strip()


def real_run(cwd, argv):
    argv = [str(a) for a in argv]
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": os.environ["PATH"], "GIT_EDITOR": "true"}
    if argv[0] == "git":
        argv = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"] + argv[1:]
    out = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip() or "exit %d" % out.returncode)
    return out.stdout.strip()


# ------------------------------------------------------------------------------------------ gate closed

CLOSED = ({}, {"upkeep": {"enabled": False}}, {"upkeep": {"enabled": "true"}})


def test_new_entry_points_return_closed_before_any_call(tmp_path, monkeypatch):
    m = mod()
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    for config in CLOSED:
        assert m.ack_union(config, str(sdlc), "u", [{"sha": "a" * 40, "patch_id": "p"}])["closed"] is True
        assert m.ledger_note(config, str(sdlc), "u", "rebased", "a" * 12, append=lambda *a, **k: called.append(a))["closed"] is True
    assert called == [] and list(sdlc.rglob("*")) == []


# ------------------------------------------------------------------------------------------ the runner and hooks policy

def test_engine_env_pins_prompts_and_editor_and_drops_location_vars():
    env = mod().engine_env({"GIT_DIR": "/x", "GIT_WORK_TREE": "/y", "KEEP": "1", "GIT_EDITOR": "vim"})
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_EDITOR"] == "true" and env["KEEP"] == "1"
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env


def test_engine_runner_carries_config_pins_and_a_timeout_table(tmp_path):
    m = mod()
    git(tmp_path, "init", "-q", "-b", "main")
    run = m.engine_runner(m.hooks_policy("replay"))
    assert run(tmp_path, ["git", "config", "--get", "rebase.updateRefs"]) == "false"
    assert run(tmp_path, ["git", "config", "--get", "commit.gpgsign"]) == "false"
    assert m.GIT_LIMITS["default"] > 0 and m.GIT_LIMITS["rebase"] >= m.GIT_LIMITS["default"]


@pytest.mark.parametrize("phase,expected", [("replay", "off"), ("clean-push", "off"), ("resolved-push", "inherit"),
                                            ("anything-else", "off"), (None, "off")])
def test_hooks_policy_table(phase, expected):
    assert mod().hooks_policy(phase) == expected


# ------------------------------------------------------------------------------------------ tip restarts

def scripted(*tips):
    seq = list(tips)

    def read():
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return read


A, B, C, D = ("a", "1"), ("b", "1"), ("c", "1"), ("d", "1")


def test_recheck_tips_stable():
    ran = []
    out = mod().recheck_tips(scripted(A), [lambda t: ran.append("one"), lambda t: ran.append("two")])
    assert out == {"outcome": "ok", "restarts": 0, "tips": A} and ran == ["one", "two"]


def test_recheck_tips_moves_once_then_succeeds():
    ran = []
    # start A; before step one: B (restart 1), then stable
    out = mod().recheck_tips(scripted(A, B, B), [lambda t: ran.append(("one", t)), lambda t: ran.append(("two", t))])
    assert out["outcome"] == "ok" and out["restarts"] == 1 and out["tips"] == B
    assert ran == [("one", B), ("two", B)]


def test_recheck_tips_moves_twice_then_succeeds():
    out = mod().recheck_tips(scripted(A, B, C, C), [lambda t: None])
    assert out["outcome"] == "ok" and out["restarts"] == 2 and out["tips"] == C


def test_recheck_tips_third_move_is_moved_and_nothing_runs():
    ran = []
    out = mod().recheck_tips(scripted(A, B, C, D), [lambda t: ran.append(t)])
    assert out["outcome"] == "moved" and out["restarts"] == 2 and ran == []


def test_recheck_tips_a_step_that_moves_the_tips_before_the_push_stops_it():
    ran = []
    out = mod().recheck_tips(scripted(A, A, B, C, D), [lambda t: ran.append("replay"), lambda t: ran.append("push")])
    assert out["outcome"] == "moved" and "push" not in ran


def test_recheck_tips_unreadable():
    def boom():
        raise RuntimeError("no")
    assert mod().recheck_tips(boom, [lambda t: None])["outcome"] == "unreadable"


# ------------------------------------------------------------------------------------------ acks

def test_ack_union_writes_union_atomically_and_idempotently(tmp_path, monkeypatch):
    m = mod()
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: (replaced.append((pathlib.Path(s).parent, pathlib.Path(d))), real(s, d))[1])
    one = [{"sha": "a" * 40, "patch_id": "p1"}]
    assert m.ack_union(OPEN, str(sdlc), "u", one)["ok"] is True
    assert m.ack_union(OPEN, str(sdlc), "u", [{"sha": "b" * 40, "patch_id": "p1"}, {"sha": "c" * 40, "patch_id": "p2"}])["ok"] is True
    assert m.ack_union(OPEN, str(sdlc), "other", one)["ok"] is True
    path = sdlc / "state" / "upkeep" / "acks.json"
    data = json.loads(path.read_text())
    assert [e["sha"][0] for e in data["units"]["u"]] == ["a", "b", "c"]
    assert [e["sha"][0] for e in data["units"]["other"]] == ["a"]
    assert all(src == path.parent and dst == path for src, dst in replaced) and len(replaced) == 3
    assert sorted(p.name for p in path.parent.iterdir()) == ["acks.json"]


def test_ack_union_unreadable_existing_file_is_replaced_not_crashed(tmp_path):
    m = mod()
    path = tmp_path / ".sdlc" / "state" / "upkeep" / "acks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert m.ack_union(OPEN, str(tmp_path / ".sdlc"), "u", [{"sha": "a" * 40, "patch_id": "p"}])["ok"] is True
    assert json.loads(path.read_text())["units"]["u"][0]["patch_id"] == "p"


def test_acked_reads_the_runtime_union(tmp_path):
    m = mod()
    rebase = support.script("feature_rebase")
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    m.ack_union(OPEN, str(sdlc), "u", [{"sha": "a" * 40, "patch_id": "pid"}])
    pids, shas = rebase._acked(str(sdlc), lambda cwd, argv: (_ for _ in ()).throw(RuntimeError("x")), str(tmp_path), "u", "origin/main")
    assert pids == {"pid"} and shas == {"a" * 40}


def test_acked_without_a_runtime_file_is_unchanged(tmp_path):
    rebase = support.script("feature_rebase")
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    assert rebase._acked(str(sdlc), lambda cwd, argv: (_ for _ in ()).throw(RuntimeError("x")), str(tmp_path), "u", "origin/main") == (set(), set())


# ------------------------------------------------------------------------------------------ re-anchor and push refusals

@pytest.mark.parametrize("level,green,expected", [(1, False, True), ("mechanical", False, True), (2, True, True), ("agent", True, True),
                                                  (2, False, False), ("agent", False, False), (None, False, False), (1, None, True),
                                                  ("off", "yes", False), (0, 1, False)])
def test_reanchor_allowed_table(level, green, expected):
    assert mod().reanchor_allowed(level, green) is expected


def test_push_refusal_to_a_deleted_branch_after_merge_is_a_skip_with_a_reason():
    m = mod()
    skip = m.classify_push_refusal(pr_merged=True, remote_branch_exists=False)
    assert skip["skip"] is True and skip["reason"] == "goal-branch-deleted-after-merge"
    for merged, exists in ((True, True), (False, False), (None, False), (True, None)):
        assert m.classify_push_refusal(pr_merged=merged, remote_branch_exists=exists)["skip"] is False


# ------------------------------------------------------------------------------------------ the ledger note

def test_ledger_note_is_unaddressed_in_process_and_keyed(tmp_path, monkeypatch):
    m = mod()
    seen = []
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(a))

    def append(sdlc_dir, kind, goal, config=None, **fields):
        seen.append((kind, goal, fields))
        return {"id": "x:1"}
    out = m.ledger_note(OPEN, str(tmp_path), "Voice", "rebased", "abcdef0123456789" + "0" * 24, append=append)
    assert out["ok"] is True and spawned == []
    kind, goal, fields = seen[0]
    assert kind == "note" and goal == "upkeep-voice"
    assert fields == {"ref": "upkeep:rebased:abcdef012345"}      # no `to`, no area, no issue: nothing addresses anyone
    assert "to" not in fields


def test_ledger_note_default_appender_is_safe_append_never_loop_py():
    import ast
    src = (ROOT / "skills" / "sigma-loop" / "scripts" / "feature_upkeep_pass.py").read_text()
    code = [n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and not n.value.startswith(("The ", "One ", "Recompute", "Run ", "Evidence", "A refused", "Hooks", "-> ")) and len(n.value) < 40]
    assert "loop.py" not in code and "safe_append" in src


# ------------------------------------------------------------------------------------------ the cut tip

@pytest.fixture
def world(tmp_path):
    """origin (bare) + a clone with main and feature/u; a goal worktree cut from origin/feature/u with one goal commit."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), "clone")
    (clone / "f.txt").write_text("1\n2\n3\n")
    git(clone, "add", "f.txt")
    git(clone, "commit", "-q", "-m", "seed")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "checkout", "-q", "-b", "feature/u")
    (clone / "f.txt").write_text("1\n2\nunit-line\n3\n")
    git(clone, "commit", "-q", "-am", "unit work")
    git(clone, "push", "-q", "origin", "feature/u")
    cut = git(clone, "rev-parse", "HEAD")
    goal = tmp_path / "goalwt"
    git(clone, "worktree", "add", "-q", "-b", "sdlc/9", str(goal), "origin/feature/u")
    (goal / "g.txt").write_text("goal\n")
    git(goal, "add", "g.txt")
    git(goal, "commit", "-q", "-m", "goal work")
    git(goal, "push", "-q", "origin", "sdlc/9")
    sdlc = clone / ".sdlc"
    (sdlc / "goals").mkdir(parents=True)
    return types_ns(origin=origin, clone=clone, goal=goal, sdlc=sdlc, cut=cut)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def rewrite_unit(w, resolved_line):
    """What the engine does to the unit: replay it on a moved main, resolving the one conflict differently."""
    git(w.clone, "checkout", "-q", "main")
    (w.clone / "f.txt").write_text("1\n2\n3\nmain-extra\n")
    git(w.clone, "commit", "-q", "-am", "main moves")
    git(w.clone, "push", "-q", "origin", "main")
    git(w.clone, "checkout", "-q", "-B", "feature/u", "main")
    (w.clone / "f.txt").write_text("1\n2\n%s\n3\nmain-extra\n" % resolved_line)
    git(w.clone, "commit", "-q", "-am", "unit work (rewritten)")
    git(w.clone, "push", "-q", "-f", "origin", "feature/u")
    return git(w.clone, "rev-parse", "HEAD")


def record(w, cut=True):
    rec = {"worktree": str(w.goal), "branch": "sdlc/9", "base": "feature/u", "remote": "origin", "base_resolved": True, "pr": ""}
    if cut:
        rec["cut_tip"] = w.cut
    work()._save(str(w.sdlc), "9", rec)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cwd, argv):
        self.calls.append([str(a) for a in argv])
        return real_run(cwd, argv)


def test_cut_tip_helper_gate_closed_adds_no_key_and_no_call():
    calls = []
    for config in CLOSED:
        assert work()._cut_tip_fields(config, lambda c, a: calls.append(a), "/x", "origin", "feature/u") == {}
    assert work()._cut_tip_fields(OPEN, lambda c, a: calls.append(a), "/x", "origin", "main") == {}     # not a unit branch
    assert calls == []


def test_cut_tip_helper_records_the_unit_tip_under_the_gate(world):
    got = work()._cut_tip_fields(OPEN, real_run, str(world.goal), "origin", "feature/u")
    assert got == {"cut_tip": world.cut}


def test_cut_tip_helper_failure_records_nothing():
    def boom(cwd, argv):
        raise RuntimeError("no")
    assert work()._cut_tip_fields(OPEN, boom, "/x", "origin", "feature/u") == {}


def test_goal_rebase_replays_onto_the_rewritten_unit_with_the_recorded_cut_tip(world):
    new_unit = rewrite_unit(world, "resolved-line")
    record(world)
    run = Recorder()
    out = work().rebase(str(world.sdlc), OPEN, "9", run=run)
    assert out == "rebased", out
    assert any("--onto" in c and world.cut in c for c in run.calls if c[:2] == ["git", "rebase"])
    assert git(world.goal, "rev-list", "--count", "origin/feature/u..HEAD") == "1"
    assert git(world.goal, "rev-parse", "HEAD~1") == new_unit
    rec = json.loads((world.sdlc / "goals" / "9.json").read_text()) if (world.sdlc / "goals" / "9.json").exists() else work()._record(str(world.sdlc), "9")
    assert rec["cut_tip"] == new_unit                                   # re-recorded after the goal rebase


def test_goal_rebase_without_a_record_uses_the_legacy_argv(world):
    rewrite_unit(world, "unit-line")            # same change: the plain replay is clean too
    record(world, cut=False)
    run = Recorder()
    work().rebase(str(world.sdlc), OPEN, "9", run=run)
    rebases = [c for c in run.calls if c[:2] == ["git", "rebase"]]
    assert rebases and all("--onto" not in c for c in rebases)
    assert "cut_tip" not in work()._record(str(world.sdlc), "9")


def test_goal_rebase_gate_closed_ignores_a_record(world):
    rewrite_unit(world, "unit-line")
    record(world)
    run = Recorder()
    work().rebase(str(world.sdlc), {}, "9", run=run)
    rebases = [c for c in run.calls if c[:2] == ["git", "rebase"]]
    assert rebases and all("--onto" not in c for c in rebases)
    assert run.calls[-1][:3] != ["git", "rev-parse", "origin/feature/u"]


def test_goal_rebase_with_a_cut_tip_not_in_history_uses_the_legacy_path(world):
    rewrite_unit(world, "unit-line")
    record(world)
    rec = work()._record(str(world.sdlc), "9")
    rec["cut_tip"] = "f" * 40
    work()._save(str(world.sdlc), "9", rec)
    run = Recorder()
    work().rebase(str(world.sdlc), OPEN, "9", run=run)
    assert all("--onto" not in c for c in run.calls if c[:2] == ["git", "rebase"])


# ------------------------------------------------------------------------------------------ the attended door

@pytest.fixture
def door(world):
    git(world.clone, "checkout", "-q", "main")
    return world


def test_attended_door_gate_closed_is_the_legacy_call(world):
    b = brief()
    calls = []

    def fake(run, cwd, remote, branch, base, **kw):
        calls.append((remote, branch, base, kw))
        return {"outcome": b.CURRENT, "why": ""}
    b.attempt_rebase = fake
    for config in CLOSED:
        assert b.attended_rebase(config, str(world.sdlc), real_run, str(world.clone), "origin", "feature/u", "main")["outcome"] == "current"
    assert calls == [("origin", "feature/u", "main", {})] * 3          # no new keyword, no lock


def test_attended_door_takes_the_lock_in_the_caller_and_leases_an_explicit_sha(world):
    b = brief()
    rb = b.feature_rebase
    seen = {}

    def fake(run, cwd, remote, branch, base, **kw):
        seen.update(kw)
        seen["locked"] = rb._acquire(rb.lock_path(str(world.sdlc), "u"), timeout=0) is None      # held by the caller: a second take fails
        return {"outcome": b.CURRENT, "why": ""}
    b.attempt_rebase = fake
    out = b.attended_rebase(OPEN, str(world.sdlc), real_run, str(world.clone), "origin", "feature/u", "main")
    assert out["outcome"] == "current" and seen["locked"] is True and seen["lease"] is True
    again = rb._acquire(rb.lock_path(str(world.sdlc), "u"), timeout=0)         # released afterwards
    assert again is not None
    rb._release(again)


def test_attended_door_refuses_when_another_holder_has_the_lock(world):
    b = brief()
    rb = b.feature_rebase
    held = rb._acquire(rb.lock_path(str(world.sdlc), "u"), timeout=0)
    assert held is not None
    called = []
    b.attempt_rebase = lambda *a, **k: called.append(1)
    try:
        out = b.attended_rebase(OPEN, str(world.sdlc), real_run, str(world.clone), "origin", "feature/u", "main", lock_timeout=0)
    finally:
        rb._release(held)
    assert out["outcome"] == b.FAILED and "lock" in out["why"] and called == []


def test_attended_door_names_missing_lock_support_not_a_holder(world, monkeypatch):
    b = brief()
    monkeypatch.setattr(b.feature_rebase.sync, "fcntl", None)
    called = []
    b.attempt_rebase = lambda *a, **k: called.append(1)
    out = b.attended_rebase(OPEN, str(world.sdlc), real_run, str(world.clone), "origin", "feature/u", "main", lock_timeout=0)
    assert out["outcome"] == b.FAILED and "not supported" in out["why"] and "holds" not in out["why"] and called == []


def test_push_branch_explicit_lease_never_bare(world):
    b = brief()
    run = Recorder()
    git(world.clone, "checkout", "-q", "feature/u")
    tip = git(world.clone, "rev-parse", "origin/feature/u")
    out = b.push_branch(run, str(world.clone), "origin", "feature/u", lease_sha=tip)
    pushes = [c for c in run.calls if c[:2] == ["git", "push"]]
    assert out["ok"] is True and pushes == [["git", "push", "--force-with-lease=feature/u:" + tip, "origin", "HEAD:feature/u"]]


def test_push_branch_legacy_argv_without_a_lease_sha(world):
    b = brief()
    run = Recorder()
    git(world.clone, "checkout", "-q", "feature/u")
    b.push_branch(run, str(world.clone), "origin", "feature/u")
    assert [c for c in run.calls if c[:2] == ["git", "push"]] == [["git", "push", "--force-with-lease", "origin", "HEAD:feature/u"]]


def test_push_branch_explicit_lease_refuses_when_the_remote_moved(world):
    b = brief()
    git(world.clone, "checkout", "-q", "feature/u")
    stale = git(world.clone, "rev-parse", "origin/feature/u")
    other = world.clone.parent / "other"
    git(world.clone.parent, "clone", "-q", "-b", "feature/u", str(world.origin), "other")
    (other / "x.txt").write_text("x")
    git(other, "add", "x.txt")
    git(other, "commit", "-q", "-m", "someone else")
    git(other, "push", "-q", "origin", "feature/u")
    git(world.clone, "fetch", "-q", "origin")                       # the re-arming fetch: a bare lease would now pass
    (world.clone / "y.txt").write_text("y")
    git(world.clone, "add", "y.txt")
    git(world.clone, "commit", "-q", "-m", "mine")
    out = b.push_branch(real_run, str(world.clone), "origin", "feature/u", lease_sha=stale)
    assert out["ok"] is False


def test_runtime_acks_path_is_one_string_in_both_modules():
    assert mod().ACKS_REL == support.script("feature_rebase").RUNTIME_ACKS_REL
