"""#935: the test substrate for REST merges. Each piece is proven here, and the three guards that must SEE a REST
merge (`_merges`, the chat spy, the unmatched-call refusal) each have a test that goes red when the hook is removed."""
import importlib.util
import pathlib

import pytest

import rest_merge_support as rs

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


gh_api = _load("gh_api")

needs_merge_tree = pytest.mark.skipif(not rs.git_has_merge_tree(), reason="git merge-tree --write-tree needs 2.38")


@pytest.fixture
def world(tmp_path):
    w = rs.make_world(tmp_path)
    rs.open_pr(w, 7, "sdlc/x", {"a.txt": "a\n"})
    rs.advance_base(w, "b.txt", "b\n")
    return w


@needs_merge_tree
def test_a_merge_makes_a_real_two_parent_commit(world):
    run = rs.fake_run(world)
    base_before, head = rs.remote_rev(world, "refs/heads/main"), rs.remote_rev(world, "refs/heads/sdlc/x")
    reply = gh_api.merge_pr_pinned(run, rs.REPO, 7, head, merge_method="merge")
    sha = gh_api.merge_reply_sha(reply)
    assert rs.remote_parents(world, sha) == [base_before, head]
    assert gh_api.commit_parents(run, rs.REPO, sha) == [base_before, head]
    assert rs.remote_rev(world, "refs/heads/main") == sha


@needs_merge_tree
def test_a_squash_has_one_parent_and_a_stale_pin_moves_nothing(world):
    run = rs.fake_run(world)
    base_before = rs.remote_rev(world, "refs/heads/main")
    with pytest.raises(Exception):
        gh_api.merge_pr_pinned(run, rs.REPO, 7, "0" * 40, merge_method="squash")
    assert rs.remote_rev(world, "refs/heads/main") == base_before
    head = rs.remote_rev(world, "refs/heads/sdlc/x")
    sha = gh_api.merge_reply_sha(gh_api.merge_pr_pinned(run, rs.REPO, 7, head, merge_method="squash"))
    assert rs.remote_parents(world, sha) == [base_before]


def test_the_pr_object_carries_auto_merge(world):
    import json
    obj = json.loads(rs.fake_run(world)(["api", "repos/%s/pulls/7" % rs.REPO]))
    assert "auto_merge" in obj and obj["auto_merge"] is None
    rs.open_pr(world, 8, "sdlc/y", {"c.txt": "c\n"}, auto_merge={"merge_method": "squash"})
    assert json.loads(rs.fake_run(world)(["api", "repos/%s/pulls/8" % rs.REPO]))["auto_merge"]["merge_method"] == "squash"


def test_the_fake_refuses_and_records_an_unmatched_call(world):
    proc = rs.fake_proc(world, ["api", "repos/%s/issues/9" % rs.REPO])
    assert proc.returncode != 0
    assert rs.unmatched_calls(world) == [["api", "repos/%s/issues/9" % rs.REPO]]
    assert rs.fake_proc(world, ["pr", "list"]).returncode != 0


def test_the_strict_runner_records_and_raises_on_an_unmatched_call():
    run = rs.strict_runner([("pulls/7", "{}")])
    assert run("/", ["api", "repos/a/b/pulls/7"]) == "{}"
    with pytest.raises(rs.UnmatchedCall):
        run("/", ["api", "repos/a/b/issues/1"])
    assert run.unmatched == ["api repos/a/b/issues/1"]


def test_the_older_runner_would_have_answered_nothing():
    """The contrast this module exists for: an unmatched call must not look like a quiet success."""
    run = rs.strict_runner([])
    with pytest.raises(rs.UnmatchedCall):
        run("/", ["api", "x"])


@needs_merge_tree
def test_merges_sees_a_rest_put_and_ignores_a_get(world):
    head = rs.remote_rev(world, "refs/heads/sdlc/x")
    seen = []
    inner = rs.fake_run(world)
    gh_api.merge_pr_pinned(lambda a: (seen.append("gh " + " ".join(a)), inner(a))[1], rs.REPO, 7, head,
                           merge_method="merge")
    gh_api.merge_pr(lambda a: (seen.append("gh " + " ".join(a)), "{}")[1], 8, repo=rs.REPO)
    assert len(rs.rest_merges(seen)) == 2
    assert rs.rest_merges(["gh api repos/a/b/pulls/7/merge --method GET", "gh api repos/a/b/pulls/7"]) == []
    assert rs.rest_merges(["gh pr merge 7 --squash", "git merge x"]) == ["gh pr merge 7 --squash", "git merge x"]


def test_the_unit_completion_matcher_sees_a_rest_put():
    import test_unit_completion as tuc
    assert tuc._merges(["gh api repos/a/b/pulls/7/merge --method PUT -f merge_method=squash"]) != []
    assert tuc._merges(["gh api repos/a/b/pulls/7/merge --method GET"]) == []


def test_the_chat_spy_sees_a_rest_merge(monkeypatch):
    import types
    vm = types.SimpleNamespace(**{n: None for n in
                                  ("ensure_landing_pr", "merge_pr", "verify_and_offer_merge", "_interactive_decide")})
    calls = rs.spy_landing(monkeypatch, vm, gh_api)
    gh_api.merge_pr_pinned(None, rs.REPO, 7, "a" * 40, merge_method="merge")
    assert "gh_api.merge_pr_pinned" in calls
    del calls[:]
    # an entry nobody named: the real `merge_pr` body is not patched on a fresh module instance
    fresh = _load("gh_api")
    with pytest.raises(Exception):
        fresh.merge_pr(None, 7, repo=rs.REPO)
    assert rs.rest_merges(calls) != []
