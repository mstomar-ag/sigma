"""D2 F02 (owner ruling 2026-10-14): a FAILED independent reviewer is never an approval; the goal parks.

`reviewer.py run --goal G --phase P` leaves ONE receipt per goal+phase (latest run wins);
`record-review` / `record-plan-review` refuse an APPROVING verdict on a process/command route unless
that receipt says the process ran, printed a lone `VERDICT: approve`, and (plan-review) saw these
plan bytes. Each guard clause has its own test below; the PR body names the red test per break."""
import hashlib
import importlib.util
import time
import json
import os
import pathlib
import subprocess
import sys

import pytest

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
GOAL = ".sdlc/goals/0001-x.md"
PLAN = "# Plan\n\n## Tests\n- `tests/t.py`\n"
SHA = hashlib.sha256(PLAN.encode()).hexdigest()
CLEAN = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID")


def _rv():
    spec = importlib.util.spec_from_file_location("reviewer_f02", S / "reviewer.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in CLEAN:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SIGMA_ALLOW_COEXIST", "1")


def _project(tmp_path, review=None, action_log=True):
    d = tmp_path / ".sdlc"
    for sub in ("goals", "state", "plans", "research"):
        (d / sub).mkdir(parents=True)
    review = {"command": "sh %s/rev.sh" % tmp_path} if review is None else review
    (d / "config.json").write_text(json.dumps({"action_log": {"enabled": action_log}, "review": review}))
    (tmp_path / GOAL).write_text("---\nstatus: open\n---\n# Goal x\nbody\n")
    (d / "plans" / "0001-x.md").write_text(PLAN)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "--local",
                    "sigma.allowRepositoryShellCommands", "true"], check=True)
    (tmp_path / "code.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "code.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "-m", "init"], check=True)
    (tmp_path / "brief.md").write_text(brief_text("code-review"))
    (tmp_path / "scratch").mkdir()
    return tmp_path


def brief_text(kind):
    return "# Independent review brief - %s\nPlan sha256: %s\nreview it\n" % (kind, SHA)


def reviewer_script(p, body):
    (p / "rev.sh").write_text(body)


APPROVE = "echo 'looks fine'\necho 'VERDICT: approve'\n"
BLOCK = "echo 'broken'\necho 'VERDICT: block'\n"
FAILURES = {
    "nonzero_exit": "exit 3\n",
    "unauthenticated": "echo 'Error: not logged in' >&2\nexit 1\n",
    "empty_output": "exit 0\n",
    "timeout": "sleep 30\n",
}


def run_reviewer(p, goal=GOAL, phase="review", write_brief=True):
    """The documented gesture: reviewer.py run .sdlc <brief> --scratch <dir> --goal --phase."""
    if write_brief:
        (p / "brief.md").write_text(brief_text("plan-review" if phase == "plan_review" else "code-review"))
    return subprocess.run([sys.executable, str(S / "reviewer.py"), "run", ".sdlc", "brief.md",
                           "--scratch", "scratch", "--goal", goal, "--phase", phase],
                          cwd=p, capture_output=True, text=True)


def work(p, *args, env=None):
    return subprocess.run([sys.executable, str(S / "work.py"), *args], cwd=p, capture_output=True, text=True,
                          env=env)


def record_review(p, verdict="APPROVE", env=None):
    return work(p, "record-review", ".sdlc", GOAL, "--verdict", verdict, env=env)


def record_plan(p, verdict="SOUND", sha=SHA, env=None):
    return work(p, "record-plan-review", ".sdlc", GOAL, "--verdict", verdict, "--plan-sha256", sha, env=env)


def verdict_rows(p):
    f = p / ".sdlc/state/log/0001-x.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines() if '"verdict"' in x] if f.exists() else []


def timeout_cfg(p):
    cfg = json.loads((p / ".sdlc/config.json").read_text())
    cfg["review"]["timeout_seconds"] = 1
    (p / ".sdlc/config.json").write_text(json.dumps(cfg))


@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_each_failure_kind_refuses_an_approving_review_and_a_plan_review(tmp_path, kind):
    p = _project(tmp_path)
    if kind == "timeout":
        timeout_cfg(p)
    for phase, record in (("review", record_review), ("plan_review", record_plan)):
        reviewer_script(p, FAILURES[kind])
        assert run_reviewer(p, phase=phase).returncode != 0
        r = record(p)
        assert r.returncode == 2, (kind, phase, r.stdout, r.stderr)
        assert "FAILED" in r.stderr and "reviewer_down" in r.stderr and "re-run" in r.stderr
    assert verdict_rows(p) == []


def test_start_failure_is_a_failed_run(tmp_path, monkeypatch):
    p = _project(tmp_path)
    rv = _rv()
    real = subprocess.Popen
    def boom(cmd, *a, **k):
        if cmd[0] == "git":                      # shell_policy's trust probe must still work
            return real(cmd, *a, **k)
        raise OSError("no exec")
    monkeypatch.setattr(rv.subprocess, "Popen", boom)
    assert rv.run_review(str(p / ".sdlc"), str(p / "brief.md"), [str(p / "scratch")],
                         goal=GOAL, phase="review") == 1
    assert "start_failed" in rv.independent_run_refusal(str(p / ".sdlc"), GOAL, "review")


def test_a_failure_leaves_the_named_kind_in_the_receipt(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, FAILURES["empty_output"])
    run_reviewer(p)
    got = json.loads((p / ".sdlc/state/reviewer-runs/0001-x.review.json").read_text())
    assert got["outcome"] == "failed" and got["failure"] == "empty_output"


def test_a_run_refused_before_the_reviewer_starts_keeps_an_earlier_good_receipt(tmp_path):
    """S5: a leak-guard refusal is a maker typo, not a reviewer failure; it must not strand a good receipt."""
    p = _project(tmp_path)
    reviewer_script(p, APPROVE)
    assert run_reviewer(p).returncode == 0
    leak = tmp_path.parent / ("leak-%s.md" % tmp_path.name)               # outside the checkout, like /tmp
    leak.write_text("# b\nsee %s/scratch/x\n" % p)                       # names a shared scratch path
    got = subprocess.run([sys.executable, str(S / "reviewer.py"), "run", ".sdlc", str(leak), "--scratch",
                          "scratch", "--goal", GOAL, "--phase", "review"], cwd=p, capture_output=True, text=True)
    assert got.returncode == 1                                            # leak check refuses before spawn
    assert record_review(p).returncode == 0


def test_a_run_refused_by_shell_policy_keeps_an_earlier_good_receipt(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE)
    assert run_reviewer(p).returncode == 0
    subprocess.run(["git", "-C", str(p), "config", "--local", "--unset", "sigma.allowRepositoryShellCommands"],
                   check=True)
    assert run_reviewer(p).returncode == 2
    subprocess.run(["git", "-C", str(p), "config", "--local", "sigma.allowRepositoryShellCommands", "true"],
                   check=True)
    assert record_review(p).returncode == 0


def test_a_killed_run_counts_as_failed_not_as_the_earlier_success(tmp_path, monkeypatch):
    """#1551: once launched the receipt says RUNNING (with the runner's pid), so an interrupted run must be
    FINALISED to not_completed by the runner itself. The test process staying alive is the precondition, not
    an assertion: with its pid live, only that finalise write (not the dead-pid read) can satisfy this. The
    assertions read the RAW file, so `_read_receipt`'s classifier cannot stand in for the write."""
    p = _project(tmp_path)
    reviewer_script(p, APPROVE)
    assert run_reviewer(p).returncode == 0
    rv = _rv()
    real = subprocess.Popen
    class Boom:
        def __init__(self, *a, **k): self.pid = 0
        def communicate(self, *a, **k): raise KeyboardInterrupt
        def kill(self): pass
        def wait(self, *a, **k): pass
    monkeypatch.setattr(rv.subprocess, "Popen", lambda cmd, *a, **k: real(cmd, *a, **k) if cmd[0] == "git" else Boom())
    monkeypatch.setattr(rv, "_stop_review_process", lambda proc: None)
    with pytest.raises(KeyboardInterrupt):
        rv.run_review(str(p / ".sdlc"), str(p / "brief.md"), [str(p / "scratch")], goal=GOAL, phase="review")
    monkeypatch.undo()
    raw = json.loads((p / ".sdlc/state/reviewer-runs/0001-x.review.json").read_text())
    assert raw["outcome"] == "failed" and raw["failure"] == "not_completed", raw
    assert "pid" not in raw, raw                       # the finalise replaced the running receipt
    assert isinstance(raw.get("run_id"), str) and raw["run_id"], raw      # written under this run's nonce
    r = record_review(p)
    assert r.returncode == 2 and "not_completed" in r.stderr


