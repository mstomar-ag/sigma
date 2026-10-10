"""Tests for #930 (upkeep part C, slice 1): the shared "landed" predicate.

Ancestry uses REAL git (a default-success fake would read an ancestry call as "contained"). The merged-PR half uses a
purpose-built fake that answers only the calls it was built for and refuses every other one. The drift watcher's
optional `landed` field is tested through the real `sweep`, gate closed and gate open.
"""
import ast
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, str(S / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fl = _load("feature_landed")

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=ENV)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def commit(work, name, text="x"):
    (work / name).write_text(text)
    git(work, "add", name)
    git(work, "commit", "-m", name)
    return git(work, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """work checkout on main with origin set; feature/x branched off main."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    git(tmp_path, "clone", str(remote), str(work))
    commit(work, "a.txt")
    git(work, "push", "origin", "main")
    git(work, "checkout", "-b", "feature/x")
    return work


REAL = fl.make_runner(timeout=30)


class Gh:
    """Answers `gh api repos/.../pulls?...` from canned pages; git goes to real git; anything else is refused."""

    def __init__(self, pages=None, rc=0, raw=None):
        self.pages, self.rc, self.raw, self.paths = pages or [[]], rc, raw, []

    def __call__(self, argv, cwd):
        if argv[0] == "git":
            return REAL(argv, cwd)
        if argv[:2] == ["gh", "api"] and "/pulls?" in argv[2]:
            self.paths.append(argv[2])
            if self.rc:
                return self.rc, "", "boom"
            if self.raw is not None:
                return 0, self.raw, ""
            page = int(argv[2].split("&page=")[1].split("&")[0])
            return 0, json.dumps(self.pages[page - 1] if page <= len(self.pages) else []), ""
        raise AssertionError("unmatched call: %r" % (argv,))


def pr(number, head, base="main", merged=True, created="2026-01-01T00:00:00Z"):
    return {"number": number, "state": "closed", "draft": False, "merged_at": "2026-01-02T00:00:00Z" if merged else None,
            "created_at": created, "merge_commit_sha": "m" * 40, "head_sha": head, "base_ref": base}


def verdict(repo, gh, tip="feature/x", base_ref="origin/main", slug="acme/app"):
    return fl.landed(tip, base_ref, "feature/x", slug, gh, repo, base_name="main")


# ------------------------------------------------------------------------------------------ ancestry


def test_a_tip_contained_in_the_base_is_landed_by_ancestry(repo):
    tip = commit(repo, "f.txt")
    git(repo, "push", "origin", "feature/x")
    git(repo, "checkout", "main")
    git(repo, "merge", "--ff-only", "feature/x")
    git(repo, "push", "origin", "main")
    gh = Gh()
    v = verdict(repo, gh)
    assert (v.verdict, v.via) == (fl.LANDED, fl.VIA_ANCESTRY)
    assert v.base_sha == git(repo, "rev-parse", "origin/main")
    assert gh.paths == []                          # ancestry proved it: the PR half is not even asked


def test_not_an_ancestor_and_no_merged_pr_is_not_landed(repo):
    commit(repo, "f.txt")
    v = verdict(repo, Gh())
    assert v.verdict == fl.NOT_LANDED and v.merged_head is None


def test_a_squash_landed_tip_is_found_by_its_merged_pr(repo):
    tip = commit(repo, "f.txt")
    v = verdict(repo, Gh([[pr(7, tip)]]))
    assert (v.verdict, v.via, v.pr, v.merged_head) == (fl.LANDED, fl.VIA_PR, 7, tip)


def test_an_unmerged_or_other_base_pr_does_not_land_the_tip(repo):
    tip = commit(repo, "f.txt")
    v = verdict(repo, Gh([[pr(7, tip, merged=False), pr(8, tip, base="release")]]))
    assert v.verdict == fl.NOT_LANDED


def test_a_unit_that_gained_commits_after_a_hand_landing_reports_the_merged_head(repo):
    old = commit(repo, "f.txt")
    commit(repo, "g.txt")
    v = verdict(repo, Gh([[pr(7, old)]]))
    assert v.verdict == fl.NOT_LANDED and v.merged_head == old and v.pr == 7


def test_an_ancestry_failure_is_unknown_not_not_an_ancestor(repo):
    commit(repo, "f.txt")
    real = Gh([[]])

    def run(argv, cwd):
        if "--is-ancestor" in argv:
            return 128, "", "fatal"
        return real(argv, cwd)

    v = verdict(repo, run)
    assert v.verdict == fl.UNKNOWN and "ancestry" in v.reason


def test_a_missing_base_ref_is_unknown(repo):
    commit(repo, "f.txt")
    v = verdict(repo, Gh(), base_ref="origin/nope")
    assert v.verdict == fl.UNKNOWN and "base ref" in v.reason


def test_an_unresolvable_tip_is_unknown(repo):
    v = verdict(repo, Gh(), tip="feature/missing")
    assert v.verdict == fl.UNKNOWN and "tip" in v.reason


def test_a_truncated_page_list_is_unknown_never_not_landed(repo, monkeypatch):
    commit(repo, "f.txt")
    monkeypatch.setattr(fl, "PER_PAGE", 1)
    monkeypatch.setattr(fl, "MAX_PAGES", 2)
    gh = Gh([[pr(1, "a" * 40)], [pr(2, "b" * 40)], [pr(3, "c" * 40)]])
    v = verdict(repo, gh)
    assert v.verdict == fl.UNKNOWN and "incomplete" in v.reason
    assert len(gh.paths) == 2                      # bounded: the third page was never asked


def test_a_pr_read_error_or_garbage_is_unknown(repo):
    commit(repo, "f.txt")
    assert verdict(repo, Gh(rc=1)).verdict == fl.UNKNOWN
    assert verdict(repo, Gh(raw="not json")).verdict == fl.UNKNOWN
    assert verdict(repo, Gh(raw='{"a": 1}')).verdict == fl.UNKNOWN


def test_no_slug_leaves_the_pr_half_unanswered(repo):
    commit(repo, "f.txt")
    assert verdict(repo, Gh(), slug=None).verdict == fl.UNKNOWN


def test_the_predicate_never_raises(repo):
    def run(argv, cwd):
        raise RuntimeError("boom")
    assert fl.landed("x", "y", "z", "a/b", run, repo).verdict == fl.UNKNOWN


def test_pages_are_walked_with_head_filter_and_opt_in_base(repo):
    commit(repo, "f.txt")
    gh = Gh([[pr(1, "a" * 40)] * 2, []])
    rows, truncated, error = fl.merged_pull_requests(gh, repo, "acme/app", "feature/x", base="main", per_page=2)
    assert error is None and not truncated and len(rows) == 2
    assert "head=acme:feature/x" in gh.paths[0] and "state=all" in gh.paths[0] and "base=main" in gh.paths[0]
    gh2 = Gh()
    fl.merged_pull_requests(gh2, repo, "acme/app", "feature/x")
    assert "base=" not in gh2.paths[0]


def test_rows_are_sorted_explicitly(repo):
    gh = Gh([[pr(2, "b" * 40, created="2026-03-01T00:00:00Z"), pr(1, "a" * 40, created="2026-02-01T00:00:00Z")]])
    rows, _, _ = fl.merged_pull_requests(gh, repo, "acme/app", "feature/x")
    assert [r["number"] for r in rows] == [1, 2]


# ------------------------------------------------------------------------------------------ the runner


def test_the_runner_keeps_the_exit_code_and_does_not_raise(repo):
    assert REAL(["git", "-C", str(repo), "merge-base", "--is-ancestor", "HEAD", "HEAD"], repo)[0] == 0
    rc, _, _ = REAL(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "nope^{commit}"], repo)
    assert rc == 1


def test_the_runner_stops_a_slow_command_as_124_and_a_missing_binary_as_127(tmp_path):
    run = fl.make_runner(timeout=0.5)
    assert run(["sleep", "30"], tmp_path)[0] == fl.RC_TIMEOUT
    assert run(["definitely-not-a-binary-930"], tmp_path)[0] == fl.RC_UNRUNNABLE


def test_the_runner_timeout_follows_the_watch_call_timeout_convention():
    assert fl._timeout_seconds({}) == 120
    assert fl._timeout_seconds({"SIGMA_WATCH_CALL_TIMEOUT": "40"}) == 40
    assert fl._timeout_seconds({"SIGMA_WATCH_CALL_TIMEOUT": "1"}) == 5
    assert fl._timeout_seconds({"SIGMA_WATCH_CALL_TIMEOUT": "abc"}) == 120


def test_the_runner_scrubs_the_repository_location_variables(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("GIT_DIR", str(other))
    rc, out, _ = REAL(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], repo)
    assert rc == 0 and os.path.realpath(out.strip()) == os.path.realpath(str(repo))


# ------------------------------------------------------------------------------------------ the slug


def _cfg(slug=""):
    return {"discovery": {"github": {"repo": slug}}}


def test_the_slug_from_the_remote_is_accepted_when_it_matches(repo):
    git(repo, "remote", "set-url", "origin", "git" "@github.com:Acme/App.git")
    assert fl.resolve_slug(_cfg(), REAL, repo, "origin") == ("Acme/App", "")
    assert fl.resolve_slug(_cfg("acme/app"), REAL, repo, "origin") == ("acme/app", "")


def test_a_configured_slug_that_differs_from_the_remote_is_refused_without_echoing_either(repo):
    git(repo, "remote", "set-url", "origin", "git" "@github.com:acme/app.git")
    slug, reason = fl.resolve_slug(_cfg("other/thing"), REAL, repo, "origin")
    assert slug is None and "differs" in reason and "other" not in reason and "app" not in reason


def test_a_placeholder_slug_and_an_unreadable_remote_are_refused(repo):
    git(repo, "remote", "set-url", "origin", "git" "@github.com:acme/app.git")
    assert fl.resolve_slug(_cfg("{owner}/{repo}"), REAL, repo, "origin")[0] == "acme/app"   # falls back to the remote
    assert fl.resolve_slug(_cfg("acme/app"), REAL, repo, "missing-remote")[0] is None


# ------------------------------------------------------------------------------------------ structural


def test_there_is_one_predicate_and_the_callers_share_it():
    tree = ast.parse((S / "feature_landed.py").read_text())
    public = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "landed"]
    assert public == ["landed"]
    callers = [m for m in ("drift_watch", "feature_land", "feature_upkeep_pass") if (S / (m + ".py")).exists()]
    assert "drift_watch" in callers
    for module in callers:
        source = (S / (module + ".py")).read_text()
        assert "feature_landed" in source, module + " must call the shared predicate"
        assert "--is-ancestor" not in source, module + " carries its own ancestry check"


def test_no_script_gains_a_private_ancestry_check_without_review():
    known = {"merge_observation.py", "sync.py", "worktree_prune.py", "feature_landed.py", "work.py"}
    found = {p.name for p in S.glob("*.py") if "--is-ancestor" in p.read_text()}
    assert found <= known, "a new ancestry check must route through feature_landed.landed: %s" % sorted(found - known)


# ------------------------------------------------------------------------------------------ drift watcher


def _drift_repo(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    git(tmp_path, "clone", str(remote), str(work))
    commit(work, "a.txt")
    git(work, "push", "origin", "main")
    git(work, "checkout", "-b", "feature/x")
    commit(work, "own.txt")
    git(work, "push", "origin", "feature/x")
    git(work, "checkout", "main")
    commit(work, "b.txt")
    git(work, "push", "origin", "main")
    git(work, "remote", "set-url", "origin", str(remote))
    return work


def _sweep(tmp_path, upkeep, landed_run):
    dw = _load("drift_watch")
    feature_registry = _load("feature_registry")
    work = _drift_repo(tmp_path)
    cfg = {"ledger": {"enabled": True, "actor": "w"},
           "drift_watch": {"enabled": True, "ttl_minutes": 90, "channels": {"sigma": "C1", "org": None}},
           "work": {"base": "main", "remote": "origin"}, "discovery": {"github": {"repo": "acme/app"}}}
    if upkeep is not None:
        cfg["upkeep"] = upkeep
    sdlc = work / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    (sdlc / "config.json").write_text(json.dumps(cfg))
    features_dir = feature_registry.registry_dir(str(sdlc))
    features_dir.mkdir(parents=True, exist_ok=True)
    feature_registry.write_index(features_dir, {"x": {"open": True}})

    def run(cwd, argv):
        if argv[0] == "git":
            p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, env=ENV)
            if p.returncode:
                raise RuntimeError(p.stderr)
            return p.stdout.strip()
        return "[]"

    posts = []
    result = dw.sweep(str(sdlc), config=cfg, run=run, now=1000.0, post=lambda *a: posts.append(a) or True,
                      landed_run=landed_run)
    return result, posts


def test_gate_closed_the_drift_text_is_byte_identical_and_the_predicate_is_never_run(tmp_path):
    def boom(argv, cwd):
        raise AssertionError("the predicate must not run with the gate closed")

    for i, upkeep in enumerate((None, {"enabled": False}, {"enabled": "true"})):
        (tmp_path / str(i)).mkdir()
        result, posts = _sweep(tmp_path / str(i), upkeep, boom)
        assert "posted" in result
        text = posts[0][1]
        assert "landed check" not in text and "1 commit(s) behind — landing PR none" in text


def test_gate_open_the_report_gains_a_landed_field_and_the_text_names_it(tmp_path):
    seen = []

    def runner(argv, cwd):
        seen.append(argv[0])
        if "get-url" in argv:
            return 0, "git" "@github.com:acme/app.git\n", ""
        if argv[0] == "gh":
            return 0, "[]", ""
        return REAL(argv, cwd)

    result, posts = _sweep(tmp_path, {"enabled": True}, runner)
    assert "posted" in result and "landed check: NOT_LANDED" in posts[0][1]
    assert "git" in seen and "gh" in seen
