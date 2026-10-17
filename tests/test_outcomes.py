"""outcomes.py report (#1636, PRD-0008 R-04): reverts and quick fixes per merged goal.

Fixtures are REAL: a `git init` repo with squash-style subjects and pinned committer dates, and
action-log ledgers written by the real writer (actionlog.append), so the reader is validated
against production-shaped inputs. Time is injected (`NOW`), never read from the clock."""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG_S = ROOT / "skills" / "sigma-log" / "scripts"
LOOP_S = ROOT / "skills" / "sigma-loop" / "scripts"
OUTCOMES = LOG_S / "outcomes.py"
DAY = 86400
NOW = 1792195200  # 2026-10-17T00:00:00Z


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


actionlog = _load(LOOP_S / "actionlog.py", "actionlog_for_outcomes_tests")


def _run(*args, cwd, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(OUTCOMES), *args], cwd=str(cwd),
                          capture_output=True, text=True, env=env, timeout=60)


# --------------------------------------------------------------------- Task 1: help and refusals


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_script_help(tmp_path, flag):
    """-h works before any git or config read: run in a directory that is not a repo."""
    r = _run(flag, cwd=tmp_path)
    assert r.returncode == 0
    assert "usage" in r.stdout.lower()


@pytest.mark.parametrize("window", ["91", "0", "-3", "abc", "1.5", "", "1_5", " 5", "+5", "\u0663\u0660"])
def test_bad_window_is_a_typed_refusal(tmp_path, window):
    r = _run("report", "--window", window, cwd=tmp_path)
    assert r.returncode == 2
    assert r.stdout == ""
    assert r.stderr.startswith("outcomes.py: REFUSED [")
    assert "]:" in r.stderr


def test_unknown_verb_and_missing_verb_are_refused(tmp_path):
    for argv in (["frobnicate"], []):
        r = _run(*argv, cwd=tmp_path)
        assert r.returncode == 2 and r.stdout == ""
        assert "REFUSED [" in r.stderr


def test_unknown_flag_and_missing_flag_value_are_refused(tmp_path):
    for argv in (["report", "--nope"], ["report", "--window"]):
        r = _run(*argv, cwd=tmp_path)
        assert r.returncode == 2 and r.stdout == ""
        assert "REFUSED [" in r.stderr


# --------------------------------------------------------------------- fixtures: real git, real log

outcomes = _load(OUTCOMES, "outcomes_under_test")
PROXY = outcomes.PROXY
ON = {"action_log": {"enabled": True}}


