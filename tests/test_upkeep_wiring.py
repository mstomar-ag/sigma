"""Goal 1016: the slice-6 helpers wired into `upkeep_pass`. Real local git repositories, injected fakes, no network."""
import json
import subprocess

import pytest

import upkeep_support as support
from test_upkeep_pass import commit, git, mod, repo  # noqa: F401 - the fixture and helpers of the pass tests

OPEN = {"upkeep": {"enabled": True}}


def diverged(root):
    """The unit has a commit of its own and the base moved on: behind, not landed."""
    commit(root, "feature/u", "u.txt")
    commit(root, "main", "b.txt")


def run_pass(root, config=OPEN, **kw):
    return mod().upkeep_pass(config, str(root / ".sdlc"), "u", "feature/u", "main", "main", 1000, cwd=root, **kw)


class Notes:
    def __init__(self):
        self.calls = []

    def __call__(self, sdlc_dir, kind, goal, **kw):
        self.calls.append((kind, goal, kw))
        return {"goal": goal}


def ok_rewrite(run, tips):
    return {"ok": True}


def files(root):
    return sorted(p.name for p in (root / ".sdlc").rglob("*") if p.is_file())


# ------------------------------------------------------------------ gate closed: one test per wired call

@pytest.mark.parametrize("name", ["rewrite", "push", "goals", "direct_commits", "ledger_append", "runner_factory"])
def test_gate_closed_touches_no_wired_call(repo, name):
    diverged(repo)

    def boom(*a, **k):
        raise AssertionError("%s was called with the gate closed" % name)
    kw = {"rewrite": ok_rewrite, "push": ok_rewrite, "goals": [{"name": "g", "replay": boom}],
          "direct_commits": boom, "ledger_append": boom, "runner_factory": boom}
    kw[name] = boom if name not in ("goals",) else [{"name": "g", "replay": boom}]
    for config in ({}, {"upkeep": {"enabled": False}}):
        result = run_pass(repo, config, **kw)
        assert result["closed"] is True
    assert files(repo) == []


# ------------------------------------------------------------------ (1) restarts

def test_tip_moving_once_restarts_the_sequence(repo):
    diverged(repo)
    seen = []

    def rewrite(run, tips):
        seen.append(tips[0])
        if len(seen) == 1:
            commit(repo, "feature/u", "other.txt")         # somebody else pushed mid-pass
        return {"ok": True}
    pushes = []
    result = run_pass(repo, rewrite=rewrite, push=lambda run, tips: pushes.append(tips), ledger_append=Notes())
    assert result["result"] == "rewritten" and result["restarts"] == 1
    assert len(seen) == 2 and seen[0] != seen[1] and len(pushes) == 1 and pushes[0][0] == seen[1]


def test_tip_moving_every_time_is_moved_and_never_pushes(repo):
    diverged(repo)
    n = []

    def rewrite(run, tips):
        n.append(1)
        commit(repo, "feature/u", "m%d.txt" % len(n))
        return {"ok": True}
    pushes, notes = [], Notes()
    result = run_pass(repo, rewrite=rewrite, push=lambda run, tips: pushes.append(1), ledger_append=notes)
    assert result["result"] == "moved" and pushes == [] and len(n) == 3     # 1 first run + 2 restarts
    assert notes.calls[0][2]["ref"].startswith("upkeep:moved:")


@pytest.mark.parametrize("rewrite", [lambda run, tips: {"ok": False}, lambda run, tips: 1 / 0])
def test_a_failed_or_raising_rewrite_never_pushes(repo, rewrite):
    diverged(repo)
    pushes = []
    result = run_pass(repo, rewrite=rewrite, push=lambda run, tips: pushes.append(1), ledger_append=Notes())
    assert result["result"] == "failed" and pushes == []


def test_a_raising_push_is_a_failed_pass(repo):
    diverged(repo)

    def push(run, tips):
        raise RuntimeError("refused")
    assert run_pass(repo, rewrite=ok_rewrite, push=push, ledger_append=Notes())["result"] == "failed"


# ------------------------------------------------------------------ (2) engine runner and hooks policy

def test_replay_and_push_get_runners_built_with_the_hooks_policy(repo):
    diverged(repo)
    m = mod()
    phases = []

    def factory(hooks, environ=None):
        phases.append(hooks)
        return lambda cwd, argv: ""
    for resolved, expect in ((False, m.CLEAN_PUSH), (True, m.RESOLVED_PUSH)):
        del phases[:]
        run_pass(repo, runner_factory=factory, rewrite=lambda run, tips: {"ok": True, "resolved": resolved},
                 push=lambda run, tips: None, ledger_append=Notes())
        assert phases == [m.hooks_policy(m.REPLAY), m.hooks_policy(expect)]
    assert m.hooks_policy(m.REPLAY) != m.hooks_policy(m.RESOLVED_PUSH)


# ------------------------------------------------------------------ (3) acks