def test_success_then_failure_then_success_clears_the_marker(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p)
    reviewer_script(p, FAILURES["nonzero_exit"]); run_reviewer(p)
    assert record_review(p).returncode == 2
    reviewer_script(p, APPROVE); assert run_reviewer(p).returncode == 0
    r = record_review(p)
    assert r.returncode == 0, r.stderr
    assert [x["verdict"] for x in verdict_rows(p)] == ["approve"]


def test_no_run_on_record_refuses_on_a_process_or_command_route(tmp_path):
    p = _project(tmp_path)
    r = record_review(p)
    assert r.returncode == 2 and "no run of it is on record" in r.stderr


def test_the_reviewers_own_block_cannot_be_recorded_as_approve_but_a_block_can(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, BLOCK); run_reviewer(p)
    assert record_review(p).returncode == 2
    assert record_review(p, "BLOCK").returncode == 0                      # a cautious record is never refused
    assert record_review(p, "SEND-BACK").returncode == 0


def test_output_without_a_lone_verdict_line_cannot_be_approved(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, "echo 'verdict: approve would be premature'\n")
    assert run_reviewer(p).returncode == 0
    r = record_review(p)
    assert r.returncode == 2 and "lone `VERDICT: approve`" in r.stderr


def test_plan_review_approval_is_bound_to_the_plan_the_reviewer_saw(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p, phase="plan_review")
    assert record_plan(p).returncode == 0
    (p / ".sdlc/plans/0001-x.md").write_text(PLAN + "- changed\n")
    new = hashlib.sha256((PLAN + "- changed\n").encode()).hexdigest()
    r = record_plan(p, sha=new)
    assert r.returncode == 2 and "different plan" in r.stderr


def test_plan_review_is_refused_even_with_the_action_log_off(tmp_path):
    p = _project(tmp_path, action_log=False)
    reviewer_script(p, FAILURES["nonzero_exit"]); run_reviewer(p, phase="plan_review")
    r = record_plan(p)
    assert r.returncode == 2 and "FAILED" in r.stderr
    assert not (p / ".sdlc/state/gates/0001-x.json").exists()


def test_process_route_is_guarded_like_command(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"; bindir.mkdir()
    (bindir / "cursor-agent").write_text("#!/bin/sh\nexit 4\n"); (bindir / "cursor-agent").chmod(0o755)
    monkeypatch.setenv("PATH", "%s%s%s" % (bindir, os.pathsep, os.environ["PATH"]))
    p = _project(tmp_path, review={"host": "cursor"})
    assert json.loads(subprocess.run([sys.executable, str(S / "reviewer.py"), "resolve", ".sdlc"], cwd=p,
                                     capture_output=True, text=True).stdout)["mechanism"] == "process"
    assert run_reviewer(p).returncode == 1
    r = record_review(p)
    assert r.returncode == 2 and "nonzero_exit" in r.stderr


def test_inline_route_still_works_when_it_is_what_resolved(tmp_path):
    for review in ({"independent": False, "command": "sh x"}, {"host": "inline"}):
        d = tmp_path / ("c%d" % len(review)); d.mkdir()
        p = _project(d, review=review)
        r = record_review(p)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["route"] == "inline"


def test_subagent_route_is_not_governed_by_the_receipt(tmp_path, monkeypatch):
    p = _project(tmp_path, review={"host": "auto"})
    monkeypatch.setenv("CLAUDECODE", "1")
    assert _rv().independent_run_refusal(str(p / ".sdlc"), GOAL, "review") is None


def test_goal_and_phase_go_together_and_phase_is_closed(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE)
    r = subprocess.run([sys.executable, str(S / "reviewer.py"), "run", ".sdlc", "brief.md",
                        "--scratch", "scratch", "--goal", GOAL], cwd=p, capture_output=True, text=True)
    assert r.returncode == 2
    assert run_reviewer(p, phase="pr_review").returncode == 2


def test_documented_gesture_end_to_end_then_park_with_the_stated_qkind(tmp_path):
    """The gesture the refusal prints: park with reviewer_down; the done gate still refuses."""
    p = _project(tmp_path)
    reviewer_script(p, FAILURES["unauthenticated"]); run_reviewer(p)
    r = record_review(p)
    assert r.returncode == 2
    park = subprocess.run([sys.executable, str(S / "loop.py"), "record", ".sdlc", GOAL, "parked",
                           "independent reviewer failed: nonzero_exit", "--qkind", "reviewer_down"],
                          cwd=p, capture_output=True, text=True)
    assert park.returncode == 0, park.stderr
    assert "status: parked" in (p / GOAL).read_text() or "parked" in (p / GOAL).read_text()


# ---- security review of #1384: fail closed, and bind the code-review receipt to the reviewed bytes ----

def _approved_review(p):
    reviewer_script(p, APPROVE)
    assert run_reviewer(p).returncode == 0
    assert record_review(p).returncode == 0          # control: approvable while nothing has changed
    (p / ".sdlc/state/log/0001-x.jsonl").unlink()    # forget that row, keep the receipt


def test_a_corrupt_config_is_refused_in_process_too(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p)
    (p / ".sdlc/config.json").write_text("{not json")
    assert "config" in _rv().independent_run_refusal(str(p / ".sdlc"), GOAL, "review")


def test_a_corrupt_config_cannot_turn_a_failed_route_into_an_inline_approval(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, FAILURES["nonzero_exit"]); run_reviewer(p)
    (p / ".sdlc/config.json").write_text("{not json")
    r = record_review(p)
    assert r.returncode == 2, r.stdout


@pytest.mark.parametrize("damage", ["garbage", "list", "directory", "unreadable"])
def test_a_damaged_receipt_refuses(tmp_path, damage):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p)
    f = p / ".sdlc/state/reviewer-runs/0001-x.review.json"
    if damage == "garbage":
        f.write_text("\x00{")
    elif damage == "list":
        f.write_text("[]")
    elif damage == "directory":
        f.unlink(); f.mkdir()
    else:
        f.chmod(0)
    r = record_review(p)
    f.chmod(0o600) if damage == "unreadable" else None
    assert r.returncode == 2, damage


def test_unknown_phase_and_unsafe_goal_are_refused_not_allowed(tmp_path):
    p = _project(tmp_path)
    rv = _rv()
    assert rv.independent_run_refusal(str(p / ".sdlc"), GOAL, "pr_review")
    assert rv.independent_run_refusal(str(p / ".sdlc"), "../../etc/passwd", "review")


def test_plan_review_needs_a_plan_hash_to_be_approved(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p, phase="plan_review")
    assert _rv().independent_run_refusal(str(p / ".sdlc"), GOAL, "plan_review", "")


def test_a_review_receipt_is_dead_once_a_tracked_file_changes(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p)
    (p / "code.py").write_text("x = 2\n")
    r = record_review(p)
    assert r.returncode == 2 and "changed since" in r.stderr


def test_a_review_receipt_is_dead_once_an_untracked_file_appears_or_changes(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p)
    (p / "new.py").write_text("a\n")
    assert record_review(p).returncode == 2
    reviewer_script(p, APPROVE); run_reviewer(p)
    assert record_review(p).returncode == 0
    (p / "new.py").write_text("b\n")
    assert record_review(p).returncode == 2


def test_a_review_receipt_is_dead_once_head_moves(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p)
    subprocess.run(["git", "-C", str(p), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "moved"], check=True)
    assert record_review(p).returncode == 2


def test_sdlc_state_changes_do_not_kill_the_receipt(tmp_path):
    """The receipt and the loop's own state live in .sdlc; they must not invalidate themselves."""
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p)
    (p / ".sdlc/state/log").mkdir(parents=True, exist_ok=True)
    (p / ".sdlc/state/log/other.jsonl").write_text("{}\n")
    (p / ".sdlc/work/0001-x").mkdir(parents=True, exist_ok=True)
    (p / ".sdlc/work/0001-x/scratch.txt").write_text("maker scratch\n")
    assert record_review(p).returncode == 0


def test_plans_research_and_config_under_sdlc_ARE_covered(tmp_path):
    """The carve-out is `<sdlc>/state` and `<sdlc>/work` only; the rest of .sdlc is reviewed content."""
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p)
    (p / ".sdlc/plans/0001-x.md").write_text(PLAN + "- edited after review\n")
    assert record_review(p).returncode == 2


