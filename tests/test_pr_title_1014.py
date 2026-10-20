"""#1014: the PR `work.py pr` opens is titled after the goal (the branch's first real commit), never after a later fix-up.
Controls (each clause broken once, seen red): last commit instead of first; wip/merge/fixup subjects not skipped; the
goal reference not added; an existing PR retitled. Gesture: `python -m pytest tests/test_pr_title_1014.py -q`."""
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_test_first_gate import case, load, pr_case  # noqa: F401,E402  (case is a fixture)

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def git(root, *args):
    return subprocess.run(GIT + list(args), cwd=root, check=True, capture_output=True, text=True).stdout


def fixture_branch(tmp_path, subjects):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("base\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    for i, subject in enumerate(subjects):
        (tmp_path / "f.txt").write_text("change %d\n" % i)
        git(tmp_path, "commit", "-q", "-am", subject)
    return tmp_path


def title(tmp_path, subjects, goal="1014"):
    w = load("work")
    root = fixture_branch(tmp_path, subjects)
    return w._pr_title(str(root), w._run, "origin", "main", goal)


def test_a_feature_commit_then_a_fixup_titles_the_pr_after_the_feature(tmp_path):
    got = title(tmp_path, ["feat(claim): the claim ref primitive (#986)", "test: register the header as a non-marker"], goal="986")
    assert got == "feat(claim): the claim ref primitive (#986)"


def test_work_in_progress_merge_and_fixup_subjects_are_never_the_title(tmp_path):
    got = title(tmp_path, ["wip: first pass", "Merge branch 'x'", "fixup! thing", "feat(a): real goal", "chore: tidy"], goal="77")
    assert got == "feat(a): real goal (#77)"


def test_the_goal_reference_is_added_once_and_only_for_a_numbered_goal(tmp_path):
    (tmp_path / "a").mkdir()
    assert title(tmp_path / "a", ["feat: x"], goal="5") == "feat: x (#5)"
    (tmp_path / "b").mkdir()
    assert title(tmp_path / "b", ["feat: x (#5)"], goal="5") == "feat: x (#5)"
    (tmp_path / "c").mkdir()
    assert title(tmp_path / "c", ["feat: x"], goal="goals/foo.md") == "feat: x"


def test_a_branch_with_only_wip_commits_falls_back_to_its_first_subject(tmp_path):
    assert title(tmp_path, ["wip: only"], goal="9") == "wip: only (#9)"


def test_pr_creates_the_pull_request_with_the_goal_title(case, monkeypatch):
    w, config, run, calls = pr_case(case, monkeypatch)

    def seam(cwd, argv):
        if argv[:3] == ["git", "log", "--reverse"] and "--format=%s" in argv:
            return "feat: the goal\ntest: fix-up\n"
        if argv[:2] == ["git", "log"]:
            return "test: fix-up"
        return run(cwd, argv)
    case[2].write_text("# Plan\n\n## Tests\nnone: docs\n")
    assert w.pr(case[1], config, "42", run=seam, no_tests="Docs") == "PR #7"
    sent = [x for a in calls for x in a if x.startswith("title=")]
    assert sent == ["title=feat: the goal (#42)"], sent


def test_a_later_push_never_retitles_an_existing_pull_request(case, monkeypatch):
    w, config, run, calls = pr_case(case, monkeypatch)
    patched = []

    def existing(cwd, argv):
        if argv[:2] == ["gh", "api"] and "pulls?head=" in argv[2]:
            return "7"
        if argv[:3] == ["gh", "api", "repos/{owner}/{repo}/pulls/7"] and "--method" not in argv:
            return '{"body": "Existing body\\n"}'
        if "--method" in argv and any(x.startswith("title=") for x in argv):
            patched.append(argv)
        return run(cwd, argv)
    assert w.pr(case[1], config, "42", run=existing, no_tests="Docs") == "PR #7"
    assert patched == [] and not [x for a in calls for x in a if x.startswith("title=")]
