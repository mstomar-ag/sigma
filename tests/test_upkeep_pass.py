"""Goal 922: the upkeep pass under the opt-in. Real local git repositories, a fake landing-request source, no network."""
import json
import pathlib
import subprocess

import pytest

import upkeep_support as support

OPEN = {"upkeep": {"enabled": True}}


def git(cwd, *argv):
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *argv],
                         cwd=str(cwd), capture_output=True, text=True, check=True, env=env)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a")
    git(root, "add", "a.txt")
    git(root, "commit", "-q", "-m", "a")
    git(root, "branch", "feature/u")
    (root / ".sdlc").mkdir()
    return root


def commit(root, branch, name, message=None):
    git(root, "checkout", "-q", branch)
    (root / name).write_text(name)
    git(root, "add", name)
    git(root, "commit", "-q", "-m", message or name)
    git(root, "checkout", "-q", "main")


def call(mod, root, config=OPEN, **kw):
    return mod.upkeep_pass(config, str(root / ".sdlc"), "u", "feature/u", "main", "main", 1000, cwd=root, **kw)


def mod():
    return support.script("feature_upkeep_pass")


def state_files(root):
    return sorted(p.name for p in (root / ".sdlc").rglob("*") if p.is_file())


def test_gate_closed_does_nothing(repo):
    m = mod()
    calls = []
    for config in ({}, {"upkeep": {"enabled": False}}, {"upkeep": {"enabled": "true"}}):
        result = call(m, repo, config, run=lambda cwd, argv: calls.append(argv))
        assert result["closed"] is True
    assert calls == [] and state_files(repo) == []


def test_contained_unit_is_skipped_and_recorded(repo):
    commit(repo, "main", "b.txt")                      # the unit tip is behind main and contained in it
    result = call(mod(), repo)
    assert (result["result"], result["reason"]) == ("skipped", "landed-contained")
    assert result["recorded"] is True and state_files(repo) == ["u.json"]
    doc = json.loads(next((repo / ".sdlc").rglob("u.json")).read_text())
    assert doc["outcome"] == "current" and doc["unit_tip"] == result["unit_tip"] and doc["base_tip"] == result["base_tip"]


def test_unit_ahead_of_base_is_not_landed(repo):
    commit(repo, "feature/u", "c.txt")
    result = call(mod(), repo)
    assert (result["result"], result["reason"]) == ("not-landed", "not-landed")
    assert state_files(repo) == []


def _squash_landed(repo):
    commit(repo, "feature/u", "c.txt")
    tip = git(repo, "rev-parse", "feature/u")
    commit(repo, "main", "c.txt", "squash")           # same content landed as a different commit: contained test says no
    return tip


def test_squash_landing_found_by_merged_request_head(repo):
    tip = _squash_landed(repo)
    prs = lambda: [{"merged": True, "base": "main", "head_sha": tip}]
    result = call(mod(), repo, landing_prs=prs)
    assert (result["result"], result["reason"]) == ("skipped", "landed-pr")


@pytest.mark.parametrize("request_", [
    {"merged": False, "base": "main", "head_sha": "TIP"},          # not merged
    {"merged": True, "base": "other", "head_sha": "TIP"},          # landed elsewhere
    {"merged": True, "base": "main", "head_sha": "0" * 40},        # a different head: new commits since, never replayed-as-landed
])
def test_requests_that_do_not_prove_landing(repo, request_):
    tip = _squash_landed(repo)
    request_ = dict(request_, head_sha=tip if request_["head_sha"] == "TIP" else request_["head_sha"])
    result = call(mod(), repo, landing_prs=lambda: [request_])
    assert result["result"] == "not-landed" and state_files(repo) == []


def test_a_failing_request_source_leaves_the_git_answer(repo):
    commit(repo, "feature/u", "c.txt")

    def boom():
        raise RuntimeError("offline")
    assert call(mod(), repo, landing_prs=boom)["result"] == "not-landed"


def test_unanswerable_git_is_unreadable_never_landed(repo):
    def broken(cwd, argv):
        raise RuntimeError("timeout")
    result = call(mod(), repo, run=broken)
    assert result["result"] == "unreadable" and state_files(repo) == []
    assert call(mod(), repo, run=lambda cwd, argv: "not-a-sha")["result"] == "unreadable"


# ---- break-it controls: each mutant must turn a pin above red
def test_control_contained_test_inverted_is_caught(repo):
    commit(repo, "main", "b.txt")
    mutant = support.script("feature_upkeep_pass", ('if answer == "yes":', 'if answer != "yes":'))
    assert call(mutant, repo)["result"] != "skipped"


def test_control_head_comparison_dropped_is_caught(repo):
    tip = _squash_landed(repo)
    mutant = support.script("feature_upkeep_pass", ('request.get("head_sha") == unit_tip', "True"))
    result = call(mutant, repo, landing_prs=lambda: [{"merged": True, "base": "main", "head_sha": "1" * 40}])
    assert result["result"] == "skipped"                # the mutant wrongly skips ...
    assert call(mod(), repo, landing_prs=lambda: [{"merged": True, "base": "main", "head_sha": "1" * 40}])["result"] == "not-landed"  # ... the real one does not
    assert tip


def test_control_unreadable_read_as_landed_is_caught(repo):
    mutant = support.script("feature_upkeep_pass", ("return UNREADABLE, None, None", "return LANDED_CONTAINED, None, None"))
    def broken(cwd, argv):
        raise RuntimeError("x")
    assert call(mutant, repo, run=broken)["result"] == "skipped"
    assert call(mod(), repo, run=broken)["result"] == "unreadable"


def test_an_injected_raising_runner_does_not_turn_not_landed_into_unreadable(repo):
    """The engine runner raises on any non-zero exit; ancestry "no" (exit 1) must still read as not landed."""
    commit(repo, "feature/u", "c.txt")

    def raising_on_failure(cwd, argv):
        out = subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError("git failed")
        return out.stdout
    result = call(mod(), repo, run=raising_on_failure)
    assert (result["result"], result["reason"]) == ("not-landed", "not-landed")