class World:
    """A repo with a `.sdlc` inside it (never committed) and a real action log."""

    def __init__(self, tmp_path, config=ON, name="repo"):
        self.repo = tmp_path / name
        self.repo.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        # No background git while a test snapshots the tree: a commit otherwise starts auto
        # maintenance whose objects/maintenance.lock appears mid-snapshot and reads as a write.
        self.git("config", "maintenance.auto", "false")
        self.git("config", "gc.auto", "0")
        self.sdlc = self.repo / ".sdlc"
        (self.sdlc / "state").mkdir(parents=True)
        if config is not None:
            (self.sdlc / "config.json").write_text(json.dumps(config))
        self.n = 0

    def git(self, *args, env=None):
        e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        e.update(env or {})
        r = subprocess.run(["git", *args], cwd=str(self.repo), capture_output=True, text=True, env=e)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def commit(self, subject, files=("f.txt",), ago=0, body="", delete=(), renames=(), at=None):
        for path in files:
            p = self.repo / path
            p.parent.mkdir(parents=True, exist_ok=True)
            self.n += 1
            p.write_text(f"{self.n}\n")
            self.git("add", "--", path)
        for path in delete:
            self.git("rm", "-q", "--", path)
        for old, new in renames:
            (self.repo / new).parent.mkdir(parents=True, exist_ok=True)
            self.git("mv", old, new)
        stamp = f"{int(NOW - ago * DAY) if at is None else int(at)} +0000"
        msg = [subject] + ([body] if body else [])
        args = ["commit", "-q", "--allow-empty"]
        for m in msg:
            args += ["-m", m]
        self.git(*args, env={"GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp})
        return self.git("rev-parse", "HEAD")

    def log(self, goal, kind, ago, **fields):
        return actionlog.append(str(self.sdlc), str(goal), kind, "loop", now=NOW - ago * DAY, **fields)

    def log_at(self, goal, kind, at, **fields):
        return actionlog.append(str(self.sdlc), str(goal), kind, "loop", now=at, **fields)

    def goal(self, goal, pr, files, ago, kind="merged", subject=None, logged=True):
        sha = self.commit(subject or f"feat: goal {goal} ({'#' + str(pr)})", files, ago)
        if logged:
            self.log(goal, kind, ago, pr=str(pr))
        return sha

    def mature_goals(self, count, first=100, start_ago=29.0, step=0.5):
        """`count` mature goals (merged 15+ days ago), oldest first, each on its own file."""
        for i in range(count):
            self.goal(first + i, first + i, [f"g{first + i}.txt"], start_ago - i * step)

    def run(self, capsys, *args, now=NOW):
        rc = outcomes.main(["outcomes.py", "report", "--sdlc", str(self.sdlc), *args], now=now)
        out = capsys.readouterr()
        return rc, out.out.splitlines(), out.err


def assert_proxy_first_and_no_rate(lines):
    import re
    assert lines[0] == PROXY
    for line in lines[1:]:
        assert not re.search(r"\brate\b|\d\s*%", line), line


# --------------------------------------------------------------------- Task 2: unmeasured gates


def test_log_off_is_unmeasured(tmp_path, capsys):
    w = World(tmp_path, config={"action_log": {"enabled": False}})
    w.commit("feat: a (#1)")
    rc, lines, _ = w.run(capsys)
    assert rc == 0
    assert_proxy_first_and_no_rate(lines)
    assert lines[1].startswith("unmeasured: action log off")
    assert len(lines) == 2


def test_no_log_directory_is_unmeasured(tmp_path, capsys):
    w = World(tmp_path)
    w.commit("feat: a (#1)")
    rc, lines, _ = w.run(capsys)
    assert rc == 0
    assert_proxy_first_and_no_rate(lines)
    assert lines[1].startswith("unmeasured: no action log")


def test_unreadable_config_is_unknown_not_off(tmp_path, capsys):
    """A goal worktree has no config.json: 'could not look' is not 'looked and it is off' (#1778)."""
    w = World(tmp_path, config=None)
    w.commit("feat: a (#1)")
    rc, lines, _ = w.run(capsys)
    assert rc == 0
    assert lines[1].startswith("unmeasured: action log state unknown")
    assert "off" not in lines[1].split("unknown", 1)[0]


def test_shallow_clone_is_unmeasured(tmp_path, capsys):
    w = World(tmp_path)
    sha = w.commit("feat: a (#1)")
    w.log(1, "merged", 20, pr="1")
    (w.repo / ".git" / "shallow").write_text(sha + "\n")
    rc, lines, _ = w.run(capsys)
    assert rc == 0
    assert_proxy_first_and_no_rate(lines)
    assert lines[1].startswith("unmeasured: shallow clone")


def test_missing_base_ref_is_unmeasured(tmp_path, capsys):
    w = World(tmp_path)
    w.commit("feat: a (#1)")
    w.log(1, "merged", 20, pr="1")
    rc, lines, _ = w.run(capsys, "--base", "no-such-ref")
    assert rc == 0
    assert lines[1].startswith("unmeasured: base ref")
    assert "no-such-ref" in lines[1]


@pytest.mark.parametrize("window", ["1", "14"])
def test_window_of_fourteen_days_or_less_can_never_be_mature(tmp_path, capsys, window):
    w = World(tmp_path)
    w.commit("feat: a (#1)")
    w.log(1, "merged", 20, pr="1")
    rc, lines, _ = w.run(capsys, "--window", window)
    assert rc == 0
    assert_proxy_first_and_no_rate(lines)
    assert lines[1].startswith("unmeasured:")
    assert "14" in lines[1] and "mature" in lines[1]


# --------------------------------------------------------------------- refinement 4: retention cap


def test_window_over_default_retention_is_refused_typed(tmp_path, capsys):
    w = World(tmp_path)
    rc, lines, err = w.run(capsys, "--window", "91")
    assert rc == 2 and lines == []
    assert err.startswith("outcomes.py: REFUSED [window-exceeds-retention]:")
    assert "90" in err


def test_window_is_capped_at_the_configured_retention(tmp_path, capsys):
    w = World(tmp_path, config={"action_log": {"enabled": True, "retention_days": 45}})
    w.commit("feat: a (#1)")
    rc, lines, err = w.run(capsys, "--window", "46")
    assert rc == 2 and lines == []
    assert "window-exceeds-retention" in err and "45" in err
    rc, lines, _ = w.run(capsys, "--window", "45")
    assert rc == 0


def test_retention_below_the_floor_reads_as_the_floor(tmp_path, capsys):
    """retention.py floors retention_days at 30, so a configured 7 still allows a 30-day window."""
    w = World(tmp_path, config={"action_log": {"enabled": True, "retention_days": 7}})
    w.commit("feat: a (#1)")
    rc, lines, err = w.run(capsys, "--window", "30")
    assert rc == 0, err
    rc, lines, err = w.run(capsys, "--window", "31")
    assert rc == 2 and "30" in err


def test_disabled_retention_is_said_and_allows_a_longer_window(tmp_path, capsys):
    w = World(tmp_path, config={"action_log": {"enabled": True, "retention_days": False}})
    (w.sdlc / "state" / "log").mkdir()
    w.commit("feat: a (#1)")
    rc, lines, err = w.run(capsys, "--window", "120")
    assert rc == 0, err
    assert "retention=disabled" in "\n".join(lines)
    rc, lines, err = w.run(capsys, "--window", str(outcomes.HARD_MAX_WINDOW + 1))
    assert rc == 2 and "window-exceeds-retention" in err


# --------------------------------------------------------------------- Task 3: merge records to goals


def header(lines):
    """`window=30d mature=10 ...` -> dict, from line 2 of a measured report."""
    assert lines[1].startswith("window="), lines[:3]
    return dict(kv.split("=", 1) for kv in lines[1].split())


def counts(lines):
    """mature / immature / unresolved from either shape of line 2."""
    import re
    if lines[1].startswith("window="):
        h = header(lines)
        return int(h["mature"]), int(h["immature"]), int(h["unresolved"])
    m = re.search(r"(\d+) mature goals.*immature=(\d+) unresolved=(\d+)", lines[1])
    assert m, lines[1]
    return tuple(int(x) for x in m.groups())


def test_only_merge_records_that_resolve_on_base_make_a_goal(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(10)
    # none of these may become a goal: no merged/merge_armed record that resolves
    w.commit("feat: only recorded done (#300)", ["r.txt"], 20)
    w.log(300, "recorded", 20, result="done")
    w.commit("feat: review only (#301)", ["s.txt"], 20)
    w.log(301, "recorded", 20, result="review", detail="pr 301")
    w.log(302, "claimed", 20)
    w.log(303, "merge_armed", 20, pr="9999")        # armed, never landed on base: pending, invisible
    rc, lines, _ = w.run(capsys)
    assert rc == 0
    assert counts(lines) == (10, 0, 0)


def test_merge_armed_that_resolves_on_base_counts_without_recorded_done(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(9)
    w.goal(400, 400, ["armed.txt"], 20, kind="merge_armed")
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (10, 0, 0)
    assert lines[1].startswith("window=")


def test_a_merged_record_that_does_not_resolve_is_unresolved_not_dropped(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(10)
    w.log(500, "merged", 20, pr="7777")             # no such PR on base
    w.log(501, "merged", 20)                        # no pr at all
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (10, 0, 2)


def test_armed_pr_wins_when_the_merged_pr_does_not_resolve(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(9)
    w.commit("feat: real (#600)", ["x.txt"], 20)
    w.log(600, "merged", 20, pr="8888")
    w.log(600, "merge_armed", 19, pr="600")
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (10, 0, 0)


def test_merge_time_is_the_commit_time_not_the_log_time(tmp_path, capsys):
    """A reconcile pass can write the record days late; the committer time is the truth."""
    w = World(tmp_path)
    w.mature_goals(9)
    w.commit("feat: late record (#700)", ["l.txt"], 20)      # merged 20 days ago ...
    w.log(700, "merged", 1, pr="700")                        # ... but logged yesterday
    w.commit("feat: early record (#701)", ["e.txt"], 3)      # merged 3 days ago ...
    w.log(701, "merged", 25, pr="701")                       # ... logged as 25 days ago
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (10, 1, 0)


def test_a_goal_merged_before_the_window_is_excluded_not_unresolved(tmp_path, capsys):
    w = World(tmp_path)
    w.commit("feat: ancient (#800)", ["old.txt"], 40)
    w.log(800, "merged", 40, pr="800")
    w.log(801, "merged", 80, pr="123")              # logged long ago, commit older than the git pass
    w.mature_goals(10)
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (10, 0, 0)


def test_malformed_log_lines_are_skipped(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(10)
    d = w.sdlc / "state" / "log"
    with open(d / "100.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json\n\n[1,2]\n")
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (10, 0, 0)


# --------------------------------------------------------------------- floor (AC-3)


def test_nine_mature_goals_are_unmeasured(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(9)
    rc, lines, _ = w.run(capsys)
    assert rc == 0 and len(lines) == 2
    assert_proxy_first_and_no_rate(lines)
    assert lines[1].startswith("unmeasured:") and "9 mature" in lines[1]


def test_the_floor_counts_mature_goals_only(tmp_path, capsys):
    """12 goals merged, 9 of them mature: immature goals must not lift the count to measured."""
    w = World(tmp_path)
    w.mature_goals(9)
    for i, ago in enumerate((3, 2, 1)):
        w.goal(900 + i, 900 + i, [f"i{i}.txt"], ago)
    rc, lines, _ = w.run(capsys)
    assert lines[1].startswith("unmeasured:")
    assert counts(lines) == (9, 3, 0)


def test_exactly_ten_mature_goals_is_measured(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(10)
    rc, lines, _ = w.run(capsys)
    assert rc == 0
    assert lines[0] == PROXY
    assert lines[1].startswith("window=30d mature=10 ")


# --------------------------------------------------------------------- Task 4: one git pass


def _pass(w, since_days=30):
    return outcomes._git_log(w.repo, "main", NOW - since_days * DAY)


def test_git_log_parses_subject_body_time_parents_and_files(tmp_path):
    w = World(tmp_path)
    first = w.commit("feat: x (#12)", ["a.txt"], 5, body="line one\r\nline two")
    second = w.commit("fix(a): y (#11) (#12)", ["b.txt", "c.txt"], 4)
    commits = _pass(w)
    assert [c["sha"] for c in commits] == [second, first]       # newest first = base order
    assert commits[0]["subject"] == "fix(a): y (#11) (#12)"
    assert commits[0]["ts"] == NOW - 4 * DAY
    assert commits[0]["files"] == {"b.txt", "c.txt"}
    assert "line two" in commits[1]["body"]


def test_squash_subject_resolves_by_the_last_marker(tmp_path):
    assert outcomes._merge_pr("fix(a): y (#11) (#12)") == "12"
    assert outcomes._merge_pr("feat: x (#12)") == "12"
    assert outcomes._merge_pr('Revert "feat: x (#12)"') is None       # the quoted marker is not a merge
    assert outcomes._merge_pr("Merge pull request #12 from a/b") is None
    assert outcomes._merge_pr("feat: no marker") is None


def test_rename_counts_both_paths_and_odd_paths_parse(tmp_path):
    w = World(tmp_path)
    w.commit("feat: seed (#1)", ["dir/a b.txt", "dir/über.txt"], 6)
    w.commit("refactor: move (#2)", [], 5, renames=[("dir/a b.txt", "dir/moved name.txt")])
    commits = _pass(w)
    assert commits[0]["files"] == {"dir/a b.txt", "dir/moved name.txt"}
    assert commits[1]["files"] == {"dir/a b.txt", "dir/über.txt"}


def test_git_pass_is_first_parent_only(tmp_path):
    """Branch commits that reached base through a merge commit are not base history."""
    w = World(tmp_path)
    w.commit("feat: seed (#1)", ["a.txt"], 9)
    w.git("checkout", "-q", "-b", "side")
    w.commit("fix: on the side", ["a.txt"], 8)
    w.git("checkout", "-q", "main")
    w.commit("feat: other (#2)", ["o.txt"], 7)
    w.git("merge", "--no-ff", "-q", "-m", "Merge pull request #3 from x/side", "side",
          env={"GIT_COMMITTER_DATE": f"{NOW - 6 * DAY} +0000"})
    subjects = [c["subject"] for c in _pass(w)]
    assert "fix: on the side" not in subjects
    assert "Merge pull request #3 from x/side" in subjects


# --------------------------------------------------------------------- Task 5: classification

SHA = "a" * 40


@pytest.mark.parametrize("subject,body,kind", [
    ("fix: a", "", "fix"),
    ("Fix(x): a", "", "fix"),
    ("fix(x)!: a", "", "fix"),
    ("FIX a", "", "fix"),
    ("fix", "", "fix"),
    ("prefix: a", "", None),
    ("fixture: a", "", None),
    ("feat: fix the thing", "", None),
    ("fixup! a", "", None),
    ('Revert "feat: a"', "", "revert"),
    ("chore: undo", f"This reverts commit {SHA}.", "revert"),
    ('Revert "Revert "feat: a""', "", None),                    # a re-land is not a defect
    ('Revert "Revert "feat: a""', f"This reverts commit {SHA}.", None),
])
def test_kind_table(subject, body, kind):
    assert outcomes._kind(subject, body) == kind


# --------------------------------------------------------------------- Task 6: attribution and totals


def attr_world(tmp_path, pad=8):
    """10 filler goals, then goal 200 (a.py, merged 22d ago) and goal 201 (b.py, 21d ago), then `pad`
    unrelated commits so no file's share of the pass is accidentally 'hot'."""
    w = World(tmp_path)
    w.mature_goals(10)
    w.goal(200, 200, ["a.py"], 22)
    w.goal(201, 201, ["b.py"], 21)
    for i in range(pad):
        w.commit(f"chore: pad {i}", [f"pad{i}.txt"], 20.5 - i * 0.01)
    return w


def measure(w, capsys, *args):
    rc, lines, err = w.run(capsys, *args)
    assert rc == 0, err
    assert lines[1].startswith("window="), lines
    return lines


def row(lines, goal):
    for line in lines[2:]:
        f = line.split("\t")
        if f[0] == str(goal) and len(f) == 5:
            return int(f[3]), int(f[4])
    raise AssertionError(f"no row for {goal}: {lines}")


def totals(lines):
    import re
    m = re.fullmatch(r"totals reverts=(\d+) fixes=(\d+) \(distinct commits\) multi-attributed=(\d+)", lines[-1])
    assert m, lines[-1]
    return tuple(int(x) for x in m.groups())


def test_ac1_revert_within_fourteen_days_counts_and_after_does_not(tmp_path, capsys):
    w = attr_world(tmp_path)
    w.commit('Revert "feat: goal 200 (#200)"', ["a.py"], 19)            # 3 days after merge
    lines = measure(w, capsys)
    assert row(lines, 200) == (1, 0)
    assert totals(lines) == (1, 0, 0)
    (tmp_path / "late").mkdir()
    late = attr_world(tmp_path / "late")
    late.commit('Revert "feat: goal 200 (#200)"', ["a.py"], 5)          # 17 days after merge
    assert row(measure(late, capsys), 200) == (0, 0)


def test_ac2_fix_touching_a_goal_file_counts_unrelated_fix_does_not(tmp_path, capsys):
    w = attr_world(tmp_path)
    w.commit("fix: a bug in a (#990)", ["a.py"], 15)
    w.commit("fix: something else entirely", ["unrelated.py"], 14.5)
    lines = measure(w, capsys)
    assert row(lines, 200) == (0, 1)
    assert row(lines, 201) == (0, 0)
    assert totals(lines) == (0, 1, 0)


def test_a_fix_after_fourteen_days_does_not_count(tmp_path, capsys):
    w = attr_world(tmp_path)
    w.commit("fix: too late", ["a.py"], 5)
    assert row(measure(w, capsys), 200) == (0, 0)


def test_a_commit_on_the_goals_own_branch_never_counts(tmp_path, capsys):
    """Squash puts no branch commit on base; a merge-commit path puts it on base's second parent,
    which the first-parent walk never visits."""
    w = attr_world(tmp_path)
    w.git("checkout", "-q", "-b", "goal-200")
    w.commit("fix: on goal 200's own branch", ["a.py"], 19)
    w.git("checkout", "-q", "main")
    w.git("merge", "--no-ff", "-q", "-m", "Merge pull request #5 from x/goal-200", "goal-200",
          env={"GIT_COMMITTER_DATE": f"{NOW - 18 * DAY} +0000"})
    w.git("checkout", "-q", "-b", "never-merged")
    w.commit("fix: on an unmerged branch", ["a.py"], 17)
    w.git("checkout", "-q", "main")
    assert row(measure(w, capsys), 200) == (0, 0)


def test_a_fix_made_by_a_later_goal_is_skipped_but_its_revert_counts(tmp_path, capsys):
    w = attr_world(tmp_path)
    w.goal(210, 210, ["a.py"], 18, subject="fix: z (#210)")
    w.goal(212, 212, ["a.py"], 16, subject='Revert "feat: goal 200 (#200)" (#212)')
    lines = measure(w, capsys)
    assert row(lines, 200) == (1, 0)


def test_denylisted_files_attribute_nothing(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(10)
    w.goal(200, 200, ["CHANGELOG.md", "a.py"], 22)
    w.goal(201, 201, ["CHANGELOG.md", "b.py"], 21)
    w.goal(202, 202, [".sdlc/x.md", "deps.lock"], 20.9)
    for i in range(10):
        w.commit(f"chore: pad {i}", [f"pad{i}.txt"], 20.5 - i * 0.01)
    w.commit("fix: only the changelog", ["CHANGELOG.md"], 19)
    w.commit("fix: changelog and a", ["CHANGELOG.md", "a.py"], 18.9)
    w.commit("fix: lock and sdlc", [".sdlc/x.md", "deps.lock"], 18.8)
    lines = measure(w, capsys)
    assert row(lines, 200) == (0, 1)
    assert row(lines, 201) == (0, 0)
    assert row(lines, 202) == (0, 0)
    assert totals(lines) == (0, 1, 0)


def test_a_file_in_most_commits_is_derived_hot_and_attributes_nothing(tmp_path, capsys):
    w = World(tmp_path)
    for i in range(10):
        w.goal(100 + i, 100 + i, [f"g{100 + i}.txt", "hot.py"], 29 - i * 0.5)
    w.commit("fix: touches only the hot file", ["hot.py"], 18)
    lines = measure(w, capsys)
    assert totals(lines) == (0, 0, 0)
    assert "hot=hot.py" in lines[1]


def test_a_file_in_few_commits_is_not_hot(tmp_path, capsys):
    w = attr_world(tmp_path)
    w.commit("fix: a bug in a", ["a.py"], 15)
    lines = measure(w, capsys)
    assert "hot=a.py" not in lines[1] and totals(lines) == (0, 1, 0)


def test_one_fix_overlapping_two_goals_is_one_total_and_a_row_in_each(tmp_path, capsys):
    w = attr_world(tmp_path)
    w.commit("fix: touches a and b", ["a.py", "b.py"], 15)
    lines = measure(w, capsys)
    assert row(lines, 200) == (0, 1) and row(lines, 201) == (0, 1)
    assert totals(lines) == (0, 1, 1)


def test_revert_target_wins_over_file_overlap(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(10)
    a = w.goal(200, 200, ["a.py"], 22)
    w.goal(201, 201, ["b.py"], 21)
    for i in range(8):
        w.commit(f"chore: pad {i}", [f"pad{i}.txt"], 20.5 - i * 0.01)
    w.commit("chore: undo the first goal", ["other.py", "b.py"], 15, body=f"This reverts commit {a}.")
    lines = measure(w, capsys)
    assert row(lines, 200) == (1, 0)          # by target, with no file overlap
    assert row(lines, 201) == (0, 0)          # overlap on b.py is NOT used when the target resolves
    assert totals(lines) == (1, 0, 0)


def test_unresolvable_revert_target_falls_back_to_overlap(tmp_path, capsys):
    w = attr_world(tmp_path)
    w.commit('Revert "something"', ["a.py"], 15, body="This reverts commit " + "d" * 40 + ".")
    assert row(measure(w, capsys), 200) == (1, 0)


def test_a_reland_is_not_a_defect(tmp_path, capsys):
    w = attr_world(tmp_path)
    w.commit('Revert "Revert "feat: goal 200 (#200)""', ["a.py"], 15)
    assert totals(measure(w, capsys)) == (0, 0, 0)


def test_position_on_base_decides_not_the_clock_a_skewed_later_fix_counts(tmp_path, capsys):
    w = attr_world(tmp_path)
    w.commit("fix: later on base, older by the committer clock", ["a.py"], 22.5)   # skewed clock
    assert row(measure(w, capsys), 200) == (0, 1)


def test_a_fix_earlier_on_base_never_counts_even_inside_the_time_cap(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(10)
    w.commit("fix: before goal 200 merged", ["a.py"], 23)
    w.goal(200, 200, ["a.py"], 22)
    w.goal(201, 201, ["b.py"], 21)
    for i in range(8):
        w.commit(f"chore: pad {i}", [f"pad{i}.txt"], 20.5 - i * 0.01)
    assert row(measure(w, capsys), 200) == (0, 0)


def test_a_goal_with_more_files_than_the_cap_is_truncated_and_flagged(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(10)
    w.goal(200, 200, [f"big/f{i:03d}.txt" for i in range(300)], 22)
    for i in range(8):
        w.commit(f"chore: pad {i}", [f"pad{i}.txt"], 20.5 - i * 0.01)
    w.commit("fix: inside the cap", ["big/f000.txt"], 15)
    w.commit("fix: beyond the cap", ["big/f299.txt"], 14.9)
    lines = measure(w, capsys)
    assert row(lines, 200) == (0, 1)
    assert "files-capped=1" in lines[1]


# --------------------------------------------------------------------- Task 7: report, end to end


def test_full_report_shape(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(10)
    w.goal(200, 200, ["a.py"], 22)
    w.goal(201, 201, ["b.py"], 3)                    # immature
    w.log(202, "merged", 20, pr="31337")             # unresolved
    w.commit("fix: a (#991)", ["a.py"], 15)
    rc, lines, _ = w.run(capsys)
    assert rc == 0
    assert lines[0] == ("proxy: counts reverts and fix-subject commits that touch a goal's files; "
                        "unrelated fixes and unreported defects are not counted, so this bounds no defect rate")
    h = header(lines)
    assert (h["window"], h["mature"], h["immature"], h["unresolved"]) == ("30d", "11", "1", "1")
    assert h["denylist"] == ",".join(outcomes.DENYLIST) and h["retention"] == "90d" and h["base"] == "main"
    rows = [line.split("\t") for line in lines[2:-1]]
    assert len(rows) == 11 and all(len(r) == 5 for r in rows)
    assert rows[0][0] == "100" and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rows[0][2])
    assert [r[2] for r in rows] == sorted(r[2] for r in rows)
    assert rows[-1][:2] == ["200", "200"] and rows[-1][3:] == ["0", "1"]
    assert lines[-1] == "totals reverts=0 fixes=1 (distinct commits) multi-attributed=0"
    assert not any(re.search(r"\brate\b|\d\s*%", line) for line in lines[1:])


def test_rows_order_by_merge_time_then_goal(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(8)
    w.goal("zeta", 300, ["z.txt"], 20)
    w.goal("alpha", 301, ["al.txt"], 20)             # same committer second as zeta: goal breaks the tie
    w.goal("050", 302, ["o.txt"], 16)                # sorts first by name, merged last
    lines = measure(w, capsys)
    tail = [line.split("\t")[0] for line in lines[2:-1]][-3:]
    assert tail == ["alpha", "zeta", "050"]


def test_merged_record_wins_over_an_armed_one_when_both_resolve(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(9)
    w.commit("feat: via direct merge (#400)", ["m.txt"], 25)
    w.commit("feat: via armed auto-merge (#401)", ["n.txt"], 22)
    w.log(400, "merge_armed", 21, pr="401")          # later record, resolves too
    w.log(400, "merged", 22, pr="400")               # but `merged` outranks it
    lines = measure(w, capsys)
    rows = {line.split("\t")[0]: line.split("\t") for line in lines[2:-1]}
    assert rows["400"][1] == "400"


def test_row_cap_summarises_but_totals_cover_every_goal(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(101, step=0.13)
    w.commit("fix: in the newest goal", ["g200.txt"], 14.5)
    lines = measure(w, capsys)
    assert len(lines) == 2 + outcomes.ROW_CAP + 1 + 1
    assert lines[-2].startswith("... 1 more goals not listed")
    assert header(lines)["mature"] == "101"
    assert lines[-1].startswith("totals reverts=0 fixes=1 ")


# --------------------------------------------------------------------- Task 8: AC-5 read-only, no network


def _snapshot(*roots):
    """(path, mtime_ns, size) of every file and directory under the roots, __pycache__ included."""
    seen = []
    for root in roots:
        for p in sorted(pathlib.Path(root).rglob("*")):
            st = p.lstat()
            seen.append((str(p), st.st_mtime_ns, st.st_size, p.is_dir()))
    return seen


def _isolated_copy(tmp_path):
    """The two scripts in a fresh directory with no __pycache__, so a bytecode write by the run is
    visible as a new file (in the real tree an earlier import may already have left one)."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("outcomes.py", "log.py"):
        (scripts / name).write_text((LOG_S / name).read_text(encoding="utf-8"), encoding="utf-8")
    return scripts


def _run_copy(scripts, repo, home, *args):
    """PYTHONDONTWRITEBYTECODE deliberately UNSET: with it set, a bytecode write could never show."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    env["HOME"] = str(home)
    return subprocess.run([sys.executable, str(scripts / "outcomes.py"), "report", *args], cwd=str(repo),
                          capture_output=True, text=True, env=env, timeout=60)


def test_fixture_repos_disable_background_maintenance(tmp_path):
    """The writes-nothing snapshot is only meaningful if no git housekeeping runs beside it."""
    world = World(tmp_path / "q")
    assert world.git("config", "--get", "maintenance.auto").strip() == "false"
    assert world.git("config", "--get", "gc.auto").strip() == "0"


def test_a_run_writes_nothing_anywhere(tmp_path):
    measured = World(tmp_path / "m")
    measured.mature_goals(10)
    measured.commit("fix: a (#1)", ["g100.txt"], 20)
    unmeasured = World(tmp_path / "u", config={"action_log": {"enabled": False}})
    unmeasured.commit("feat: a (#1)")
    scripts, home = _isolated_copy(tmp_path), tmp_path / "home"
    home.mkdir()
    # Warm the interpreter once, unmeasured: Apple's /usr/bin/python3 writes its OWN bytecode caches
    # under $HOME/Library/Caches on first use, which is the interpreter and not outcomes.py (the
    # planned-test proof runs under that interpreter). The pyc assertion below still catches any
    # bytecode written for this script's modules, and the snapshot then measures steady state.
    _run_copy(scripts, measured.repo, home)
    for world, expect in ((measured, "window="), (unmeasured, "unmeasured:")):
        before = _snapshot(world.repo, scripts, home)
        r = _run_copy(scripts, world.repo, home)
        assert r.returncode == 0, r.stderr
        assert expect in r.stdout
        assert _snapshot(world.repo, scripts, home) == before
    assert not list(scripts.rglob("*.pyc"))


import ast  # noqa: E402

_SOURCE = OUTCOMES.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_FORBIDDEN_ATTRS = {"write_text", "write_bytes", "mkdir", "makedirs", "touch", "unlink", "remove",
                    "rmdir", "rename", "replace", "environ", "getenv", "putenv", "urlopen", "connect"}
_FORBIDDEN_MODULES = {"urllib", "socket", "http", "ftplib", "smtplib", "requests", "shutil", "tempfile",
                      "os"}


def test_source_holds_no_write_env_or_network_call():
    attrs = {n.attr for n in ast.walk(_TREE) if isinstance(n, ast.Attribute)}
    assert not attrs & _FORBIDDEN_ATTRS, attrs & _FORBIDDEN_ATTRS
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & _FORBIDDEN_MODULES
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in _FORBIDDEN_MODULES
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            raise AssertionError("open() is not used: the module reads through log.py and git")
    strings = {n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not {"gh", "curl", "wget"} & strings
    assert "timed_main" not in _SOURCE.replace("timing wrapper", "")


def test_git_is_only_ever_asked_to_log_or_rev_parse():
    verbs = set()
    for node in ast.walk(_TREE):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("_git", "_git_text") and node.args
                and isinstance(node.args[0], ast.List) and node.args[0].elts
                and isinstance(node.args[0].elts[0], ast.Constant)):
            verbs.add(node.args[0].elts[0].value)
    assert verbs == {"log", "rev-parse"}
    runs = [n for n in ast.walk(_TREE) if isinstance(n, ast.Attribute) and n.attr in
            ("run", "Popen", "call", "check_call", "check_output")]
    assert len(runs) == 1                           # one subprocess call site: _git


def test_loading_log_py_restores_the_bytecode_flag():
    for before in (False, True):
        sys.dont_write_bytecode = before
        try:
            outcomes._load_log()
            assert sys.dont_write_bytecode is before
        finally:
            sys.dont_write_bytecode = True


# --------------------------------------------------------------------- review round: boundaries, one clause each


def test_a_goals_own_revert_subject_never_counts_against_itself(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(10)
    w.goal(300, 300, ["own.py"], 22, subject='Revert "feat: something older" (#300)')
    for i in range(8):
        w.commit(f"chore: pad {i}", [f"pad{i}.txt"], 20.5 - i * 0.01)
    assert row(measure(w, capsys), 300) == (0, 0)


def _attr_at(tmp_path, at_offset):
    """Goal 200 merged at NOW-22d exactly; one fix on its file `at_offset` seconds after its merge."""
    w = World(tmp_path)
    w.mature_goals(10)
    merged_at = NOW - 22 * DAY
    w.commit("feat: goal 200 (#200)", ["a.py"], at=merged_at)
    w.log_at(200, "merged", merged_at, pr="200")
    for i in range(8):
        w.commit(f"chore: pad {i}", [f"pad{i}.txt"], 20.5 - i * 0.01)
    w.commit("fix: edge", ["a.py"], at=merged_at + at_offset)
    return w


def test_fix_exactly_fourteen_days_after_the_merge_counts(tmp_path, capsys):
    assert row(measure(_attr_at(tmp_path, 14 * DAY), capsys), 200) == (0, 1)


def test_fix_one_second_past_fourteen_days_does_not_count(tmp_path, capsys):
    assert row(measure(_attr_at(tmp_path, 14 * DAY + 1), capsys), 200) == (0, 0)


def _edge_goal(tmp_path, at, newest):
    w = World(tmp_path)
    if not newest:
        w.commit("feat: edge (#50)", ["edge.txt"], at=at)
        w.log_at(50, "merged", at, pr="50")
    w.mature_goals(10)
    if newest:
        w.commit("feat: edge (#50)", ["edge.txt"], at=at)
        w.log_at(50, "merged", at, pr="50")
    return w


def test_goal_merged_exactly_at_the_window_start_is_included(tmp_path, capsys):
    w = _edge_goal(tmp_path, NOW - 30 * DAY, newest=False)
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (11, 0, 0)


def test_goal_merged_one_second_before_the_window_start_is_excluded(tmp_path, capsys):
    w = _edge_goal(tmp_path, NOW - 30 * DAY - 1, newest=False)
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (10, 0, 0)


def test_goal_merged_exactly_fourteen_days_ago_is_mature(tmp_path, capsys):
    w = _edge_goal(tmp_path, NOW - 14 * DAY, newest=True)
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (11, 0, 0)


def test_goal_merged_one_second_inside_fourteen_days_is_immature(tmp_path, capsys):
    w = _edge_goal(tmp_path, NOW - 14 * DAY + 1, newest=True)
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (10, 1, 0)


def test_a_file_in_exactly_a_fifth_of_the_commits_is_not_hot(tmp_path, capsys):
    """4 of 20 commits touch h.py: exactly 1/5, and hot means MORE than 1/5."""
    w = World(tmp_path)
    w.mature_goals(10)
    for i, ago in enumerate((22, 21.5, 21)):
        w.goal(200 + i, 200 + i, [f"s{i}.py", "h.py"], ago)
    for i in range(6):
        w.commit(f"chore: pad {i}", [f"pad{i}.txt"], 20.5 - i * 0.01)
    w.commit("fix: only the shared file", ["h.py"], 15)
    lines = measure(w, capsys)
    assert "hot=none" in lines[1]
    assert totals(lines) == (0, 1, 1)


def test_the_longest_allowed_window_without_pruning_is_365_days(tmp_path, capsys):
    w = World(tmp_path, config={"action_log": {"enabled": True, "retention_days": False}})
    (w.sdlc / "state" / "log").mkdir()
    w.commit("feat: a (#1)")
    assert w.run(capsys, "--window", "365")[0] == 0
    rc, lines, err = w.run(capsys, "--window", "366")
    assert rc == 2 and "window-exceeds-retention" in err
    assert outcomes.HARD_MAX_WINDOW == 365


def test_a_pr_marker_on_two_commits_resolves_to_the_newest(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(9)
    w.commit("feat: first try (#400)", ["m.txt"], 26)
    newest = w.commit("feat: second try (#400)", ["n.txt"], 22)
    w.log(400, "merged", 22, pr="400")
    lines = measure(w, capsys)
    merged = {line.split("\t")[0]: line.split("\t")[2] for line in lines[2:-1]}
    assert merged["400"] == outcomes._iso(NOW - 22 * DAY), newest


def test_a_log_record_exactly_at_the_record_floor_is_kept(tmp_path, capsys):
    """Floor = window + 14 days back. A `merged` record there that does not resolve is unresolved."""
    w = World(tmp_path)
    w.mature_goals(10)
    w.log_at(600, "merged", NOW - 44 * DAY, pr="7777")
    w.log_at(601, "merged", NOW - 44 * DAY - 1, pr="7778")     # one second older: silent
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (10, 0, 1)


def test_a_hash_prefixed_pr_in_the_log_still_resolves(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(9)
    w.commit("feat: hashed (#701)", ["h.txt"], 20)
    w.log(701, "merged", 20, pr="#701")
    rc, lines, _ = w.run(capsys)
    assert counts(lines) == (10, 0, 0)


def test_revert_sha_shorter_than_seven_characters_is_not_a_revert():
    assert outcomes._kind("chore: x", "This reverts commit abc123") is None
    assert outcomes._kind("chore: x", "This reverts commit abc1234") == "revert"
    assert outcomes._kind("chore: x", "This reverts commit " + "a" * 40) == "revert"


# a squash body that merely lists an internal revert is not a revert (concern 3)


def test_body_only_revert_of_an_unknown_commit_is_not_a_revert(tmp_path, capsys):
    w = attr_world(tmp_path)
    w.commit("chore: squash of a branch", ["a.py"], 15, body="This reverts commit " + "d" * 40 + ".")
    assert totals(measure(w, capsys)) == (0, 0, 0)


def test_body_only_revert_of_an_unknown_commit_still_counts_as_a_fix_by_subject(tmp_path, capsys):
    w = attr_world(tmp_path)
    w.commit("fix: real fix", ["a.py"], 15, body="This reverts commit " + "d" * 40 + ".")
    assert totals(measure(w, capsys)) == (0, 1, 0)


def test_body_only_revert_of_a_commit_in_the_pass_counts_by_overlap(tmp_path, capsys):
    w = attr_world(tmp_path)
    pad = w.commit("chore: the thing being undone", ["undone.py"], 18)
    w.commit("chore: undo it", ["a.py"], 15, body=f"This reverts commit {pad}.")
    lines = measure(w, capsys)
    assert row(lines, 200) == (1, 0) and totals(lines) == (1, 0, 0)


# unreadable goal logs are counted, not silent (nit)


def test_a_garbage_goal_log_is_counted_in_the_header(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(10)
    d = w.sdlc / "state" / "log"
    (d / "garbage.jsonl").write_text("{not json\n", encoding="utf-8")
    (d / "empty.jsonl").write_text("", encoding="utf-8")
    lines = measure(w, capsys)
    assert header(lines)["unreadable"] == "2"


def test_unreadable_goal_logs_are_named_when_unmeasured(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(9)
    (w.sdlc / "state" / "log" / "garbage.jsonl").write_text("[1]\n", encoding="utf-8")
    rc, lines, _ = w.run(capsys)
    assert lines[1].startswith("unmeasured:") and "unreadable=1" in lines[1]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
def test_a_goal_log_that_cannot_be_opened_is_counted(tmp_path, capsys):
    w = World(tmp_path)
    w.mature_goals(11)
    locked = w.sdlc / "state" / "log" / "100.jsonl"
    locked.chmod(0)
    try:
        lines = measure(w, capsys)
    finally:
        locked.chmod(0o644)
    assert header(lines)["unreadable"] == "1" and header(lines)["mature"] == "10"