def test_content_changing_while_the_reviewer_runs_is_a_failed_run(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, "echo changed > %s/code.py\n%s" % (p, APPROVE))
    assert run_reviewer(p).returncode == 1
    r = record_review(p)
    assert r.returncode == 2 and "content_changed_during_run" in r.stderr


def _stub_host(tmp_path, monkeypatch, name, body):
    bindir = tmp_path / "bin"; bindir.mkdir(exist_ok=True)
    (bindir / name).write_text("#!/bin/sh\n" + body); (bindir / name).chmod(0o755)
    monkeypatch.setenv("PATH", "%s%s%s" % (bindir, os.pathsep, os.environ["PATH"]))
    return bindir


def _without(bindir):
    return {**os.environ, "PATH": os.pathsep.join(x for x in os.environ["PATH"].split(os.pathsep) if x != str(bindir))}


def test_a_directory_that_is_not_a_git_checkout_fails_closed_and_names_why(tmp_path, monkeypatch):
    _stub_host(tmp_path, monkeypatch, "cursor-agent", "echo 'ok'\necho 'VERDICT: approve'\n")
    p = _project(tmp_path, review={"host": "cursor"})
    import shutil
    shutil.rmtree(p / ".git")
    assert run_reviewer(p).returncode == 1
    r = record_review(p)
    assert r.returncode == 2 and "content_unbound" in r.stderr and "not a git checkout" in r.stderr