def test_acks_are_unioned_after_the_rewrite(repo):
    diverged(repo)
    acks = [{"sha": "a" * 40, "patch_id": "p1"}]
    run_pass(repo, rewrite=lambda run, tips: {"ok": True, "acks": acks}, ledger_append=Notes())
    run_pass(repo, rewrite=lambda run, tips: {"ok": True, "acks": acks + [{"sha": "b" * 40, "patch_id": "p2"}]},
             ledger_append=Notes())
    data = json.loads(next((repo / ".sdlc").rglob("acks.json")).read_text())
    assert [a["patch_id"] for a in data["units"]["u"]] == ["p1", "p2"]


# ------------------------------------------------------------------ (4) re-anchor limit and (5) deleted goal branch

def goal(**kw):
    calls = {"replay": 0, "reanchor": 0}

    def replay(run_replay, run_push):
        calls["replay"] += 1
        return {"ok": True, "level": kw.get("level", 2), "verify_green": kw.get("verify_green"),
                "push_refused": kw.get("push_refused", False)}

    def reanchor():
        calls["reanchor"] += 1
    entry = {"name": "g1", "replay": replay, "reanchor": reanchor, "pr_merged": kw.get("pr_merged", False),
             "remote_exists": lambda: kw.get("remote_exists", True)}
    return entry, calls


@pytest.mark.parametrize("level,green,expected", [(1, None, 1), ("mechanical", False, 1), (2, True, 1), (2, None, 0),
                                                  (2, "yes", 0), (None, False, 0)])
def test_reanchor_only_after_mechanical_change_or_green_verify(repo, level, green, expected):
    diverged(repo)
    entry, calls = goal(level=level, verify_green=green)
    result = run_pass(repo, rewrite=ok_rewrite, goals=[entry], ledger_append=Notes())
    assert calls["replay"] == 1 and calls["reanchor"] == expected
    assert result["goals"][0]["reanchored"] is bool(expected)


def test_deleted_goal_branch_after_merge_is_a_skip_and_never_replayed(repo):
    diverged(repo)
    entry, calls = goal(pr_merged=True, remote_exists=False)
    result = run_pass(repo, rewrite=ok_rewrite, goals=[entry], ledger_append=Notes())
    assert calls == {"replay": 0, "reanchor": 0}                     # no resurrection
    assert result["goals"][0] == {"name": "g1", "result": "skipped", "reason": "goal-branch-deleted-after-merge",
                                  "reanchored": False}
    assert result["conflicts"] == []


def test_refused_push_is_classified_after_the_replay_too(repo):
    diverged(repo)
    merged = {"pr": False}
    entry, calls = goal(push_refused=True)
    entry["pr_merged"] = lambda: merged["pr"]                         # learned late: merged while we replayed
    entry["remote_exists"] = lambda: not merged["pr"]
    first = entry["replay"]

    def replay(a, b):
        merged["pr"] = True
        return first(a, b)
    entry["replay"] = replay
    result = run_pass(repo, rewrite=ok_rewrite, goals=[entry], ledger_append=Notes())
    assert result["goals"][0]["reason"] == "goal-branch-deleted-after-merge" and result["conflicts"] == []
    other, _ = goal(push_refused=True, pr_merged=False)
    result = run_pass(repo, rewrite=ok_rewrite, goals=[other], ledger_append=Notes())
    assert result["conflicts"] == ["g1"] and result["goals"][0]["result"] == "refused"


# ------------------------------------------------------------------ (6) direct commits on a current unit

def test_direct_commits_on_a_current_unit_are_a_skip_reason_not_a_rewrite(repo):
    commit(repo, "feature/u", "u.txt")                                # base is an ancestor: current
    rewrites = []
    result = run_pass(repo, rewrite=lambda run, tips: rewrites.append(1), direct_commits=lambda tips: ["abc"],
                      ledger_append=Notes())
    assert rewrites == [] and (result["result"], result["reason"]) == ("skipped", "direct-commits-on-current-unit")


def test_current_unit_without_direct_commits_is_still_not_rewritten(repo):
    commit(repo, "feature/u", "u.txt")
    rewrites = []
    result = run_pass(repo, rewrite=lambda run, tips: rewrites.append(1), direct_commits=lambda tips: [],
                      ledger_append=Notes())
    assert rewrites == [] and (result["result"], result["reason"]) == ("skipped", "current")


# ------------------------------------------------------------------ (7) the ledger note

def test_ledger_note_is_unaddressed_and_keyed_by_the_unit(repo):
    diverged(repo)
    notes = Notes()
    result = run_pass(repo, rewrite=ok_rewrite, ledger_append=notes)
    (kind, key, kw), = notes.calls
    assert kind == "note" and key == "upkeep-u" and "to" not in kw
    assert kw["ref"] == "upkeep:rewritten:%s" % result["unit_tip"][:12]


def test_landed_skip_also_writes_the_note(repo):
    commit(repo, "main", "b.txt")
    notes = Notes()
    result = run_pass(repo, ledger_append=notes)
    assert result["result"] == "skipped" and notes.calls[0][2]["ref"].startswith("upkeep:skipped:")


def test_plain_pass_without_the_wiring_is_unchanged(repo):
    diverged(repo)
    result = run_pass(repo)
    assert result["result"] == "not-landed" and files(repo) == []
