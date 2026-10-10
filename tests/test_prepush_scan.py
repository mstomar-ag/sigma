"""tools/prepush_scan.py: the pre-push private-term guard (goal 1088).

Every test builds local temp git repositories (a bare "remote" and a clone) and runs the documented
gesture: the two-line hook from docs/prepush-guard.md, fired by a real `git push`. The deny
pattern here is a neutral made-up token, never a real private term.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "prepush_scan.py"
TERM = "zzdeny-token"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
for _k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
    ENV.pop(_k, None)


def git(cwd, *a, check=True):
    r = subprocess.run(["git", *a], cwd=cwd, env=ENV, capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError("git %s: %s" % (a, r.stderr))
    return r


class Repo:
    def __init__(self, tmp, patterns_text="# neutral example\n%s\n" % TERM, install=True):
        self.tmp = pathlib.Path(tmp)
        self.patterns = self.tmp / "patterns.txt"
        if patterns_text is not None:
            self.patterns.write_text(patterns_text)
        self.remote = self.tmp / "remote.git"
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(self.tmp, "clone", "-q", str(self.remote), str(self.a))
        git(self.a, "checkout", "-q", "-b", "main")
        self.commit(self.a, "base.txt", "base\n", "base")
        git(self.a, "push", "-q", "origin", "main")
        git(self.tmp, "clone", "-q", str(self.remote), str(self.b))
        if install:
            hook = self.b / ".git" / "hooks" / "pre-push"
            hook.write_text('#!/bin/sh\nexec %s %s --patterns %s "$@"\n'
                            % (sys.executable, TOOL, self.patterns))
            hook.chmod(0o755)

    def commit(self, repo, name, text, msg):
        (repo / name).write_text(text)
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", msg)

    def push(self, *a):
        return git(self.b, "push", *a, check=False)


class PrePushScan(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.r = Repo(self._t.name)

    def test_clean_push_passes(self):
        self.r.commit(self.r.b, "f.txt", "fine\n", "clean change")
        p = self.r.push("origin", "main")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_planted_added_line_refused(self):
        self.r.commit(self.r.b, "f.txt", "x %s y\n" % TERM, "innocent")
        p = self.r.push("origin", "main")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("REFUSED", p.stderr)
        self.assertIn("added line", p.stderr)
        self.assertIn("pattern 2", p.stderr)  # the file's line number (comment is line 1)
        self.assertNotIn(TERM, p.stderr)       # the matched text is never echoed

    def test_planted_message_refused(self):
        self.r.commit(self.r.b, "f.txt", "fine\n", "mentions %s here" % TERM)
        p = self.r.push("origin", "main")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("commit message", p.stderr)

    def test_merged_upstream_commit_not_scanned_but_own_commit_is(self):
        # the other clone lands a commit carrying the term on the remote default branch
        self.r.commit(self.r.a, "up.txt", "u\n", "upstream %s note" % TERM)
        git(self.r.a, "push", "-q", "origin", "main")
        git(self.r.b, "checkout", "-q", "-b", "topic")
        self.r.commit(self.r.b, "own.txt", "own\n", "own clean change")
        git(self.r.b, "fetch", "-q", "origin")
        git(self.r.b, "merge", "-q", "--no-ff", "-m", "merge main", "origin/main")
        p = self.r.push("origin", "topic")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("skipped", p.stderr)
        # a planted term in a new commit on top of that merge is still refused
        self.r.commit(self.r.b, "own2.txt", "%s\n" % TERM, "more")
        p = self.r.push("origin", "topic")
        self.assertNotEqual(p.returncode, 0)

    def test_new_branch_scans_only_unpushed_commits(self):
        git(self.r.b, "checkout", "-q", "-b", "fresh")
        self.r.commit(self.r.b, "n.txt", "ok\n", "ok")
        p = self.r.push("origin", "fresh")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("scanned 1 commit", p.stderr)

    def test_delete_push_skipped(self):
        git(self.r.b, "push", "-q", "origin", "main:gone")
        p = self.r.push("origin", "--delete", "gone")
        self.assertEqual(p.returncode, 0, p.stderr)

    def run_tool(self, args, stdin=""):
        return subprocess.run([sys.executable, str(TOOL), *args], input=stdin, cwd=self.r.b, env=ENV,
                              capture_output=True, text=True)

    def test_missing_patterns_exits_2(self):
        p = self.run_tool(["--patterns", str(self.r.tmp / "nope.txt")])
        self.assertEqual(p.returncode, 2)
        p = self.run_tool([])
        self.assertEqual(p.returncode, 2)

    def test_empty_patterns_exits_2(self):
        (self.r.tmp / "e.txt").write_text("# only a comment\n\n")
        self.assertEqual(self.run_tool(["--patterns", str(self.r.tmp / "e.txt")]).returncode, 2)

    def test_bad_regex_refused_by_line_without_echo(self):
        (self.r.tmp / "bad.txt").write_text("ok\nsecret-(unclosed\n")
        p = self.run_tool(["--patterns", str(self.r.tmp / "bad.txt")])
        self.assertEqual(p.returncode, 2)
        self.assertIn("line 2", p.stderr)
        self.assertNotIn("unclosed", p.stderr)

    def ref_line(self, local, remote):
        return "refs/heads/main %s refs/heads/main %s\n" % (local, remote)

    def head(self, repo=None):
        return git(repo or self.r.b, "rev-parse", "HEAD").stdout.strip()

    def test_unknown_remote_sha_refuses_not_fails_open(self):
        self.r.commit(self.r.b, "f.txt", "x %s y\n" % TERM, "innocent")
        p = self.run_tool(["--patterns", str(self.r.patterns)], self.ref_line(self.head(), "ab" * 20))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("REFUSED", p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1)
        self.assertNotIn(TERM, p.stderr)
        self.assertNotIn("scanned 0", p.stderr)

    def test_git_error_on_log_refuses(self):
        import shutil
        fake = self.r.tmp / "fakebin"
        fake.mkdir()
        g = fake / "git"
        g.write_text('#!/bin/sh\nif [ "$1" = log ]; then exit 128; fi\nexec %s "$@"\n' % shutil.which("git"))
        g.chmod(0o755)
        self.r.commit(self.r.b, "f.txt", "x %s y\n" % TERM, "innocent")
        env = {**ENV, "PATH": "%s:%s" % (fake, ENV["PATH"])}
        base = git(self.r.b, "rev-parse", "origin/main").stdout.strip()
        p = subprocess.run([sys.executable, str(TOOL), "--patterns", str(self.r.patterns)],
                           input=self.ref_line(self.head(), base), cwd=self.r.b, env=env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(len(p.stderr.strip().splitlines()), 1)

    def test_env_patterns_path(self):
        self.r.commit(self.r.b, "f.txt", "x %s y\n" % TERM, "innocent")
        base = git(self.r.b, "rev-parse", "origin/main").stdout.strip()
        env = {**ENV, "SIGMA_PREPUSH_PATTERNS": str(self.r.patterns)}
        p = subprocess.run([sys.executable, str(TOOL)], input=self.ref_line(self.head(), base),
                           cwd=self.r.b, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("added line", p.stderr)

    def test_base_path(self):
        git(self.r.b, "checkout", "-q", "-b", "topic")
        self.r.commit(self.r.b, "f.txt", "x %s y\n" % TERM, "innocent")
        mid = self.head()
        self.r.commit(self.r.b, "g.txt", "fine\n", "clean")
        args = ["--patterns", str(self.r.patterns), "--base"]
        line = self.ref_line(self.head(), "0" * 40)
        # base below the planted commit: it is scanned
        self.assertEqual(self.run_tool(args + ["origin/main"], line).returncode, 1)
        # base at the planted commit: it is excluded, only the clean one scans
        p = self.run_tool(args + [mid], line)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("scanned 1 commit", p.stderr)

    def test_merge_conflict_resolution_only_line_caught(self):
        git(self.r.b, "checkout", "-q", "-b", "topic")
        self.r.commit(self.r.b, "base.txt", "topic side\n", "topic edit")
        self.r.commit(self.r.a, "base.txt", "main side\n", "main edit")
        git(self.r.a, "push", "-q", "origin", "main")
        git(self.r.b, "fetch", "-q", "origin")
        m = git(self.r.b, "merge", "--no-ff", "-m", "merge main", "origin/main", check=False)
        self.assertNotEqual(m.returncode, 0)  # conflict, as intended
        (self.r.b / "base.txt").write_text("resolved %s\n" % TERM)
        git(self.r.b, "add", "base.txt")
        git(self.r.b, "commit", "-q", "-m", "merge main")
        p = self.r.push("origin", "topic")
        self.assertNotEqual(p.returncode, 0, p.stderr)
        self.assertIn("added line", p.stderr)

    def test_output_names_the_exclusion(self):
        self.r.commit(self.r.b, "f.txt", "fine\n", "clean change")
        p = self.r.push("origin", "main")
        self.assertIn("commits already on a remote-tracking ref", p.stderr)

    def test_help(self):
        p = self.run_tool(["--help"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("usage", p.stdout.lower())


if __name__ == "__main__":
    unittest.main()