@pytest.mark.parametrize("case", ["nested_repo", "broken_symlink", "newline_name", "no_commit"])
def test_ordinary_checkouts_bind_instead_of_stranding(tmp_path, case):
    p = _project(tmp_path)
    if case == "nested_repo":
        n = p / "vendor"; n.mkdir()
        subprocess.run(["git", "init", "-q", str(n)], check=True)
        (n / "f").write_text("1"); subprocess.run(["git", "-C", str(n), "add", "f"], check=True)
        subprocess.run(["git", "-C", str(n), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"], check=True)
    elif case == "broken_symlink":
        (p / "dangling").symlink_to("/nonexistent/target")
    elif case == "newline_name":
        (p / "a\nb.txt").write_text("x")
    else:
        import shutil
        shutil.rmtree(p / ".git")
        subprocess.run(["git", "init", "-q", str(p)], check=True)
        subprocess.run(["git", "-C", str(p), "config", "--local", "sigma.allowRepositoryShellCommands", "true"], check=True)
    reviewer_script(p, APPROVE)
    r = run_reviewer(p)
    assert r.returncode == 0, (case, r.stderr)
    assert record_review(p).returncode == 0
    # and the binding still bites on a change to the thing that used to strand
    if case == "nested_repo":
        subprocess.run(["git", "-C", str(p / "vendor"), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "y"], check=True)
    elif case == "broken_symlink":
        (p / "dangling").unlink(); (p / "dangling").symlink_to("/other")
    elif case == "newline_name":
        (p / "a\nb.txt").write_text("changed")
    else:
        (p / "code.py").write_text("changed\n")
    assert record_review(p).returncode == 2


def test_too_many_untracked_files_fails_closed_naming_that_cause(tmp_path, monkeypatch):
    p = _project(tmp_path)
    for i in range(4):
        (p / ("u%d" % i)).write_text("x")
    rv = _rv()
    monkeypatch.setattr(rv, "_MAX_UNTRACKED", 3)
    reviewer_script(p, APPROVE)
    assert rv.run_review(str(p / ".sdlc"), str(p / "brief.md"), [str(p / "scratch")], goal=GOAL, phase="review") == 1
    r = record_review(p)
    assert r.returncode == 2 and "untracked" in r.stderr and "content_unbound" in r.stderr
    assert "Park the goal" not in r.stderr          # a checkout problem is not a reviewer failure


def test_the_diff_is_streamed_not_buffered(tmp_path):
    import tracemalloc
    p = _project(tmp_path)
    (p / "code.py").write_bytes(os.urandom(30 * 1024 * 1024))
    rv = _rv()
    tracemalloc.start()
    key, cause = rv._content_key(str(p / ".sdlc"), GOAL)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert key and cause is None
    assert peak < 12 * 1024 * 1024, peak


def test_a_slow_or_huge_diff_fails_closed_naming_the_diff(tmp_path, monkeypatch):
    p = _project(tmp_path)
    (p / "code.py").write_bytes(os.urandom(8 * 1024 * 1024))
    rv = _rv()
    monkeypatch.setattr(rv, "_KEY_SECONDS", 0.0001)
    key, cause = rv._content_key(str(p / ".sdlc"), GOAL)
    assert key is None and "diff too large or slow" in cause


def test_a_reviewer_that_writes_into_the_checkout_says_a_rerun_usually_passes(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, "echo cache > %s/.reviewer-cache\n%s" % (p, APPROVE))
    r = run_reviewer(p)
    assert r.returncode == 1 and "re-run usually passes" in r.stderr
    assert run_reviewer(p).returncode == 0                                # the file now exists in the pre-run key


def test_a_plan_review_brief_cannot_stand_in_for_a_code_review(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE)
    (p / "brief.md").write_text(brief_text("plan-review"))
    assert run_reviewer(p, phase="review", write_brief=False).returncode == 0
    r = record_review(p)
    assert r.returncode == 2 and "plan-review" in r.stderr


def test_a_review_receipt_with_no_content_key_cannot_approve(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p)
    f = p / ".sdlc/state/reviewer-runs/0001-x.review.json"
    got = json.loads(f.read_text()); got["content_key"] = None
    f.write_text(json.dumps(got))
    assert record_review(p).returncode == 2


# ---- attack review of #1384 @9d7c29c --------------------------------------------------------------

def test_a_failed_receipt_blocks_approval_even_when_the_route_later_resolves_inline(tmp_path, monkeypatch):
    """D1: the owner ruling. The codex binary vanishes after the failed run, so resolve() says inline."""
    bindir = _stub_host(tmp_path, monkeypatch, "codex", "exit 1\n")
    p = _project(tmp_path, review={"host": "codex"})
    assert run_reviewer(p).returncode == 1
    bare = _without(bindir)
    r = record_review(p, env=bare)
    assert r.returncode == 2 and "FAILED" in r.stderr, (r.stdout, r.stderr)
    (p / "brief.md").write_text(brief_text("plan-review"))
    assert run_reviewer(p, phase="plan_review", write_brief=False).returncode == 1
    r = record_plan(p, env=bare)
    assert r.returncode == 2 and "FAILED" in r.stderr, (r.stdout, r.stderr)


def test_a_configured_host_that_is_not_installed_refuses_approval(tmp_path):
    p = _project(tmp_path, review={"host": "codex"})
    r = record_review(p, env={**os.environ, "PATH": "/usr/bin:/bin"})
    assert r.returncode == 2 and "configured" in r.stderr and "not installed" in r.stderr
    r = record_plan(p, env={**os.environ, "PATH": "/usr/bin:/bin"})
    assert r.returncode == 2 and "not installed" in r.stderr


def test_a_machine_with_no_mechanism_and_nothing_configured_still_reviews_inline(tmp_path):
    p = _project(tmp_path, review={})
    r = record_review(p)
    assert r.returncode == 0, r.stderr


def test_independent_off_ignores_a_failed_receipt(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, FAILURES["nonzero_exit"]); run_reviewer(p)
    cfg = json.loads((p / ".sdlc/config.json").read_text()); cfg["review"] = {"independent": False}
    (p / ".sdlc/config.json").write_text(json.dumps(cfg))
    assert record_review(p).returncode == 0


def _generation(d, goal="2577", name="gen"):
    root = pathlib.Path(d) / "state"; gen = root / "review-generations" / name; gen.mkdir(parents=True)
    (gen / "brief.md").write_text("brief")
    manifest = root / ("manifest-%s.json" % name)
    manifest.write_text(json.dumps({"generation_id": name, "goal": goal, "brief": "review-generations/%s/brief.md" % name,
                                    "brief_sha256": hashlib.sha256(b"brief").hexdigest()}))
    resolution = gen / "resolution.json"
    resolution.write_text(json.dumps(_rv().resolve(d)))
    return manifest, resolution


def _work():
    spec = importlib.util.spec_from_file_location("work_f02", S / "work.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_the_pr_review_inline_branch_refuses_a_degraded_route_and_a_failed_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    p = _project(tmp_path, review={"host": "codex"})
    d = str(p / ".sdlc")
    manifest, resolution = _generation(d)
    w = _work()
    assert json.loads(resolution.read_text())["mechanism"] == "inline"
    with pytest.raises(ValueError, match="not installed"):
        w.run_resolved_review(d, "2577", str(manifest), str(resolution), verdict="approve", reason="self")
    # a plain machine with a FAILED receipt for the goal
    q = tmp_path / "q"; q.mkdir()
    p2 = _project(q, review={})
    d2 = str(p2 / ".sdlc")
    _rv()._write_receipt(d2, "2577", "review", outcome="failed", failure="nonzero_exit")
    manifest, resolution = _generation(d2)
    with pytest.raises(ValueError, match="FAILED"):
        w.run_resolved_review(d2, "2577", str(manifest), str(resolution), verdict="approve", reason="self")


def test_lone_verdict_is_none_unless_exactly_one_verdict_is_printed():
    rv = _rv()
    assert rv.lone_verdict("x\nVERDICT: approve\n") == "approve"
    for out in ("VERDICT: approve\nVERDICT: block\n", "VERDICT: block\nVERDICT: approve\n", "no verdict here"):
        assert rv.lone_verdict(out) is None, out


@pytest.mark.parametrize("order", ["approve_first", "block_first"])
def test_a_reviewer_printing_both_verdicts_cannot_be_approved(tmp_path, order):
    p = _project(tmp_path)
    lines = ["VERDICT: approve", "VERDICT: block"]
    reviewer_script(p, "".join("echo '%s'\n" % x for x in (lines if order == "approve_first" else lines[::-1])))
    assert run_reviewer(p).returncode == 0
    got = json.loads((p / ".sdlc/state/reviewer-runs/0001-x.review.json").read_text())
    assert got["verdict"] is None
    assert record_review(p).returncode == 2


def test_a_later_failed_run_retracts_the_earlier_approving_row_in_the_phase_record(tmp_path):
    p = _project(tmp_path)
    reviewer_script(p, APPROVE); run_reviewer(p)
    assert record_review(p).returncode == 0
    for verb, extra in (("start", ["--model", "sonnet"]), ("end", [])):
        subprocess.run([sys.executable, str(S / "phase_report.py"), verb, ".sdlc", GOAL, "review", *extra],
                       cwd=p, capture_output=True, text=True, check=True)
    spec = importlib.util.spec_from_file_location("pg_f02", S / "phase_gate.py")
    pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)
    cfg = json.loads((p / ".sdlc/config.json").read_text())
    def row():
        return next(r for r in pg.status(str(p / ".sdlc"), cfg, GOAL)["rows"] if r[0] == "review")
    assert "reviewer failed" not in row()[2]
    reviewer_script(p, FAILURES["nonzero_exit"]); run_reviewer(p)
    name, ok, detail, fix = row()
    assert ok is False and "independent reviewer failed" in detail and "reviewer_down" in fix


def test_the_repro_host_codex_missing_from_path_refuses_with_and_without_a_receipt(tmp_path, monkeypatch):
    """Coordinator's exact repro: review.host=codex, codex gone from PATH, (a) failed receipt, (b) no receipt."""
    bindir = _stub_host(tmp_path, monkeypatch, "codex", "exit 1\n")
    p = _project(tmp_path, review={"host": "codex"})
    bare = _without(bindir)
    r = record_review(p, env=bare)                                   # (b) no receipt at all
    assert r.returncode == 2 and "not installed" in r.stderr, (r.stdout, r.stderr)
    assert run_reviewer(p).returncode == 1                           # (a) a failed receipt
    r = record_review(p, env=bare)
    assert r.returncode == 2 and "FAILED" in r.stderr


def test_the_decision_is_made_from_config_not_from_a_flag(tmp_path):
    rv = _rv()
    inline = {"mechanism": "inline"}
    for review, expect in (({"host": "codex"}, True), ({"host": "cursor"}, True), ({"host": "claude"}, True),
                           ({"host": "command"}, True), ({"host": "bogus"}, True),
                           ({"command": "sh x"}, True),                       # auto + a command configured
                           ({"host": "inline"}, False), ({}, False), ({"host": "auto"}, False)):
        assert rv.configured_reviewer_unavailable({"review": review}, inline) is expect, review
    assert rv.configured_reviewer_unavailable({"review": {"host": "codex"}}, {"mechanism": "process"}) is False
    assert rv.configured_reviewer_unavailable({"review": {"host": "codex"}}, {"mechanism": "subagent"}) is False


# ---- security review: untrusted git config, coverage gaps, framing ------------------------------------

def _key(p, goal=GOAL):
    return _rv()._content_key(str(p / ".sdlc"), goal)


def _marker_script(tmp_path):
    marker = tmp_path.parent / ("ran-%s" % tmp_path.name)
    script = tmp_path.parent / ("hook-%s.sh" % tmp_path.name)
    script.write_text("#!/bin/sh\ntouch %s\n" % marker); script.chmod(0o755)
    return marker, script


def test_a_repo_fsmonitor_hook_is_not_executed_while_hashing(tmp_path):
    marker, script = _marker_script(tmp_path)
    p = _project(tmp_path)
    subprocess.run(["git", "-C", str(p), "config", "core.fsmonitor", str(script)], check=True)
    (p / "code.py").write_text("changed\n")
    marker.unlink(missing_ok=True)
    key, cause = _key(p)
    assert key, cause
    assert not marker.exists(), "git ran the repository's fsmonitor command while computing the key"


def test_git_is_never_run_inside_a_nested_repository(tmp_path):
    marker, script = _marker_script(tmp_path)
    p = _project(tmp_path)
    n = p / "vendor"; n.mkdir()
    subprocess.run(["git", "init", "-q", str(n)], check=True)
    subprocess.run(["git", "-C", str(n), "config", "core.fsmonitor", str(script)], check=True)
    (n / "f").write_text("1")
    marker.unlink(missing_ok=True)
    key, cause = _key(p)
    assert key, cause
    assert not marker.exists(), "git ran inside an untracked nested repository"


def test_a_nested_repos_working_files_and_head_are_covered(tmp_path):
    p = _project(tmp_path)
    n = p / "vendor"; n.mkdir()
    subprocess.run(["git", "init", "-q", str(n)], check=True)
    (n / "f").write_text("1")
    k1 = _key(p)[0]
    (n / "f").write_text("2")
    k2 = _key(p)[0]
    (n / ".git" / "HEAD").write_text("1" * 40 + "\n")            # detached: only HEAD itself differs
    k3 = _key(p)[0]
    (n / ".git" / "HEAD").write_text("2" * 40 + "\n")
    k4 = _key(p)[0]
    assert len({k1, k2, k3, k4}) == 4


def _tracked_variant(p, how):
    if how == "chmod":
        (p / "code.py").chmod(0o755)
    elif how == "delete":
        (p / "code.py").unlink()
    elif how == "empty":
        (p / "code.py").write_text("")
    elif how == "rename":
        (p / "code.py").rename(p / "moved.py")


def test_tracked_mode_deletion_emptying_and_rename_each_change_the_key(tmp_path):
    seen = set()
    for how in ("none", "chmod", "delete", "empty", "rename"):
        d = tmp_path / how; d.mkdir()
        p = _project(d)
        subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"], check=True, capture_output=True)
        _tracked_variant(p, how)
        key, cause = _key(p)
        assert key, cause
        seen.add((how, key))
    # HEAD differs per repo (commit time), so compare WITHIN one repo instead:
    p = _project(tmp_path)
    keys = []
    for how in ("none", "chmod", "delete", "empty"):
        keys.append(_key(p)[0]) if how == "none" else None
        _tracked_variant(p, how)
        if how != "none":
            keys.append(_key(p)[0])
        subprocess.run(["git", "-C", str(p), "checkout", "-q", "--", "code.py"], check=True)
    assert len(set(keys)) == len(keys), keys


def test_a_tracked_symlink_retarget_changes_the_key(tmp_path):
    p = _project(tmp_path)
    (p / "ln").symlink_to("a")
    subprocess.run(["git", "-C", str(p), "add", "ln"], check=True)
    subprocess.run(["git", "-C", str(p), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ln"], check=True)
    k1 = _key(p)[0]
    (p / "ln").unlink(); (p / "ln").symlink_to("b")
    assert _key(p)[0] != k1


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="posix only")
def test_a_fifo_is_never_opened(tmp_path):
    p = _project(tmp_path)
    os.mkfifo(p / "pipe")                     # git does not list it, so the key never touches it
    assert _key(p)[0]
    n = p / "vendor"; n.mkdir()
    subprocess.run(["git", "init", "-q", str(n)], check=True)
    os.mkfifo(n / "pipe")                     # inside a nested repo WE walk it: it must be refused, not opened
    import signal

    def alarm(*_):
        raise AssertionError("the key blocked on a FIFO: it was opened")
    signal.signal(signal.SIGALRM, alarm); signal.alarm(10)
    try:
        key, cause = _key(p)
    finally:
        signal.alarm(0)
    assert key is None and "FIFO" in cause


def test_framing_two_trees_that_collide_under_naive_concatenation_differ(tmp_path):
    """Naive framing: NUL + name + NUL + kind + content. Tree 1 = files a:'1' and b:'2'; tree 2 = ONE file
    a whose content is '1' + the bytes that would introduce b."""
    p = _project(tmp_path)
    (p / "a").write_bytes(b"1"); (p / "b").write_bytes(b"2")
    k1 = _key(p)[0]
    (p / "b").unlink()
    (p / "a").write_bytes(b"1\0b\0F2")
    assert _key(p)[0] != k1


def test_framing_empty_file_and_missing_file_differ(tmp_path):
    p = _project(tmp_path)
    (p / "e").write_bytes(b"")
    empty = _key(p)[0]
    (p / "e").unlink()
    assert _key(p)[0] != empty


def test_a_goal_name_that_is_not_a_safe_path_component_is_unbound(tmp_path):
    p = _project(tmp_path)
    key, cause = _key(p, goal="../../etc")
    assert key is None and "not usable" in cause


def test_a_mode_change_on_an_already_modified_file_changes_the_key(tmp_path):
    p = _project(tmp_path)
    (p / "code.py").write_text("edited\n")
    k1 = _key(p)[0]
    (p / "code.py").chmod(0o755)
    assert _key(p)[0] != k1


def test_frames_are_length_prefixed_so_a_name_cannot_swallow_the_next_record():
    rv = _rv()
    i, j = b"I" * 32, b"J" * 32
    two = rv._frame(b"u", b"a", i) + rv._frame(b"u", b"b", j)
    one = rv._frame(b"u", b"a" + i + b"u" + b"b", j)       # one record whose NAME imitates the first
    assert two != one
    assert rv._frame(b"uF", b"e", __import__("hashlib").sha256(b"").digest()) != rv._frame(b"uG", b"e", __import__("hashlib").sha256(b"").digest())


# ---- re-attack of #1384 @22cc97b: git filters, and a recovery lever that works ------------------------

def _filter_script(tmp_path):
    marker = tmp_path.parent / ("filter-ran-%s" % tmp_path.name)
    script = tmp_path.parent / ("filter-%s.sh" % tmp_path.name)
    script.write_text("#!/bin/sh\ntouch %s\ncat\n" % marker); script.chmod(0o755)
    return marker, script


@pytest.mark.parametrize("variant", ["clean", "process", "info_attributes"])
def test_a_git_clean_or_process_filter_is_never_executed_while_hashing(tmp_path, variant):
    marker, script = _filter_script(tmp_path)
    p = _project(tmp_path)
    key = "process" if variant == "process" else "clean"
    subprocess.run(["git", "-C", str(p), "config", "filter.ev.%s" % key, str(script)], check=True)
    attrs = "code.py filter=ev\n"
    if variant == "info_attributes":
        (p / ".git" / "info").mkdir(exist_ok=True); (p / ".git" / "info" / "attributes").write_text(attrs)
    else:
        (p / ".gitattributes").write_text(attrs)
    now = time.time_ns()
    os.utime(p / "code.py", ns=(now, now))                 # stat-dirty, content unchanged
    marker.unlink(missing_ok=True)
    k, cause = _key(p)
    assert k, cause
    assert not marker.exists(), "git ran the repository's %s filter while computing the key" % key
    reviewer_script(p, APPROVE)
    assert run_reviewer(p).returncode == 0
    assert not marker.exists()


def test_a_nested_repo_whose_dot_git_is_a_symlink_runs_no_git(tmp_path):
    marker, script = _marker_script(tmp_path)
    p = _project(tmp_path)
    real = tmp_path.parent / ("realgit-%s" % tmp_path.name)
    subprocess.run(["git", "init", "-q", str(real)], check=True)
    subprocess.run(["git", "-C", str(real), "config", "core.fsmonitor", str(script)], check=True)
    n = p / "vendor"; n.mkdir()
    (n / ".git").symlink_to(real / ".git")
    (n / "f").write_text("1")
    marker.unlink(missing_ok=True)
    key, cause = _key(p)
    assert not marker.exists()
    assert key or cause


def _config_host(p, host, **extra):
    cfg = json.loads((p / ".sdlc/config.json").read_text())
    cfg["review"] = {"host": host, **extra}
    (p / ".sdlc/config.json").write_text(json.dumps(cfg))


def _failed_codex(tmp_path, monkeypatch):
    bindir = _stub_host(tmp_path, monkeypatch, "codex", "exit 1\n")
    p = _project(tmp_path, review={"host": "codex"})
    assert run_reviewer(p).returncode == 1
    return p, bindir


def test_lever_subagent_route_is_not_blocked_by_an_old_process_failure(tmp_path, monkeypatch):
    """The old process failure does not apply to the subagent route; its own proof does: after a failure that
    proof must be a transcript-backed agent id (an unverifiable one is refused)."""
    p, bindir = _failed_codex(tmp_path, monkeypatch)
    _config_host(p, "claude")
    env = {**_without(bindir), "CLAUDECODE": "1"}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    r = work(p, "record-review", ".sdlc", GOAL, "--verdict", "APPROVE", env=env)
    assert r.returncode == 2 and "--agent-id" in r.stderr and "FAILED" not in r.stderr      # its own proof applies
    r = work(p, "record-review", ".sdlc", GOAL, "--verdict", "APPROVE", "--agent-id", "rev9", env=env)
    assert r.returncode == 2 and "transcript" in r.stderr                                   # unverifiable after a failure
    home = _transcript_home(tmp_path)
    env["HOME"] = str(home); env["CLAUDE_CODE_SESSION_ID"] = "sess1"
    r = work(p, "record-review", ".sdlc", GOAL, "--verdict", "APPROVE", "--agent-id", "real", env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["route"] == "subagent"


def test_lever_a_different_process_host_runs_and_supersedes_the_old_failure(tmp_path, monkeypatch):
    p, bindir = _failed_codex(tmp_path, monkeypatch)
    _stub_host(tmp_path, monkeypatch, "cursor-agent", "echo ok\necho 'VERDICT: approve'\n")
    _config_host(p, "cursor")
    r = record_review(p)                                          # no run on THIS route yet; the old failure is not its
    assert r.returncode == 2 and "no run of it is on record" in r.stderr and "FAILED" not in r.stderr
    assert run_reviewer(p).returncode == 0
    assert record_review(p).returncode == 0


def test_lever_same_route_after_a_fix_clears_it(tmp_path, monkeypatch):
    p, bindir = _failed_codex(tmp_path, monkeypatch)
    assert record_review(p).returncode == 2
    (bindir / "codex").write_text("#!/bin/sh\necho ok\necho 'VERDICT: approve'\n")
    assert run_reviewer(p).returncode == 0
    assert record_review(p).returncode == 0


def test_lever_inline_needs_the_recorded_owner_choice_not_just_a_config_edit(tmp_path, monkeypatch):
    p, bindir = _failed_codex(tmp_path, monkeypatch)
    _config_host(p, "inline")
    r = record_review(p)
    assert r.returncode == 2 and "accept-inline" in r.stderr            # the refusal names the lever
    gesture = [sys.executable, str(S / "reviewer.py"), "accept-inline", ".sdlc", "--goal", GOAL, "--phase", "review"]
    assert subprocess.run(gesture, cwd=p, capture_output=True, text=True).returncode == 2       # --reason required
    got = subprocess.run([*gesture, "--reason", "owner chose inline review in /sigma-unpark"], cwd=p,
                         capture_output=True, text=True)
    assert got.returncode == 0, got.stderr
    r = record_review(p)
    assert r.returncode == 0 and json.loads(r.stdout)["route"] == "inline"
    receipt = json.loads((p / ".sdlc/state/reviewer-runs/0001-x.review.json").read_text())
    assert receipt["outcome"] == "waived_inline" and receipt["failure"] == "nonzero_exit" and receipt["reason"]


def test_accept_inline_is_refused_where_it_would_launder_a_failure(tmp_path, monkeypatch):
    p, bindir = _failed_codex(tmp_path, monkeypatch)
    cmd = [sys.executable, str(S / "reviewer.py"), "accept-inline", ".sdlc", "--goal", GOAL, "--phase", "review",
           "--reason", "x"]
    env = _without(bindir)
    r = subprocess.run(cmd, cwd=p, capture_output=True, text=True, env=env)       # host still codex, now missing
    assert r.returncode == 2 and "review.host" in r.stderr
    r = subprocess.run(cmd, cwd=p, capture_output=True, text=True)                 # route is still process
    assert r.returncode == 2 and "not inline" in r.stderr
    q = tmp_path / "q"; q.mkdir()
    p2 = _project(q, review={"host": "inline"})
    r = subprocess.run(cmd, cwd=p2, capture_output=True, text=True)                # nothing failed
    assert r.returncode == 2 and "nothing to accept" in r.stderr


def test_the_d1_repro_stays_closed_after_scoping_the_receipt_to_its_route(tmp_path, monkeypatch):
    p, bindir = _failed_codex(tmp_path, monkeypatch)
    r = record_review(p, env=_without(bindir))                  # host still codex, binary gone
    assert r.returncode == 2 and "FAILED" in r.stderr and "not installed" in r.stderr
    _config_host(p, "auto")                                     # a config edit alone is not the owner's choice
    r = record_review(p, env=_without(bindir))
    assert r.returncode == 2 and "accept-inline" in r.stderr


def test_phase_status_ignores_a_failure_from_another_route(tmp_path, monkeypatch):
    p, bindir = _failed_codex(tmp_path, monkeypatch)
    rv = _rv()
    monkeypatch.setenv("CLAUDECODE", "1")
    _config_host(p, "claude")
    assert rv.failed_receipt(str(p / ".sdlc"), GOAL, "review") is None


def test_a_receipt_with_no_route_still_applies_except_on_the_subagent_route(tmp_path, monkeypatch):
    p = _project(tmp_path)
    _rv()._write_receipt(str(p / ".sdlc"), GOAL, "review", outcome="failed", failure="nonzero_exit")   # untagged
    rv = _rv()
    assert rv.failed_receipt(str(p / ".sdlc"), GOAL, "review") == ("nonzero_exit", "")
    monkeypatch.setenv("CLAUDECODE", "1")
    _config_host(p, "claude")
    assert rv.failed_receipt(str(p / ".sdlc"), GOAL, "review") is None


# ---- third attack on #1384 @947b0c4 ----------------------------------------------------------------

ACCEPT = lambda p, phase="review", reason="owner chose inline review": subprocess.run(
    [sys.executable, str(S / "reviewer.py"), "accept-inline", ".sdlc", "--goal", GOAL, "--phase", phase,
     "--reason", reason], cwd=p, capture_output=True, text=True)


def test_pr_review_path_leaves_a_receipt_and_its_inline_branch_refuses_after_a_failure(tmp_path, monkeypatch):
    """Exact sequence: failing codex stub -> the PR path is refused AND leaves a receipt; codex removed and
    host set inline -> the PR path's inline branch still refuses; accept-inline lets it through."""
    bindir = _stub_host(tmp_path, monkeypatch, "codex", "exit 1\n")
    p = _project(tmp_path, review={"host": "codex"})
    d = str(p / ".sdlc")
    w = _work()
    manifest, resolution = _generation(d, name="g1")
    with pytest.raises(ValueError, match="resolved reviewer failed"):
        w.run_resolved_review(d, "2577", str(manifest), str(resolution))
    assert (p / ".sdlc/state/reviewer-runs/2577.review.json").exists()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _config_host(p, "inline")
    manifest, resolution = _generation(d, name="g2")
    with pytest.raises(ValueError, match="accept-inline"):
        w.run_resolved_review(d, "2577", str(manifest), str(resolution), verdict="approve", reason="self")
    got = subprocess.run([sys.executable, str(S / "reviewer.py"), "accept-inline", ".sdlc", "--goal", "2577",
                          "--phase", "review", "--reason", "owner chose inline"], cwd=p, capture_output=True, text=True)
    assert got.returncode == 0, got.stderr
    manifest, resolution = _generation(d, name="g3")
    assert w.run_resolved_review(d, "2577", str(manifest), str(resolution), verdict="approve",
                                 reason="self")["verdict"] == "approve"


def test_record_subagent_review_needs_the_same_agent_id_proof_as_record_review(tmp_path, monkeypatch):
    p = _project(tmp_path, review={"host": "claude"})
    monkeypatch.setenv("CLAUDECODE", "1")
    d = str(p / ".sdlc")
    manifest, resolution = _generation(d, goal=GOAL, name="s1")
    args = ["record-subagent-review", ".sdlc", GOAL, "--manifest", str(manifest), "--resolution", str(resolution),
            "--verdict", "approve", "--reason", "clean"]
    env = {**os.environ}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    r = work(p, *args, env=env)
    assert r.returncode == 2 and "--agent-id" in r.stderr
    r = work(p, *args, "--agent-id", "rev9", env=env)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("sibling", ["record-review", "record-plan-review", "record-subagent-review", "pr-inline"])
def test_every_sibling_refuses_the_same_laundering_attack_alike(tmp_path, monkeypatch, sibling):
    """SAME attack for every sibling: a failed independent run, then the route flips to inline by a config
    edit. Each refuses; accept-inline then clears exactly the phase it was run for."""
    bindir = _stub_host(tmp_path, monkeypatch, "codex", "exit 1\n")
    p = _project(tmp_path, review={"host": "codex"})
    goal = "2577" if sibling == "pr-inline" else GOAL
    phase = "plan_review" if sibling == "record-plan-review" else "review"
    if phase == "plan_review":
        (p / "brief.md").write_text(brief_text("plan-review"))
    got = subprocess.run([sys.executable, str(S / "reviewer.py"), "run", ".sdlc", "brief.md", "--scratch", "scratch",
                          "--goal", goal, "--phase", phase], cwd=p, capture_output=True, text=True)
    assert got.returncode == 1
    _config_host(p, "inline")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    d = str(p / ".sdlc")

    def attempt():
        if sibling == "record-review":
            r = record_review(p, env=os.environ.copy()); return r.returncode, r.stderr
        if sibling == "record-plan-review":
            r = record_plan(p, env=os.environ.copy()); return r.returncode, r.stderr
        if sibling == "record-subagent-review":
            manifest, resolution = _generation(d, goal=goal, name="x%d" % attempt.n); attempt.n += 1
            r = work(p, "record-subagent-review", ".sdlc", goal, "--manifest", str(manifest), "--resolution",
                     str(resolution), "--verdict", "approve", "--reason", "r", "--agent-id", "a1", env=os.environ.copy())
            return r.returncode, r.stderr
        manifest, resolution = _generation(d, goal=goal, name="x%d" % attempt.n); attempt.n += 1
        try:
            _work().run_resolved_review(d, goal, str(manifest), str(resolution), verdict="approve", reason="r")
            return 0, ""
        except ValueError as exc:
            return 2, str(exc)
    attempt.n = 0
    code, err = attempt()
    assert code == 2, (sibling, err)
    if sibling != "record-subagent-review":              # that verb needs the subagent route outright
        assert "accept-inline" in err, (sibling, err)
        assert ACCEPT(p, phase).returncode == 0 if goal == GOAL else subprocess.run(
            [sys.executable, str(S / "reviewer.py"), "accept-inline", ".sdlc", "--goal", goal, "--phase", phase,
             "--reason", "owner"], cwd=p, capture_output=True, text=True).returncode == 0
        code, err = attempt()
        assert code == 0, (sibling, err)


def test_accept_inline_is_stamped_on_the_verdict_row_and_survives_finish(tmp_path, monkeypatch):
    p, bindir = _failed_codex(tmp_path, monkeypatch)
    _config_host(p, "inline")
    assert ACCEPT(p, reason="owner  chose\x07 inline\nreview").returncode == 0
    r = record_review(p)
    assert r.returncode == 0, r.stderr
    row = json.loads(r.stdout)
    assert row["waiver"] == "accept-inline" and row["waiver_reason"] == "owner chose inline review"
    _rv().clear_receipts(str(p / ".sdlc"), GOAL)                       # what work.py finish does
    assert "waiver" in json.loads([x for x in (p / ".sdlc/state/log/0001-x.jsonl").read_text().splitlines()
                                   if '"verdict"' in x][-1])
    for verb, extra in (("start", ["--model", "sonnet"]), ("end", [])):
        subprocess.run([sys.executable, str(S / "phase_report.py"), verb, ".sdlc", GOAL, "review", *extra],
                       cwd=p, capture_output=True, text=True, check=True)
    spec = importlib.util.spec_from_file_location("pg_f02b", S / "phase_gate.py")
    pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)
    cfg = json.loads((p / ".sdlc/config.json").read_text())
    notes = " ".join(pg.status(str(p / ".sdlc"), cfg, GOAL)["notes"])
    assert "INLINE by owner decision: owner chose inline review" in notes and "cannot spawn" not in notes


def test_accept_inline_reason_is_bounded_and_a_repeat_is_refused(tmp_path, monkeypatch):
    p, bindir = _failed_codex(tmp_path, monkeypatch)
    _config_host(p, "inline")
    r = ACCEPT(p, reason="x" * 501)
    assert r.returncode == 2 and "500" in r.stderr
    assert ACCEPT(p).returncode == 0
    r = ACCEPT(p, reason="a different reason")
    assert r.returncode == 2 and "already" in r.stderr
    receipt = json.loads((p / ".sdlc/state/reviewer-runs/0001-x.review.json").read_text())
    assert receipt["reason"] == "owner chose inline review"


def test_a_waiver_is_bound_to_the_bytes_it_was_given_for(tmp_path, monkeypatch):
    p, bindir = _failed_codex(tmp_path, monkeypatch)
    _config_host(p, "inline")
    assert ACCEPT(p).returncode == 0
    (p / "code.py").write_text("edited after the owner's choice\n")
    r = record_review(p)
    assert r.returncode == 2 and "accept-inline" in r.stderr


def test_a_plan_review_waiver_is_bound_to_the_plan_it_was_given_for(tmp_path, monkeypatch):
    bindir = _stub_host(tmp_path, monkeypatch, "codex", "exit 1\n")
    p = _project(tmp_path, review={"host": "codex"})
    (p / "brief.md").write_text(brief_text("plan-review"))
    got = subprocess.run([sys.executable, str(S / "reviewer.py"), "run", ".sdlc", "brief.md", "--scratch", "scratch",
                          "--goal", GOAL, "--phase", "plan_review"], cwd=p, capture_output=True, text=True)
    assert got.returncode == 1
    _config_host(p, "inline")
    assert ACCEPT(p, "plan_review").returncode == 0
    rv = _rv()
    assert rv.independent_run_refusal(str(p / ".sdlc"), GOAL, "plan_review", SHA) is None
    assert "accept-inline" in rv.independent_run_refusal(str(p / ".sdlc"), GOAL, "plan_review", "f" * 64)


def test_docs_and_skills_name_accept_inline_as_an_owner_audit_trail():
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("skills/sigma-review/SKILL.md", "skills/sigma-plan-review/SKILL.md",
                "skills/sigma-loop/references/running.md", "skills/sigma-goal-review/references/adjudication.md",
                "skills/sigma-loop/SKILL.md", "skills/sigma-loop/references/landing.md", "docs/label-model.md"):
        text = (root / rel).read_text()
        assert "accept-inline" in text, rel
        assert "audit trail" in text or "not an authority check" in text, rel


def test_the_refusal_says_accept_inline_is_an_audit_trail_the_owner_runs(tmp_path, monkeypatch):
    p, bindir = _failed_codex(tmp_path, monkeypatch)
    _config_host(p, "inline")
    r = record_review(p)
    assert "audit trail" in r.stderr and "owner" in r.stderr and "not the maker" in r.stderr


# ---- the key must not read through symlinks, nor block on a FIFO ------------------------------------

def _outside(tmp_path, name="canary"):
    d = tmp_path.parent / ("out-%s-%s" % (name, tmp_path.name)); d.mkdir(exist_ok=True)
    (d / "code.py").write_text("OUTSIDE BYTES\n")
    return d


def test_a_tracked_directory_replaced_by_a_symlink_is_refused_not_read_through(tmp_path):
    p = _project(tmp_path)
    (p / "pkg").mkdir(); (p / "pkg" / "code.py").write_text("in\n")
    subprocess.run(["git", "-C", str(p), "add", "pkg"], check=True)
    subprocess.run(["git", "-C", str(p), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "pkg"], check=True)
    import shutil
    shutil.rmtree(p / "pkg"); (p / "pkg").symlink_to(_outside(tmp_path))
    key, cause = _key(p)
    assert key is None and "symlink" in cause


def test_a_regular_file_swapped_for_a_symlink_after_lstat_is_not_followed(tmp_path, monkeypatch):
    p = _project(tmp_path)
    out = _outside(tmp_path) / "code.py"
    rv = _rv()
    real = rv.os.lstat
    swapped = []
    def lstat(path, *a, **k):
        got = real(path, *a, **k)
        if str(path).endswith("/code.py") and not swapped and str(p) in str(path):
            swapped.append(1); os.unlink(path); os.symlink(out, path)
        return got
    monkeypatch.setattr(rv.os, "lstat", lstat)
    key, cause = rv._content_key(str(p / ".sdlc"), GOAL)
    assert swapped and key is None, (key, cause)


def test_a_regular_file_swapped_for_a_fifo_after_lstat_does_not_hang(tmp_path, monkeypatch):
    import signal
    p = _project(tmp_path)
    rv = _rv()
    real = rv.os.lstat
    swapped = []
    def lstat(path, *a, **k):
        got = real(path, *a, **k)
        if str(path).endswith("/code.py") and not swapped and str(p) in str(path):
            swapped.append(1); os.unlink(path); os.mkfifo(path)
        return got
    monkeypatch.setattr(rv.os, "lstat", lstat)
    def alarm(*_):
        raise AssertionError("the key blocked on a FIFO")
    signal.signal(signal.SIGALRM, alarm); signal.alarm(10)
    try:
        key, cause = rv._content_key(str(p / ".sdlc"), GOAL)
    finally:
        signal.alarm(0)
    assert swapped and key is None


def test_the_deadline_is_one_overall_budget_even_for_symlinks_and_absent_paths(tmp_path, monkeypatch):
    p = _project(tmp_path)
    (p / "code.py").unlink(); (p / "brief.md").unlink()          # only symlinks and an absent tracked file remain
    import shutil
    shutil.rmtree(p / ".sdlc")                                   # the key needs no config; leave no regular file
    for i in range(30):
        (p / ("ln%d" % i)).symlink_to("target")
    rv = _rv()
    monkeypatch.setattr(rv, "_KEY_SECONDS", -1.0)
    key, cause = rv._content_key(str(p / ".sdlc"), GOAL)
    assert key is None and "too large or slow" in cause


# ---- final verification round ----------------------------------------------------------------------

def _nested_git(p, name="nested"):
    n = p / name; n.mkdir()
    subprocess.run(["git", "init", "-q", str(n)], check=True)
    (n / "f").write_text("x")
    subprocess.run(["git", "-C", str(n), "-c", "user.name=t", "-c", "user.email=t@t", "add", "f"], check=True)
    subprocess.run(["git", "-C", str(n), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "m"], check=True)
    return n


def test_a_symlinked_ref_directory_inside_a_nested_dot_git_is_refused_not_read(tmp_path):
    import shutil
    p = _project(tmp_path)
    n = _nested_git(p)
    out = tmp_path.parent / ("ref-out-%s" % tmp_path.name); out.mkdir()
    (out / "canary").write_text("OUTSIDE\n")
    shutil.rmtree(n / ".git/refs/heads"); (n / ".git/refs/heads").symlink_to(out)
    (n / ".git/HEAD").write_text("ref: refs/heads/canary\n")
    key, cause = _key(p)
    assert key is None and "symlink" in cause


@pytest.mark.parametrize("ref", [b"refs/../../x", b"/etc/passwd", b"heads/x", b"refs//x", b"refs/heads/..", b"refs\\x", b"refs/a\0b"])
def test_an_unusable_ref_name_is_refused_before_it_is_read(tmp_path, ref):
    """git itself rejects most of these HEADs (it then lists the files individually), so the guard is
    exercised directly: it is the second wall behind git's own."""
    rv = _rv()
    gitdir = tmp_path / "g"; gitdir.mkdir()
    with pytest.raises(rv._Unbound, match="unusable ref"):
        rv._ref_path_is_clean(str(gitdir), ref)


def test_a_nested_head_that_is_a_fifo_or_a_link_is_refused_without_blocking(tmp_path):
    import signal
    rv = _rv()
    fifo = tmp_path / "HEAD-fifo"; os.mkfifo(fifo)
    link = tmp_path / "HEAD-link"; link.symlink_to(tmp_path / "elsewhere")
    deadline = time.monotonic() + 60

    def alarm(*_):
        raise AssertionError("_read_head blocked or followed a link")
    signal.signal(signal.SIGALRM, alarm); signal.alarm(10)
    try:
        for path in (fifo, link):
            with pytest.raises(rv._Unbound, match="regular file"):
                rv._read_head(str(path), deadline)
    finally:
        signal.alarm(0)


def test_a_fifo_at_a_nested_head_is_bounded_end_to_end(tmp_path, monkeypatch):
    """Here git itself blocks reading the FIFO; the subprocess timeout bounds it and the key fails closed."""
    import signal
    p = _project(tmp_path)
    n = _nested_git(p)
    os.unlink(n / ".git/HEAD"); os.mkfifo(n / ".git/HEAD")
    rv = _rv()
    monkeypatch.setattr(rv, "_KEY_SECONDS", 2.0)

    def alarm(*_):
        raise AssertionError("the key blocked on a FIFO at .git/HEAD")
    signal.signal(signal.SIGALRM, alarm); signal.alarm(30)
    try:
        key, cause = rv._content_key(str(p / ".sdlc"), GOAL)
    finally:
        signal.alarm(0)
    assert key is None and cause


def _fail_codex_then_claude(tmp_path, monkeypatch, phase):
    bindir = _stub_host(tmp_path, monkeypatch, "codex", "exit 1\n")
    p = _project(tmp_path, review={"host": "codex"})
    if phase == "plan_review":
        (p / "brief.md").write_text(brief_text("plan-review"))
    got = subprocess.run([sys.executable, str(S / "reviewer.py"), "run", ".sdlc", "brief.md", "--scratch", "scratch",
                          "--goal", GOAL, "--phase", phase], cwd=p, capture_output=True, text=True)
    assert got.returncode == 1
    _config_host(p, "claude")
    return p, bindir


def _claude_env(bindir, home=None, session=None):
    env = {**_without(bindir), "CLAUDECODE": "1"}
    env.pop("CLAUDE_CODE_SESSION_ID", None); env.pop("CLAUDE_SESSION_ID", None)
    if home:
        env["HOME"] = str(home); env["CLAUDE_CODE_SESSION_ID"] = session
    return env


def _transcript_home(tmp_path):
    home = tmp_path / "home"
    (home / ".claude/projects/proj/sess1/subagents").mkdir(parents=True)
    (home / ".claude/projects/proj/sess1/subagents/agent-real.jsonl").write_text("{}\n")
    return home


@pytest.mark.parametrize("verb", ["record-review", "record-plan-review", "record-subagent-review"])
def test_after_a_failed_run_a_subagent_approval_needs_a_transcript_backed_agent_id(tmp_path, monkeypatch, verb):
    phase = "plan_review" if verb == "record-plan-review" else "review"
    p, bindir = _fail_codex_then_claude(tmp_path, monkeypatch, phase)
    d = str(p / ".sdlc")

    def attempt(agent, env, n):
        if verb == "record-review":
            return work(p, verb, ".sdlc", GOAL, "--verdict", "APPROVE", "--agent-id", agent, env=env)
        if verb == "record-plan-review":
            return work(p, verb, ".sdlc", GOAL, "--verdict", "SOUND", "--plan-sha256", SHA, "--agent-id", agent, env=env)
        monkeypatch.setenv("CLAUDECODE", "1")                       # so the resolution says `subagent`
        manifest, resolution = _generation(d, goal=GOAL, name="m%d" % n)
        return work(p, verb, ".sdlc", GOAL, "--manifest", str(manifest), "--resolution", str(resolution),
                    "--verdict", "approve", "--reason", "r", "--agent-id", agent, env=env)
    r = attempt("fake1", _claude_env(bindir), 1)                       # no readable store: only "unverified"
    assert r.returncode == 2 and "transcript" in r.stderr and "accept-inline" in r.stderr, (verb, r.stderr)
    home = _transcript_home(tmp_path)
    r = attempt("ghost", _claude_env(bindir, home, "sess1"), 2)         # a readable store with no such agent
    assert r.returncode == 2
    r = attempt("real", _claude_env(bindir, home, "sess1"), 3)          # a real transcript-backed id
    assert r.returncode == 0, (verb, r.stderr)


def test_with_no_prior_failure_an_unverified_subagent_id_is_still_accepted(tmp_path):
    p = _project(tmp_path, review={"host": "claude"})
    env = _claude_env(tmp_path / "nobin")
    r = work(p, "record-review", ".sdlc", GOAL, "--verdict", "APPROVE", "--agent-id", "fake1", env=env)
    assert r.returncode == 0, r.stderr


def test_the_read_loop_budget_check_stops_a_file_that_becomes_too_slow_mid_read(tmp_path, monkeypatch):
    import shutil
    p = _project(tmp_path)
    shutil.rmtree(p / ".sdlc"); (p / "brief.md").unlink(missing_ok=True)
    (p / "code.py").write_bytes(os.urandom(3 * 1024 * 1024))             # the only record, three 1 MB chunks
    rv = _rv()
    clock = [0.0]
    real_read = rv.os.read
    monkeypatch.setattr(rv.time, "monotonic", lambda: clock[0])
    def read(fd, n):
        got = real_read(fd, n)
        if n == 1 << 20:                                                # only the key's own file reads (not git's pipes)
            clock[0] += 1000.0                                          # time passes while reading
        return got
    monkeypatch.setattr(rv.os, "read", read)
    key, cause = rv._content_key(str(p / ".sdlc"), GOAL)
    assert key is None and "too large or slow" in cause


def test_the_pr_review_result_carries_the_waiver_and_its_reason(tmp_path, monkeypatch):
    bindir = _stub_host(tmp_path, monkeypatch, "codex", "exit 1\n")
    p = _project(tmp_path, review={"host": "codex"})
    d = str(p / ".sdlc")
    w = _work()
    manifest, resolution = _generation(d, name="w1")
    with pytest.raises(ValueError):
        w.run_resolved_review(d, "2577", str(manifest), str(resolution))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _config_host(p, "inline")
    got = subprocess.run([sys.executable, str(S / "reviewer.py"), "accept-inline", ".sdlc", "--goal", "2577",
                          "--phase", "review", "--reason", "owner chose inline"], cwd=p, capture_output=True, text=True)
    assert got.returncode == 0, got.stderr
    manifest, resolution = _generation(d, name="w2")
    out = w.run_resolved_review(d, "2577", str(manifest), str(resolution), verdict="approve", reason="self")
    saved = json.loads(pathlib.Path(out["path"]).read_text())
    assert saved["waiver"] == "accept-inline" and saved["waiver_reason"] == "owner chose inline"


@pytest.mark.parametrize("swap", ["symlink", "fifo"])
def test_a_nested_head_swapped_after_lstat_is_refused_not_followed_or_blocked_on(tmp_path, monkeypatch, swap):
    import signal
    rv = _rv()
    head = tmp_path / "HEAD"; head.write_text("ref: refs/heads/main\n")
    outside = tmp_path / "outside"; outside.write_text("OUTSIDE\n")
    real = rv.os.lstat
    done = []

    def lstat(path, *a, **k):
        got = real(path, *a, **k)
        if str(path) == str(head) and not done:
            done.append(1); os.unlink(path)
            os.symlink(outside, path) if swap == "symlink" else os.mkfifo(path)
        return got
    monkeypatch.setattr(rv.os, "lstat", lstat)

    def alarm(*_):
        raise AssertionError("_read_head blocked on a FIFO")
    signal.signal(signal.SIGALRM, alarm); signal.alarm(10)
    try:
        with pytest.raises(rv._Unbound):
            rv._read_head(str(head), time.monotonic() + 60)
    finally:
        signal.alarm(0)
    assert done
